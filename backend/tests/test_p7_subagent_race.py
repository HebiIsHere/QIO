"""P7 scenario 3: a finishing subagent must never notify the wrong turn.

Two timings are covered, each repeated under slightly different event-loop
timing so we do not validate only one interleaving:

  (a) the subagent finishes while turn A is still active;
  (b) the subagent finishes after A is gone, while B is queued.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from agent.adapters.base import ChatMessage, Completion, ToolCall
from agent.api.bus import EventBus
from agent.config import Settings
from agent.credentials.store import MemoryKeyring
from agent.services.app import AppContext
from agent.storage.db import connect
from agent.storage.migrate import apply_migrations
from agent.tools.base import ToolResult
from agent.tools.task_manager import TaskRecord

RESULT_MARKER = "SUBAGENT_RESULT_MARKER_7"


@pytest.fixture()
def ctx(tmp_path) -> AppContext:
    conn = connect(tmp_path / "app.db")
    apply_migrations(conn)
    app_ctx = AppContext(Settings(data_dir=tmp_path), conn, EventBus())
    app_ctx.credentials._kr = MemoryKeyring()
    return app_ctx


class _RaceAdapter:
    """Turn A: block on the first planning step, then call a tool, then answer.
    Turn B / notify turns: answer immediately. Records every request."""

    mode = "native"
    model = "deepseek-v4-flash"

    def __init__(self) -> None:
        self.inputs: list[str] = []
        self.a_calls = 0
        self.a_started = asyncio.Event()
        self.release = asyncio.Event()

    @staticmethod
    def _current(text: str) -> str:
        return text.rsplit("【用户消息】", 1)[-1]

    async def complete(self, messages, tools, **kwargs):
        text = "\n".join(m.content or "" for m in messages)
        self.inputs.append(text)
        current = self._current(text)
        if "唯一A" in current:
            self.a_calls += 1
            if self.a_calls == 1:
                self.a_started.set()
                await self.release.wait()
                return Completion(
                    message=ChatMessage(
                        role="assistant",
                        content=None,
                        tool_calls=[ToolCall(id="c1", name="now", arguments={})],
                    )
                )
            return Completion(message=ChatMessage(role="assistant", content="回复A"))
        return Completion(message=ChatMessage(role="assistant", content="回复B"))

    def requests_containing(self, needle: str) -> int:
        return sum(1 for text in self.inputs if needle in text)

    def b_requests_containing(self, needle: str) -> int:
        return sum(
            1
            for text in self.inputs
            if needle in text and "唯一B" in self._current(text)
        )

    def a_requests_containing(self, needle: str) -> int:
        return sum(
            1
            for text in self.inputs
            if needle in text and "唯一A" in self._current(text)
        )


def _record() -> TaskRecord:
    return TaskRecord(
        task_id="task_race",
        tool="research_x",
        status="done",
        result=ToolResult(ok=True, content=f"研究发现 {RESULT_MARKER}"),
    )


@pytest.mark.parametrize("delay", [0.0, 0.005, 0.02])
async def test_notice_reaches_active_turn_and_not_the_queued_turn(ctx: AppContext, delay):
    topic = ctx.topics.nodes.create_topic("t").id
    adapter = _RaceAdapter()
    mp = pytest.MonkeyPatch()
    mp.setattr(ctx, "build_adapter", AsyncMock(return_value=adapter))

    try:
        t_a = asyncio.create_task(ctx.run_turn("唯一A", topic_id=topic))
        await asyncio.wait_for(adapter.a_started.wait(), timeout=5)

        t_b = asyncio.create_task(ctx.run_turn("唯一B", topic_id=topic))
        await asyncio.sleep(delay)

        # subagent finishes while A is still active
        await ctx._handle_subagent_notify("task_race", _record())

        adapter.release.set()
        await asyncio.wait_for(asyncio.gather(t_a, t_b), timeout=20)
    finally:
        mp.undo()

    # A saw the notice on a later planning step ...
    assert adapter.a_requests_containing(RESULT_MARKER) >= 1
    # ... and B never saw it
    assert adapter.b_requests_containing(RESULT_MARKER) == 0


@pytest.mark.parametrize("delay", [0.0, 0.005, 0.02])
async def test_notice_after_turn_ends_becomes_its_own_turn(ctx: AppContext, delay):
    topic = ctx.topics.nodes.create_topic("t").id
    adapter = _RaceAdapter()
    mp = pytest.MonkeyPatch()
    mp.setattr(ctx, "build_adapter", AsyncMock(return_value=adapter))

    try:
        # no active turn: the notice cannot be routed into a loop
        assert ctx.turns.active is None
        t_b = asyncio.create_task(ctx.run_turn("唯一B", topic_id=topic))
        await asyncio.sleep(delay)
        await ctx._handle_subagent_notify("task_race", _record())
        await asyncio.wait_for(t_b, timeout=20)
        # let the queued notify turn run
        for _ in range(200):
            if ctx.turns.active is None and ctx.turns.queued_count() == 0:
                break
            await asyncio.sleep(0.01)
    finally:
        mp.undo()

    assert adapter.b_requests_containing(RESULT_MARKER) == 0
    # the notify turn ran as its own turn, with its own trace
    traces = ctx.trace_store.list(limit=20)
    assert len(traces) == 2, [t["turn_id"] for t in traces]
    assert all(t["status"] == "done" for t in traces)
    # notify turn writes an assistant message only, never a user message
    rows = ctx.conn.execute("SELECT role, content FROM messages ORDER BY rowid").fetchall()
    users = [r["content"] for r in rows if r["role"] == "user"]
    assert users == ["唯一B"]
