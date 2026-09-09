from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import pytest

from agent.adapters.base import AdapterMode, ChatMessage, ToolCall, ToolSpec
from agent.adapters.native import NativeAdapter
from agent.api.events import EventType, make_event
from agent.api.server import EventBus
from agent.core.loop import AgentLoop
from agent.tools.builtin import EchoTool
from agent.tools.registry import ToolRegistry

TOOLS = [
    ToolSpec(
        name="echo",
        description="Echoes text",
        parameters={"type": "object", "properties": {"text": {"type": "string"}}},
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


class ScriptedClient:
    """Plays a script of raw responses, then falls back to plain text."""

    def __init__(self, script: list[Any]) -> None:
        self.script = list(script)
        self.calls = 0

    @property
    def chat(self) -> "ScriptedClient":
        return self

    @property
    def completions(self) -> "ScriptedClient":
        return self

    async def create(self, **kwargs: Any) -> FakeCompletion:
        self.calls += 1
        if self.script:
            return self.script.pop(0)
        return FakeCompletion([FakeChoice(FakeMessage("final answer", None))])


def _make_loop(client: ScriptedClient, **loop_kwargs: Any):
    adapter = NativeAdapter(client=client, model="m1")
    registry = ToolRegistry()
    registry.register(EchoTool())
    bus = EventBus()
    loop = AgentLoop(adapter, registry, bus, **loop_kwargs)
    return loop, bus


async def test_plain_text_turn():
    client = ScriptedClient([])
    loop, _ = _make_loop(client)
    result = await loop.run("hello")
    assert result.phase.value == "done"
    assert result.final_content == "final answer"
    assert result.tool_calls_made == 0


async def test_single_tool_turn():
    client = ScriptedClient(
        [FakeCompletion([FakeChoice(FakeMessage(None, [_tc("c1", "echo", '{"text": "hi"}')]))])]
    )
    loop, _ = _make_loop(client)
    result = await loop.run("say hi")
    assert result.tool_calls_made == 1
    assert result.final_content == "final answer"
    assert client.calls == 2  # tool round + final


async def test_budget_stops_runaway_loop():
    client = ScriptedClient(
        [FakeCompletion([FakeChoice(FakeMessage(None, [_tc(f"c{i}", "echo", '{"text": "x"}')]))]) for i in range(50)]
    )
    loop, _ = _make_loop(client, max_iterations=3)
    result = await loop.run("loop")
    assert result.phase.value == "stopped"
    assert result.iterations_used == 3


async def test_budget_stop_emits_warning_event():
    """预算耗尽停止时须发出 WARNING，前端不再静默无输出。"""
    client = ScriptedClient(
        [FakeCompletion([FakeChoice(FakeMessage(None, [_tc(f"c{i}", "echo", '{"text": "x"}')]))]) for i in range(50)]
    )
    loop, bus = _make_loop(client, max_iterations=3)

    collected: list[str] = []

    async def consumer():
        async for chunk in bus.stream():
            collected.append(chunk)

    task = asyncio.create_task(consumer())
    await asyncio.sleep(0.05)
    await loop.run("loop")
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    joined = "\n".join(collected)
    assert "event: WARNING" in joined
    assert "budget" in joined or "迭代" in joined or "token" in joined


async def test_force_continue_overrides_budget():
    client = ScriptedClient(
        [FakeCompletion([FakeChoice(FakeMessage(None, [_tc(f"c{i}", "echo", '{"text": "x"}')]))]) for i in range(50)]
    )
    loop, _ = _make_loop(client, max_iterations=3, force_continue=True)
    result = await loop.run("loop")
    assert result.phase.value == "done"  # ran past the budget to completion


async def test_unknown_tool_failure_isolated_and_warns():
    client = ScriptedClient(
        [FakeCompletion([FakeChoice(FakeMessage(None, [_tc("c1", "ghost", "{}")]))])]
    )
    loop, _ = _make_loop(client)
    result = await loop.run("call ghost")
    assert result.tool_calls_made == 1
    assert len(result.warnings) == 1
    assert "ghost" in result.warnings[0]
    assert result.phase.value == "done"


async def test_turn_events_emitted():
    client = ScriptedClient([])
    loop, bus = _make_loop(client)

    collected: list[str] = []

    async def consumer():
        async for chunk in bus.stream():
            collected.append(chunk)

    task = asyncio.create_task(consumer())
    await asyncio.sleep(0.05)
    await loop.run("hello")
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    joined = "\n".join(collected)
    assert "TURN_START" in joined
    assert "TURN_END" in joined
    assert "USAGE" in joined
async def test_interim_assistant_event_emitted_for_native_commentary():
    client = ScriptedClient(
        [FakeCompletion([FakeChoice(FakeMessage("我先查一下仓库", [_tc("c1", "echo", '{"text": "hi"}')]))])]
    )
    loop, bus = _make_loop(client)

    collected: list[str] = []

    async def consumer():
        async for chunk in bus.stream():
            collected.append(chunk)

    task = asyncio.create_task(consumer())
    await asyncio.sleep(0.05)
    await loop.run("查一下仓库")
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    joined = "\n".join(collected)
    assert "event: ASSISTANT" in joined
    assert "我先查一下仓库" in joined
    assert joined.index("我先查一下仓库") < joined.index("TURN_END")


async def test_plain_text_turn_no_interim_assistant_event():
    client = ScriptedClient([])
    loop, bus = _make_loop(client)

    collected: list[str] = []

    async def consumer():
        async for chunk in bus.stream():
            collected.append(chunk)

    task = asyncio.create_task(consumer())
    await asyncio.sleep(0.05)
    await loop.run("hello")
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert "event: ASSISTANT" not in "\n".join(collected)
