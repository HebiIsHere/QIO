"""审批跨重启：重启后不恢复等待，但那次操作不能凭空消失。

等待中的审批以前只活在进程内存里：重启后它既不会执行，也没人告诉用户
「那件事没做」。这里把它落成一条明确记录（status = interrupted），
读出来就是「那次操作没有执行」。
"""

from __future__ import annotations

import asyncio
import sqlite3

from agent.api.bus import EventBus
from agent.tools.approval import ApprovalService


def _service(conn=None) -> ApprovalService:
    return ApprovalService(EventBus(), timeout_seconds=5, conn=conn)


async def test_pending_approval_survives_as_an_interrupted_record(
    db_conn: sqlite3.Connection,
):
    first = _service(db_conn)
    task = asyncio.create_task(
        first.request("tool_execution", {"description": "想运行一段命令"})
    )
    await asyncio.sleep(0.05)
    assert first.pending(), "请求期间应该还在等待"

    # 「重启」：同一把连接上换一个进程内的实例
    restarted = _service(db_conn)
    records = restarted.interrupted()
    assert len(records) == 1
    assert records[0]["kind"] == "tool_execution"
    assert records[0]["what"] == "想运行一段命令"
    assert records[0]["outcome"] == "not_executed"

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


async def test_answered_approval_is_not_reported_as_interrupted(
    db_conn: sqlite3.Connection,
):
    first = _service(db_conn)
    task = asyncio.create_task(
        first.request("tool_create", {"description": "想创建一个工具"})
    )
    await asyncio.sleep(0.05)
    approval_id = first.pending()[0]["approval_id"]
    assert await first.respond(approval_id, "approved") is True
    await task

    restarted = _service(db_conn)
    assert restarted.interrupted() == []


def test_without_a_database_nothing_changes():
    """老装配（不传 conn）必须照旧工作：不落库、不报错。"""
    service = _service(None)
    assert service.interrupted() == []


async def test_app_context_wires_the_connection_into_approvals(tmp_path):
    """真实装配必须把连接交进来，否则重启后什么记录都没有。"""
    from agent.config import Settings
    from agent.services.app import AppContext
    from agent.storage.db import connect
    from agent.storage.migrate import apply_migrations

    conn = connect(tmp_path / "app.db")
    apply_migrations(conn)
    ctx = AppContext(Settings(data_dir=tmp_path), conn, EventBus())

    task = asyncio.create_task(
        ctx.approvals.request("tool_execution", {"description": "想运行一段命令"})
    )
    await asyncio.sleep(0.05)

    restarted = ApprovalService(EventBus(), conn=conn)
    assert [r["what"] for r in restarted.interrupted()] == ["想运行一段命令"]

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
