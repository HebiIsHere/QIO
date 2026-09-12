"""P7 scenarios 10 & 11: hard token boundary, and the provider matrix."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from agent.adapters.base import AdapterMode, ChatMessage, Completion, ToolCall, ToolSpec
from agent.adapters.errors import (
    AuthenticationError,
    NetworkError,
    ProviderError,
    RateLimitError,
)
from agent.api.bus import EventBus
from agent.config import Settings
from agent.core.loop import AgentLoop
from agent.credentials.store import MemoryKeyring
from agent.memory.index import estimate_tokens
from agent.services.app import AppContext
from agent.services.injection import PlannedItem
from agent.storage.db import connect
from agent.storage.migrate import apply_migrations
from agent.tools.base import Tool, ToolResult
from agent.tools.registry import ToolRegistry


@pytest.fixture()
def ctx(tmp_path) -> AppContext:
    conn = connect(tmp_path / "app.db")
    apply_migrations(conn)
    app_ctx = AppContext(Settings(data_dir=tmp_path), conn, EventBus())
    app_ctx.credentials._kr = MemoryKeyring()
    return app_ctx


# -- scenario 10: token boundary -------------------------------------------


def _huge_short_term(count: int = 20) -> list[PlannedItem]:
    items = []
    for i in range(count):
        text = f"短期记忆 {i} " + "内容" * 800
        items.append(
            PlannedItem(
                source="memory",
                surface="short_term",
                item_id=f"s{i}",
                text=text,
                tokens=estimate_tokens(text),
                score=1.0,
            )
        )
    return items


def test_hard_cap_holds_for_huge_inputs(ctx: AppContext):
    topic = ctx.topics.nodes.create_topic("t").id
    payload = ctx.build_injection(
        "超大查询 " * 5000,
        topic_id=topic,
        short_term=_huge_short_term(),
        model="unknown-model",
        system_prompt_tokens=20_000,
        adapter_overhead_tokens=5_000,
        tool_definitions_tokens=100_000,
        completion_reserve=4_096,
    )
    plan = payload.plan
    # every budget number stays non-negative
    assert plan.hard_cap >= 0
    assert plan.total_tokens >= 0
    breakdown = plan.budget_breakdown
    assert breakdown["usable_for_injection"] >= 0
    assert breakdown["injection_hard_cap"] == plan.hard_cap
    # the input budget never exceeds the room left after the reserves
    assert plan.total_tokens <= plan.hard_cap
    assert plan.total_tokens <= breakdown["usable_for_injection"]


def test_tiny_context_window_injects_nothing_rather_than_overflowing(ctx: AppContext):
    topic = ctx.topics.nodes.create_topic("t").id
    # force a genuinely small window by seeding the registry cache
    ctx.context_registry._data[(None, "tiny-model")] = (8_000, 4_102_444_800.0)
    payload = ctx.build_injection(
        "查询",
        topic_id=topic,
        model="tiny-model",
        system_prompt_tokens=100_000,  # alone exceeds the window
        completion_reserve=4_096,
    )
    assert payload.plan.hard_cap == 0
    assert payload.plan.total_tokens == 0
    assert payload.text == "" or payload.plan.total_tokens == 0


def test_massive_tool_schema_shrinks_the_injection_room(ctx: AppContext):
    topic = ctx.topics.nodes.create_topic("t").id
    small = ctx.build_injection("查询", topic_id=topic, model="unknown-model",
                                tool_definitions_tokens=1_000)
    large = ctx.build_injection("查询", topic_id=topic, model="unknown-model",
                                tool_definitions_tokens=20_000)
    assert large.plan.hard_cap < small.plan.hard_cap


def test_giant_query_alone_cannot_break_the_boundary(ctx: AppContext):
    topic = ctx.topics.nodes.create_topic("t").id
    payload = ctx.build_injection(
        "很长的当前输入 " * 20_000,
        topic_id=topic,
        short_term=_huge_short_term(5),
        model="unknown-model",
    )
    assert payload.plan.total_tokens <= payload.plan.hard_cap
    assert payload.plan.hard_cap >= 0


# -- scenario 11: provider matrix ------------------------------------------


class _EchoTool(Tool):
    name = "echo"
    description = "echo"
    parameters = {"type": "object", "properties": {}}

    async def run(self, **kwargs):
        return ToolResult(ok=True, content="ok")


class _ScriptedProvider:
    """QIO-native provider: same shape for native and text modes."""

    def __init__(self, mode: AdapterMode, calls: int = 1) -> None:
        self.mode = mode
        self.model = f"fake-{mode.value}"
        self.calls = calls
        self.n = 0
        self.seen_types: set[str] = set()
        self.seen_tool_types: set[str] = set()

    async def complete(self, messages, tools, **kwargs):
        for m in messages:
            assert isinstance(m, ChatMessage), type(m)
            self.seen_types.add(type(m).__name__)
        for spec in tools:
            assert isinstance(spec, ToolSpec), type(spec)
            self.seen_tool_types.add(type(spec).__name__)
        self.n += 1
        if self.n <= self.calls:
            return Completion(
                message=ChatMessage(
                    role="assistant",
                    content=None,
                    tool_calls=[ToolCall(id="c1", name="echo", arguments={})],
                ),
                finish_reason="tool_calls",
            )
        return Completion(
            message=ChatMessage(role="assistant", content="done"),
            finish_reason="stop",
        )


@pytest.mark.parametrize("mode", [AdapterMode.NATIVE, AdapterMode.TEXT])
def test_same_loop_runs_on_both_provider_shapes(mode):
    registry = ToolRegistry()
    registry.register(_EchoTool())
    provider = _ScriptedProvider(mode)
    result = asyncio.run(AgentLoop(provider, registry, EventBus()).run("hi"))
    assert result.final_content == "done"
    assert result.tool_calls_made == 1
    # the loop only ever handed over QIO's own models
    assert provider.seen_types == {"ChatMessage"}
    assert provider.seen_tool_types == {"ToolSpec"}


class _FailingProvider:
    mode = AdapterMode.NATIVE
    model = "failing"

    def __init__(self, exc: Exception) -> None:
        self.exc = exc

    async def complete(self, messages, tools, **kwargs):
        raise self.exc


@pytest.mark.parametrize(
    "error",
    [
        AuthenticationError("bad key"),
        RateLimitError("429"),
        NetworkError("connection reset"),
    ],
)
def test_provider_errors_are_normalized_not_sdk_types(error):
    """The loop must see QIO's own error taxonomy, never an SDK exception."""
    assert isinstance(error, ProviderError)
    registry = ToolRegistry()
    loop = AgentLoop(_FailingProvider(error), registry, EventBus())
    with pytest.raises(ProviderError) as excinfo:
        asyncio.run(loop.run("hi"))
    assert type(excinfo.value) is type(error)
    assert type(excinfo.value).__module__.startswith("agent.")


async def test_unsupported_provider_ends_turn_without_a_model_call(ctx: AppContext):
    """UNSUPPORTED → build_adapter returns None → a warning, not a crash."""
    topic = ctx.topics.nodes.create_topic("t").id
    mp = pytest.MonkeyPatch()
    mp.setattr(ctx, "build_adapter", AsyncMock(return_value=None))
    try:
        result = await ctx.run_turn("你好", topic_id=topic)
    finally:
        mp.undo()
    assert result.get("ok") is False
    # the turn failed cleanly; the runtime is still usable
    assert ctx.turns.active is None
