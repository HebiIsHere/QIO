"""状态序列测试：把前后端共同依赖的 turn 生命周期当成一段**序列**来验证。

这一轮的很多缺陷不是「某个函数写错了」，而是「每一步都看起来对、组合起来错」：

    A 正在运行 → 用户发送 B（后端只是排队）→ 前端却把 B 当成 active
    → Stop 打到 B，且真正在跑那一轮的 TURN_END 被当成旧事件丢掉。

所以这里按事件序列断言，而不是只断言单个字段。
"""

from __future__ import annotations

import asyncio

from agent.core.turn import TurnManager


class _Recorder:
    """记录 (事件名, turn_id, status) 序列 —— 前端看到的就是这一串。"""

    def __init__(self) -> None:
        self.events: list[tuple[str, str, str]] = []

    async def __call__(self, name: str, data: dict) -> None:
        self.events.append(
            (name, str(data.get("turn_id") or ""), str(data.get("status") or ""))
        )

    def names_for(self, turn_id: str) -> list[str]:
        return [name for name, tid, _ in self.events if tid == turn_id]


async def test_sequence_continuous_send_active_changes_only_at_turn_start():
    """A START → B SEND(queued) → A END → B START → B END"""
    started = asyncio.Event()
    release = asyncio.Event()
    recorder = _Recorder()

    async def runner(ctx):
        if ctx.message == "A":
            started.set()
            await release.wait()

    tm = TurnManager(runner, emitter=recorder)
    a = tm.submit("A")
    await started.wait()
    assert tm.active is a

    b = tm.submit("B")
    # 受理 B 不得抢走 active —— 它只排队
    assert tm.active is a
    assert b.status == "queued"
    assert tm.queued_count() == 1

    release.set()
    await tm.wait(a.turn_id)
    await tm.wait(b.turn_id)

    assert recorder.names_for(a.turn_id) == ["TURN_START", "TURN_END"]
    assert recorder.names_for(b.turn_id) == ["TURN_START", "TURN_END"]
    # 顺序：A 完全结束之后 B 才开始（事件序列是前端状态机的事实来源）
    order = [e for e in recorder.events if e[0] in ("TURN_START", "TURN_END")]
    assert order == [
        ("TURN_START", a.turn_id, ""),
        ("TURN_END", a.turn_id, "completed"),
        ("TURN_START", b.turn_id, ""),
        ("TURN_END", b.turn_id, "completed"),
    ]
    await tm.shutdown()


async def test_sequence_stop_targets_running_turn_not_queued_one():
    """A START → B QUEUED → STOP：停止的必须是 A，B 不受影响。"""
    started = asyncio.Event()
    release = asyncio.Event()
    recorder = _Recorder()
    ran: list[str] = []

    async def runner(ctx):
        ran.append(ctx.message)
        if ctx.message == "A":
            started.set()
            await release.wait()

    tm = TurnManager(runner, emitter=recorder)
    a = tm.submit("A")
    await started.wait()
    b = tm.submit("B")
    await asyncio.sleep(0)

    assert tm.cancel_active() is True
    release.set()
    await tm.wait(a.turn_id)

    assert a.status == "cancelled"
    # 这次停止只作用于真正在跑的 A；排队的 B 没有被取消，仍然正常执行完
    assert b.cancelled is False
    await tm.wait(b.turn_id)
    assert ran == ["A", "B"]
    assert b.status == "completed"
    assert recorder.names_for(b.turn_id) == ["TURN_START", "TURN_END"]
    await tm.shutdown()


async def test_sequence_cancel_queued_turn_never_starts_and_next_one_runs():
    """A START → B QUEUED → C QUEUED → cancel B → A END：C 正常开始，B 永不开始。"""
    started = asyncio.Event()
    release = asyncio.Event()
    recorder = _Recorder()
    ran: list[str] = []

    async def runner(ctx):
        ran.append(ctx.message)
        if ctx.message == "A":
            started.set()
            await release.wait()

    tm = TurnManager(runner, emitter=recorder)
    a = tm.submit("A")
    await started.wait()
    b = tm.submit("B")
    c = tm.submit("C")

    assert tm.cancel(b.turn_id) is True
    assert tm.queued_count() == 1
    release.set()
    await tm.wait(a.turn_id)
    await tm.wait(b.turn_id)
    await tm.wait(c.turn_id)

    assert ran == ["A", "C"]
    assert recorder.names_for(b.turn_id) == []
    assert b.status == "cancelled"
    assert recorder.names_for(c.turn_id) == ["TURN_START", "TURN_END"]
    await tm.shutdown()


