"""源文件引用（契约 §6）：登记不复制 / 首次落盘 / 唯一解析入口 / 删除只删引用。

覆盖：
* 桌面路径登记不复制、不移动、不改名（源文件 mtime/size/identity 都不变，磁盘无新副本）；
* 只有字节的入口首次落盘到 workspace/materials/（只落一次），
  记录 source_kind=browser_upload 与 origin=user_saved_material，不伪造路径；
* 读取 / 预览 / read_attachment 工具 / 写回都解析到同一份源文件；
* 写回前重核对 identity+hash，源文件变化不覆盖；
* 删除引用不删源文件；源文件缺失时如实报错并可重新定位；
* 历史记录（source_kind 为 NULL）保留只读回放能力（旧副本仍可读）。
"""

from __future__ import annotations

import hashlib
import time
from pathlib import Path
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from agent.api.server import create_app
from agent.config import Settings
from agent.credentials.store import MemoryKeyring
from agent.services.attachments import (
    AttachmentService,
    REFERENCE_AVAILABLE,
    REFERENCE_MISSING,
)
from agent.services.file_identity import identity_of
from agent.storage.db import connect
from agent.storage.migrate import apply_migrations
from agent.tools.attachment_tools import ReadAttachmentTool

CAPTURED: dict[str, dict] = {}


@pytest.fixture()
def service(db_conn, tmp_path: Path) -> AttachmentService:
    return AttachmentService(db_conn, tmp_path / "data")


@pytest.fixture()
def client(tmp_path: Path):
    conn = connect(tmp_path / "attachments_refs.db")
    apply_migrations(conn)
    settings = Settings(data_dir=tmp_path / "data")
    app = create_app(settings, conn)
    app.state.ctx.credentials._kr = MemoryKeyring()
    with TestClient(app) as c:
        yield c


