r"""D 独立验证（R7 问题四）：放弃预留必须**先兑现等待者**。

契约：docs/plans/2026-10-08-cancel-readiness-spill.md §1.2 / §3（冻结）。
基线 966e2fc 缺陷（core/turn.py:360）：\`abandon()\` **先** \`self._futures.pop(ctx.turn_id, None)\`，
**再** \`_resolve()\`（内部再 pop，已找不到）→ **等待调用永远不返回**。

判定规则（用户可见结果）：等待调用必须**真的返回**；「管理器里已无该 Future」「状态已 cancelled」
都**不算**通过。本文件只依赖公开 API（reserve / activate / abandon / wait / shutdown）。

运行：cd backend; $env:PYTHONPATH='src'; .\.venv\Scripts\python.exe -m pytest tests/test_r7_waiter_finalize_verify.py -q
"""

from __future__ import annotations

import asyncio

import pytest

from agent.core.turn import TurnManager

WAIT_S = 3.0  # 只作失败判定：等待调用必须在有限时间内返回


def _manager() -> TurnManager:
    return TurnManager()


async def _start_waiter(manager: TurnManager, turn_id: str, *, timeout: float | None = None):
    task = asyncio.create_task(manager.wait(turn_id, timeout))
    await asyncio.sleep(0.05)  # 确保它真的进入了等待
    assert not task.done(), "等待任务在断言前就结束了（装置失效）"
    return task


# ---- 1. 单个等待者：abandon 之后必须返回 --------------------------------------------


async def test_abandon_resolves_single_waiter():
    manager = _manager()
    ctx = manager.reserve("被放弃的一轮", None)
    waiter = await _start_waiter(manager, ctx.turn_id)

    manager.abandon(ctx, reason="prepare_failed")

    try:
        result = await asyncio.wait_for(waiter, timeout=WAIT_S)
    except asyncio.TimeoutError:
        raise AssertionError(
            "abandon 之后等待调用没有返回 —— 等待者被永久挂住（基线 core/turn.py:360 先 pop 再 resolve）",
            {"turn_id": ctx.turn_id, "status": ctx.status, "future_in_manager": ctx.turn_id in manager._futures},
        )
    assert result is not None and result.get("ok") is False, ("等待者拿到的结果必须如实表示失败/取消", result)
    assert result.get("reason") == "prepare_failed", result


# ---- 2. 多个等待者：都要拿到一致结果 ------------------------------------------------


async def test_abandon_resolves_all_waiters_with_same_result():
    manager = _manager()
    ctx = manager.reserve("被放弃的一轮（多等待者）", None)
    waiters = [await _start_waiter(manager, ctx.turn_id) for _ in range(3)]

    manager.abandon(ctx, reason="prepare_failed")

    results = []
    for index, waiter in enumerate(waiters):
        try:
            results.append(await asyncio.wait_for(waiter, timeout=WAIT_S))
        except asyncio.TimeoutError:
            raise AssertionError("第 %d 个等待者没有返回" % (index + 1), {"ja": []})
    assert all(r is not None and r.get("ok") is False for r in results), results
    assert len({str(r.get("reason")) for r in results}) == 1, ("多个等待者结果必须一致", results)


# ---- 3. 超时的等待者不影响其他等待者 ------------------------------------------------


async def test_timed_out_waiter_does_not_break_others():
    manager = _manager()
    ctx = manager.reserve("被放弃的一轮（一个超时）", None)
    short = asyncio.create_task(manager.wait(ctx.turn_id, 0.2))
    long_waiter = await _start_waiter(manager, ctx.turn_id)

    assert await asyncio.wait_for(short, timeout=WAIT_S) is None, "超时的等待应当返回 None"
    manager.abandon(ctx, reason="prepare_failed")

    try:
        result = await asyncio.wait_for(long_waiter, timeout=WAIT_S)
    except asyncio.TimeoutError:
        raise AssertionError("一个等待者超时之后，另一个等待者仍然被永久挂住")
    assert result.get("ok") is False, result


# ---- 4. 取消一个等待者不影响其他等待者 ----------------------------------------------


async def test_cancelling_one_waiter_does_not_break_others():
    manager = _manager()
    ctx = manager.reserve("被放弃的一轮（取消一个等待者）", None)
    doomed = await _start_waiter(manager, ctx.turn_id)
    survivor = await _start_waiter(manager, ctx.turn_id)

    doomed.cancel()
    with pytest.raises(asyncio.CancelledError):
        await doomed

    manager.abandon(ctx, reason="prepare_failed")
    try:
        result = await asyncio.wait_for(survivor, timeout=WAIT_S)
    except asyncio.TimeoutError:
        raise AssertionError("取消一个等待者之后，另一个等待者也拿不到结果了")
    assert result.get("ok") is False, result


# ---- 5. 重复 abandon 幂等，且不改写已终态 --------------------------------------------


async def test_repeated_abandon_is_idempotent():
    manager = _manager()
    ctx = manager.reserve("被放弃的一轮（重复 abandon）", None)
    waiter = await _start_waiter(manager, ctx.turn_id)

    manager.abandon(ctx, reason="prepare_failed")
    first = await asyncio.wait_for(waiter, timeout=WAIT_S)
    assert first.get("reason") == "prepare_failed", first

    manager.abandon(ctx, reason="第二次放弃")  # 幂等：不得抛异常
    assert ctx.status == "cancelled", ctx.status
    late = await manager.wait(ctx.turn_id, timeout=0.2)
    assert late in (None, first), ("迟到的等待不得拿到与首次冲突的结果", late)


# ---- 6. 服务关闭：等待者也要被兑现 --------------------------------------------------


async def test_shutdown_resolves_waiters():
    manager = _manager()
    ctx = manager.reserve("服务关闭时仍在准备的一轮", None)
    waiter = await _start_waiter(manager, ctx.turn_id)

    await manager.shutdown()

    try:
        result = await asyncio.wait_for(waiter, timeout=WAIT_S)
    except asyncio.TimeoutError:
        raise AssertionError("服务关闭之后等待调用没有返回 —— 等待者被永久挂住")
    assert result is not None and result.get("ok") is False, ("关闭时等待者必须拿到失败/取消结果", result)
