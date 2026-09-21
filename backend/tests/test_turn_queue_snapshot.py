"""TURN_QUEUE 快照的权威性与版本号。

前端要能同时做到两件相反的事：

* **恢复**：服务器说 running=A，本地就要认 A（重连 / 丢帧后）；
* **清除**：服务器说 running=null、queued=[]，本地就必须回到空闲
  （否则服务器早跑完了，界面还停在「正在运行」）。

同时又不能让**旧快照覆盖新状态**（TURN_START(A) 之后再来一个 running=null 的旧快照）。
所以快照必须带一个单调递增的 revision，前端按它做「只接受更新的」判断。
"""

from __future__ import annotations

import asyncio
import sqlite3

import pytest
from fastapi.testclient import TestClient

from agent.api.server import create_app
from agent.config import Settings
from agent.core.turn import TurnManager


@pytest.fixture()
def client(db_conn: sqlite3.Connection, settings: Settings):
    from agent.credentials.store import MemoryKeyring

    app = create_app(settings, db_conn)
    app.state.ctx.credentials._kr = MemoryKeyring()
    with TestClient(app) as c:
        yield c


def test_queue_endpoint_returns_the_authoritative_snapshot(client):
    """resync 需要一个 HTTP 入口来取权威快照，而不是只能等 SSE。"""
    body = client.get("/api/turns/queue").json()
    assert body["running"] is None
    assert body["queued"] == []
    assert body["cancelled"] == []
    assert isinstance(body["revision"], int)


async def test_snapshot_revision_is_monotonic_across_state_changes():
    """每一次对快照有影响的状态变化都必须推进 revision。"""
    started = asyncio.Event()
    release = asyncio.Event()

    async def runner(ctx):
        if ctx.message == "A":
            started.set()
            await release.wait()

    tm = TurnManager(runner)
    rev_idle = tm.snapshot()["revision"]

    a = tm.submit("A")
    rev_accepted = tm.snapshot()["revision"]
    assert rev_accepted > rev_idle, "受理一个 turn 必须推进 revision"

    await started.wait()
    rev_running = tm.snapshot()["revision"]
    assert rev_running > rev_accepted, "开始执行必须推进 revision"
    assert tm.snapshot()["running"]["turn_id"] == a.turn_id

    release.set()
    await tm.wait(a.turn_id)
    rev_done = tm.snapshot()["revision"]
    assert rev_done > rev_running, "结束一轮必须推进 revision"
    assert tm.snapshot()["running"] is None
    await tm.shutdown()


async def test_queued_turn_also_advances_revision():
    """排队也是一次状态变化：前端要能用 revision 判断哪份快照更新。"""
    started = asyncio.Event()
    release = asyncio.Event()

    async def runner(ctx):
        if ctx.message == "A":
            started.set()
            await release.wait()

    tm = TurnManager(runner)
    tm.submit("A")
    await started.wait()
    before = tm.snapshot()["revision"]
    tm.submit("B")
    after = tm.snapshot()["revision"]
    assert after > before
    assert [q["turn_id"] for q in tm.snapshot()["queued"]]
    release.set()
    await tm.shutdown()


async def test_turn_events_carry_revision():
    """TURN_START / TURN_END 也要带 revision：否则「新状态」无法与旧快照比较。"""
    seen: list[tuple[str, int]] = []
    release = asyncio.Event()

    async def emitter(name, data):
        if name in ("TURN_START", "TURN_END"):
            seen.append((name, data.get("revision")))

    async def runner(ctx):
        await release.wait()

    tm = TurnManager(runner, emitter=emitter)
    a = tm.submit("A")
    await asyncio.sleep(0)
    release.set()
    await tm.wait(a.turn_id)
    await asyncio.sleep(0.02)

    assert [name for name, _ in seen] == ["TURN_START", "TURN_END"]
    assert all(isinstance(rev, int) for _, rev in seen)
    assert seen[1][1] >= seen[0][1]
    await tm.shutdown()


def test_queue_endpoint_revision_matches_sse_snapshot_shape(client):
    """HTTP 快照与 SSE 快照必须同源同形（同一个 snapshot()）。"""
    from agent.services.app import AppContext  # noqa: F401  (导入自证依赖方向)

    ctx = client.app.state.ctx
    body = client.get("/api/turns/queue").json()
    assert body == ctx.turns.snapshot()


def test_snapshot_and_turn_events_carry_the_instance_id(client):
    """后端重启后 revision 会从 0 重新计数：事件必须自证来自哪个实例。"""
    instance_id = client.app.state.instance_id
    body = client.get("/api/turns/queue").json()
    assert body["instance_id"] == instance_id
    assert client.get("/api/runtime/state").json()["instance_id"] == instance_id
