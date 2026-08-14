"""并行执行（is_concurrency_safe）与取消（Task.cancel → aborted）。"""
from __future__ import annotations

import asyncio
import time

from agent.adapters.base import ToolCall
from agent.api.bus import EventBus
from agent.core.loop import AgentLoop
from agent.tools.base import Tool, ToolResult
from agent.tools.registry import ToolRegistry


class StubAdapter:
    mode = "native"
    model = "m"


def make_call(name: str, arguments: dict | None = None) -> ToolCall:
    return ToolCall(id=f"c_{len(__import__('uuid').uuid4().hex[:6])}", name=name, arguments=arguments or {})


class SafeSleepTool(Tool):
    name = "safe_sleep"
    description = "safe sleep"
    parameters = {"type": "object", "properties": {}}
    is_concurrency_safe = True

    async def run(self, **kwargs):
        await asyncio.sleep(kwargs.get("seconds", 0.1))
        return ToolResult(ok=True, content="done")


class UnsafeSleepTool(Tool):
    name = "unsafe_sleep"
    description = "unsafe sleep"
    parameters = {"type": "object", "properties": {}}

    async def run(self, **kwargs):
        await asyncio.sleep(kwargs.get("seconds", 0.1))
        return ToolResult(ok=True, content="done")


class LongTool(Tool):
    name = "long"
    description = "long"
    parameters = {"type": "object", "properties": {}}

    async def run(self, **kwargs):
        await asyncio.sleep(30)
        return ToolResult(ok=True, content="done")


class TrackingSafeTool(Tool):
    name = "track_safe"
    description = "track"
    parameters = {"type": "object", "properties": {}}
    is_concurrency_safe = True

    def __init__(self, tracker) -> None:
        self.tracker = tracker

    async def run(self, **kwargs):
        t = self.tracker
        t["active"] += 1
        t["max"] = max(t["max"], t["active"])
        await asyncio.sleep(0.06)
        t["active"] -= 1
        return ToolResult(ok=True, content="done")


async def test_safe_tools_run_in_parallel():
    reg = ToolRegistry()
    reg.register(SafeSleepTool())
    loop = AgentLoop(StubAdapter(), reg, EventBus())
    calls = [make_call("safe_sleep", {"seconds": 0.15}) for _ in range(3)]
    t0 = time.perf_counter()
    results = await loop._dispatch_tool_calls(calls)
    dt = time.perf_counter() - t0
    assert all(results[c.id].ok for c in calls)
    assert dt < 0.4  # 串行需 0.45+


async def test_unsafe_tools_run_serially():
    reg = ToolRegistry()
    reg.register(UnsafeSleepTool())
    loop = AgentLoop(StubAdapter(), reg, EventBus())
    calls = [make_call("unsafe_sleep", {"seconds": 0.1}) for _ in range(3)]
    t0 = time.perf_counter()
    await loop._dispatch_tool_calls(calls)
    dt = time.perf_counter() - t0
    assert dt >= 0.28  # 串行 ~0.30


async def test_max_parallel_tools_limits_concurrency():
    reg = ToolRegistry()
    tracker = {"active": 0, "max": 0}
    for i in range(5):
        tool = TrackingSafeTool(tracker)
        tool.name = f"track_safe_{i}"
        reg.register(tool)
    loop = AgentLoop(StubAdapter(), reg, EventBus(), max_parallel_tools=2)
    calls = [make_call(f"track_safe_{i}") for i in range(5)]
    await loop._dispatch_tool_calls(calls)
    assert tracker["max"] <= 2
    assert tracker["max"] >= 2  # 确有并行


async def test_registry_cancel_aborts_tool():
    reg = ToolRegistry()
    reg.register(LongTool())
    seen_end: list[dict] = []

    def on_end(data):
        seen_end.append(data)

    reg.register_policy("tool/end", on_end)
    task = asyncio.create_task(reg.execute(make_call("long")))
    await asyncio.sleep(0.05)
    task.cancel()
    result = await task  # 不抛异常，转成 aborted 失败结果
    assert not result.ok
    assert "aborted" in result.error
    assert seen_end and seen_end[0]["ok"] is False
    assert "aborted" in (seen_end[0]["error"] or "")


async def test_loop_cancel_aborts_in_flight_unsafe():
    reg = ToolRegistry()
    reg.register(LongTool())
    loop = AgentLoop(StubAdapter(), reg, EventBus())
    call = make_call("long")
    task = asyncio.create_task(loop._dispatch_tool_calls([call]))
    await asyncio.sleep(0.05)
    loop.cancel()
    results = await task
    assert not results[call.id].ok
    assert "aborted" in results[call.id].error
