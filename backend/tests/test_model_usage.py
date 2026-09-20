"""ModelUsage：把供应商差异关在 Adapter 层，上层只看统一语义。

    usage.input_tokens
    usage.output_tokens
    usage.total_tokens

OpenAI 的 `prompt_tokens/completion_tokens`、Anthropic 的
`input_tokens/output_tokens`、文本兼容档的 usage 字段都必须归一化成同一套语义。
"""

from __future__ import annotations

import json

import httpx

from agent.adapters.base import ChatMessage, Completion, ModelUsage
from agent.adapters.native import NativeAdapter
from agent.adapters.text import TextAdapter
from agent.api.bus import EventBus
from agent.api.events import EventType, make_event
from agent.core.loop import AgentLoop
from agent.tools.registry import ToolRegistry


def test_from_provider_openai_shape():
    usage = ModelUsage.from_provider(
        {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}
    )
    assert (usage.input_tokens, usage.output_tokens, usage.total_tokens) == (11, 7, 18)


def test_from_provider_anthropic_shape():
    usage = ModelUsage.from_provider({"input_tokens": 11, "output_tokens": 7})
    assert (usage.input_tokens, usage.output_tokens) == (11, 7)
    # 供应商没给 total 时由 input + output 推导，而不是留空
    assert usage.total_tokens == 18


def test_completion_accepts_provider_dict_and_normalizes():
    completion = Completion(
        message=ChatMessage(role="assistant", content="hi"),
        usage={"prompt_tokens": 3, "completion_tokens": 4},
    )
    assert isinstance(completion.usage, ModelUsage)
    assert completion.usage.input_tokens == 3
    assert completion.usage.output_tokens == 4
    assert completion.usage.total_tokens == 7


def test_missing_usage_stays_none():
    completion = Completion(message=ChatMessage(role="assistant", content="hi"))
    assert completion.usage is None


class _FakeUsage:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def model_dump(self) -> dict:
        return self._payload


class _FakeOpenAIClient:
    """只实现 chat.completions.create 的最小替身。"""

    def __init__(self, content: str, usage: dict | None) -> None:
        payload = type(
            "Payload",
            (),
            {"usage": _FakeUsage(usage) if usage is not None else None},
        )()
        message = type("Message", (), {"content": content, "tool_calls": None})()
        choice = type("Choice", (), {"message": message, "finish_reason": "stop"})()
        self._response = type("Response", (), {"choices": [choice], "usage": payload.usage})()

    @property
    def chat(self) -> "_FakeOpenAIClient":
        return self

    @property
    def completions(self) -> "_FakeOpenAIClient":
        return self

    async def create(self, **kwargs):  # noqa: ANN003 - 与 SDK 签名对齐
        return self._response


async def test_native_adapter_reports_unified_usage():
    client = _FakeOpenAIClient(
        "回答", {"prompt_tokens": 30, "completion_tokens": 12, "total_tokens": 42}
    )
    adapter = NativeAdapter(client, "test-model")
    completion = await adapter.complete([ChatMessage(role="user", content="hi")], [])
    usage = completion.usage
    assert usage is not None
    assert (usage.input_tokens, usage.output_tokens, usage.total_tokens) == (30, 12, 42)


async def test_text_adapter_reports_unified_usage_for_plain_text():
    """纯文本回答也要有用量统计（之前 TextAdapter 直接返回 usage=None）。"""
    client = _FakeOpenAIClient(
        "这是一段普通回答", {"prompt_tokens": 8, "completion_tokens": 5, "total_tokens": 13}
    )
    adapter = TextAdapter(client, "test-model")
    completion = await adapter.complete([ChatMessage(role="user", content="hi")], [])
    usage = completion.usage
    assert usage is not None
    assert (usage.input_tokens, usage.output_tokens, usage.total_tokens) == (8, 5, 13)


async def test_anthropic_adapter_reports_unified_usage():
    from agent.adapters.anthropic import AnthropicAdapter

    adapter = AnthropicAdapter(api_key="k", model="claude-sonnet-4")
    try:
        completion = adapter._to_completion(
            {
                "content": [{"type": "text", "text": "回答"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 100, "output_tokens": 20},
            }
        )
    finally:
        await adapter.close()
    usage = completion.usage
    assert usage is not None
    assert (usage.input_tokens, usage.output_tokens, usage.total_tokens) == (100, 20, 120)


def test_loop_counts_output_tokens_only():
    """输出闸只能看输出 token：用 total（含输入）会让 Anthropic 被过早截断。"""
    loop = AgentLoop(_StaticAdapter({"input_tokens": 900, "output_tokens": 7}), ToolRegistry(), EventBus())
    completion = Completion(
        message=ChatMessage(role="assistant", content="x"),
        usage={"input_tokens": 900, "output_tokens": 7},
    )
    assert loop._tokens_of(completion) == 7


class _StaticAdapter:
    mode = "native"
    model = "test-model"

    def __init__(self, usage: dict | None = None) -> None:
        self._usage = usage

    async def complete(self, messages, tools, **kwargs):
        return Completion(
            message=ChatMessage(role="assistant", content="答案"), usage=self._usage
        )


async def test_usage_event_carries_unified_fields():
    bus = EventBus()
    seen: list[dict] = []

    async def consume() -> None:
        async for chunk in bus.stream():
            for line in chunk.splitlines():
                if line.startswith("data: "):
                    seen.append(json.loads(line[6:]))

    import asyncio

    task = asyncio.create_task(consume())
    await asyncio.sleep(0)
    loop = AgentLoop(
        _StaticAdapter({"input_tokens": 100, "output_tokens": 20}), ToolRegistry(), bus
    )
    await loop.run("hi")
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    usage_events = [e for e in seen if e["type"] == EventType.USAGE.value]
    assert usage_events
    data = usage_events[-1]["data"]
    assert data["tokens"] == 20  # 向后兼容字段 = 输出 token
    assert data["output_tokens"] == 20
    assert data["input_tokens"] == 100
    assert data["total_tokens"] == 120


async def test_anthropic_payload_normalization_matches_openai():
    """同一份「输入 100 / 输出 20」在两种协议下必须给出同一组统一语义。"""
    from agent.adapters.anthropic import AnthropicAdapter

    adapter = AnthropicAdapter(api_key="k", model="m")
    try:
        completion = adapter._to_completion(
            {"content": [], "usage": {"input_tokens": 100, "output_tokens": 20}}
        )
    finally:
        await adapter.close()
    openai_side = ModelUsage.from_provider(
        {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}
    )
    assert completion.usage == openai_side


def test_http_transport_is_importable_for_anthropic_tests():
    """保证测试环境有 httpx（Anthropic 适配层依赖它）。"""
    assert httpx is not None
    assert make_event(EventType.USAGE, {"tokens": 1}).type == EventType.USAGE
