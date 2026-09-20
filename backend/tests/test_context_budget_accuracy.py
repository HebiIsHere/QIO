"""上下文预算必须贴近 Adapter 真正发出去的内容。

历史缺陷：`TurnOrchestrator.build_context` 只传了 `tool_definitions_tokens`，
`system_prompt_tokens` 与 `adapter_overhead_tokens` 恒为 0。于是：

* text 兼容档把**全部工具说明**拼进 system prompt（可能上千 token），预算里完全没算；
* 预算高估可用空间 → 注入内容可能把请求顶到 context window 之外。
"""

from __future__ import annotations

from agent.adapters.base import ToolSpec
from agent.adapters.native import NativeAdapter
from agent.adapters.text import TextAdapter
from agent.memory.index import estimate_tokens
from agent.services.turn_orchestrator import adapter_prompt_costs

BIG_TOOLS = [
    ToolSpec(
        name="search_everything",
        description="搜索。" * 200,
        parameters={
            "type": "object",
            "properties": {"q": {"type": "string", "description": "查询。" * 50}},
        },
    ),
    ToolSpec(name="echo", description="回显。" * 100, parameters={"type": "object", "properties": {}}),
]


class _FakeClient:
    @property
    def chat(self):  # noqa: ANN201
        return self

    @property
    def completions(self):  # noqa: ANN201
        return self


def test_native_adapter_has_no_synthesized_system_prompt():
    adapter = NativeAdapter(_FakeClient(), "m")
    assert adapter.system_prompt_text(BIG_TOOLS) == ""
    # 原生工具定义走 API 的 tools 字段，由 registry 侧单独计数
    assert adapter.tools_in_prompt is False


def test_text_adapter_reports_its_synthesized_prompt():
    adapter = TextAdapter(_FakeClient(), "m")
    prompt = adapter.system_prompt_text(BIG_TOOLS)
    assert prompt
    # text 档的工具说明直接拼进 system prompt，必须被计入预算
    assert adapter.tools_in_prompt is True
    assert estimate_tokens(prompt) > 200


def test_adapter_prompt_costs_counts_text_prompt_and_avoids_double_counting():
    class _Registry:
        def specs(self):
            return BIG_TOOLS

    text_adapter = TextAdapter(_FakeClient(), "m")
    system_tokens, overhead_tokens, tool_tokens = adapter_prompt_costs(text_adapter, _Registry())
    # text 档：工具说明已经在 system prompt 里，不能再按 API tools 计一遍
    assert system_tokens > 200
    assert tool_tokens == 0
    assert overhead_tokens >= 0

    native_adapter = NativeAdapter(_FakeClient(), "m")
    n_system, n_overhead, n_tools = adapter_prompt_costs(native_adapter, _Registry())
    assert n_system == 0
    assert n_tools > 200  # 原生档：工具定义走 API 字段
    assert n_overhead >= 0


def test_adapter_prompt_costs_survives_broken_registry():
    """预算估算失败不能让整轮对话失败。"""

    class _BrokenRegistry:
        def specs(self):
            raise RuntimeError("registry exploded")

    system_tokens, overhead_tokens, tool_tokens = adapter_prompt_costs(
        NativeAdapter(_FakeClient(), "m"), _BrokenRegistry()
    )
    # 拿不到工具定义时不得抛异常：prompt / 工具定义为 0，
    # 协议固定开销仍然如实计入（它与工具定义无关）。
    assert (system_tokens, tool_tokens) == (0, 0)
    assert overhead_tokens >= 0
