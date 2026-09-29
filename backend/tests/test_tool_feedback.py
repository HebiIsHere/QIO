"""统一失败反馈：失败绝不能以空正文交给模型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agent.adapters.base import AdapterMode, ChatMessage, Completion, ToolCall
from agent.api.server import EventBus
from agent.core import tool_feedback
from agent.core.loop import AgentLoop
from agent.tools.base import Tool, ToolResult
from agent.tools.registry import ToolRegistry


# ---------- 纯函数 ----------

def test_failed_empty_content_still_carries_facts():
    result = ToolResult(ok=False, error="ZeroDivisionError: division by zero")
    text = tool_feedback.message(result, tool_name="calc", call_id="call_1")
    assert text.strip()  # 不再是空正文
    assert "失败" in text
    assert "代码异常" in text
    assert "ZeroDivisionError" in text
    assert "call_1" in text
    assert "是否可重试: 是" in text


def test_success_non_empty_content_is_unchanged():
    result = ToolResult(ok=True, content='{"sum": 3}')
    assert tool_feedback.message(result) == '{"sum": 3}'


def test_success_empty_content_is_explained():
    text = tool_feedback.message(ToolResult(ok=True, content=""), tool_name="noop")
    assert text.strip() and "成功" in text


def test_secret_is_redacted_in_error_and_diagnostics():
    result = ToolResult(
        ok=False,
        error="auth failed with api_key=sk-abcdef123456",
        content="Authorization: Bearer tok_abcdefghij",
    )
    text = tool_feedback.message(result)
    assert "sk-abcdef123456" not in text
    assert "tok_abcdefghij" not in text


def test_long_diagnostics_are_truncated_visibly():
    result = ToolResult(ok=False, error="boom", content="x" * 5000)
    text = tool_feedback.message(result, diagnostic_limit=200)
    assert "已截断" in text


def test_categories_and_recoverability():
    assert tool_feedback.classify_error(
        "ModuleNotFoundError: No module named 'requests'"
    ) is tool_feedback.ErrorCategory.MISSING_DEPENDENCY
    assert tool_feedback.classify_error("timeout after 10s") is tool_feedback.ErrorCategory.TIMEOUT
    assert tool_feedback.classify_error("该工具未执行（用户拒绝）") is tool_feedback.ErrorCategory.PERMISSION
    # 缺依赖默认不可重试（解决前置条件前重试只是空转）
    r = ToolResult(ok=False, error="ModuleNotFoundError: No module named 'requests'")
    assert tool_feedback.is_recoverable(r) is False
    # 显式标注优先于文本推断
    r2 = ToolResult(ok=False, error="something", category="timeout", recoverable=False)
    assert tool_feedback.resolve_category(r2) is tool_feedback.ErrorCategory.TIMEOUT
    assert tool_feedback.is_recoverable(r2) is False
    assert tool_feedback.status_of(ToolResult(ok=False, error="工具已被取消")) == "cancelled"


def test_facts_expose_machine_readable_fields():
    facts = tool_feedback.facts(ToolResult(ok=False, error="exit code 1", category="code_error"))
    assert facts["status"] == "failed"
    assert facts["category"] == "code_error"
    assert facts["category_label"] == "代码异常"
    assert facts["ok"] is False


# ---------- 主循环集成：原生模式 ----------

class FailingTool(Tool):
    name = "boom"
    description = "always fails without content"
    parameters = {"type": "object", "properties": {}}

    async def run(self, **kwargs: Any) -> ToolResult:
        return ToolResult(ok=False, error="ZeroDivisionError: division by zero")


@dataclass
class _RecordingAdapter:
    """记录每次交给模型的 messages；第一次请求工具，之后直接结束。"""

    mode: str = AdapterMode.NATIVE.value
    model: str = "fake"
    calls: list = field(default_factory=list)
    _n: int = 0

    async def complete(self, messages, tools, **kwargs):
        self.calls.append(list(messages))
        self._n += 1
        if self._n == 1:
            return Completion(
                message=ChatMessage(
                    role="assistant",
                    content=None,
                    tool_calls=[ToolCall(id="call_1", name="boom", arguments={})],
                )
            )
        return Completion(message=ChatMessage(role="assistant", content="done"))


async def test_loop_feeds_failure_facts_to_model_native():
    registry = ToolRegistry()
    registry.register(FailingTool())
    adapter = _RecordingAdapter()
    loop = AgentLoop(adapter, registry, EventBus())
    await loop.run("do something")

    tool_messages = [m for m in adapter.calls[-1] if getattr(m, "role", None) == "tool"]
    assert tool_messages, "工具结果没有回填给模型"
    content = tool_messages[-1].content or ""
    assert content.strip(), "失败且 content 为空时，模型收到了空正文"
    assert "失败" in content and "ZeroDivisionError" in content


# ---------- 沙箱 → 工具：stderr 作为可诊断信息 ----------

async def test_code_tool_surfaces_stderr_and_category():
    from agent.tools.runtime_tools import CodeTool
    from agent.tools.sandbox import SandboxExecutor
    from agent.tools.spec import ToolDefinition

    definition = ToolDefinition(
        name="boom_tool",
        description="raises",
        code="def run(**kwargs):\n    raise ValueError('kaboom')\n",
        tests=[{"name": "t", "input": {}, "expect": {}}],
    )
    result = await CodeTool(definition, SandboxExecutor()).run()
    assert result.ok is False
    assert result.category == "code_error"
    # 诊断里有真实 traceback，而不是只有「沙箱执行失败」
    assert "ValueError" in (result.content or "")


async def test_code_tool_classifies_missing_dependency():
    from agent.tools.runtime_tools import CodeTool
    from agent.tools.sandbox import SandboxExecutor
    from agent.tools.spec import ToolDefinition

    definition = ToolDefinition(
        name="needs_dep",
        description="missing dep",
        code="import totally_missing_module_xyz\ndef run(**kwargs):\n    return {}\n",
        tests=[{"name": "t", "input": {}, "expect": {}}],
    )
    result = await CodeTool(definition, SandboxExecutor()).run()
    assert result.ok is False
    assert result.category == "missing_dependency"
    assert "totally_missing_module_xyz" in (result.content or "")
