"""审批重启恢复：不续期、恢复 waiting、executing 不自动重放（契约 §5.1、§12）。"""

from __future__ import annotations

import asyncio

import pytest

from agent.api.bus import EventBus
from agent.security.operations import PendingOperation
from agent.storage.approvals import ApprovalConflict, ApprovalStore
from agent.tools.approval import ApprovalService

from _r9_auth_support import counting_executor, make_security, record_events, wait_for


def make_operation(*, label="可恢复操作", path="C:\\work\\a.txt"):
    return PendingOperation(
        kind="test.op",
        label=label,
        payload={"value": 1},
        effect={"kind": "write", "targets": [{"kind": "path", "value": path}]},
        fingerprint={"path": path, "sha256": "abc"},
        instruction_id="instr_turn_1",
        instruction_text="把文件改成 X 并保存",
        purpose="测试",
        confirmations_required=1,
        source="tool",
    )


async def test_waiting_approval_is_restored_not_cancelled(db_conn, tmp_path):
    security = make_security(tmp_path, db_conn, mode="restricted")
    ticket = await security.approvals.submit(make_operation())
    expires_before = ticket.expires_at

    restarted = ApprovalService(EventBus(), conn=db_conn)
    events = record_events(restarted.bus)
    report = await restarted.recover()

    row = restarted.store.get(ticket.approval_id)
    assert row is not None
    assert row["state"] == "waiting", "有效 waiting 不能因为重启永久取消"
    assert row["expires_at"] == expires_before, "重启/刷新不续期（仍是原 600s 期限）"
    assert ticket.approval_id in (report["waiting"] + report["taken_over"])
    assert restarted.interrupted() == []
    required = [e for e in events if e.type.value == "APPROVAL_REQUIRED"]
    assert required and required[-1].data["phase"] == "waiting"
    # 恢复后的审批仍可被批准并真正执行
    counter: list = []
    restarted.operations.register("test.op", counting_executor(counter))
    await restarted.decide(ticket.approval_id, "approve")
    assert await wait_for(
        lambda: (restarted.store.get(ticket.approval_id) or {}).get("state") == "completed"
    )
    assert counter


async def test_expired_waiting_becomes_expired_on_restart(db_conn):
    store = ApprovalStore(db_conn)
    row = store.create(make_operation(), timeout_seconds=0.001)
    await asyncio.sleep(0.02)

    restarted = ApprovalService(EventBus(), conn=db_conn)
    report = await restarted.recover()
    assert row["approval_id"] in report["expired"]
    updated = restarted.store.get(row["approval_id"])
    assert updated["state"] == "expired"
    with pytest.raises(ApprovalConflict):
        await restarted.decide(row["approval_id"], "approve")


async def test_executing_becomes_interrupted_unverified_without_replay(db_conn, tmp_path):
    security = make_security(tmp_path, db_conn, mode="restricted")
    counter: list = []
    security.operations.register("test.op", counting_executor(counter))
    row = security.approvals.store.create(make_operation())
    security.approvals.store.decide(row["approval_id"], "approve")
    claimed = security.approvals.store.claim(row["approval_id"])
    assert claimed is not None and claimed["state"] == "executing"

    restarted = ApprovalService(EventBus(), conn=db_conn)
    events = record_events(restarted.bus)
    report = await restarted.recover()
    assert row["approval_id"] in report["interrupted"]
    updated = restarted.store.get(row["approval_id"])
    assert updated["state"] == "interrupted_unverified"
    await asyncio.sleep(0.1)
    assert counter == [], "executing 崩溃后不自动重放（可能已部分执行）"
    interrupted = restarted.interrupted()
    assert any(item["approval_id"] == row["approval_id"] for item in interrupted)
    assert any(
        item["outcome"] == "not_executed"
        for item in interrupted
        if item["approval_id"] == row["approval_id"]
    )
    phases = [e.data.get("phase") for e in events if e.type.value == "APPROVAL_RESULT"]
    assert "interrupted_unverified" in phases


async def test_legacy_request_rows_are_still_reported_interrupted(db_conn):
    """旧 request() 的内存等待随进程消失：按旧行为如实记为「那次没有执行」。"""
    first = ApprovalService(EventBus(), conn=db_conn)
    task = asyncio.create_task(first.request("tool_execution", {"description": "旧等待"}))
    await asyncio.sleep(0.05)

    restarted = ApprovalService(EventBus(), conn=db_conn)
    records = restarted.interrupted()
    assert any(item["what"] == "旧等待" for item in records)
    row = db_conn.execute(
        "SELECT status, state FROM pending_approvals WHERE status = 'interrupted'"
    ).fetchone()
    assert row is not None
    assert row["status"] == "interrupted"
    assert row["state"] == "interrupted_unverified", "旧行按新状态映射"

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
