"""R9-F W3：附件路径的来源记账（契约 §3.4）。

真实 AttachmentService + 真实迁移 55 DB + 真实临时文件；
fake 网关只让写回/回收通过（接口级验证），不冒充端到端授权。
覆盖：桌面路径登记=user_original、上传首次落盘=user_saved_material（不是生成物）、
写回/原子替换后来源保留且身份更新、回收后来源保留 missing、重启后可查、
外部替换后写回被拒（不覆盖、不继承）。
"""

from __future__ import annotations

import os

import pytest

from agent.security.effects import AuthDecision, Verdict
from agent.services import file_origins as fo
from agent.services.attachments import AttachmentError, AttachmentService
from agent.storage.db import connect
from agent.storage.migrate import apply_migrations


class AllowGateway:
    """fake 网关：写/删都 allow，只用于让服务走完真实写回/回收路径（接口测试）。"""

    def mode(self):
        return "restricted"

    def instruction(self):
        return None

    async def authorize(self, effect, *, ctx=None):
        return AuthDecision(verdict=Verdict.ALLOW, reason="fake: allow (接口测试)")

    async def submit(self, operation, *, ctx=None):  # pragma: no cover - 本文件不测审批
        raise AssertionError("本文件不应提交审批")


@pytest.fixture()
def env(tmp_path):
    conn = connect(tmp_path / "origins_att.db")
    apply_migrations(conn)
    data_dir = tmp_path / "data"
    service = AttachmentService(conn, data_dir, workspace_dir=data_dir / "workspace")
    yield {"conn": conn, "service": service, "gateway": AllowGateway(), "tmp": tmp_path}
    conn.close()


def _atomic_replace(path, data: bytes) -> None:
    temp = path.with_name(path.name + ".new")
    temp.write_bytes(data)
    os.replace(temp, path)


def test_register_source_records_user_original(env):
    tmp = env["tmp"]
    source = tmp / "我的文档.txt"
    source.write_text("用户原文件", encoding="utf-8")
    att = env["service"].register_source(path=str(source), source_kind="desktop_path")
    record = env["service"].file_origins.lookup(source)
    assert record is not None
    assert record.origin == fo.ORIGIN_USER_ORIGINAL
    assert record.attachment_id == att.id
    assert env["service"].file_origins.is_generated(source) is False


def test_upload_material_is_not_generated(env):
    tmp = env["tmp"]
    att = env["service"].write_material_bytes(
        b"browser-upload-bytes", name="截图.png", source_kind="browser_upload"
    )
    path = tmp / "data" / "workspace" / "materials"
    record = env["service"].file_origins.lookup(
        __import__("pathlib").Path(att.source_path)
    )
    assert record is not None
    assert record.origin == fo.ORIGIN_USER_SAVED_MATERIAL
    assert record.attachment_id == att.id
    assert env["service"].file_origins.is_generated(__import__("pathlib").Path(att.source_path)) is False


@pytest.mark.asyncio
async def test_write_back_keeps_origin_and_updates_identity(env):
    """写回源文件：内容更新、OS 身份变化，来源仍是 user_original（不是生成物）。"""
    service = env["service"]
    source = env["tmp"] / "要写回的.txt"
    source.write_text("旧内容", encoding="utf-8")
    att = service.register_source(path=str(source), source_kind="desktop_path")
    before = service.file_origins.lookup(source)
    assert before is not None

    outcome = await service.write_back_source(
        att.id, "新内容（用户已确认）".encode("utf-8"), gateway=env["gateway"]
    )
    assert outcome["ok"] is True
    assert source.read_text(encoding="utf-8") == "新内容（用户已确认）"
    assert outcome.get("identity_changed") is True, "原子替换必须改变 OS 文件身份并如实记录"

    after = service.file_origins.lookup(source)
    assert after is not None
    assert after.origin == fo.ORIGIN_USER_ORIGINAL, "写回用户文件不得变成 QIO 生成物"
    assert after.attachment_id == att.id
    assert after.identity_key == outcome["identity"]
    assert after.identity_key != before.identity_key
    assert service.file_origins.is_generated(source) is False


@pytest.mark.asyncio
async def test_write_back_refuses_external_replacement(env):
    """源文件被外部替换（身份变化）→ 写回拒绝，不覆盖、不继承归属。"""
    service = env["service"]
    source = env["tmp"] / "被外部替换.txt"
    source.write_text("登记时内容", encoding="utf-8")
    att = service.register_source(path=str(source), source_kind="desktop_path")
    recorded = service.file_origins.lookup(source)
    assert recorded is not None

    _atomic_replace(source, "外部程序改的内容".encode("utf-8"))
    with pytest.raises(AttachmentError):
        await service.write_back_source(
            att.id, "QIO 想写回的内容".encode("utf-8"), gateway=env["gateway"]
        )
    assert source.read_text(encoding="utf-8") == "外部程序改的内容", "被拒后必须保留外部文件"
    # 外部新身份不得继承旧来源；旧记录仍是历史事实
    assert service.file_origins.lookup(source) is None
    assert service.file_origins._by_identity(recorded) is not None


@pytest.mark.asyncio
async def test_recycle_keeps_origin_fact_missing(env, monkeypatch):
    """删除用户材料：进回收站后来源事实保留，只标 missing。"""
    import agent.services.recycle_bin as recycle_bin

    service = env["service"]
    att = service.write_material_bytes(
        b"to-be-recycled", name="待回收.bin", source_kind="browser_upload"
    )
    from pathlib import Path

    path = Path(att.source_path)

    def _fake_recycle(target):
        os.unlink(target)
        return {"ok": True, "mechanism": "fake-recycle", "deleted_permanently": False}

    monkeypatch.setattr(recycle_bin, "send_to_recycle_bin", _fake_recycle)
    result = await service.recycle_material_files(
        [{"attachment_id": att.id, "path": str(path), "identity": None}],
        gateway=env["gateway"],
    )
    assert result["ok"] is True and result["results"][0]["ok"] is True
    assert not path.exists()
    record = service.file_origins.lookup(path)
    assert record is not None, "回收后来源事实必须保留"
    assert record.origin == fo.ORIGIN_USER_SAVED_MATERIAL
    assert record.missing_at


def test_origin_survives_restart(env):
    """重启（新 AttachmentService 同 DB）后同一路径的来源仍可查。"""
    service = env["service"]
    source = env["tmp"] / "重启后可查.txt"
    source.write_text("persist", encoding="utf-8")
    service.register_source(path=str(source), source_kind="desktop_path")

    restarted = AttachmentService(
        env["conn"],
        env["tmp"] / "data",
        workspace_dir=env["tmp"] / "data" / "workspace",
    )
    record = restarted.file_origins.lookup(source)
    assert record is not None and record.origin == fo.ORIGIN_USER_ORIGINAL