async def test_sequence_shutdown_leaves_no_hanging_wait():
    """A RUNNING + B QUEUED + APP SHUTDOWN：所有等待都必须结束。"""
    started = asyncio.Event()
    release = asyncio.Event()

    async def runner(ctx):
        if ctx.message == "A":
            started.set()
            await release.wait()

    tm = TurnManager(runner)
    a = tm.submit("A")
    await started.wait()
    b = tm.submit("B")

    waits = [asyncio.create_task(tm.wait(tid)) for tid in (a.turn_id, b.turn_id)]
    await asyncio.sleep(0)
    await asyncio.wait_for(tm.shutdown(), timeout=2.0)

    results = await asyncio.wait_for(asyncio.gather(*waits), timeout=1.0)
    assert all(r is not None for r in results), "关闭时不能留下永远等不到结果的 wait"
    assert a.status in ("cancelled", "completed")
    assert b.status == "cancelled"
    assert tm.snapshot()["running"] is None


async def test_sequence_repeated_wait_timeouts_do_not_accumulate_waiters():
    """反复 wait → timeout：既不能累积 waiter，也不能破坏 turn 的 completion future。

    `wait` 不创建 future（future 由 `submit` 建立、到终态才兑现并清理），
    所以「不累积」的可观测形态是：等待表大小不随超时次数增长、且始终是同一个 future，
    同时这一轮仍然能正常跑到终态并 resolve。
    """
    release = asyncio.Event()

    async def runner(ctx):
        await release.wait()
        ctx.result = {"ok": True}

    tm = TurnManager(runner)
    a = tm.submit("A")
    await asyncio.sleep(0)
    original = tm._futures[a.turn_id]
    for _ in range(5):
        assert await tm.wait(a.turn_id, timeout=0.005) is None
        assert len(tm._futures) == 1, "反复超时不得在等待表里累积新条目"
        assert tm._futures[a.turn_id] is original, "completion future 必须始终是同一个"

    waiter = asyncio.create_task(tm.wait(a.turn_id))
    release.set()
    assert await asyncio.wait_for(waiter, timeout=1.0) == {"ok": True}
    assert a.status == "completed"
    await tm.shutdown()


async def test_sequence_subagent_concurrency_four_running_fifth_queued():
    """最大并发 4：前 4 个 running，第 5 个 queued，释放名额后才 running。"""
    from agent.tools.task_manager import TaskManager
    from agent.tools.base import ToolResult

    class _Bus:
        def __init__(self) -> None:
            self.statuses: list[tuple[str, str]] = []

        async def publish(self, event) -> None:  # noqa: ANN001
            data = getattr(event, "data", {}) or {}
            if data.get("task_id"):
                self.statuses.append((data["task_id"], data["status"]))

    bus = _Bus()
    manager = TaskManager(bus, max_concurrent=4)
    gates = [asyncio.Event() for _ in range(5)]

    def factory(index: int):
        async def run() -> ToolResult:
            await gates[index].wait()
            return ToolResult(ok=True, content=f"done {index}")

        return run

    ids = [manager.submit(f"tool{ i }", factory(i)) for i in range(5)]
    await asyncio.sleep(0.05)

    records = [manager.record_info(tid) for tid in ids]
    running = [r for r in records if r and r.status == "running"]
    queued = [r for r in records if r and r.status == "queued"]
    assert len(running) == 4
    assert len(queued) == 1
    # 排队任务绝不能提前显示成 running
    assert all(status != "running" for tid, status in bus.statuses if tid == ids[4])

    gates[0].set()
    await asyncio.sleep(0.05)
    assert manager.record_info(ids[4]).status == "running"
    for gate in gates[1:]:
        gate.set()
    for tid in ids:
        await manager.await_result(tid, timeout=2.0)
