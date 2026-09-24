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


def test_runaway_guard_halts_repeated_failures_without_approvals():
    """没有审批通道时（子任务/测试）：护栏到阈值仍然终止本轮，不静默继续。"""
    guard = RunawayGuard()
    loop = _loop(_ScriptedAdapter("boom"), guard=guard)
    loop.budget = IterationBudget(max_iterations=50, token_budget=0)
    result = asyncio.run(loop.run("go"))
    assert result.phase.value == "stopped"
    # 2026-09-22 起阈值为同一工具累计失败 30 次 → 用掉的迭代数远小于 50
    assert result.iterations_used >= 29
    assert result.iterations_used <= 35


def test_runaway_guard_asks_instead_of_stopping():
    """有审批通道时：到阈值先问用户 —— 批准＝清零继续（不会停在同一处）。"""
    approvals = _ApproveThenReject()
    guard = RunawayGuard()
    loop = _loop(_ScriptedAdapter("boom"), approvals=approvals, guard=guard)
    loop.budget = IterationBudget(max_iterations=40, token_budget=0)
    result = asyncio.run(loop.run("go"))
    # 第一次到阈值被批准 → 计数清零、继续跑；第二次到阈值被拒绝 → 本轮结束
    assert approvals.calls >= 2
    assert result.phase.value == "stopped"
    assert result.iterations_used > 30  # 证明"不是到 30 就直接停"


def test_tokens_counted_from_completion_tokens():
    loop = _loop(_ScriptedAdapter())
    loop.budget = IterationBudget(max_iterations=1, token_budget=0)
    asyncio.run(loop.run("go"))
    # 每轮 completion_tokens=1；已用过至少 1 轮
    assert loop.budget.used_tokens >= 1


def test_guard_halt_never_leaves_an_empty_answer():
    """真实事故：护栏终止后回答是空串，界面上只剩一个没有内容的回答，
    用户不知道发生了什么，事后也无法复盘。"""
    guard = RunawayGuard()
    loop = _loop(_ScriptedAdapter("boom"), guard=guard)
    loop.budget = IterationBudget(max_iterations=50, token_budget=0)
    result = asyncio.run(loop.run("go"))
    assert (result.final_content or "").strip()
    assert "boom" in result.final_content
    assert "nope" in result.final_content  # 带上最后一次失败原因


def test_budget_stop_never_leaves_an_empty_answer():
    approvals = _ApproveThenReject()
    loop = _loop(_ScriptedAdapter(), approvals=approvals)
    result = asyncio.run(loop.run("go"))
    assert (result.final_content or "").strip()
    assert "达到上限" in result.final_content
