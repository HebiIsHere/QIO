"""耗时事实（plan §3）：TURN_END 的 duration_ms / queue_ms / started_at / ended_at。

口径：

* core 侧用进程内单调钟测「执行窗口」，权威时长优先取 trace 台账的 duration_ms；
* queue_ms 是「受理 → 真正开跑」，是用户等的时间，不是执行时间；
* 拿不到台账时给**真实测量值**，不伪造 0、也不留 null。
"""

from __future__ import annotations

import asyncio
from datetime import datetime

from agent.core.turn import TurnManager


class _LedgerStore:
    def __init__(self, row: dict | None = None, error: Exception | None = None) -> None:
        self.row = row
        self.error = error
        self.asked: list[str] = []

    def get(self, turn_id: str):
        self.asked.append(turn_id)
        if self.error is not None:
            raise self.error
        return self.row


class _LedgerTracer:
    def __init__(self, store: _LedgerStore) -> None:
        self.store = store


def _collector() -> tuple[list[tuple[str, dict]], object]:
    events: list[tuple[str, dict]] = []

    async def emitter(name: str, data: dict) -> None:
        events.append((name, data))

    return events, emitter


def _end_event(events: list[tuple[str, dict]]) -> dict:
    ends = [data for name, data in events if name == "TURN_END"]
    assert len(ends) == 1
    return ends[0]


async def test_turn_end_carries_real_timing_facts():
    events, emitter = _collector()

    async def runner(ctx):
        await asyncio.sleep(0.03)

    manager = TurnManager(runner=runner, emitter=emitter)
    ctx = manager.submit("hi")
    await manager.wait(ctx.turn_id, timeout=3)
    await manager.shutdown()

    end = _end_event(events)
    assert end["duration_ms"] >= 20  # 真实测得的执行窗口
    assert end["queue_ms"] >= 0
    assert end["started_at"] and end["ended_at"]
    assert datetime.fromisoformat(end["ended_at"]) >= datetime.fromisoformat(end["started_at"])


async def test_queue_ms_measures_waiting_not_execution():
    events, emitter = _collector()
    release = asyncio.Event()
    started: list[str] = []

    async def runner(ctx):
        started.append(ctx.turn_id)
        if len(started) == 1:
            await release.wait()

    manager = TurnManager(runner=runner, emitter=emitter)
    first = manager.submit("first")
    while not started:  # 第一轮真的开跑之后再提交第二个
        await asyncio.sleep(0.001)
    second = manager.submit("second")
    await asyncio.sleep(0.06)  # 第二个 turn 在队列里等着：这段时间就是 queue_ms
    release.set()
    await manager.wait(second.turn_id, timeout=3)
    await manager.shutdown()

    ends = [data for name, data in events if name == "TURN_END"]
    by_turn = {d["turn_id"]: d for d in ends}
    assert by_turn[second.turn_id]["queue_ms"] >= 50  # 排队等待被如实记下
    assert by_turn[first.turn_id]["queue_ms"] <= by_turn[second.turn_id]["queue_ms"]


async def test_trace_ledger_duration_is_authoritative():
    events, emitter = _collector()
    ledger = _LedgerStore(
        {
            "duration_ms": 1234,
            "started_at": "2026-10-06T08:00:00+00:00",
            "ended_at": "2026-10-06T08:00:01.234000+00:00",
        }
    )

    async def runner(ctx):
        ctx.trace = _LedgerTracer(ledger)

    manager = TurnManager(runner=runner, emitter=emitter)
    ctx = manager.submit("hi")
    await manager.wait(ctx.turn_id, timeout=3)
    await manager.shutdown()

    end = _end_event(events)
    assert end["duration_ms"] == 1234  # 台账是权威总时长
    assert end["started_at"] == "2026-10-06T08:00:00+00:00"
    assert end["ended_at"] == "2026-10-06T08:00:01.234000+00:00"
    assert ledger.asked == [ctx.turn_id]


async def test_ledger_failure_falls_back_to_measured_duration():
    events, emitter = _collector()

    async def runner(ctx):
        ctx.trace = _LedgerTracer(_LedgerStore(error=RuntimeError("ledger down")))
        await asyncio.sleep(0.02)

    manager = TurnManager(runner=runner, emitter=emitter)
    ctx = manager.submit("hi")
    await manager.wait(ctx.turn_id, timeout=3)
    await manager.shutdown()

    end = _end_event(events)  # 台账坏了也必须照常发 TURN_END
    assert end["duration_ms"] >= 10
    assert end["started_at"] and end["ended_at"]


# ---- 服务层：TURN_END 出口用台账覆盖 core 的值 -------------------------------


def _app_ctx(tmp_path):
    from agent.api.bus import EventBus
    from agent.config import Settings
    from agent.services.app import AppContext
    from agent.storage.db import connect
    from agent.storage.migrate import apply_migrations

    conn = connect(tmp_path / "timing.db")
    apply_migrations(conn)
    return AppContext(Settings(data_dir=tmp_path), conn, EventBus())


async def test_app_turn_end_prefers_trace_ledger(tmp_path):
    ctx = _app_ctx(tmp_path)
    ctx.trace_store.begin("turn_9")
    ctx.trace_store.finish("turn_9", "completed")
    ledger_row = ctx.trace_store.get("turn_9")
    assert ledger_row is not None and ledger_row["duration_ms"] is not None

    await ctx._publish_turn_event(
        "TURN_END",
        {"turn_id": "turn_9", "status": "completed", "duration_ms": 5, "queue_ms": 7},
    )
    data = ctx.bus._history[-1].data
    assert data["duration_ms"] == ledger_row["duration_ms"]
    assert data["queue_ms"] == 7  # core 的值被保留（台账里没有这一项）
    assert data["started_at"] == ledger_row["started_at"]
    assert data["ended_at"] == ledger_row["ended_at"]


async def test_app_turn_end_without_ledger_keeps_core_values(tmp_path):
    ctx = _app_ctx(tmp_path)
    await ctx._publish_turn_event(
        "TURN_END",
        {"turn_id": "turn_missing", "status": "failed", "duration_ms": 42, "queue_ms": 3},
    )
    data = ctx.bus._history[-1].data
    assert data["duration_ms"] == 42  # 台账没有这一行：不覆盖成因 null / 0
    assert data["queue_ms"] == 3