def _register(client: TestClient, path: Path, **extra) -> dict:
    body = {"source_path": str(path), "source_kind": "desktop_path", **extra}
    resp = client.post("/api/attachments", json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()["attachment"]


def _source_snapshot(path: Path) -> dict:
    stat = path.stat()
    identity = identity_of(path)
    return {
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "identity": identity.key if identity is not None else None,
        "bytes": path.read_bytes(),
    }


def _files_under(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    return [item for item in root.rglob("*") if item.is_file()]


# ---------------------------------------------------------------------------
# 登记：不复制、不移动、不改名
# ---------------------------------------------------------------------------


def test_desktop_registration_never_copies_or_touches_source(client: TestClient, tmp_path: Path):
    data_dir = tmp_path / "data"
    source = tmp_path / "用户原件.docx"
    source.write_bytes(b"docx-bytes-placeholder")
    before = _source_snapshot(source)

    att = _register(client, source, topic_id="t_reg", user_note="这是会议材料")

    assert att["kind"] == "reference"
    assert att["stored_path"] is None, "新记录不得有副本路径"
    assert att["source_path"] == str(source)
    assert att["source_kind"] == "desktop_path"
    assert att["origin"] == "user_original"
    assert att["state"] == "ready"
    assert att["reference_state"] == REFERENCE_AVAILABLE
    assert att["user_note"] == "这是会议材料"
    assert att["material_enabled"] is True
    assert att["file_id"]
    assert att["size"] == before["size"]

    assert _source_snapshot(source) == before, "登记不得改动用户源文件（内容/mtime/身份都不变）"
    assert _files_under(data_dir / "attachments") == [], "登记路径不得产生副本文件"


def test_payload_exposes_full_file_ref(client: TestClient, tmp_path: Path):
    source = tmp_path / "材料.txt"
    source.write_text("内容", encoding="utf-8")
    att = _register(client, source)
    fetched = client.get(f"/api/attachments/{att['id']}").json()["attachment"]
    for key in (
        "attachment_id",
        "filename",
        "media_kind",
        "origin",
        "source_kind",
        "availability",
        "size",
        "user_note",
        "material_enabled",
        "missing_reason",
        "reference_id",
    ):
        assert key in fetched, f"payload 缺少 §6.4 字段：{key}"
    assert fetched["attachment_id"] == fetched["id"]
    assert fetched["filename"] == "材料.txt"
    assert fetched["reference_id"] == fetched["id"]
    assert fetched["file_ref"]["attachment_id"] == fetched["id"]
    assert fetched["file_ref"]["material_enabled"] is True


# ---------------------------------------------------------------------------
# 只有字节：首次落盘为用户材料
# ---------------------------------------------------------------------------


def test_browser_upload_lands_material_once_with_origin(client: TestClient, tmp_path: Path):
    payload = "浏览器上传的内容\n第二行\n".encode("utf-8")
    resp = client.post(
        "/api/attachments/upload",
        content=payload,
        headers={
            "Content-Type": "application/octet-stream",
            "X-QIO-Source-Kind": "browser_upload",
            "X-QIO-Name": quote("粘贴的材料.txt"),
            "X-QIO-Topic-Id": "t_upload",
            "X-QIO-User-Note": quote("从浏览器拖进来"),
        },
    )
    assert resp.status_code == 200, resp.text
    att = resp.json()["attachment"]

    assert att["stored_path"] is None
    assert att["source_kind"] == "browser_upload"
    assert att["origin"] == "user_saved_material"
    assert att["state"] == "ready"
    assert att["user_note"] == "从浏览器拖进来"
    landed = Path(att["source_path"])
    assert landed.is_file() and landed.read_bytes() == payload
    assert "materials" in landed.parts, "无路径材料的首次落盘必须在 workspace/materials/ 下"
    assert landed.name.endswith("__粘贴的材料.txt")
    assert len([item for item in landed.parent.iterdir() if item.is_file()]) == 1, (
        "只允许落盘一次，不得留临时文件或第二份"
    )
    assert client.get(f"/api/attachments/{att['id']}/content").content == payload


def test_upload_without_source_kind_defaults_to_browser_upload(client: TestClient, tmp_path: Path):
    """缺省来源按 browser_upload 首次落盘（不再有旧副本语义）。"""
    payload = b"no-header-bytes"
    resp = client.post(
        "/api/attachments/upload",
        content=payload,
        headers={"Content-Type": "application/octet-stream", "X-QIO-Name": "old.txt"},
    )
    assert resp.status_code == 200, resp.text
    att = resp.json()["attachment"]
    assert att["source_kind"] == "browser_upload", "缺省必须标注浏览器上传来源"
    assert att["origin"] == "user_saved_material"
    assert att["stored_path"] is None
    assert "materials" in Path(att["source_path"]).parts
    assert Path(att["source_path"]).read_bytes() == payload


# ---------------------------------------------------------------------------
# 唯一解析入口：读取 / 预览 / 工具读同一份源文件
# ---------------------------------------------------------------------------


async def test_reads_preview_and_tool_use_the_same_source(client: TestClient, tmp_path: Path):
    source = tmp_path / "同一份.txt"
    source.write_bytes("第一版内容\n第二行\n".encode("utf-8"))
    att = _register(client, source)

    assert client.get(f"/api/attachments/{att['id']}/content").content.decode("utf-8") == (
        "第一版内容\n第二行\n"
    )
    preview = client.get(f"/api/attachments/{att['id']}/preview").json()
    assert preview["status"] == "ready", preview
    assert "第一版内容" in str(preview.get("data"))

    # 用户直接编辑源文件：所有入口看到的都是同一份最新内容（没有副本可回退）
    source.write_text("第二版（编辑后）\n第二行\n", encoding="utf-8")
    assert "第二版" in client.get(f"/api/attachments/{att['id']}/content").content.decode("utf-8")
    preview_after = client.get(f"/api/attachments/{att['id']}/preview?refresh=1").json()
    assert "第二版" in str(preview_after.get("data"))

    # 没有任何副本被创建
    assert _files_under(tmp_path / "data" / "attachments") == []


async def test_read_attachment_tool_resolves_source(service: AttachmentService, tmp_path: Path):
    source = tmp_path / "工具读取.txt"
    source.write_text("工具读到的正文", encoding="utf-8")
    att = service.register_source(path=str(source), source_kind="desktop_path")
    tool = ReadAttachmentTool(service, active_turn_id=lambda: None)
    result = await tool.run(attachment_id=att.id)
    assert result.ok is True, result.error
    assert "工具读到的正文" in result.content


# ---------------------------------------------------------------------------
# 统一授权：读取 / 写回 / 删除用户材料都必须经网关（W1 未合入时用 Fake）
# ---------------------------------------------------------------------------


class FakeGateway:
    """测试用授权网关：只记录 effect、按预设返回 verdict，不做任何判定。"""

    def __init__(self, verdict: str = "allow", reason: str = "") -> None:
        self.verdict = verdict
        self.reason = reason
        self.effects: list = []
        self.submitted: list = []

    def mode(self):
        return "restricted"

    def instruction(self):
        return None

    async def authorize(self, effect, ctx=None):
        self.effects.append(effect)
        return {
            "verdict": self.verdict,
            "reason": self.reason or ("允许" if self.verdict == "allow" else "需要审批"),
            "approval_id": None,
        }

    async def submit(self, operation, ctx=None):
        self.submitted.append(operation)
        return {
            "approval_id": "appr_fake_1",
            "state": "waiting",
            "expires_at": "",
            "confirmations_required": 1,
        }


def _effect_kind(effect) -> str:
    kind = getattr(effect, "kind", "")
    return str(getattr(kind, "value", kind))


async def test_write_back_requires_write_effect_and_rechecks(service: AttachmentService, tmp_path: Path):
    source = tmp_path / "要改的.txt"
    source.write_text("旧内容", encoding="utf-8")
    att = service.register_source(path=str(source), source_kind="desktop_path")
    gateway = FakeGateway("allow")

    result = await service.write_back_source(
        att.id, "新内容".encode("utf-8"), gateway=gateway
    )
    assert result["ok"] is True and result.get("pending_approval") is not True
    assert source.read_bytes() == "新内容".encode("utf-8")
    # R9-G §7：判定 + 执行前重核对各一次（两次都必须是 write，且对象一致）
    assert [_effect_kind(item) for item in gateway.effects] == ["write", "write"], gateway.effects
    target = gateway.effects[0].targets[0]
    assert str(target.value) == str(source)
    assert target.identity
    refreshed = service.get(att.id)
    assert refreshed.content_sha256 == hashlib.sha256("新内容".encode("utf-8")).hexdigest()
    assert refreshed.size_bytes == len("新内容".encode("utf-8"))
    assert result["identity_changed"] is True
    assert refreshed.file_id == result["identity"]
    assert "atomic_write" in refreshed.legacy_paths

    # 用户在写回之外又改了文件：下一次写回必须拒绝（登记哈希已对不上），且不覆盖
    source.write_text("用户又改的内容", encoding="utf-8")
    with pytest.raises(Exception) as excinfo:
        await service.write_back_source(
            att.id, "覆盖用户改动".encode("utf-8"), gateway=FakeGateway("allow")
        )
    assert "不一致" in str(excinfo.value) or "变化" in str(excinfo.value)
    assert source.read_text(encoding="utf-8") == "用户又改的内容"


async def test_write_back_denied_or_deferred_never_touches_file(service: AttachmentService, tmp_path: Path):
    source = tmp_path / "受保护.txt"
    source.write_bytes("用户内容".encode("utf-8"))
    att = service.register_source(path=str(source), source_kind="desktop_path")

    with pytest.raises(Exception) as denied:
        await service.write_back_source(
            att.id, b"should-not-write", gateway=FakeGateway("deny", "受限模式下需要明确指令授权")
        )
    assert "授权" in str(denied.value) or "受限" in str(denied.value)
    assert source.read_bytes() == "用户内容".encode("utf-8")

    pending_gateway = FakeGateway("approve")
    pending = await service.write_back_source(
        att.id, b"approved-later", gateway=pending_gateway
    )
    assert pending["pending_approval"] is True
    assert pending["approval"]["state"] == "waiting"
    assert pending_gateway.submitted, "approve 必须提交待审批操作（不阻塞等待）"
    assert source.read_bytes() == "用户内容".encode("utf-8"), "待审批时绝不能先写"

    # 没有网关（W1 未接入）时写操作必须拒绝，绝不静默放行
    with pytest.raises(Exception):
        await service.write_back_source(att.id, b"no-gateway")
    assert source.read_bytes() == "用户内容".encode("utf-8")


async def test_read_tool_deny_and_allow_via_gateway(service: AttachmentService, tmp_path: Path):
    source = tmp_path / "工具授权.txt"
    source.write_text("正文", encoding="utf-8")
    att = service.register_source(path=str(source), source_kind="desktop_path")

    denied_tool = ReadAttachmentTool(
        service, active_turn_id=lambda: None, security=FakeGateway("deny", "红线保护")
    )
    denied = await denied_tool.run(attachment_id=att.id)
    assert denied.ok is False and denied.category == "permission"
    assert "红线保护" in (denied.error or "")

    allow_gateway = FakeGateway("allow")
    allowed_tool = ReadAttachmentTool(
        service, active_turn_id=lambda: None, security=allow_gateway
    )
    allowed = await allowed_tool.run(attachment_id=att.id)
    assert allowed.ok is True and "正文" in allowed.content
    # R9-G §7：读取也必须在真正读文件前重新核对一次（两次都是 read）
    assert [_effect_kind(item) for item in allow_gateway.effects] == ["read", "read"]


def test_content_route_denied_by_gateway(client: TestClient, tmp_path: Path):
    source = tmp_path / "接口授权.txt"
    source.write_text("正文", encoding="utf-8")
    att = _register(client, source)
    ctx = client.app.state.ctx
    original = getattr(ctx, "authorization", None)
    try:
        ctx.authorization = FakeGateway("deny", "接口读取被拒")
        denied = client.get(f"/api/attachments/{att['id']}/content")
        assert denied.status_code == 403
        assert "接口读取被拒" in str(denied.json().get("detail"))
        ctx.authorization = FakeGateway("allow")
        ok = client.get(f"/api/attachments/{att['id']}/content")
        assert ok.status_code == 200 and ok.content == "正文".encode("utf-8")
    finally:
        ctx.authorization = original


async def test_delete_material_goes_through_delete_effect_and_recycle_bin(
    service: AttachmentService, tmp_path: Path, monkeypatch
):
    import agent.services.recycle_bin as recycle_bin_module

    target = tmp_path / "冗余副本.txt"
    target.write_bytes(b"redundant")
    recycled: list[str] = []

    def fake_recycle(path):
        recycled.append(str(path))
        return {
            "ok": True,
            "path": str(path),
            "mechanism": "fake",
            "reason": "已移入回收站",
            "deleted_permanently": False,
        }

    monkeypatch.setattr(recycle_bin_module, "send_to_recycle_bin", fake_recycle)

    with pytest.raises(Exception):
        await service.recycle_material_files(
            [{"attachment_id": "att_1", "path": str(target)}],
            gateway=FakeGateway("deny", "删除需要用户批准"),
        )
    assert recycled == [] and target.is_file()

    allowed = await service.recycle_material_files(
        [{"attachment_id": "att_1", "path": str(target)}],
        gateway=FakeGateway("allow"),
    )
    assert allowed["ok"] is True and allowed["deleted_permanently"] is False
    assert recycled == [str(target)]

    pending_gateway = FakeGateway("approve")
    pending = await service.recycle_material_files(
        [{"attachment_id": "att_1", "path": str(target)}], gateway=pending_gateway
    )
    assert pending["pending_approval"] is True and pending_gateway.submitted


# ---------------------------------------------------------------------------
# 写回：重核对 identity+hash，原子写
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 删除引用 / 缺失 / 材料开关
# ---------------------------------------------------------------------------


def test_delete_reference_keeps_source_file(client: TestClient, tmp_path: Path):
    source = tmp_path / "删引用不删文件.txt"
    source.write_bytes("必须保留".encode("utf-8"))
    att = _register(client, source)

    resp = client.delete(f"/api/attachments/{att['id']}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["removed"] is True
    assert body["deleted_copy"] is None
    assert body["kept_source"] == str(source)
    assert source.read_bytes() == "必须保留".encode("utf-8")
    assert client.get(f"/api/attachments/{att['id']}").status_code == 404


def test_missing_source_reports_honestly_and_can_relocate(client: TestClient, tmp_path: Path):
    source = tmp_path / "会丢的文件.txt"
    source.write_text("原始", encoding="utf-8")
    att = _register(client, source)
    source.unlink()

    detail = client.get(f"/api/attachments/{att['id']}").json()["attachment"]
    assert detail["state"] == REFERENCE_MISSING
    assert detail["missing_reason"]
    assert detail["actions"] == ["relocate"]

    content = client.get(f"/api/attachments/{att['id']}/content")
    assert content.status_code == 409

    restored = tmp_path / "找回的文件.txt"
    restored.write_text("原始", encoding="utf-8")
    fixed = client.post(
        f"/api/attachments/{att['id']}/relocate",
        json={"source_path": str(restored), "confirmed_identity": True},
    )
    assert fixed.status_code == 200, fixed.text
    assert fixed.json()["attachment"]["state"] == "ready"
    assert client.get(f"/api/attachments/{att['id']}/content").content == "原始".encode("utf-8")


def test_material_switch_is_context_only(client: TestClient, tmp_path: Path):
    source = tmp_path / "材料开关.txt"
    source.write_text("内容", encoding="utf-8")
    att = _register(client, source)
    off = client.patch(f"/api/attachments/{att['id']}/material", json={"enabled": False})
    assert off.status_code == 200, off.text
    assert off.json()["attachment"]["material_enabled"] is False
    # 关掉的是"主动纳入互动上下文"，不是 OS 读取权限：明确要求读取仍可读
    assert client.get(f"/api/attachments/{att['id']}/content").status_code == 200
    on = client.post(f"/api/attachments/{att['id']}/material-enabled", json={"enabled": True})
    assert on.json()["attachment"]["material_enabled"] is True


# ---------------------------------------------------------------------------
# 历史记录：只读回放（旧副本代码路径不丢覆盖）
# ---------------------------------------------------------------------------


def test_legacy_rows_still_replay_old_copies(service: AttachmentService, tmp_path: Path):
    source = tmp_path / "历史文件.txt"
    source.write_text("历史内容", encoding="utf-8")
    legacy = service.prepare(str(source))
    legacy = service.run_prepare(legacy.id)
    assert legacy.kind == "copy" and legacy.stored_path
    assert legacy.source_kind is None, "历史记录 = source_kind 为 NULL"

    resolved = service.resolve_source(legacy.id)
    assert resolved.legacy is True
    assert resolved.state == REFERENCE_AVAILABLE
    assert resolved.path == Path(legacy.stored_path), "历史记录先回放旧副本，符合旧口径"

    payload = service.payload(service.get(legacy.id))
    assert payload["stored_path"] == legacy.stored_path
    assert payload["display"] == "已保存副本"


# ---------------------------------------------------------------------------
# 绑定：新语义记录按"引用型"进入轮次
# ---------------------------------------------------------------------------


async def test_source_ref_binds_to_turn(service: AttachmentService, tmp_path: Path):
    source = tmp_path / "本轮材料.txt"
    source.write_text("正文", encoding="utf-8")
    att = service.register_source(path=str(source), source_kind="dropped_path", topic_id="topic_1")
    outcome = await service.bind_for_turn("turn_1", [att.id], topic_id="topic_1")
    assert outcome.bound == [att.id], outcome.rejected
    turn_note = service.turn_note("turn_1")
    assert turn_note and "源文件引用" in turn_note
    payloads = service.payloads_for_messages([{"id": "msg_1", "turn_id": "turn_1"}])
    assert payloads["msg_1"][0]["attachment_id"] == att.id
    assert payloads["msg_1"][0]["source_kind"] == "dropped_path"
