"""Cross-loop tool events must not leak: listeners filter by dispatched call id."""

from __future__ import annotations

import asyncio
import json

from agent.adapters.base import AdapterMode, ToolCall
from agent.api.bus import EventBus
from agent.core.loop import AgentLoop
from agent.tools.base import Tool, ToolResult
from agent.tools.registry import ToolRegistry


class _EchoTool(Tool):
    name = "echo"
    description = "echo"
    parameters = {"type": "object", "properties": {}}

    async def run(self, **kwargs):
        return ToolResult(ok=True, content="ok")


class _Adapter:
    mode = AdapterMode.NATIVE
    model = "t"


def _events(joined: list[str]) -> list[dict]:
    out = []
    for line in joined:
        for part in line.splitlines():
            if part.startswith("data: "):
                out.append(json.loads(part[6:]))
    return out


async def test_tool_events_do_not_cross_loops():
    reg = ToolRegistry()
    reg.register(_EchoTool())
    bus = EventBus()
    loop_a = AgentLoop(_Adapter(), reg, bus, turn_id="A")
    loop_b = AgentLoop(_Adapter(), reg, bus, turn_id="B")

    collected: list[str] = []

    async def consume():
        async for chunk in bus.stream():
            collected.append(chunk)
            if len(_events(collected)) >= 8:
                return

    task = asyncio.create_task(consume())
    await asyncio.sleep(0)

    await asyncio.gather(
        loop_a._dispatch_tool_calls([ToolCall(id="a1", name="echo", arguments={"x": 1})]),
        loop_b._dispatch_tool_calls([ToolCall(id="b1", name="echo", arguments={"x": 2})]),
    )
    await asyncio.sleep(0.05)
    task.cancel()
    loop_a.dispose()
    loop_b.dispose()

    evs = _events(collected)
    starts = [e for e in evs if e["type"] == "TOOL_START"]
    # 每个 loop 只应看到自己那一次调用
    assert sum(1 for e in starts if e["data"].get("turn_id") == "A") == 1
    assert sum(1 for e in starts if e["data"].get("turn_id") == "B") == 1


async def test_listeners_released_after_dispose():
    reg = ToolRegistry()
    reg.register(_EchoTool())
    bus = EventBus()

    def counts():
        listeners = reg.events._listeners
        return sum(len(v) for v in listeners.values())

    before = counts()
    loop = AgentLoop(_Adapter(), reg, bus, turn_id="A", tool_trace=lambda d: None)
    during = counts()
    assert during > before  # 注册了监听器
    loop.dispose()
    assert counts() == before  # dispose 后恢复，无泄漏
