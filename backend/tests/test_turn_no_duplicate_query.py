"""Invariant: the current user query appears exactly once in the model input.

The current message is written into the fragment *before* injection (so topic
switch/create can migrate it), then `_short_term_items` must exclude it —
otherwise it shows up both as historical short-term and as the explicit
current message.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from agent.adapters.base import ChatMessage, Completion
from agent.api.bus import EventBus
from agent.config import Settings
from agent.credentials.store import MemoryKeyring
from agent.services.app import AppContext
from agent.storage.db import connect
from agent.storage.migrate import apply_migrations


@pytest.fixture()
def ctx(tmp_path) -> AppContext:
    conn = connect(tmp_path / "app.db")
    apply_migrations(conn)
    app_ctx = AppContext(Settings(data_dir=tmp_path), conn, EventBus())
    app_ctx.credentials._kr = MemoryKeyring()
    return app_ctx


class _CapturingAdapter:
    mode = "native"
    model = "deepseek-v4-flash"

    def __init__(self) -> None:
        # 每次调用的消息正文（按调用分组）：契约 §1.1 起一轮有两次调用
        # （工作调用 + 回答调用），不变量是「**每次调用里**当前查询只出现一次」。
        self.requests: list[list[str]] = []

    async def complete(self, messages, tools, **kwargs):
        self.requests.append([m.content for m in messages if m.content])
        return Completion(message=ChatMessage(role="assistant", content="好的"))


async def test_current_query_appears_once_with_history(ctx: AppContext):
    node = ctx.topics.nodes.create_topic("测试话题")
    topic = node.id
    # 已有历史（在同一 open fragment）
    for i in range(3):
        ctx.memory.append_message(
            topic_id=topic, role="user", content=f"历史消息 {i}"
        )
        ctx.memory.append_message(
            topic_id=topic, role="assistant", content=f"历史回复 {i}"
        )

    adapter = _CapturingAdapter()
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(ctx, "build_adapter", AsyncMock(return_value=adapter))

    query = "唯一查询词XYZ"
    await ctx.run_turn(query, topic_id=topic)
    monkeypatch.undo()

    # 契约 §1.1 变更：回答由 [[QIO:ANSWER]] 声明，未声明走降级交付 ——
    # 合规直接问答只有 1 次调用
    assert len(adapter.requests) == 1, "合规直接问答只需 1 次调用（契约 §1.1）"
    joined = "\n".join(adapter.requests[0])
    assert joined.count(query) == 1, f"query injected {joined.count(query)} times"
    # 历史仍在（未被误删）
    assert "历史消息 0" in joined
    # 每一次调用里，当前查询都只能出现一次
    for seen in adapter.requests:
        text = "\n".join(seen)
        assert text.count(query) == 1, f"query injected {text.count(query)} times"


async def test_current_query_appears_once_empty_history(ctx: AppContext):
    node = ctx.topics.nodes.create_topic("空话题")
    topic = node.id
    adapter = _CapturingAdapter()
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(ctx, "build_adapter", AsyncMock(return_value=adapter))
    query = "第一次问ABCD"
    await ctx.run_turn(query, topic_id=topic)
    monkeypatch.undo()
    for seen in adapter.requests:
        text = "\n".join(seen)
        assert text.count(query) == 1, f"query injected {text.count(query)} times"
