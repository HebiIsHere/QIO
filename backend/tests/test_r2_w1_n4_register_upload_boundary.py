"""审计 N4（W1b）：register_upload 必须走与浏览器上传同一条 R1 提交边界。

背景（独立验证组 V 报告第 9 节 N4）：register_upload() → 旧私有 _write_upload() 直接
os.replace 并自行写行 —— 不绑代际、不领提交票号、不查目标身份，完全绕过 R1 的提交边界。
它当前只有测试调用（生产上传走 write_upload_stream），但公开 API 上留着这条旁路，
与 R1 声明的不变量形状不符。

修复要求（frozen）：
* register_upload 走**同一条**三段式：登记（prepared + 在飞）→ 落盘（提交边界：
  代际 + 票号 + 目标身份）→ 落库（apply_outcome 按代际/票号丢弃过期结果）；
* 旧旁路入口必须真的消失（不得只写注释）；
* 对外的既有契约不变：空字节 / 超阈值结构化拒绝、成功时 kind=copy / state=ready /
  stored_path 字节 / original_name / sha256 正确。

运行：cd backend; uv run --frozen --extra dev pytest tests/test_r2_w1_n4_register_upload_boundary.py -q
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from agent.services.attachments import STATE_CANCELLED, STATE_READY, AttachmentService

PAYLOAD = b"12345678"


@pytest.fixture()
def svc(db_conn, tmp_path: Path) -> AttachmentService:
    return AttachmentService(db_conn, tmp_path / "data")


def test_register_upload_uses_the_same_commit_boundary(svc, monkeypatch):
    """结构面：register_upload 必须走上传落盘入口，结果带代际 + 票号，落库走 apply_outcome。"""
    writes: list[str] = []
    applied: list[tuple[str, object, object]] = []
    real_write = svc.write_upload_stream
    real_apply = svc.apply_outcome

    def spy_write(att, chunks, **kwargs):
        writes.append(str(att.id))
        return real_write(att, chunks, **kwargs)

    def spy_apply(attachment_id, outcome, **kwargs):
        applied.append(
            (str(attachment_id), outcome.commit_generation, outcome.commit_ticket)
        )
        return real_apply(attachment_id, outcome, **kwargs)

    monkeypatch.setattr(svc, "write_upload_stream", spy_write)
    monkeypatch.setattr(svc, "apply_outcome", spy_apply)

    att = svc.register_upload(PAYLOAD, name="n4.txt", topic_id="t1")

    assert writes == [att.id], (
        "register_upload 没有走上传落盘入口（还是自己 os.replace，绕过了提交边界）",
        writes,
    )
    assert len(applied) == 1 and applied[0][0] == att.id, applied
    assert applied[0][1] is not None, "落库的结果必须带上自己的代际（代际是硬闸）"
    assert applied[0][2] is not None, "落库的结果必须带上自己的提交票号"
    # 对外契约不变
    assert att.kind == "copy" and att.state == STATE_READY
    assert Path(att.stored_path).read_bytes() == PAYLOAD
    assert att.original_name == "n4.txt"
    assert att.sha256 == hashlib.sha256(PAYLOAD).hexdigest()


def test_legacy_bypass_entry_is_gone(svc):
    """旧旁路入口必须真的消失（N4 不接受「只补注释」）。"""
    assert not hasattr(svc, "_write_upload"), (
        "仍然留着绕过提交边界的私有落盘入口：不绑代际 / 不领票号 / 不查目标身份"
    )


def test_upload_with_a_stale_generation_never_commits(svc):
    """行为面：代际前进之后，过期上传既不提交文件，也不把行写成 ready。"""
    att = svc.begin_upload(name="stale.txt", topic_id="t1")
    generation = svc.prepare_generation(att.id)
    svc.mark_preparing_for_retry(att.id)  # 更新的准备登记：这次上传变成过期代际
    assert svc.prepare_generation(att.id) > generation

    outcome = svc.write_upload_stream(att, [b"stale payload"], generation=generation)
    assert outcome.state == STATE_CANCELLED, {
        "state": outcome.state,
        "error": outcome.error,
    }
    assert not svc.copy_path(att).exists(), "过期上传不得提交出副本"

    applied = svc.apply_outcome(att.id, outcome)  # 调用方没给代际 → 用结果携带的那一份
    assert applied is not None
    assert applied.state != STATE_READY, applied.state
    assert applied.stored_path is None
    assert not svc.copy_path(att).exists()


def test_register_upload_rejections_keep_the_existing_contract(svc, monkeypatch):
    """空字节 / 超阈值仍按既有形状结构化拒绝，且不留下任何行或文件。"""
    from agent.services.attachments import AttachmentError
    from agent.services import attachments as attachments_mod

    monkeypatch.setattr(attachments_mod, "COPY_MAX_BYTES", 8)
    with pytest.raises(AttachmentError) as excinfo:
        svc.register_upload(b"123456789")
    assert "只用于" in str(excinfo.value)
    with pytest.raises(AttachmentError):
        svc.register_upload(b"")
    assert svc.list(limit=10, check=False) == [], "被拒的上传不得留下任何行"
    if svc.root.is_dir():
        assert list(svc.root.rglob("*")) == [], "被拒的上传不得留下任何文件"
