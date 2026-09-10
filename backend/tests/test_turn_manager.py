from __future__ import annotations

import asyncio

from agent.core.turn import TurnManager


class _FakeLoop:
    def __init__(self) -> None:
        self.notices: list[str] = []
        self.cancelled = False

    def push_notice(self, text: str) -> None:
        self.notices.append(text)

    def cancel(self) -> None:
        self.cancelled = True


async def test_single_flight_and_fifo_order():
    order: list[str] = []
    peak = 0
    running = 0

    async def runner(ctx):
        nonlocal peak, running
        running += 1
        peak = max(peak, running)
        order.append(ctx.message)
        await asyncio.sleep(0.01)
        running -= 1
        ctx.result = {"ok": True, "msg": ctx.message}

    tm = TurnManager(runner)
    a = tm.submit("A")
    b = tm.submit("B")
    c = tm.submit("C")
    ra = await tm.wait(a.turn_id)
    await tm.wait(b.turn_id)
    await tm.wait(c.turn_id)

    assert order == ["A", "B", "C"]
    assert peak == 1  # 主循环最大并发数 = 1
    assert ra == {"ok": True, "msg": "A"}
    assert a.status == "done"
    await tm.shutdown()


async def test_no_message_dropped():
    seen: list[str] = []

    async def runner(ctx):
        seen.append(ctx.message)

    tm = TurnManager(runner)
    ids = [tm.submit(str(i)).turn_id for i in range(5)]
    for tid in ids:
        await tm.wait(tid)
    assert seen == ["0", "1", "2", "3", "4"]
    await tm.shutdown()


async def test_notice_routes_to_active_turn_only():
    started = asyncio.Event()
    release = asyncio.Event()

    async def runner(ctx):
        ctx.loop = _FakeLoop()
        started.set()
        await release.wait()

    tm = TurnManager(runner)
    a = tm.submit("A")
    await started.wait()
    # active turn 存在 → notice 进它的 loop
    assert tm.push_notice("hello") is True
    assert a.loop.notices == ["hello"]
    assert tm.active_loop() is a.loop
    release.set()
    await tm.wait(a.turn_id)
    # 结束后无 active → 不再路由
    assert tm.push_notice("late") is False
    await tm.shutdown()


async def test_cancel_targets_active_loop():
    started = asyncio.Event()
    release = asyncio.Event()

    async def runner(ctx):
        ctx.loop = _FakeLoop()
        started.set()
        await release.wait()

    tm = TurnManager(runner)
    a = tm.submit("A")
    await started.wait()
    assert tm.cancel_active() is True
    assert a.loop.cancelled is True
    assert a.cancelled is True
    release.set()
    await tm.wait(a.turn_id)
    # idle 时 cancel 无目标
    assert tm.cancel_active() is False
    await tm.shutdown()


async def test_runner_exception_marks_failed_not_crash():
    async def runner(ctx):
        raise RuntimeError("boom")

    tm = TurnManager(runner)
    a = tm.submit("A")
    await tm.wait(a.turn_id)
    assert a.status == "failed"
    assert "boom" in (a.error or "")
    await tm.shutdown()


async def test_snapshot_reflects_running_and_queued():
    started = asyncio.Event()
    release = asyncio.Event()

    async def runner(ctx):
        started.set()
        await release.wait()

    tm = TurnManager(runner)
    a = tm.submit("A")
    b = tm.submit("B")
    c = tm.submit("C")
    await started.wait()
    snap = tm.snapshot()
    assert snap["running"]["turn_id"] == a.turn_id
    assert [q["turn_id"] for q in snap["queued"]] == [b.turn_id, c.turn_id]
    release.set()
    await tm.wait(a.turn_id)
    await tm.shutdown()


async def test_cancel_queued_turn_removes_it():
    started = asyncio.Event()
    release = asyncio.Event()
    ran: list[str] = []

    async def runner(ctx):
        ran.append(ctx.message)
        if ctx.message == "A":
            started.set()
            await release.wait()

    tm = TurnManager(runner)
    a = tm.submit("A")
    b = tm.submit("B")
    await started.wait()
    assert tm.cancel(b.turn_id) is True
    assert tm.queued_count() == 0
    assert [c["turn_id"] for c in tm.snapshot()["cancelled"]] == [b.turn_id]
    release.set()
    await tm.wait(a.turn_id)
    await tm.wait(b.turn_id)
    assert "B" not in ran  # 被取消的排队 turn 不会执行
    assert b.status == "cancelled"
    await tm.shutdown()
