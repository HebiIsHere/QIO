from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any

import pytest

from agent.adapters.base import (
    AdapterMode,
    ChatMessage,
    ToolCall,
    ToolSpec,
    ToolCallParseError,
)
from agent.adapters.native import NativeAdapter
from agent.adapters.probe import ProbeCache, ProbeResult, probe_adapter
from agent.adapters.text import TextAdapter

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


@dataclass
class FakeMessage:
    content: str | None
    tool_calls: list[Any] | None = None


@dataclass
class FakeChoice:
    message: FakeMessage


@dataclass
class FakeCompletion:
    choices: list[FakeChoice]
    usage: Any = None


def _tc(tool_id: str, name: str, arguments: str) -> Any:
    return type(
        "TC",
        (),
        {"id": tool_id, "function": type("F", (), {"name": name, "arguments": arguments})()},
    )()


class FakeNativeClient:
    """Returns a tool call first, then plain text."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.tool_call = _tc("call_1", "get_weather", '{"city": "Shanghai"}')
        self.fail_arguments: str | None = None
        self.failures_before_success = 0

    @property
    def chat(self) -> "FakeNativeClient":
        return self

    @property
    def completions(self) -> "FakeNativeClient":
        return self

    async def create(self, **kwargs: Any) -> FakeCompletion:
        self.calls.append(kwargs)
        if self.fail_arguments is not None and self.failures_before_success > 0:
            self.failures_before_success -= 1
            return FakeCompletion(
                [FakeChoice(FakeMessage(None, [_tc("call_bad", "get_weather", self.fail_arguments)]))]
            )
        if self.tool_call is not None:
            return FakeCompletion([FakeChoice(FakeMessage(None, [self.tool_call]))])
        return FakeCompletion([FakeChoice(FakeMessage("it is sunny", None))])


async def test_native_returns_parsed_tool_call():
    client = FakeNativeClient()
    adapter = NativeAdapter(client=client, model="m1")
    completion = await adapter.complete([ChatMessage("user", "weather?")], TOOLS)
    assert completion.tool_calls is not None
    assert completion.tool_calls[0].name == "get_weather"
    assert completion.tool_calls[0].arguments == {"city": "Shanghai"}


async def test_native_plain_text_when_no_tool_call():
    client = FakeNativeClient()
    client.tool_call = None
    adapter = NativeAdapter(client=client, model="m1")
    completion = await adapter.complete([ChatMessage("user", "hi")], TOOLS)
    assert completion.tool_calls is None
    assert completion.message.content == "it is sunny"


async def test_native_parse_retry_then_success():
    client = FakeNativeClient()
    client.fail_arguments = "{bad json"
    client.failures_before_success = 1
    adapter = NativeAdapter(client=client, model="m1")
    completion = await adapter.complete([ChatMessage("user", "weather?")], TOOLS)
    assert completion.tool_calls is not None
    assert completion.tool_calls[0].name == "get_weather"
    assert len(client.calls) == 2
    assert client.calls[1]["messages"][-1]["role"] == "tool"


async def test_native_parse_retry_exhausted_raises():
    client = FakeNativeClient()
    client.fail_arguments = "{bad json"
    client.failures_before_success = 99
    adapter = NativeAdapter(client=client, model="m1")
    with pytest.raises(ToolCallParseError):
        await adapter.complete([ChatMessage("user", "weather?")], TOOLS)


async def test_text_adapter_parses_json_block():
    class FakeTextClient:
        def __init__(self) -> None:
            self.content = '```json\n{"tool_calls": [{"name": "get_weather", "arguments": {"city": "Beijing"}}]}\n```'

        @property
        def chat(self) -> "FakeTextClient":
            return self

        @property
        def completions(self) -> "FakeTextClient":
            return self

        async def create(self, **kwargs: Any) -> FakeCompletion:
            return FakeCompletion([FakeChoice(FakeMessage(self.content, None))])

    adapter = TextAdapter(client=FakeTextClient(), model="m1")
    completion = await adapter.complete([ChatMessage("user", "weather?")], TOOLS)
    assert completion.tool_calls is not None
    assert completion.tool_calls[0].name == "get_weather"
    assert completion.tool_calls[0].arguments == {"city": "Beijing"}
    assert adapter.success_rate == 1.0


async def test_text_adapter_tracks_failure_rate():
    class FlakyClient:
        def __init__(self) -> None:
            self.responses = ["not json at all", "```json\n{\"tool_calls\": []}\n```"]

        @property
        def chat(self) -> "FlakyClient":
            return self

        @property
        def completions(self) -> "FlakyClient":
            return self

        async def create(self, **kwargs: Any) -> FakeCompletion:
            return FakeCompletion([FakeChoice(FakeMessage(self.responses.pop(0), None))])

    adapter = TextAdapter(client=FlakyClient(), model="m1")
    await adapter.complete([ChatMessage("user", "hi")], TOOLS)
    await adapter.complete([ChatMessage("user", "hi")], TOOLS)
    assert adapter.success_rate == 0.5


async def test_probe_cache():
    cache = ProbeCache()
    result = ProbeResult(AdapterMode.NATIVE, "ok", 0.0)
    cache.set("ep", "m", result)
    assert cache.get("ep", "m") == result
    assert cache.get("ep", "other") is None


async def test_probe_native():
    class GoodClient:
        @property
        def chat(self) -> "GoodClient":
            return self

        @property
        def completions(self) -> "GoodClient":
            return self

        async def create(self, **kwargs: Any) -> FakeCompletion:
            assert "tools" in kwargs
            return FakeCompletion([FakeChoice(FakeMessage("pong", None))])

    result = await probe_adapter(GoodClient(), "m1")
    assert result.mode == AdapterMode.NATIVE


async def test_probe_text_rejected():
    from openai import BadRequestError

    class BadClient:
        @property
        def chat(self) -> "BadClient":
            return self

        @property
        def completions(self) -> "BadClient":
            return self

        async def create(self, **kwargs: Any) -> FakeCompletion:
            raise BadRequestError(
                "tools are not supported for this model",
                response=type("R", (), {"status_code": 400, "request": None, "headers": {}})(),
                body=None,
            )

    result = await probe_adapter(BadClient(), "m1")
    assert result.mode == AdapterMode.TEXT