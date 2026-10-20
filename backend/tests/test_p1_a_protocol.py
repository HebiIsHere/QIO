"""Phase 1 A：回答协议（三档一致 / 只注入一次 / 补救规则）。"""

from __future__ import annotations

from agent import prompts
from agent.adapters.base import AdapterMode, BaseAdapter, ChatMessage, Completion, ToolCall, ToolSpec
from agent.adapters.fake import FakeStreamAdapter, ScriptedToolCall, StreamScript
from agent.adapters.text import TextAdapter
from agent.api.bus import EventBus
from agent.core.loop import AgentLoop
from agent.tools.base import Tool, ToolResult
from agent.tools.registry import ToolRegistry


class _Echo(Tool):
    name = "echo"
    description = "回显"
    parameters = {"type": "object", "properties": {"text": {"type": "string"}}}

    async def run(self, **kwargs):
        return ToolResult(ok=True, content=str(kwargs.get("text") or "ok"))


def _registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(_Echo())
    return registry


def _tools() -> list[ToolSpec]:
    return [
        ToolSpec(
            name="echo",
            description="回显",
            parameters={"type": "object", "properties": {"text": {"type": "string"}}},
        )
    ]


def test_answer_marker_original_value_is_kept():
    assert prompts.ANSWER_MARKER == "[[QIO:ANSWER]]"
    assert prompts.ANSWER_MARKER in prompts.answer_protocol()
    assert prompts.ANSWER_MARKER in prompts.answer_fallback_hint()


async def test_native_protocol_injected_exactly_once_per_call():
    adapter = FakeStreamAdapter(
        [
            StreamScript(
                tool_calls=[
                    ScriptedToolCall(id="c1", name="echo", arguments={"text": "甲"})
                ]
            ),
            StreamScript(text=prompts.ANSWER_MARKER + "\n完成"),
        ]
    )
    result = await AgentLoop(
        adapter, _registry(), EventBus(), turn_id="turn_p1a_protocol"
    ).run("hi")

    assert result.final_content == "完成"
    assert len(adapter.requests) == 2
    for request in adapter.requests:
        messages = request["messages"]
        assert messages[-1].role == "system"
        assert messages[-1].content == prompts.answer_protocol()
        # 协议只发给这一次调用，不写回对话记录
        assert (
            sum(1 for m in messages if m.content == prompts.answer_protocol()) == 1
        )


def test_text_adapter_keeps_single_system_message_and_protocol_once():
    adapter = TextAdapter(client=object(), model="m")
    tools = _tools()
    assembled = prompts.system_rules(adapter.mode_rules_text(tools)) + "\n\n" + (
        prompts.render_context_blocks([{"key": "memory", "data": {"a": 1}}])
    )
    out = adapter._to_text_messages(
        [
            ChatMessage(role="system", content=assembled),
            ChatMessage(role="user", content="你好"),
        ],
        tools,
    )
    assert [m["role"] for m in out].count("system") == 1
    system = out[0]["content"]
    assert system.count(prompts.PROMPTS["TEXT_MODE_PROTOCOL"]) == 1
    assert system.count(prompts.PROMPTS["TEXT_OBSERVATION_RULES"]) == 1
    assert system.count(prompts.answer_protocol()) == 1
    assert assembled in system
    # text 档工具定义在 prompt 里：预算里算一次，registry 侧不再重复计
    assert adapter.tools_in_prompt is True


def test_text_adapter_standalone_prompt_has_each_rule_once():
    adapter = TextAdapter(client=object(), model="m")
    prompt = adapter.build_system_prompt(_tools())
    assert prompt.count(prompts.PROMPTS["TEXT_MODE_PROTOCOL"]) == 1
    assert prompt.count(prompts.answer_protocol()) == 1
    assert prompts.PROMPTS["QIO_CORE"].splitlines()[0] in prompt
    # system_prompt_text 与真正发出去的 prompt 同口径（预算不漏算）
    assert prompt == adapter.system_prompt_text(_tools())


async def test_native_fallback_keeps_general_rules_and_appends_hint():
    adapter = FakeStreamAdapter(
        [
            StreamScript(
                tool_calls=[
                    ScriptedToolCall(id="c1", name="echo", arguments={"text": "甲"})
                ]
            ),
            # 工作调用：没有工具、也没有正文 → 触发唯一一次补救调用
            StreamScript(text=""),
            StreamScript(text=prompts.ANSWER_MARKER + "\n最终回答"),
        ]
    )
    result = await AgentLoop(
        adapter, _registry(), EventBus(), turn_id="turn_p1a_fallback"
    ).run("hi")

    assert result.final_content == "最终回答"
    assert len(adapter.requests) == 3
    hint = adapter.requests[-1]["messages"][-1]
    assert hint.role == "system"
    assert prompts.answer_protocol() in hint.content
    assert prompts.answer_fallback_hint() in hint.content
    assert hint.content.count(prompts.answer_protocol()) == 1


class _TextRecordingAdapter(BaseAdapter):
    """text 档替身：记录每次请求的 messages，第三次调用给出最终回答。"""

    mode = AdapterMode.TEXT
    supports_stream = False
    protocol_in_prompt = True

    def __init__(self) -> None:
        self.model = "text"
        self.endpoint = None
        self.requests: list[list[ChatMessage]] = []

    async def complete(self, messages, tools, **kwargs):
        self.requests.append(list(messages))
        index = len(self.requests)
        if index == 1:
            return Completion(
                message=ChatMessage(
                    role="assistant",
                    content=None,
                    tool_calls=[
                        ToolCall(id="c1", name="echo", arguments={"text": "甲"})
                    ],
                )
            )
        if index == 2:
            return Completion(message=ChatMessage(role="assistant", content=None))
        return Completion(
            message=ChatMessage(
                role="assistant", content=prompts.ANSWER_MARKER + "\n最终回答"
            )
        )


async def test_text_fallback_does_not_duplicate_protocol():
    adapter = _TextRecordingAdapter()
    result = await AgentLoop(
        adapter, _registry(), EventBus(), turn_id="turn_p1a_text_fallback"
    ).run("hi")

    assert result.final_content == "最终回答"
    assert len(adapter.requests) == 3
    # text 档协议已在自己的 system prompt 里：循环不得再注入一份
    for messages in adapter.requests:
        assert not any(
            (m.content or "") == prompts.answer_protocol() for m in messages
        )
    hint = adapter.requests[-1][-1]
    assert hint.role == "system"
    assert hint.content == prompts.answer_fallback_hint()
