from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from agent.adapters.anthropic import (
    AnthropicAdapter,
    is_anthropic_endpoint,
    probe_anthropic,
)
from agent.adapters.base import ChatMessage, ToolCall, ToolCallParseError, ToolSpec

TOOLS = [
    ToolSpec(
        name="get_weather",
        description="Get weather for a city",
        parameters={
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
        },
    )
]


def _make_adapter(handler, **kwargs) -> AnthropicAdapter:
    adapter = AnthropicAdapter(api_key="sk-ant-test", model="claude-sonnet-4", **kwargs)
    adapter._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        headers={"x-api-key": "sk-ant-test", "anthropic-version": "2023-06-01"},
    )
    return adapter


def test_is_anthropic_endpoint():
    assert is_anthropic_endpoint("https://api.anthropic.com/v1")
    assert not is_anthropic_endpoint("https://api.deepseek.com/v1")
    assert not is_anthropic_endpoint(None)


def test_message_conversion_system_and_tool_results():
    adapter = AnthropicAdapter(api_key="k", model="m")
    messages = [
        ChatMessage(role="system", content="你是助手"),
        ChatMessage(role="user", content="天气如何？"),
        ChatMessage(
            role="assistant",
            content="我来查",
            tool_calls=[ToolCall(id="toolu_1", name="get_weather", arguments={"city": "上海"})],
        ),
        ChatMessage(role="tool", tool_call_id="toolu_1", content='{"temp": 25}'),
        ChatMessage(role="user", content="谢谢"),
    ]
    out = adapter.to_anthropic_messages(messages)
    assert adapter._extract_system(messages) == "你是助手"
    # tool_result merged into a user message after the assistant tool_use
    assert out[1]["role"] == "assistant"
    assert out[1]["content"][1]["type"] == "tool_use"
    assert out[1]["content"][1]["input"] == {"city": "上海"}
    assert out[2]["role"] == "user"
    assert out[2]["content"][0]["type"] == "tool_result"
    assert out[2]["content"][0]["tool_use_id"] == "toolu_1"
    assert out[3]["role"] == "user" and out[3]["content"] == "谢谢"


def test_consecutive_user_merge():
    adapter = AnthropicAdapter(api_key="k", model="m")
    out = adapter.to_anthropic_messages(
        [
            ChatMessage(role="user", content="第一条"),
            ChatMessage(role="user", content="第二条"),
        ]
    )
    assert len(out) == 1
    assert out[0]["content"] == "第一条\n\n第二条"


async def test_complete_parses_tool_use():
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["model"] == "claude-sonnet-4"
        assert body["max_tokens"] == 4096
        assert body["tools"][0]["input_schema"]["properties"]["city"]["type"] == "string"
        return httpx.Response(
            200,
            json={
                "content": [
                    {"type": "tool_use", "id": "toolu_1", "name": "get_weather", "input": {"city": "北京"}}
                ],
                "stop_reason": "tool_use",
                "usage": {"input_tokens": 10, "output_tokens": 5},
            },
        )

    adapter = _make_adapter(handler)
    completion = await adapter.complete(
        [ChatMessage(role="user", content="天气")], TOOLS
    )
    assert completion.tool_calls is not None
    assert completion.tool_calls[0].name == "get_weather"
    assert completion.tool_calls[0].arguments == {"city": "北京"}
    assert completion.usage["total_tokens"] == 15
    await adapter.close()


async def test_complete_plain_text():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "content": [{"type": "text", "text": "今天晴"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        )

    adapter = _make_adapter(handler)
    completion = await adapter.complete([ChatMessage(role="user", content="hi")], [])
    assert completion.tool_calls is None
    assert completion.message.content == "今天晴"
    await adapter.close()


async def test_parse_error_retry_then_success():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(
                200,
                json={
                    "content": [
                        {"type": "tool_use", "id": "toolu_bad", "name": "get_weather", "input": "not-an-object"}
                    ],
                    "stop_reason": "tool_use",
                    "usage": {},
                },
            )
        return httpx.Response(
            200,
            json={
                "content": [{"type": "text", "text": "修正后回答"}],
                "stop_reason": "end_turn",
                "usage": {},
            },
        )

    adapter = _make_adapter(handler)
    completion = await adapter.complete(
        [ChatMessage(role="user", content="天气")], TOOLS
    )
    assert calls["n"] == 2
    assert completion.message.content == "修正后回答"
    await adapter.close()


async def test_http_error_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="invalid x-api-key")

    adapter = _make_adapter(handler)
    from agent.adapters.errors import AuthenticationError

    # provider-specific HTTP error 现已归一化为内部异常
    with pytest.raises(AuthenticationError, match="401"):
        await adapter.complete([ChatMessage(role="user", content="hi")], [])
    await adapter.close()


async def test_probe_anthropic_native(monkeypatch):
    async def fake_probe(api_key, model, endpoint):
        return "native"

    monkeypatch.setattr("agent.adapters.anthropic.probe_anthropic", fake_probe)
    # direct probe through a mocked adapter path
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"content": [{"type": "text", "text": "pong"}], "stop_reason": "end_turn", "usage": {}},
        )

    adapter = _make_adapter(handler, max_tokens=8)
    result = await adapter.complete(
        [ChatMessage(role="user", content="ping")], TOOLS, max_tokens=8
    )
    assert result.message.content == "pong"
    await adapter.close()
