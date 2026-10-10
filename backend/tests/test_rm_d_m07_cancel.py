"""M07：请求取消的生命周期 —— 自然完成 / 普通停止 / 外层 Task.cancel / 关闭 / 请求异常。

真实的坏行为（origin/main 基线）：``AgentLoop._await_completion`` 把模型请求放进
自己的 task 里竞速，但

* ``finally: waiter.cancel()`` 之后从不 await 它 —— 留下悬挂的竞速任务；
* 外层 ``Task.cancel``（应用关闭、上层任务被取消）时只把自己摘出去：内层请求既没有
  被取消、也没有被等待，连接与生成的响应继续跑；
* 迟到的响应还有机会回到已经结束的一轮。

这里用「闸门停在请求中」的假适配器受控验证五个出口：全部要求
内层请求被取消**并等待清理完成**、无悬挂 request/waiter、取消继续传播。
"""

from __future__ import annotations

import asyncio

import pytest

from agent.adapters.base import ChatMessage, Completion, ModelUsage
from agent.api.bus import EventBus
from agent.core.loop import AgentLoop
from agent.core.turn import TurnManager
from agent.tools.registry import ToolRegistry


async def _wait_started(adapter, timeout: float = 2.0) -> None:
    """等假适配器真的进入请求（受控等待，不做长时间 sleep）。"""
    steps = int(timeout / 0.01)
    for _ in range(steps):
        if getattr(adapter, "started", False):
            return
        await asyncio.sleep(0.01)
    raise AssertionError("模型请求没有开始跑，测试前提不成立")


class GateAdapter:
    """停在请求中的假适配器：闸门不放行就永远不返回。

    取消时先做一段「真实的资源清理」再抛出取消 —— 只有调用方**真的等待**了这次请求，
    清理才会在调用方结束之前跑完（``cleaned`` 才能被观察到 True）。
    """

    mode = "native"
    model = "fake-gate"

    def __init__(self, cleanup_delay: float = 0.05) -> None:
        self.started = False
        self.aborted = False
        self.cleaned = False
        self.cleanup_delay = cleanup_delay
        self._gate = asyncio.Event()

    def release(self) -> None:
        self._gate.set()

    async def complete(self, messages, tools, **kwargs):
        self.started = True
        try:
            await self._gate.wait()
        except asyncio.CancelledError:
            self.aborted = True
            await asyncio.sleep(self.cleanup_delay)  # 模拟连接/缓冲清理
            self.cleaned = True
            raise
        self.cleaned = True
        return Completion(
            message=ChatMessage(role="assistant", content="自然完成"),
            usage=ModelUsage(input_tokens=5, output_tokens=3),
        )


class BoomAdapter:
    mode = "native"
    model = "fake-boom"

    async def complete(self, messages, tools, **kwargs):
        raise RuntimeError("provider down")


class LateAdapter:
    """装作「取消不会立刻生效」的 SDK：被取消后仍会返回一个迟到结果。"""

    mode = "native"
    model = "fake-late"

    def __init__(self) -> None:
        self.started = False
        self.returned = False

    async def complete(self, messages, tools, **kwargs):
        self.started = True
        try:
            await asyncio.sleep(0.2)
        except asyncio.CancelledError:
            await asyncio.sleep(0.05)
        self.returned = True
        return Completion(message=ChatMessage(role="assistant", content="迟到的回答"))


async def _collect_events(bus: EventBus) -> tuple[asyncio.Task, list[str]]:
    chunks: list[str] = []

    async def consume() -> None:
        async for chunk in bus.stream():
            chunks.append(chunk)

    return asyncio.create_task(consume()), chunks


async def test_outer_task_cancel_aborts_inner_request_and_waits_for_cleanup():
    """外层 Task.cancel：内层请求被取消并等待清理，取消继续传播。"""
    adapter = GateAdapter()
    loop = AgentLoop(adapter, ToolRegistry(), EventBus())

    task = asyncio.create_task(loop.run("hello"))
    await _wait_started(adapter)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert task.cancelled(), "外层取消必须继续传播（不能被吞成普通返回）"
    assert adapter.aborted is True, "内层模型请求必须被取消"
    assert adapter.cleaned is True, "必须等待内层请求清理完成再传播取消"
    assert loop.in_flight_requests() == [], "取消后不得留下悬挂的 request"
    assert loop.in_flight_waiters() == [], "取消后不得留下悬挂的 waiter"


