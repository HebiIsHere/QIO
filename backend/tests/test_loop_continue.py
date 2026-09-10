from __future__ import annotations

import asyncio

from agent.adapters.base import AdapterMode, ChatMessage, Completion, ToolCall
from agent.core.budget import IterationBudget
from agent.core.guard import GuardVerdict, RunawayGuard
from agent.core.loop import AgentLoop
from agent.tools.base import Tool, ToolResult
from agent.tools.registry import ToolRegistry


class _Bus:
    async def publish(self, event):  # noqa: ANN001
        return None


class _EchoTool(Tool):
    name = "echo"
    description = "echo"
    parameters = {"type": "object", "properties": {}}

    async def run(self, **kwargs):
        return ToolResult(ok=True, content="ok")


class _FailTool(Tool):
    name = "boom"
    description = "always fails"
    parameters = {"type": "object", "properties": {}}

    async def run(self, **kwargs):
        return ToolResult(ok=False, error="nope")


class _ScriptedAdapter:
    """每轮都请求一个工具调用，直到被打断。"""

    mode = AdapterMode.NATIVE
    model = "test"

    def __init__(self, tool_name: str = "echo") -> None:
        self.n = 0
        self._tool_name = tool_name

    async def complete(self, messages, tools, **kw):  # noqa: ANN001
        self.n += 1
        tc = ToolCall(id=f"t{self.n}", name=self._tool_name, arguments={})
        return Completion(
            message=ChatMessage(role="assistant", content=None, tool_calls=[tc]),
            usage={"completion_tokens": 1},
        )


class _ApproveThenReject:
    def __init__(self) -> None:
        self.calls = 0

    async def request(self, kind, payload):  # noqa: ANN001
        self.calls += 1
        decision = "approved" if self.calls == 1 else "rejected"
        return type("R", (), {"decision": decision})()


def _loop(adapter, approvals=None, guard=None, token_budget=0):
    reg = ToolRegistry()
    reg.register(_EchoTool())
    reg.register(_FailTool())
    loop = AgentLoop(
        adapter,
        reg,
        _Bus(),
        approvals=approvals,
        guard=guard,
        continue_batch_iterations=2,
        continue_batch_tokens=0,
    )
    loop.budget = IterationBudget(max_iterations=1, token_budget=token_budget)
    return loop


def test_loop_suspends_and_continues_on_exhaustion():
    approvals = _ApproveThenReject()
    loop = _loop(_ScriptedAdapter(), approvals=approvals)
    result = asyncio.run(loop.run("go"))
    # 至少触发过一次「继续」请求，且在拒绝后结束
    assert approvals.calls >= 1
    assert result.phase.value == "stopped"


def test_loop_without_approvals_stops_silently():
    loop = _loop(_ScriptedAdapter())
    result = asyncio.run(loop.run("go"))
    assert result.phase.value == "stopped"


def test_runaway_guard_halts_repeated_failures():
    guard = RunawayGuard()
    # 让迭代数足够大，但护栏应在同工具失败 8 次时 halt
    loop = _loop(_ScriptedAdapter("boom"), guard=guard)
    loop.budget = IterationBudget(max_iterations=50, token_budget=0)
    result = asyncio.run(loop.run("go"))
    assert result.phase.value == "stopped"
    # 护栏触发后循环提前结束，用掉的迭代数应远小于 50
    assert result.iterations_used <= 10


def test_tokens_counted_from_completion_tokens():
    loop = _loop(_ScriptedAdapter())
    loop.budget = IterationBudget(max_iterations=1, token_budget=0)
    asyncio.run(loop.run("go"))
    # 每轮 completion_tokens=1；已用过至少 1 轮
    assert loop.budget.used_tokens >= 1
