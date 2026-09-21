"""重连之后的权威状态：Turn 队列之外，还有「等待中的审批」和「在跑/排队的独立任务」。

事件流是增量；断线期间丢掉的 `APPROVAL_REQUIRED` / `SUBAGENT_STATUS` 如果不提供
查询入口，界面就会永久停在错误状态（审批永远不出现、任务卡永远停在「进行中」）。
所以 `GET /api/runtime/state` 与 RESYNC 配套：客户端拿到边界后拉一次权威快照。
"""

from __future__ import annotations

import asyncio
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from agent.api.bus import EventBus
from agent.api.server import create_app
from agent.config import Settings
from agent.tools.approval import ApprovalService


@pytest.fixture()
def client(db_conn: sqlite3.Connection, settings: Settings):
    from agent.credentials.store import MemoryKeyring

    app = create_app(settings, db_conn)
    app.state.ctx.credentials._kr = MemoryKeyring()
    with TestClient(app) as c:
        yield c


def test_runtime_state_reports_empty_defaults(client):
    body = client.get("/api/runtime/state").json()

    assert body["instance_id"] == client.app.state.instance_id
    assert isinstance(body["revision"], int)
    assert body["turn_queue"]["running"] is None
    assert body["turn_queue"]["queued"] == []
    assert body["approvals"] == []
    assert body["tasks"] == []


def test_runtime_state_exposes_pending_approval_for_reconnect(client):
    """断线错过的 APPROVAL_REQUIRED，重连后必须能从这个快照恢复出来。"""
    ctx = client.app.state.ctx

    async def arm():
        ctx.approvals.set_context(turn_id="turn_a", session_id="sess_a")
        return asyncio.create_task(
            ctx.approvals.request("computer", {"action": "read", "path": "/tmp/x"})
        )

    client.portal.call(arm)

    body = client.get("/api/runtime/state").json()
    assert len(body["approvals"]) == 1
    approval = body["approvals"][0]
    assert approval["kind"] == "computer"
    assert approval["turn_id"] == "turn_a"
    assert approval["request_digest"]
    assert approval["expires_at"]
    assert approval["payload"]["action"] == "read"

    # 应答之后就不再是「待办」了
    resp = client.post(
        f"/api/approvals/{approval['approval_id']}/respond", json={"decision": "approved"}
    )
    assert resp.status_code == 200
    assert client.get("/api/runtime/state").json()["approvals"] == []


def test_runtime_state_reports_running_and_queued_tasks(client):
    ctx = client.app.state.ctx

    async def arm():
        from agent.tools.base import ToolResult

        # 把并发额度压到 1，让「第二个任务真的在排队」变成确定性事实，
        # 而不是靠提交 5 个任务去碰运气。
        ctx.task_manager._semaphore = asyncio.Semaphore(1)
        gate = asyncio.Event()

        async def slow() -> ToolResult:
            await gate.wait()
            return ToolResult(ok=True, content="done")

        async def quick() -> ToolResult:
            return ToolResult(ok=True, content="quick")

        first = ctx.task_manager.submit("research", slow, task_id="task_run")
        second = ctx.task_manager.submit("research", quick, task_id="task_queue")
        await asyncio.sleep(0.05)
        return gate, first, second

    gate, first, second = client.portal.call(arm)
    body = client.get("/api/runtime/state").json()
    by_id = {t["task_id"]: t for t in body["tasks"]}

    assert by_id["task_run"]["status"] == "running"
    assert by_id["task_queue"]["status"] == "queued"
    assert by_id["task_run"]["tool"] == "research"

    async def release():
        gate.set()
        await asyncio.sleep(0.1)

    client.portal.call(release)
    assert client.get("/api/runtime/state").json()["tasks"] == []


# ---------------------------------------------------------------------------
# Approval 生命周期：pending → approved / rejected / expired
# ---------------------------------------------------------------------------


async def _collect(bus: EventBus) -> list[dict]:
    events: list[dict] = []

    async def consume() -> None:
        async for chunk in bus.stream():
            for line in chunk.splitlines():
                if line.startswith("data: "):
                    events.append(json.loads(line[6:]))

    task = asyncio.create_task(consume())
    await asyncio.sleep(0)
    return events, task


async def test_approval_timeout_notifies_clients_so_the_ui_stops_offering_it():
    """后台超时 = 这条审批已经过期，界面必须能把它清掉（不能一直给 Allow/Reject）。"""
    bus = EventBus()
    service = ApprovalService(bus, timeout_seconds=0.05)
    events, consumer = await _collect(bus)

    result = await service.request("computer", {"action": "read"})
    assert result.decision == "timeout"
    await asyncio.sleep(0.05)
    consumer.cancel()

    results = [e for e in events if e["type"] == "APPROVAL_RESULT"]
    assert results, "过期必须让客户端知道"
    assert results[-1]["data"]["decision"] == "timeout"
    assert results[-1]["data"]["approval_id"] == result.approval_id
    assert service.pending() == []


async def test_pending_list_only_contains_live_requests():
    service = ApprovalService(EventBus(), timeout_seconds=5.0)
    service.set_context(turn_id="t1", session_id="s1")
    pending = asyncio.create_task(service.request("computer", {"action": "read"}))
    await asyncio.sleep(0.05)

    live = service.pending()
    assert len(live) == 1
    assert live[0]["turn_id"] == "t1" and live[0]["session_id"] == "s1"

    assert await service.respond(live[0]["approval_id"], "rejected") is True
    await pending
    assert service.pending() == []


async def test_cancelled_wait_notifies_clients_and_drops_the_request():
    """等待审批的那一轮被取消（用户按 Stop）→ 审批也必须有明确结局。

    否则服务端已经不认这条审批了，界面还留着 Allow / Reject，
    用户点下去只会得到 404。
    """
    bus = EventBus()
    service = ApprovalService(bus, timeout_seconds=30.0)
    service.set_context(turn_id="t1")
    events, consumer = await _collect(bus)

    pending = asyncio.create_task(service.request("computer", {"action": "read"}))
    await asyncio.sleep(0.05)
    approval_id = service.pending()[0]["approval_id"]

    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    await asyncio.sleep(0.05)
    consumer.cancel()

    results = [e for e in events if e["type"] == "APPROVAL_RESULT"]
    assert results and results[-1]["data"]["approval_id"] == approval_id
    assert results[-1]["data"]["decision"] == "cancelled"
    assert service.pending() == []


async def test_expired_request_is_not_listed_and_cannot_be_answered():
    service = ApprovalService(EventBus(), timeout_seconds=5.0)
    service.set_context(turn_id="t1")
    pending = asyncio.create_task(service.request("computer", {"action": "read"}))
    await asyncio.sleep(0.05)
    approval_id = service.pending()[0]["approval_id"]

    service._requests[approval_id].expires_at = "2000-01-01T00:00:00+00:00"
    assert service.pending() == [], "过期的不该出现在待办里"
    assert await service.respond(approval_id, "approved") is False

    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
