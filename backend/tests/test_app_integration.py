"""AppContext integration: extraction chain, consolidation, three-surface injection."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

import pytest

from agent.adapters.base import ChatMessage, Completion
from agent.api.bus import EventBus
from agent.config import Settings
from agent.credentials.store import MemoryKeyring
from agent.services.app import AppContext
from agent.storage.db import connect
from agent.storage.migrate import apply_migrations


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class FakeSequenceAdapter:
    """Returns queued responses for complete() calls."""

    mode = "native"
    model = "deepseek-v4-flash"

    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)
        self.calls = 0

    async def complete(self, messages, tools, **kwargs):
        content = self.responses.pop(0) if self.responses else '{"title":"t","summary":"s","entities":[],"keywords":[]}'
        self.calls += 1
        return Completion(message=ChatMessage(role="assistant", content=content))


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


async def _append_messages(ctx: AppContext, topic_id: str, n: int = 3):
    for i in range(n):
        ctx.memory.append_message(topic_id=topic_id, role="user", content=f"消息 {i} 用户喜欢清淡饮食")


async def test_extraction_chain_low_impact_auto_activates(ctx: AppContext, topic: str):
    await _append_messages(ctx, topic)
    # one prior mention so the chunk-close mention reaches the lazy threshold (2)
    ctx.topics.nodes.mention("牛奶", topic, force=False)
    adapter = FakeSequenceAdapter([
        # fragment summary
        '{"title": "饮食", "summary": "用户偏好清淡饮食", "entities": ["牛奶"], "keywords": ["清淡"]}',
        # entity extraction (?????)
        '{"entities": []}',
        # knowledge extraction
        '{"candidates": ['
        '{"content": "用户偏好清淡饮食", "category": "user_profile", "attach": "user", "entity": null},'
        '{"content": "该话题讨论了清淡饮食", "category": "general_fact", "attach": "topic", "entity": null}'
        "]}",
    ])
    closed = await ctx._close_fragment(topic, adapter)
    assert closed is not None and closed.closed_at is not None

    rows = ctx.conn.execute(
        "SELECT category, state, node_ids FROM knowledge ORDER BY category"
    ).fetchall()
    states = {r["category"]: r["state"] for r in rows}
    nodes = {r["category"]: json.loads(r["node_ids"]) for r in rows}
    # user_profile is high impact -> submitted, waits for user confirmation
    assert states["user_profile"] == "pending_review"
    # general_fact is low impact -> auto activated and attached to the topic
    assert states["general_fact"] == "active"
    assert topic in nodes["general_fact"]
    # user candidate attached to the user root node
    assert nodes["user_profile"] and any(n.startswith("user_") for n in nodes["user_profile"])

    # entity lazy-created with mention edge
    entity = ctx.conn.execute(
        "SELECT n.id FROM nodes n WHERE n.type='entity' AND n.name='牛奶'"
    ).fetchone()
    assert entity is not None
    edge = ctx.conn.execute(
        "SELECT 1 FROM edges WHERE src=? AND dst=? AND type='mention'",
        (topic, entity["id"]),
    ).fetchone()
    assert edge is not None


async def test_extraction_failure_does_not_break_close(ctx: AppContext, topic: str):
    await _append_messages(ctx, topic)
    adapter = FakeSequenceAdapter([
        '{"title": "t", "summary": "s", "entities": [], "keywords": []}',
        "not valid json at all",  # extraction fails
    ])
    closed = await ctx._close_fragment(topic, adapter)
    assert closed is not None and closed.closed_at is not None
    count = ctx.conn.execute("SELECT COUNT(*) c FROM knowledge").fetchone()["c"]
    assert count == 0  # extraction failure degrades silently


async def test_consolidation_rolling_summary_and_cooldown(ctx: AppContext, topic: str):
    await _append_messages(ctx, topic, n=2)
    adapter = FakeSequenceAdapter([
        '{"title": "饮食", "summary": "合并后的滚动摘要", "entities": [], "keywords": ["清淡"]}',
    ])
    ok = await ctx.consolidate(topic, adapter)
    assert ok
    frag = ctx.fragments.get_or_create_open(topic)
    assert frag.summary == "合并后的滚动摘要"
    assert frag.meta.get("consolidated") is True
    assert frag.meta.get("consolidation_count") == 1
    # cooldown: second call within 10 minutes is skipped
    adapter2 = FakeSequenceAdapter([])
    ok2 = await ctx.consolidate(topic, adapter2)
    assert not ok2
    assert adapter2.calls == 0


async def test_three_surface_injection(ctx: AppContext, topic: str, monkeypatch):
    from agent.knowledge.lifecycle import KnowledgeService

    # seed knowledge on user + topic surfaces
    user_id = ctx._user_root_id()
    ks = KnowledgeService(ctx.conn)
    for content, nodes in (
        ("用户偏好清淡饮食", [user_id]),
        ("测试话题相关事实", [topic]),
    ):
        item = ks.create(category="general_fact", content=content, node_ids=nodes)
        ks.submit(item.id)
        ks.verify(item.id, verified_by="system")
        ks.activate(item.id)

    payload = ctx.build_injection(
        "用户 饮食 偏好",
        topic_id=topic,
        entity_ids=[],
        user_node_id=user_id,
        model="deepseek-v4-flash",
    )
    assert "长期记忆注入" in payload.text
    assert "用户偏好清淡饮食" in payload.text
    assert "测试话题相关事实" in payload.text
    surfaces = {i.surface for i in payload.plan.knowledge}
    assert {"user", "topic"} <= surfaces
    # 新预算模型：先扣 completion reserve / 当前 query 等，再乘比例。
    # 1M 窗口 - 4k reserve - query → ~248.9K（不再是裸露的 250K）
    assert 240_000 <= payload.plan.hard_cap < 250_000
    bd = payload.plan.budget_breakdown
    assert bd["context_window"] == 1_000_000
    assert bd["completion_reserve"] > 0
    assert payload.plan.hard_cap == bd["injection_hard_cap"]


async def test_run_turn_end_to_end_with_injection(ctx: AppContext, topic: str, monkeypatch):
    from agent.knowledge.lifecycle import KnowledgeService

    user_id = ctx._user_root_id()
    ks = KnowledgeService(ctx.conn)
    item = ks.create(category="user_profile", content="用户喜欢清淡饮食", node_ids=[user_id])
    ks.submit(item.id)
    ks.verify(item.id, verified_by="user")  # high impact requires user
    ks.activate(item.id)

    adapter = FakeSequenceAdapter([
        # main loop planning -> no tool calls, final answer
        '{"title":"x","summary":"x","entities":[],"keywords":[]}',  # not used by loop
    ])

    async def fake_build():
        return adapter

    monkeypatch.setattr(ctx, "build_adapter", fake_build)

    result = await ctx.run_turn("你好，请介绍一下饮食偏好", topic_id=topic)
    assert result["ok"] is True
    # user + assistant messages persisted
    count = ctx.conn.execute(
        "SELECT COUNT(*) c FROM messages WHERE fragment_id IN "
        "(SELECT id FROM fragments WHERE topic_id=?)",
        (topic,),
    ).fetchone()["c"]
    assert count >= 2
