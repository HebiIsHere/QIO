"""最终结论的事实校正：主循环在收尾时补后端事实，且不重写模型的话。"""

from __future__ import annotations

from typing import Any

from agent.adapters.base import AdapterMode, ChatMessage, Completion, ToolCall
from agent.api.server import EventBus
from agent.core.loop import AgentLoop
from agent.tools.base import Tool, ToolResult
from agent.tools.registry import ToolRegistry


class _ScriptAdapter:
    """按脚本先调工具，最后给一句结论。"""

    mode = AdapterMode.NATIVE.value
    model = "fake"

    def __init__(self, script: list[tuple[str, dict]], answer: str) -> None:
        self.script = list(script)
        self.answer = answer
        self.seen: list[list[Any]] = []

    async def complete(self, messages, tools, **kwargs):
        self.seen.append(list(messages))
        if self.script:
            name, args = self.script.pop(0)
            return Completion(
                message=ChatMessage(
                    role="assistant",
                    content=None,
                    tool_calls=[ToolCall(id=f"c{len(self.seen)}", name=name, arguments=args)],
                )
            )
        return Completion(message=ChatMessage(role="assistant", content=self.answer))


class _FailingDevTool(Tool):
    """真事故形状：测试失败，但模型随后说「全部通过」。"""

    name = "dev_run_tests"
    description = "测试（总是失败）"
    parameters = {"type": "object", "properties": {}}

    async def run(self, **kwargs: Any) -> ToolResult:
        return ToolResult(
            ok=False,
            error="测试失败 0/1 tests passed",
            category="assertion",
            facts={
                "dev_task": {
                    "id": "ws_ab12cd34ef56",
                    "tool_name": "dev_run_tests",
                    "phase": "testing_failed",
                    "version": "a" * 64,
                    "submitted": False,
                    "requires_tests": True,
                    "test": {"state": "current", "passed": False, "summary": "0/1 tests passed"},
                }
            },
        )


class _OkTool(Tool):
    name = "dev_list_files"
    description = "列文件"
    parameters = {"type": "object", "properties": {}}

    async def run(self, **kwargs: Any) -> ToolResult:
        return ToolResult(ok=True, content="- tool.json")


class _CancelledTool(Tool):
    name = "run_shell"
    description = "被取消"
    parameters = {"type": "object", "properties": {}}

    async def run(self, **kwargs: Any) -> ToolResult:
        return ToolResult(ok=False, error="工具已被取消")


class _DeclaringTool(Tool):
    """一次核对通过的结论声明。"""

    name = "declare_completion"
    description = "声明完成"
    parameters = {"type": "object", "properties": {}}

    async def run(self, **kwargs: Any) -> ToolResult:
        return ToolResult(
            ok=True,
            content="已核对：测试通过 与后端记录一致。",
            facts={
                "declaration": {
                    "accepted": True,
                    "task_id": "ws_ab12cd34ef56",
                    "version": "a" * 64,
                    "claims": ["test_passed"],
                    "basis": "版本 aaaaaaaaaaaa；测试 0/1 tests passed",
                    "missing": [],
                }
            },
        )


def _loop(registry: ToolRegistry, adapter: _ScriptAdapter) -> AgentLoop:
    return AgentLoop(adapter, registry, EventBus())


async def test_success_claim_after_failed_tool_is_corrected():
    registry = ToolRegistry()
    registry.register(_FailingDevTool())
    adapter = _ScriptAdapter([("dev_run_tests", {})], "测试全部通过，工具已就绪。")
    result = await _loop(registry, adapter).run("做一个工具")

    content = result.final_content or ""
    assert "测试全部通过，工具已就绪。" in content      # 模型的话原样保留
    assert "系统核对" in content                       # 末尾补了后端事实
    assert "dev_run_tests" in content
    assert "ws_ab12cd34ef56" in content


async def test_retry_success_keeps_the_answer_untouched():
    """同一工具后来成功了，就不算未解决的失败。"""
    registry = ToolRegistry()
    registry.register(_OkTool())
    adapter = _ScriptAdapter([("dev_list_files", {})], "已经看过了工作区。")
    result = await _loop(registry, adapter).run("看看工作区")
    assert result.final_content == "已经看过了工作区。"


async def test_cancelled_tool_is_not_annotated():
    registry = ToolRegistry()
    registry.register(_CancelledTool())
    adapter = _ScriptAdapter([("run_shell", {})], "这条命令被取消了。")
    result = await _loop(registry, adapter).run("跑个命令")
    assert result.final_content == "这条命令被取消了。"


async def test_accepted_declaration_suppresses_the_note():
    registry = ToolRegistry()
    registry.register(_FailingDevTool())
    registry.register(_DeclaringTool())
    adapter = _ScriptAdapter(
        [("dev_run_tests", {}), ("declare_completion", {})],
        "已按后端记录说明：这一版测试没通过。",
    )
    result = await _loop(registry, adapter).run("做一个工具")
    assert result.final_content == "已按后端记录说明：这一版测试没通过。"
