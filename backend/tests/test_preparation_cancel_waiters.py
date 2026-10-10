"""问题四（plan §1.2）：放弃预留时必须**先兑现等待者**，再清结果表。

真缺陷（plan §0 第 4 条）：core/turn.py 的 `abandon()` 先 `self._futures.pop(...)`，
再调 `_resolve(...)`（内部又 pop 一次）→ 已经开始的 `await turns.wait(turn_id)`
**永远不返回**。契约写明：必须证明「等待调用**真的返回**」；「管理器里已无该 Future」
或「状态已 cancelled」都**不算**通过。

时序靠调度点（`await asyncio.sleep(0)`）把等待者挂到 future 上；有限超时只用于判定失败。
"""

from __future__ import annotations

import asyncio
import contextlib
import time

import pytest

from agent.core.turn import TurnManager

#: 等待调用必须回来的上界（失败判定，不是等待手段）
WAIT_DEADLINE = 5.0


class _Recorder:
    """记录 runner 实际执行过哪些轮次（不可逆事实）。"""

    def __init__(self) -> None:
        self.executed: list[str] = []

    async def __call__(self, ctx) -> None:  # noqa: ANN001
        self.executed.append(ctx.turn_id)
        await asyncio.sleep(0)


@pytest.fixture()
def turns():
    recorder = _Recorder()
    manager = TurnManager(runner=recorder)
    manager.instance_id = "test"
    return manager, recorder


async def _wait_until(predicate, *, timeout: float = WAIT_DEADLINE, what: str = "") -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"没有在 {timeout}s 内满足：{what}")


async def _wait_once(manager, turn_id: str, *, timeout: float | None = None):
    """等一次；返回 (是否返回了, 结果)。「没返回」= 被测缺陷。"""
    task = asyncio.create_task(manager.wait(turn_id, timeout))
    done, _ = await asyncio.wait({task}, timeout=WAIT_DEADLINE)
    if not done:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
        return False, None
    return True, task.result()


async def _started_waiters(manager, turn_id: str, count: int):
    """起 count 个等待者，并确认它们**真的挂上去了**（装置前置条件，不是就绪判据）。"""
    tasks = [asyncio.create_task(manager.wait(turn_id)) for _ in range(count)]
    await asyncio.sleep(0)  # 让每个等待者跑到第一次挂起（await shield(fut)）
    assert all(not task.done() for task in tasks), "等待者在放弃之前就结束了（装置问题）"
    return tasks


async def test_abandon_resolves_every_waiter(turns):
    """abandon 之后，**所有**等待调用都必须返回同一个结果。"""
    manager, _recorder = turns
    ctx = manager.reserve("准备中的消息", None)
    waiters = await _started_waiters(manager, ctx.turn_id, 3)

    manager.abandon(ctx, reason="cancelled_during_prepare")

    results = []
    for i, task in enumerate(waiters):
        done, _ = await asyncio.wait({task}, timeout=WAIT_DEADLINE)
        assert done, f"第 {i} 个等待者在 abandon 之后仍然没有返回 —— 先 pop 再 _resolve 的缺陷"
        results.append(task.result())
    assert results == [{"ok": False, "reason": "cancelled_during_prepare"}] * 3, results


async def test_repeated_abandon_is_idempotent_and_does_not_rewrite_terminal(turns):
    """重复 abandon 幂等；迟到的 abandon 不得改写已经终态的轮次。"""
    manager, _recorder = turns

    # (1) 已取消的预留：迟到 abandon 不改终态、不抛错、已兑现的结果不变
    ctx = manager.reserve("准备中的消息", None)
    (waiter,) = await _started_waiters(manager, ctx.turn_id, 1)
    manager.abandon(ctx, reason="cancelled_during_prepare")
    done, _ = await asyncio.wait({waiter}, timeout=WAIT_DEADLINE)
    assert done, "abandon 之后等待调用没有返回"
    assert waiter.result() == {"ok": False, "reason": "cancelled_during_prepare"}

    manager.abandon(ctx, reason="late_abandon")  # 重复放弃：幂等
    assert ctx.status == "cancelled", ("重复 abandon 改了终态", ctx.status)
    returned, again = await _wait_once(manager, ctx.turn_id)
    assert returned and again is None, ("已兑现过的轮次不该再有 future", returned, again)

    # (2) 已经**正常完成**的轮次：迟到 abandon 不得把它改写成 cancelled
    finished = manager.reserve("会正常完成的消息", None)
    manager.activate(finished)
    await _wait_until(lambda: finished.status == "completed", what="预留没有被放行执行")
    manager.abandon(finished, reason="late_abandon")
    assert finished.status == "completed", ("迟到的 abandon 改写了已终态轮次", finished.status)


async def test_one_waiter_timeout_or_cancel_does_not_break_others(turns):
    """单个等待者超时 / 被取消，不得取消共享 future，也不影响其他等待者（shield 不许退回）。"""
    manager, _recorder = turns
    ctx = manager.reserve("准备中的消息", None)

    returned, result = await _wait_once(manager, ctx.turn_id, timeout=0.05)
    assert returned and result is None, ("单次等待超时应当只结束这一次等待", returned, result)

    waiters = await _started_waiters(manager, ctx.turn_id, 2)
    waiters[0].cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await waiters[0]

    manager.abandon(ctx, reason="cancelled_during_prepare")

    done, _ = await asyncio.wait({waiters[1]}, timeout=WAIT_DEADLINE)
    assert done, "另一个等待者被取消操作带走了（共享 future 被取消）"
    assert waiters[1].result() == {"ok": False, "reason": "cancelled_during_prepare"}


async def test_later_ready_reservations_still_advance_after_abandon(turns):
    """放弃队首之后，后面**已经就绪**的预留仍要能被放行（FIFO 链不能断）。"""
    manager, recorder = turns
    first = manager.reserve("第一位（永远不就绪）", None)
    second = manager.reserve("第二位（先就绪）", None)
    manager.activate(second)
    await asyncio.sleep(0)
    assert recorder.executed == [], ("第二位被提前执行了", recorder.executed)

    manager.abandon(first, reason="cancelled_during_prepare")
    await _wait_until(
        lambda: recorder.executed == [second.turn_id], what="放弃队首后，后续就绪预留没有推进"
    )


async def test_shutdown_resolves_reserved_waiters(turns):
    """服务关闭：准备中的等待者同样必须立刻拿到结果。"""
    manager, _recorder = turns
    ctx = manager.reserve("准备中的消息", None)
    (waiter,) = await _started_waiters(manager, ctx.turn_id, 1)

    await manager.shutdown()

    done, _ = await asyncio.wait({waiter}, timeout=WAIT_DEADLINE)
    assert done, "shutdown 之后等待调用没有返回"
    assert waiter.result() == {"ok": False, "reason": "shutdown"}
