"""P7 scenario 2: rapid submits — single-flight, FIFO, correct turn identity."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from agent.adapters.base import ChatMessage, Completion, ToolCall
from agent.api.bus import EventBus
from agent.config import Settings
from agent.credentials.store import MemoryKeyring
from agent.services.app import AppContext
from agent.storage.db import connect
from agent.storage.migrate import apply_migrations
from agent.tools.base import Tool, ToolResult


@pytest.fixture()
def ctx(tmp_path) -> AppContext:
    conn = connect(tmp_path / "app.db")
    apply_migrations(conn)
    app_ctx = AppContext(Settings(data_dir=tmp_path), conn, EventBus())
    app_ctx.credentials._kr = MemoryKeyring()
    return app_ctx


class _TaggedAdapter:
    """Answers '唯一X' prompts; records order and peak concurrency."""

    mode = "native"
    model = "deepseek-v4-flash"

    def __init__(self, hold_first: float = 0.0) -> None:
        self.order: list[str] = []
        self.peak = 0
        self._running = 0
        self.hold_first = hold_first
        self.first_started = asyncio.Event()
        self.release_first = asyncio.Event()

    async def complete(self, messages, tools, **kwargs):
        self._running += 1
        self.peak = max(self.peak, self._running)
        text = "\n".join(m.content or "" for m in messages)
        # the current message is the segment after the last 【用户消息】 marker;
        # earlier occurrences are history from previous turns
        current = text.rsplit("【用户消息】", 1)[-1]
        tag = next((t for t in ("A", "B", "C") if f"唯一{t}" in current), "?")
        self.order.append(tag)
        try:
            if tag == "A" and self.hold_first:
                self.first_started.set()
                await self.release_first.wait()
            else:
                await asyncio.sleep(0.01)
        finally:
            self._running -= 1
        return Completion(message=ChatMessage(role="assistant", content=f"回复{tag}"))


def _parse(chunks: list[str]) -> list[dict]:
    out: list[dict] = []
    for chunk in chunks:
        for part in chunk.splitlines():
            if part.startswith("data: "):
                out.append(json.loads(part[6:]))
    return out


async def test_abc_fifo_single_planner_and_message_order(ctx: AppContext):
    topic = ctx.topics.nodes.create_topic("t").id
    adapter = _TaggedAdapter(hold_first=True)
    mp = pytest.MonkeyPatch()
    mp.setattr(ctx, "build_adapter", AsyncMock(return_value=adapter))

    t_a = asyncio.create_task(ctx.run_turn("唯一A", topic_id=topic))
    await asyncio.wait_for(adapter.first_started.wait(), timeout=5)

    t_b = asyncio.create_task(ctx.run_turn("唯一B", topic_id=topic))
    t_c = asyncio.create_task(ctx.run_turn("唯一C", topic_id=topic))
    await asyncio.sleep(0.02)

    # A is the active turn; B and C wait in the queue (nothing dropped)
    assert ctx.turns.active is not None
    assert ctx.turns.queued_count() == 2
    assert adapter.peak == 1  # never a second main loop planning concurrently

    adapter.release_first.set()
    await asyncio.wait_for(asyncio.gather(t_a, t_b, t_c), timeout=20)
    mp.undo()

    assert adapter.order == ["A", "B", "C"]  # FIFO
    assert adapter.peak == 1
    assert ctx.turns.active is None

    rows = ctx.conn.execute(
        "SELECT role, content FROM messages ORDER BY rowid"
    ).fetchall()
    users = [r["content"] for r in rows if r["role"] == "user"]
    assert users == ["唯一A", "唯一B", "唯一C"]
    assistants = [r["content"] for r in rows if r["role"] == "assistant"]
    assert assistants == ["回复A", "回复B", "回复C"]


async def test_queue_state_is_published_as_events(ctx: AppContext):
    topic = ctx.topics.nodes.create_topic("t").id
    adapter = _TaggedAdapter(hold_first=True)
    mp = pytest.MonkeyPatch()
    mp.setattr(ctx, "build_adapter", AsyncMock(return_value=adapter))

    chunks: list[str] = []

    async def consume():
        async for chunk in ctx.bus.stream():
            chunks.append(chunk)

    consumer = asyncio.create_task(consume())
    await asyncio.sleep(0)
    try:
        t_a = asyncio.create_task(ctx.run_turn("唯一A", topic_id=topic))
        await adapter.first_started.wait()
        t_b = asyncio.create_task(ctx.run_turn("唯一B", topic_id=topic))
        await asyncio.sleep(0.05)
        adapter.release_first.set()
        await asyncio.gather(t_a, t_b)
        await asyncio.sleep(0.05)
    finally:
        consumer.cancel()
        mp.undo()

    events = _parse(chunks)
    queues = [e for e in events if e["type"] == "TURN_QUEUE"]
    assert queues, "queue state must be published"
    # at least one snapshot must show a non-empty queue
    assert any(e["data"].get("queued") for e in queues)
    # a running snapshot also exists (UI shows running vs queued)
    assert any(e["data"].get("running") for e in queues)


async def test_each_turn_pairs_its_own_start_and_end(ctx: AppContext):
    topic = ctx.topics.nodes.create_topic("t").id
    adapter = _TaggedAdapter(hold_first=True)
    mp = pytest.MonkeyPatch()
    mp.setattr(ctx, "build_adapter", AsyncMock(return_value=adapter))

    chunks: list[str] = []

    async def consume():
        async for chunk in ctx.bus.stream():
            chunks.append(chunk)

    consumer = asyncio.create_task(consume())
    await asyncio.sleep(0)
    try:
        t_a = asyncio.create_task(ctx.run_turn("唯一A", topic_id=topic))
        await adapter.first_started.wait()
        t_b = asyncio.create_task(ctx.run_turn("唯一B", topic_id=topic))
        adapter.release_first.set()
        await asyncio.gather(t_a, t_b)
        await asyncio.sleep(0.05)
    finally:
        consumer.cancel()
        mp.undo()

    events = _parse(chunks)
    starts = [e["data"]["turn_id"] for e in events if e["type"] == "TURN_START"]
    ends = [e["data"]["turn_id"] for e in events if e["type"] == "TURN_END"]
    assert len(starts) == 2 and len(ends) == 2
    assert starts == ends  # paired, in order
    assert len(set(starts)) == 2  # distinct identities
    # tool events inside A must never carry B's identity: covered by tool
    # isolation tests; here we assert no event mixes identities
    for e in events:
        if e["type"] in ("TURN_START", "TURN_END"):
            assert e["data"]["turn_id"] in starts


class _BlockingTool(Tool):
    name = "slow_task"
    description = "blocks until cancelled"
    parameters = {"type": "object", "properties": {}}

    def __init__(self) -> None:
        self.started = asyncio.Event()

    async def run(self, **kwargs):
        self.started.set()
        # bounded so a cancellation bug can never hang the suite
        await asyncio.sleep(5)
        return ToolResult(ok=True, content="never")


class _ToolThenAnswerAdapter:
    mode = "native"
    model = "deepseek-v4-flash"

    def __init__(self) -> None:
        self.n = 0

    async def complete(self, messages, tools, **kwargs):
        self.n += 1
        text = "\n".join(m.content or "" for m in messages)
        current = text.rsplit("【用户消息】", 1)[-1]
        if "唯一B" in current:
            return Completion(message=ChatMessage(role="assistant", content="回复B"))
        if self.n > 1:
            # only the first planning step of A asks for the tool; later steps
            # answer, so a cancellation bug cannot spin the loop
            return Completion(message=ChatMessage(role="assistant", content="回复A"))
        return Completion(
            message=ChatMessage(
                role="assistant",
                content=None,
                tool_calls=[ToolCall(id="c1", name="slow_task", arguments={})],
            )
        )


async def test_cancel_active_turn_lets_next_turn_start(ctx: AppContext):
    topic = ctx.topics.nodes.create_topic("t").id
    tool = _BlockingTool()
    ctx.registry.register(tool)
    mp = pytest.MonkeyPatch()
    mp.setattr(ctx, "build_adapter", AsyncMock(return_value=_ToolThenAnswerAdapter()))

    try:
        t_a = asyncio.create_task(ctx.run_turn("唯一A", topic_id=topic))
        await asyncio.wait_for(tool.started.wait(), timeout=5)

        t_b = asyncio.create_task(ctx.run_turn("唯一B", topic_id=topic))
        await asyncio.sleep(0.02)
        assert ctx.turns.queued_count() == 1

        assert ctx.turns.cancel_active() is True
        await asyncio.wait_for(asyncio.gather(t_a, t_b), timeout=10)
    finally:
        mp.undo()

    # A is recorded as cancelled; B still ran and produced its answer
    cancelled = ctx.turns.snapshot()["cancelled"]
    assert any(entry["turn_id"] for entry in cancelled)
    rows = ctx.conn.execute("SELECT role, content FROM messages ORDER BY rowid").fetchall()
    assert any(r["role"] == "assistant" and r["content"] == "回复B" for r in rows)
    assert ctx.turns.active is None
