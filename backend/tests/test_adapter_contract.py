"""Contract tests: the SAME AgentLoop runs on different provider shapes."""

from __future__ import annotations

import asyncio

from agent.adapters.base import (
    AdapterMode,
    BaseAdapter,
    ChatMessage,
    Completion,
    ToolCall,
)
from agent.api.bus import EventBus
from agent.core.loop import AgentLoop
from agent.tools.base import Tool, ToolResult
from agent.tools.registry import ToolRegistry


class _EchoTool(Tool):
    name = "echo"
    description = "echo"
    parameters = {"type": "object", "properties": {}}

    async def run(self, **kwargs):
        return ToolResult(ok=True, content="ok")


class _ToolCallingAdapter(BaseAdapter):
    """OpenAI-shaped adapter: returns QIO's own Completion (no raw objects)."""

    mode = AdapterMode.NATIVE

    def __init__(self) -> None:
        self.model = "fake-native"
        self.endpoint = None
        self.n = 0

    async def complete(self, messages, tools, *, temperature=None, max_tokens=None):
        self.n += 1
        if self.n == 1:
            return Completion(
                message=ChatMessage(
                    role="assistant",
                    content=None,
                    tool_calls=[ToolCall(id="c1", name="echo", arguments={})],
                ),
                usage={"prompt_tokens": 1, "completion_tokens": 2},
                finish_reason="tool_calls",
            )
        return Completion(
            message=ChatMessage(role="assistant", content="done"),
            usage={"prompt_tokens": 3, "completion_tokens": 4},
            finish_reason="stop",
        )


class _TextModeAdapter(BaseAdapter):
    """Text-mode adapter: same Completion contract, different mode."""

    mode = AdapterMode.TEXT

    def __init__(self) -> None:
        self.model = "fake-text"
        self.endpoint = None
        self.n = 0

    async def complete(self, messages, tools, *, temperature=None, max_tokens=None):
        self.n += 1
        if self.n == 1:
            return Completion(
                message=ChatMessage(
                    role="assistant",
                    content=None,
                    tool_calls=[ToolCall(id="t1", name="echo", arguments={})],
                ),
                finish_reason="tool_calls",
            )
        return Completion(
            message=ChatMessage(role="assistant", content="done"),
            finish_reason="stop",
        )


def _run(adapter) -> object:
    reg = ToolRegistry()
    reg.register(_EchoTool())
    loop = AgentLoop(adapter, reg, EventBus())
    return asyncio.run(loop.run("hi"))


def test_same_loop_works_with_two_providers():
    native = _ToolCallingAdapter()
    text = _TextModeAdapter()
    r_native = _run(native)
    r_text = _run(text)
    # 同一个 AgentLoop（未改动）在两种 provider 上行为一致
    assert r_native.final_content == "done"
    assert r_text.final_content == "done"
    assert r_native.tool_calls_made == 1
    assert r_text.tool_calls_made == 1
    assert r_native.phase.value == r_text.phase.value


def test_completion_finish_reason_is_internal_field():
    c = Completion(message=ChatMessage(role="assistant", content="x"), finish_reason="stop")
    assert c.finish_reason == "stop"