async def test_natural_completion_still_works():
    """自然完成路径不受影响，且收尾不留悬挂任务。"""
    adapter = GateAdapter()
    loop = AgentLoop(adapter, ToolRegistry(), EventBus())

    task = asyncio.create_task(loop.run("hello"))
    await _wait_started(adapter)
    adapter.release()
    result = await asyncio.wait_for(task, timeout=3)

    assert result.phase.value == "done"
    assert result.final_content == "自然完成"
    assert result.cancelled is False
    assert result.tokens_used == 3
    assert loop.in_flight_requests() == []
    assert loop.in_flight_waiters() == []


async def test_stop_button_interrupts_request_and_marks_turn_cancelled():
    """普通「停止」按钮的现有可用路径不能被改坏。"""
    adapter = GateAdapter()
    loop = AgentLoop(adapter, ToolRegistry(), EventBus())

    task = asyncio.create_task(loop.run("hello"))
    await _wait_started(adapter)
    loop.cancel()
    result = await asyncio.wait_for(task, timeout=3)

    assert result.cancelled is True
    assert adapter.aborted is True, "停止按钮必须掐掉在途请求"
    assert adapter.cleaned is True
    assert loop.in_flight_requests() == []
    assert loop.in_flight_waiters() == []


async def test_request_exception_leaves_no_dangling_request_or_waiter():
    """请求异常：异常照旧上抛，但 request/waiter 都要收干净。"""
    loop = AgentLoop(BoomAdapter(), ToolRegistry(), EventBus())
    with pytest.raises(RuntimeError):
        await loop.run("hello")
    assert loop.in_flight_requests() == []
    assert loop.in_flight_waiters() == []


async def test_late_result_never_reaches_the_finished_turn():
    """迟到结果不得进入已结束的 turn（也不得重新激活界面）。"""
    adapter = LateAdapter()
    bus = EventBus()
    loop = AgentLoop(adapter, ToolRegistry(), bus)
    consumer, chunks = await _collect_events(bus)
    await asyncio.sleep(0)

    task = asyncio.create_task(loop.run("hello"))
    await _wait_started(adapter)
    loop.cancel()
    result = await asyncio.wait_for(task, timeout=3)

    assert adapter.returned is True, "假适配器确实返回了迟到结果（否则测试前提不成立）"
    assert result.cancelled is True
    assert result.final_content is None, "取消之后不得采用迟到的回答"
    assert loop.in_flight_requests() == []
    assert loop.in_flight_waiters() == []

    await asyncio.sleep(0.05)
    consumer.cancel()
    try:
        await consumer
    except asyncio.CancelledError:
        pass
    joined = "\n".join(chunks)
    assert "迟到的回答" not in joined, "迟到结果不得广播出去重新激活界面"


async def test_shutdown_cancels_in_flight_request_and_ends_the_turn():
    """关闭：TurnManager.shutdown 取消在跑的一轮，内层请求被取消并等待清理。"""
    adapter = GateAdapter()
    turns = TurnManager()
    holder: dict[str, AgentLoop] = {}

    def runner(ctx):
        async def _run() -> None:
            loop = AgentLoop(adapter, ToolRegistry(), EventBus(), turn_id=ctx.turn_id)
            holder["loop"] = loop
            ctx.loop = loop
            try:
                await loop.run(ctx.message)
            finally:
                ctx.loop = None

        return _run()

    turns.set_runner(runner)
    ctx = turns.submit("hello")
    await _wait_started(adapter)

    await asyncio.wait_for(turns.shutdown(), timeout=3)

    assert ctx.status == "cancelled"
    assert ctx.turn_end_emitted is True, "被关闭掐断的一轮仍要有唯一的 TURN_END"
    assert adapter.aborted is True and adapter.cleaned is True
    assert holder["loop"].in_flight_requests() == []
    assert holder["loop"].in_flight_waiters() == []
