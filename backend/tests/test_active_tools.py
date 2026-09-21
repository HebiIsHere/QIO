"""正在执行中的工具必须能从服务端查出来。

真实缺陷：TOOL_START 到了、TOOL_END 丢在失真区间里，而主 Turn 仍在跑 ——
客户端只能一直显示「运行中」，永远不会自己好。
runtime snapshot 因此必须带上「此刻真的在跑哪些工具」，
客户端据此把不在列表里的「运行中」卡片收口。
"""

from __future__ import annotations

import asyncio

from agent.adapters.base import AdapterMode, ChatMessage, Completion, ToolCall
from agent.api.bus import EventBus
from agent.core.loop import AgentLoop
from agent.tools.base import Tool, ToolResult
from agent.tools.registry import ToolRegistry


class _Bus:
    async def publish(self, event) -> None:  # noqa: ANN001
        return None


class _SlowTool(Tool):
    name = "slow_tool"
    description = "blocks until released"
    parameters = {"type": "object", "properties": {}}

    def __init__(self, started: asyncio.Event, release: asyncio.Event) -> None:
        self._started = started
        self._release = release

    async def run(self, **kwargs) -> ToolResult:  # noqa: ANN003
        self._started.set()
        await self._release.wait()
        return ToolResult(ok=True, content="done")


class _OneToolAdapter:
    mode = AdapterMode.NATIVE
    model = "test"

    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, messages, tools, **kw):  # noqa: ANN001
        self.calls += 1
        if self.calls == 1:
            return Completion(
                message=ChatMessage(
                    role="assistant",
                    content=None,
                    tool_calls=[ToolCall(id="call_1", name="slow_tool", arguments={})],
                )
            )
        return Completion(message=ChatMessage(role="assistant", content="完成"))


async def test_agent_loop_reports_the_tools_that_are_actually_running():
    started = asyncio.Event()
    release = asyncio.Event()
    registry = ToolRegistry()
    registry.register(_SlowTool(started, release))
    loop = AgentLoop(_OneToolAdapter(), registry, _Bus(), turn_id="turn_1")

    run = asyncio.create_task(loop.run("go"))
    await started.wait()

    active = loop.active_tools()
    assert len(active) == 1, active
    assert active[0]["tool_call_id"] == "call_1"
    assert active[0]["tool_name"] == "slow_tool"
    assert active[0]["turn_id"] == "turn_1"

    release.set()
    await run
    assert loop.active_tools() == [], "结束之后不能再报告成「正在运行」"
