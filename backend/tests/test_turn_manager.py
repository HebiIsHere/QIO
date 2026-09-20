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
    assert a.status == "completed"
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


# ---------------------------------------------------------------------------
# 状态契约：accepted / queued / running / terminal
# ---------------------------------------------------------------------------


async def test_waiting_turn_is_queued_not_accepted():
    """排队中的 turn 必须能被识别成 queued：accepted ≠ queued ≠ running。"""
    started = asyncio.Event()
    release = asyncio.Event()

    async def runner(ctx):
        if ctx.message == "A":
            started.set()
            await release.wait()

    tm = TurnManager(runner)
    a = tm.submit("A")
    b = tm.submit("B")
    assert a.status == "accepted"  # 刚提交、还没有 worker 接手
    await started.wait()
    assert a.status == "running"
    assert b.status == "queued"
    release.set()
    await tm.wait(a.turn_id)
    await tm.wait(b.turn_id)
    assert a.status == "completed"
    assert b.status == "completed"
    await tm.shutdown()


async def test_cancelled_queued_turn_never_emits_turn_lifecycle_events():
    """取消的排队 turn 是 tombstone：不得再进入 running，也不得发 TURN_START/TURN_END。

    真实缺陷：cancel 只把它从 _pending 摘掉，对象仍留在底层 asyncio.Queue 里；
    worker 后来取到它时会先设置 active 并发 TURN_START，前端因此会看到
    「B 开始运行」，随后又收到 TURN_END —— 一个从未执行的 turn 污染了界面状态。
    """
    started = asyncio.Event()
    release = asyncio.Event()
    seen: list[tuple[str, dict]] = []

    async def emitter(name, data):
        seen.append((name, data))

    async def runner(ctx):
        if ctx.message == "A":
            started.set()
            await release.wait()

    tm = TurnManager(runner, emitter=emitter)
    a = tm.submit("A")
    b = tm.submit("B")
    await started.wait()
    assert tm.cancel(b.turn_id) is True
    release.set()
    await tm.wait(a.turn_id)
    await asyncio.sleep(0.02)

    assert [name for name, data in seen if data.get("turn_id") == b.turn_id] == []
    assert b.status == "cancelled"
    assert a.status == "completed"
    await tm.shutdown()


async def test_terminal_turn_never_returns_to_running():
    """终态单向：cancelled 之后不得再被迁移回 running。"""
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
    tm.cancel(b.turn_id)
    release.set()
    await tm.wait(a.turn_id)
    await tm.wait(b.turn_id)
    await asyncio.sleep(0.02)

    assert ran == ["A"]
    assert b.status == "cancelled"
    assert b.turn_start_emitted is False
    assert b.turn_end_emitted is False
    await tm.shutdown()


async def test_wait_timeout_does_not_leak_future():
    """wait 超时后不得把 future 永久留在等待表里。"""
    release = asyncio.Event()

    async def runner(ctx):
        await release.wait()

    tm = TurnManager(runner)
    a = tm.submit("A")
    await asyncio.sleep(0)
    assert a.turn_id in tm._futures
    assert await tm.wait(a.turn_id, timeout=0.01) is None
    assert a.turn_id not in tm._futures
    release.set()
    await asyncio.sleep(0.05)
    await tm.shutdown()


async def test_shutdown_resolves_pending_waits():
    """应用关闭：排队中与运行中的等待都不能悬挂。"""
    started = asyncio.Event()
    release = asyncio.Event()

    async def runner(ctx):
        if ctx.message == "A":
            started.set()
            await release.wait()

    tm = TurnManager(runner)
    a = tm.submit("A")
    b = tm.submit("B")
    await started.wait()

    waiting_a = asyncio.create_task(tm.wait(a.turn_id))
    waiting_b = asyncio.create_task(tm.wait(b.turn_id))
    await asyncio.sleep(0)

    await asyncio.wait_for(tm.shutdown(), timeout=2.0)

    result_a = await asyncio.wait_for(waiting_a, timeout=1.0)
    result_b = await asyncio.wait_for(waiting_b, timeout=1.0)
    assert result_a == {"ok": False, "reason": "cancelled"}
    assert result_b == {"ok": False, "reason": "shutdown"}
    assert a.status == "cancelled"
    assert b.status == "cancelled"
    assert tm.snapshot()["running"] is None
    assert tm.snapshot()["queued"] == []
