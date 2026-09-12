"""P7 scenario 1: the current user message appears exactly once in model input.

Marker-based: a unique token is placed in the user input and we count its
occurrences across every message the adapter actually receives, covering the
normal turn plus the switch-topic and create-topic paths (where the message is
written to memory *before* injection so it can be migrated).
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from agent.adapters.base import ChatMessage, Completion, ToolCall
from agent.api.bus import EventBus
from agent.config import Settings
from agent.credentials.store import MemoryKeyring
from agent.services.app import AppContext
from agent.storage.db import connect
from agent.storage.migrate import apply_migrations

MARKER = "QIO_CURRENT_MESSAGE_UNIQUE_928374"


@pytest.fixture()
def ctx(tmp_path) -> AppContext:
    conn = connect(tmp_path / "app.db")
    apply_migrations(conn)
    app_ctx = AppContext(Settings(data_dir=tmp_path), conn, EventBus())
    app_ctx.credentials._kr = MemoryKeyring()
    return app_ctx


class _CapturingAdapter:
    """Records each model request as one string; optionally emits a tool call first.

    `calls[i]` = every message content of the i-th request, joined. The
    invariant under test is per-request: a later planning step legitimately
    re-sends the conversation, so counting across requests would be wrong.
    """

    mode = "native"
    model = "deepseek-v4-flash"

    def __init__(self, script: list | None = None) -> None:
        self.script = list(script or [])
        self.seen: list[str] = []
        self.calls: list[str] = []

    async def complete(self, messages, tools, **kwargs):
        contents = [m.content for m in messages if m.content]
        self.seen.extend(contents)
        self.calls.append("\n".join(contents))
        if self.script:
            name, args = self.script.pop(0)
            return Completion(
                message=ChatMessage(
                    role="assistant",
                    content=None,
                    tool_calls=[
                        ToolCall(id=f"c{len(self.seen)}", name=name, arguments=args)
                    ],
                )
            )
        return Completion(message=ChatMessage(role="assistant", content="好的"))

    @property
    def joined(self) -> str:
        return "\n".join(self.seen)

    def marker_count_per_call(self, marker: str) -> list[int]:
        return [call.count(marker) for call in self.calls]


def _seed_history(ctx: AppContext, topic: str, rounds: int = 3) -> None:
    for i in range(rounds):
        ctx.memory.append_message(topic_id=topic, role="user", content=f"历史消息 {i}")
        ctx.memory.append_message(
            topic_id=topic, role="assistant", content=f"历史回复 {i}"
        )


async def test_marker_once_on_normal_turn(ctx: AppContext):
    topic = ctx.topics.nodes.create_topic("普通话题").id
    _seed_history(ctx, topic)
    adapter = _CapturingAdapter()
    mp = pytest.MonkeyPatch()
    mp.setattr(ctx, "build_adapter", AsyncMock(return_value=adapter))
    try:
        await ctx.run_turn(f"请处理 {MARKER}", topic_id=topic)
    finally:
        mp.undo()

    assert adapter.marker_count_per_call(MARKER) == [1], adapter.calls
    assert "历史消息 0" in adapter.joined  # history not silently dropped


async def test_marker_once_when_model_switches_topic(ctx: AppContext):
    source = ctx.topics.nodes.create_topic("来源话题").id
    target = ctx.topics.nodes.create_topic("目标话题").id
    _seed_history(ctx, source)
    adapter = _CapturingAdapter(
        script=[("switch_topic", {"topic_id": target, "reason": "用户换了话题"})]
    )
    mp = pytest.MonkeyPatch()
    mp.setattr(ctx, "build_adapter", AsyncMock(return_value=adapter))
    try:
        await ctx.run_turn(f"换个话题 {MARKER}", topic_id=source)
    finally:
        mp.undo()

    # one request before the tool call, one after: the marker must appear
    # exactly once in each
    assert adapter.marker_count_per_call(MARKER) == [1, 1], adapter.calls
    # the message must exist once, now attached to the target topic
    rows = ctx.conn.execute(
        "SELECT m.content FROM messages m JOIN fragments f ON f.id = m.fragment_id "
        "WHERE f.topic_id = ? AND m.role = 'user'",
        (target,),
    ).fetchall()
    assert [r["content"] for r in rows] == [f"换个话题 {MARKER}"]


async def test_marker_once_when_model_creates_topic(ctx: AppContext):
    source = ctx.topics.nodes.create_topic("旧话题").id
    _seed_history(ctx, source)
    adapter = _CapturingAdapter(
        script=[("create_topic", {"name": "全新话题XYZ", "reason": "确属新话题"})]
    )
    mp = pytest.MonkeyPatch()
    mp.setattr(ctx, "build_adapter", AsyncMock(return_value=adapter))
    try:
        await ctx.run_turn(f"开启新话题 {MARKER}", topic_id=source)
    finally:
        mp.undo()

    assert adapter.marker_count_per_call(MARKER) == [1, 1], adapter.calls
    node = ctx.conn.execute(
        "SELECT id FROM nodes WHERE type = 'topic' AND name = '全新话题XYZ'"
    ).fetchone()
    assert node is not None
    rows = ctx.conn.execute(
        "SELECT m.content FROM messages m JOIN fragments f ON f.id = m.fragment_id "
        "WHERE f.topic_id = ? AND m.role = 'user'",
        (node["id"],),
    ).fetchall()
    assert [r["content"] for r in rows] == [f"开启新话题 {MARKER}"]
