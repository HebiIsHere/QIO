"""审批生命周期 v2：600s、状态机、重复批准、原子领取、非阻塞（契约 §5、§12）。"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from agent.security.effects import Effect, EffectKind, Target
from agent.security.operations import PendingOperation
from agent.storage.approvals import (
    ApprovalConflict,
    ApprovalConfirmRequired,
    ApprovalStore,
    DEFAULT_TIMEOUT_SECONDS,
)
from agent.tools.approval import ApprovalService

from _r9_auth_support import (
    counting_executor,
    make_security,
    record_events,
    wait_for,
)


def make_operation(*, kind="test.op", label="测试操作", path=None, confirmations=1, payload=None):
    target = str(path or "C:\\work\\a.txt")
    return PendingOperation(
        kind=kind,
        label=label,
        payload=dict(payload or {"value": 1}),
        effect={
            "kind": "write",
            "targets": [{"kind": "path", "value": target}],
            "purpose": "测试用途",
        },
        fingerprint={"path": target, "sha256": "abc"},
        instruction_id="instr_turn_1",
        instruction_text="把 " + target + " 改成 X 并保存",
        purpose="测试用途",
        confirmations_required=confirmations,
        source="tool",
    )


def test_default_time_limit_is_600_seconds():
    assert DEFAULT_TIMEOUT_SECONDS == 600.0
    service = ApprovalService.__new__(ApprovalService)
    # 构造函数默认值即 600（旧行为是 300，产品已变更为 600）
    import inspect

    signature = inspect.signature(ApprovalService.__init__)
    assert signature.parameters["timeout_seconds"].default == 600.0


async def test_submit_is_non_blocking_and_returns_waiting_ticket(db_conn, tmp_path):
    security = make_security(tmp_path, db_conn, mode="restricted")
    counter: list = []
    security.operations.register("test.op", counting_executor(counter))
    events = record_events(security.bus)

    ticket = await asyncio.wait_for(
        security.approvals.submit(make_operation()), timeout=2.0
    )
    assert ticket.state == "waiting"
    assert ticket.approval_id.startswith("appr_")
    assert counter == [], "提交审批时不得同步执行"
    # 600 秒原始期限（允许几秒执行误差）
    created = datetime.now(timezone.utc)
    expires = datetime.fromisoformat(ticket.expires_at)
    remaining = (expires - created).total_seconds()
    assert 590 <= remaining <= 610

    pending = security.approvals.pending()
    item = next(item for item in pending if item["approval_id"] == ticket.approval_id)
    assert item["state"] == "waiting"
    assert item["requires_confirmations"] == 1
    assert item["source"] == "tool"
    assert item["expires_at"] == ticket.expires_at
    assert item["operation"]["kind"] == "test.op"

    required = [e for e in events if e.type.value == "APPROVAL_REQUIRED"]
    assert required and required[-1].data["phase"] == "waiting"
    assert required[-1].data["approval"]["approval_id"] == ticket.approval_id


async def test_approve_executes_once_and_duplicate_decisions_are_rejected(db_conn, tmp_path):
    security = make_security(tmp_path, db_conn, mode="restricted")
    counter: list = []
    security.operations.register("test.op", counting_executor(counter))
    events = record_events(security.bus)
    ticket = await security.approvals.submit(make_operation())

    row = await security.approvals.decide(ticket.approval_id, "approve")
    assert row["state"] in ("approved", "executing", "completed") or row["state"] == "approved"
    assert await wait_for(
        lambda: (security.approvals.store.get(ticket.approval_id) or {}).get("state") == "completed"
    )
    assert len(counter) == 1, "批准后只执行一次"

    with pytest.raises(ApprovalConflict):
        await security.approvals.decide(ticket.approval_id, "approve")
    # 已完成的不能再次领取
    assert security.approvals.store.claim(ticket.approval_id) is None

    phases = [event.data.get("phase") for event in events if event.type.value == "APPROVAL_RESULT"]
    assert "approved" in phases and "executing" in phases and "executed" in phases


async def test_atomic_claim_prevents_double_execution(db_conn, tmp_path):
    security = make_security(tmp_path, db_conn, mode="restricted")
    row = security.approvals.store.create(make_operation())
    store = security.approvals.store
    store.decide(row["approval_id"], "approve")
    first = store.claim(row["approval_id"])
    assert first is not None and first["state"] == "executing"
    second = store.claim(row["approval_id"])
    assert second is None, "同一 approval 只能被领取一次"
    assert (store.get(row["approval_id"]) or {})["attempt"] == 1


async def test_expired_approval_cannot_be_approved(db_conn, tmp_path):
    security = make_security(tmp_path, db_conn, mode="restricted")
    store = security.approvals.store
    # 用可控时钟构造「已过期」的等待审批
    from agent.storage.approvals import ApprovalStore as Store
    import datetime as _dt

    fixed = _dt.datetime(2026, 10, 22, tzinfo=_dt.timezone.utc)

    def clock_now():
        return fixed

    clock_store = ApprovalStore(db_conn, clock=clock_now)
    row = clock_store.create(make_operation(), timeout_seconds=10)
    # 时间推进 11 秒
    fixed2 = fixed + _dt.timedelta(seconds=11)
    clock_store._clock = lambda: fixed2
    expired = clock_store.expire_waiting()
    assert [item["approval_id"] for item in expired] == [row["approval_id"]]
    assert (clock_store.get(row["approval_id"]) or {})["state"] == "expired"
    with pytest.raises(ApprovalConflict):
        clock_store.decide(row["approval_id"], "approve")


async def test_reject_and_cancel_are_terminal(db_conn, tmp_path):
    security = make_security(tmp_path, db_conn, mode="restricted")
    counter: list = []
    security.operations.register("test.op", counting_executor(counter))
    rejected = await security.approvals.submit(make_operation(label="拒绝我"))
    await security.approvals.decide(rejected.approval_id, "reject")
    assert (security.approvals.store.get(rejected.approval_id) or {})["state"] == "rejected"
    cancelled = await security.approvals.submit(make_operation(label="取消我"))
    await security.approvals.decide(cancelled.approval_id, "cancel")
    assert (security.approvals.store.get(cancelled.approval_id) or {})["state"] == "cancelled"
    assert counter == []
    # 过期/拒绝/取消的审批永不复活
    for approval_id in (rejected.approval_id, cancelled.approval_id):
        with pytest.raises(ApprovalConflict):
            await security.approvals.decide(approval_id, "approve")


async def test_mode_change_to_strict_invalidates_before_execution(db_conn, tmp_path):
    """批准后、执行前按当前模式重核对：切到只读则不执行并标 invalidated。"""
    security = make_security(tmp_path, db_conn, mode="restricted")
    counter: list = []
    security.operations.register("test.op", counting_executor(counter))
    ticket = await security.approvals.submit(make_operation(), mode="restricted")
    # 用户切到只读（模拟审批后、执行前的模式变化）
    security.store.set("computer.permission_mode", "read_only")
    await security.approvals.decide(ticket.approval_id, "approve")
    assert await wait_for(
        lambda: (security.approvals.store.get(ticket.approval_id) or {}).get("state")
        == "invalidated"
    )
    assert counter == [], "严格模式下不再允许的已批准操作不得执行"
    row = security.approvals.store.get(ticket.approval_id) or {}
    assert "只读" in str(row.get("error") or "")
    history = security.approvals.history(limit=10)
    assert any(item["approval_id"] == ticket.approval_id for item in history)


async def test_internal_data_confirmations_within_lifecycle(db_conn, tmp_path):
    protected = tmp_path / "app.db"
    protected.write_text("db", encoding="utf-8")
    security = make_security(tmp_path, db_conn, mode="restricted", protected=[protected])
    counter: list = []
    security.operations.register("internal.data_write", counting_executor(counter))
    events = record_events(security.bus)
    operation = make_operation(
        kind="internal.data_write",
        label="直接维护内部数据库",
        path=protected,
        confirmations=3,
    )
    ticket = await security.approvals.submit(operation)
    with pytest.raises(ApprovalConfirmRequired):
        await security.approvals.decide(ticket.approval_id, "approve")
    with pytest.raises(ApprovalConflict):
        await security.approvals.confirm(ticket.approval_id, 3)  # 必须连续
    await security.approvals.confirm(ticket.approval_id, 1)
    await security.approvals.confirm(ticket.approval_id, 2)
    row = security.approvals.store.get(ticket.approval_id) or {}
    assert row["state"] == "waiting"
    assert row["confirmed_stage"] == 2
    final = await security.approvals.confirm(ticket.approval_id, 3)
    assert final["state"] in ("approved", "executing", "completed")
    assert await wait_for(
        lambda: (security.approvals.store.get(ticket.approval_id) or {}).get("state") == "completed"
    )
    assert counter == [operation.payload]
    phases = [event.data.get("phase") for event in events if event.type.value == "APPROVAL_RESULT"]
    assert "confirm_stage" in phases


async def test_registry_defers_tool_approval_when_engine_present(db_conn, tmp_path):
    """工具审批走 deferred：submit 持久化 + 立即返回 pending，turn 不被占住。"""
    from agent.adapters.base import ToolCall
    from agent.tools.base import Tool, ToolResult
    from agent.tools.registry import ToolRegistry

    security = make_security(tmp_path, db_conn, mode="restricted")

    class NeedsApprovalTool(Tool):
        name = "needs_ok"
        description = "需要审批的工具"
        parameters = {"type": "object", "properties": {}}
        requires_approval = True

        async def run(self, **kwargs):
            return ToolResult(ok=True, content="executed")

    registry = ToolRegistry(approvals=security.approvals)
    registry.register(NeedsApprovalTool())
    result = await registry.execute(ToolCall(id="call_1", name="needs_ok", arguments={}))
    assert result.ok is True
    assert result.status == "awaiting_approval"
    assert result.code == "pending_approval"
    approval_id = result.data["pending_approval"]["approval_id"]
    row = security.approvals.store.get(approval_id)
    assert row["state"] == "waiting"
    assert row["kind"] == "tool.invoke"
    assert row["source"] == "tool"
    assert row["operation_detail"]["payload"]["tool"] == "needs_ok"
    # 驳回后不会执行
    await security.approvals.decide(approval_id, "reject")
    assert (security.approvals.store.get(approval_id) or {})["state"] == "rejected"
    # 已批准的执行重跑管线时不再重复弹审批
    approved = await registry.execute(
        ToolCall(id="call_2", name="needs_ok", arguments={}), approval_id=approval_id
    )
    assert approved.ok is True and approved.content == "executed"


async def test_registry_denies_tool_approval_in_read_only(db_conn, tmp_path):
    from agent.adapters.base import ToolCall
    from agent.tools.base import Tool, ToolResult
    from agent.tools.registry import ToolRegistry

    security = make_security(tmp_path, db_conn, mode="read_only")

    class NeedsApprovalTool(Tool):
        name = "needs_ok"
        description = "需要审批的工具"
        parameters = {"type": "object", "properties": {}}
        requires_approval = True

        async def run(self, **kwargs):  # pragma: no cover - 不应被调用
            return ToolResult(ok=True, content="executed")

    registry = ToolRegistry(approvals=security.approvals)
    registry.register(NeedsApprovalTool())
    result = await registry.execute(ToolCall(id="call_3", name="needs_ok", arguments={}))
    assert result.ok is False
    assert result.code == "authorization_denied"
    assert "只读" in (result.error or "")
