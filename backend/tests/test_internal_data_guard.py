"""内部运行数据守卫：受保护路径、三次确认、取消、范围变化、全部模式（契约 §4、§12）。"""

from __future__ import annotations

import asyncio

import pytest

from agent.security import internal_data
from agent.security.effects import Effect, EffectKind, Target, Verdict
from agent.security.operations import PendingOperation
from agent.storage.approvals import ApprovalConflict, ApprovalConfirmRequired

from _r9_auth_support import counting_executor, make_security, record_events, wait_for


def internal_operation(path, *, payload=None, kind="internal.data_write", confirmations=3):
    return PendingOperation(
        kind=kind,
        label="直接维护 QIO 内部运行数据",
        payload=dict(payload or {"content": "v1"}),
        effect={
            "kind": "internal_data",
            "targets": [{"kind": "path", "value": str(path)}],
            "purpose": "测试直接维护内部数据",
            "detail": {"warning": internal_data.INTERNAL_DATA_WARNING},
        },
        fingerprint={"path": str(path), "content_sha256": "abc"},
        instruction_text="直接改内部数据库",
        purpose="测试",
        confirmations_required=confirmations,
        source="tool",
    )


def test_protected_path_detection(tmp_path):
    db_path = tmp_path / "app.db"
    protected = internal_data.protected_list([db_path, tmp_path / "config.toml"])
    assert internal_data.is_internal_data_path(db_path, protected)
    assert internal_data.is_internal_data_path(tmp_path / "config.toml", protected)
    assert not internal_data.is_internal_data_path(tmp_path / "workspace" / "a.txt", protected)
    assert internal_data.is_sensitive_credential_path(tmp_path / ".env")
    assert internal_data.is_sensitive_credential_path(tmp_path / ".git" / "config")
    assert not internal_data.is_sensitive_credential_path(tmp_path / "a.txt")
    assert internal_data.CONFIRMATIONS_REQUIRED == 3
    assert "内部运行数据" in internal_data.INTERNAL_DATA_WARNING


def test_confirmations_required_for_effect(tmp_path):
    protected = internal_data.protected_list([tmp_path / "app.db"])
    effect = Effect(
        kind=EffectKind.WRITE,
        targets=(Target("path", str(tmp_path / "app.db")),),
    )
    assert internal_data.requires_three_confirmations(effect, protected)
    assert internal_data.confirmations_for(effect, protected) == 3
    normal = Effect(kind=EffectKind.WRITE, targets=(Target("path", str(tmp_path / "a.txt")),))
    assert not internal_data.requires_three_confirmations(normal, protected)
    assert internal_data.confirmations_for(normal, protected) == 1


@pytest.mark.parametrize("mode", ["read_only", "restricted", "unrestricted"])
async def test_engine_requires_three_confirmations_in_all_modes(tmp_path, db_conn, mode):
    protected = tmp_path / "app.db"
    protected.write_text("db", encoding="utf-8")
    security = make_security(tmp_path, db_conn, mode=mode, protected=[protected])
    effect = Effect(
        kind=EffectKind.WRITE,
        targets=(Target("path", str(protected)),),
        purpose="直接维护内部数据库",
    )
    decision = await security.engine.authorize(effect)
    assert decision.verdict == Verdict.APPROVE
    assert "三次确认" in decision.reason
    assert "内部运行数据" in decision.reason


async def test_three_confirmations_cas_and_execution(db_conn, tmp_path):
    protected = tmp_path / "app.db"
    protected.write_text("db", encoding="utf-8")
    security = make_security(tmp_path, db_conn, mode="restricted", protected=[protected])
    counter: list = []
    security.operations.register("internal.data_write", counting_executor(counter))
    events = record_events(security.bus)

    ticket = await security.approvals.submit(internal_operation(protected))
    assert ticket.confirmations_required == 3
    # 未完成三次确认前不能直接批准
    with pytest.raises(ApprovalConfirmRequired):
        await security.approvals.decide(ticket.approval_id, "approve")
    # 后端 CAS：阶段必须连续
    with pytest.raises(ApprovalConflict):
        await security.approvals.confirm(ticket.approval_id, 2)
    stage1 = await security.approvals.confirm(ticket.approval_id, 1)
    assert stage1["confirmed_stage"] == 1 and stage1["state"] == "waiting"
    stage2 = await security.approvals.confirm(ticket.approval_id, 2)
    assert stage2["confirmed_stage"] == 2 and stage2["state"] == "waiting"
    stage3 = await security.approvals.confirm(ticket.approval_id, 3)
    assert stage3["state"] in ("approved", "executing", "completed")
    assert await wait_for(
        lambda: (security.approvals.store.get(ticket.approval_id) or {}).get("state") == "completed"
    )
    assert counter == [internal_operation(protected).payload]
    stages = [
        event.data.get("confirmed_stage")
        for event in events
        if event.type.value == "APPROVAL_RESULT" and event.data.get("phase") == "confirm_stage"
    ]
    assert stages == [1, 2]


async def test_cancel_at_any_stage_stops(db_conn, tmp_path):
    protected = tmp_path / "app.db"
    protected.write_text("db", encoding="utf-8")
    security = make_security(tmp_path, db_conn, mode="restricted", protected=[protected])
    counter: list = []
    security.operations.register("internal.data_write", counting_executor(counter))
    ticket = await security.approvals.submit(internal_operation(protected))
    await security.approvals.confirm(ticket.approval_id, 1)
    cancelled = await security.approvals.decide(ticket.approval_id, "cancel")
    assert cancelled["state"] == "cancelled"
    await asyncio.sleep(0.05)
    assert counter == []
    with pytest.raises(ApprovalConflict):
        await security.approvals.confirm(ticket.approval_id, 2)


async def test_scope_or_content_change_needs_new_approval(db_conn, tmp_path):
    protected = tmp_path / "app.db"
    protected.write_text("db", encoding="utf-8")
    security = make_security(tmp_path, db_conn, mode="restricted", protected=[protected])
    counter: list = []
    security.operations.register("internal.data_write", counting_executor(counter))
    first = await security.approvals.submit(internal_operation(protected, payload={"content": "v1"}))
    await security.approvals.confirm(first.approval_id, 1)
    # 内容变化 -> 新审批单（确认阶段从 0 开始，不复用旧状态）
    second = await security.approvals.submit(internal_operation(protected, payload={"content": "v2"}))
    second_row = security.approvals.store.get(second.approval_id)
    assert second.approval_id != first.approval_id
    assert second_row["confirmed_stage"] == 0
    assert second_row["state"] == "waiting"
    # 旧的确认不能拿来批准新的（不同 approval id）
    with pytest.raises(ApprovalConflict):
        await security.approvals.confirm(second.approval_id, 2)
    # 新审批走完三次确认后才执行，执行的是新内容
    await security.approvals.confirm(second.approval_id, 1)
    await security.approvals.confirm(second.approval_id, 2)
    await security.approvals.confirm(second.approval_id, 3)
    assert await wait_for(
        lambda: (security.approvals.store.get(second.approval_id) or {}).get("state") == "completed"
    )
    assert counter == [{"content": "v2"}]
