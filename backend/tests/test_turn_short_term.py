"""run_turn integration: short-term injection, topic switch migration, fragment tiers."""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock

from agent.adapters.base import ChatMessage, Completion, ToolCall
from agent.api.bus import EventBus
from agent.config import Settings
from agent.credentials.store import MemoryKeyring
from agent.graph.anchors import AnchorService
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


@pytest.fixture()
def topic(ctx: AppContext) -> str:
    node = ctx.topics.nodes.create_topic("测试话题")
    return node.id


class FakeToolAdapter:
    """Returns one tool call (optional), then plain completions."""

    mode = "native"
    model = "deepseek-v4-flash"

    def __init__(self, tool_call=None, final: str = "收到") -> None:
        self.tool_call = tool_call
        self.final = final
        self.prompts: list[str] = []

    async def complete(self, messages, tools, **kwargs):
        content = messages[0].content if messages else ""
        self.prompts.append(content or "")
        if self.tool_call:
            name, args = self.tool_call
            self.tool_call = None
            return Completion(
                message=ChatMessage(
                    role="assistant",
                    content=None,
                    tool_calls=[ToolCall(id="c1", name=name, arguments=args)],
                )
            )
        return Completion(message=ChatMessage(role="assistant", content=self.final))


async def _append_messages(ctx: AppContext, topic_id: str, n: int = 3):
    for i in range(n):
        ctx.memory.append_message(
            topic_id=topic_id, role="user", content=f"消息 {i} 用户喜欢清淡饮食"
        )


async def test_run_turn_injects_short_term_and_switches_topic(ctx: AppContext, topic: str, monkeypatch):
    await _append_messages(ctx, topic, n=3)
    t2 = ctx.topics.nodes.create_topic("第二个话题")
    adapter = FakeToolAdapter(tool_call=("switch_topic", {"topic_id": t2.id, "reason": "切过去"}))
    monkeypatch.setattr(ctx, "build_adapter", AsyncMock(return_value=adapter))

    result = await ctx.run_turn("继续聊数据库", topic_id=topic)
    assert result["ok"]

    # 注入包含短期记忆（开放片段原文）
    joined = "\n".join(adapter.prompts)
    assert "短期" in joined
    assert "用户喜欢清淡饮食" in joined

    # 锚点已切换，related 边建立
    assert AnchorService(ctx.conn).get_active().topic_id == t2.id
    row = ctx.conn.execute(
        "SELECT weight FROM edges WHERE type='related' "
        "AND ((src=? AND dst=?) OR (src=? AND dst=?))",
        (topic, t2.id, t2.id, topic),
    ).fetchone()
    assert row is not None and row["weight"] >= 1.0

    # 本轮用户消息与回复都迁移/写入新话题
    count = ctx.conn.execute(
        "SELECT COUNT(*) c FROM messages m JOIN fragments f ON f.id = m.fragment_id "
        "WHERE f.topic_id = ?",
        (t2.id,),
    ).fetchone()["c"]
    assert count >= 2


async def test_run_turn_create_topic(ctx: AppContext, topic: str, monkeypatch):
    adapter = FakeToolAdapter(tool_call=("create_topic", {"name": "量子物理", "reason": "新方向"}))
    monkeypatch.setattr(ctx, "build_adapter", AsyncMock(return_value=adapter))

    result = await ctx.run_turn("我们聊聊量子物理吧", topic_id=topic)
    assert result["ok"]

    node = ctx.conn.execute(
        "SELECT id FROM nodes WHERE type='topic' AND name='量子物理'"
    ).fetchone()
    assert node is not None
    assert AnchorService(ctx.conn).get_active().topic_id == node["id"]


async def test_fragment_tier_from_settings(ctx: AppContext, topic: str, monkeypatch):
    ctx.settings_store.set("fragment.max_messages", "5")
    await _append_messages(ctx, topic, n=4)
    adapter = FakeToolAdapter()
    monkeypatch.setattr(ctx, "build_adapter", AsyncMock(return_value=adapter))

    result = await ctx.run_turn("第 5 条触发封块", topic_id=topic)
    assert result["ok"]
    closed = ctx.conn.execute(
        "SELECT COUNT(*) c FROM fragments WHERE closed_at IS NOT NULL"
    ).fetchone()["c"]
    assert closed == 1

    # 默认阈值仍是 10
    ctx2_frag = ctx.fragments.get_or_create_open(topic)
    assert ctx2_frag.closed_at is None  # 新开放片段


async def test_run_turn_no_topic_switch_keeps_messages(ctx: AppContext, topic: str, monkeypatch):
    await _append_messages(ctx, topic, n=2)
    adapter = FakeToolAdapter()
    monkeypatch.setattr(ctx, "build_adapter", AsyncMock(return_value=adapter))
    result = await ctx.run_turn("继续", topic_id=topic)
    assert result["ok"]
    count = ctx.conn.execute(
        "SELECT COUNT(*) c FROM messages m JOIN fragments f ON f.id = m.fragment_id "
        "WHERE f.topic_id = ?",
        (topic,),
    ).fetchone()["c"]
    assert count == 4  # 2 旧 + 用户消息 + 回复
