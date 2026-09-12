"""P7 scenarios 6 & 7: topic graph navigation, and the knowledge injection gate."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from agent.adapters.base import ChatMessage, Completion, ToolCall
from agent.api.bus import EventBus
from agent.config import Settings
from agent.credentials.store import MemoryKeyring
from agent.graph.anchors import AnchorService
from agent.knowledge.lifecycle import KnowledgeService
from agent.services.affinity import related_topics
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


class _ScriptedAdapter:
    mode = "native"
    model = "deepseek-v4-flash"

    def __init__(self, script: list | None = None) -> None:
        self.script = list(script or [])
        self.requests: list[str] = []

    async def complete(self, messages, tools, **kwargs):
        self.requests.append("\n".join(m.content or "" for m in messages))
        if self.script:
            name, args = self.script.pop(0)
            return Completion(
                message=ChatMessage(
                    role="assistant",
                    content=None,
                    tool_calls=[ToolCall(id=f"c{len(self.requests)}", name=name, arguments=args)],
                )
            )
        return Completion(message=ChatMessage(role="assistant", content="好的"))

    @property
    def last_request(self) -> str:
        return self.requests[-1]


async def _run(ctx: AppContext, adapter: _ScriptedAdapter, text: str, topic: str) -> None:
    mp = pytest.MonkeyPatch()
    mp.setattr(ctx, "build_adapter", AsyncMock(return_value=adapter))
    try:
        await ctx.run_turn(text, topic_id=topic)
    finally:
        mp.undo()


# -- scenario 6: topic graph ------------------------------------------------


async def test_topic_graph_navigation_flow(ctx: AppContext):
    anchors = AnchorService(ctx.conn)
    a = ctx.topics.nodes.create_topic("话题A").id
    b = ctx.topics.nodes.create_topic("话题B").id
    anchors.set_active(a)

    # 1. in-topic: anchor unchanged
    await _run(ctx, _ScriptedAdapter(), "继续聊话题A的细节", a)
    assert anchors.get_active().topic_id == a

    # 2. switch to an existing topic
    await _run(
        ctx,
        _ScriptedAdapter(script=[("switch_topic", {"topic_id": b, "reason": "换话题"})]),
        "我们聊点别的",
        a,
    )
    assert anchors.get_active().topic_id == b

    # 3. create a new topic
    await _run(
        ctx,
        _ScriptedAdapter(script=[("create_topic", {"name": "话题C", "reason": "新话题"})]),
        "开始一个全新的话题",
        b,
    )
    c_row = ctx.conn.execute(
        "SELECT id FROM nodes WHERE type='topic' AND name='话题C'"
    ).fetchone()
    c = c_row["id"]
    assert anchors.get_active().topic_id == c

    # 4. switch back to A: history is preserved, not discarded
    await _run(
        ctx,
        _ScriptedAdapter(script=[("switch_topic", {"topic_id": a, "reason": "回到A"})]),
        "回到话题A",
        c,
    )
    assert anchors.get_active().topic_id == a
    stored = anchors.get_position(a)
    assert stored is not None, "switching away must keep the old anchor as history"

    # 5. related-topic recall: A and B were linked by the switches
    related = related_topics(ctx.conn, a, top_n=3)
    assert b in related


# -- scenario 7: knowledge lifecycle gate ------------------------------------


async def _injected_knowledge_lines(ctx: AppContext, topic: str, text: str) -> list[str]:
    adapter = _ScriptedAdapter()
    await _run(ctx, adapter, text, topic)
    return [ln for ln in adapter.last_request.splitlines() if ln.startswith("[知识·")]


async def test_only_active_knowledge_reaches_injection(ctx: AppContext):
    topic = ctx.topics.nodes.create_topic("知识话题").id
    ks = KnowledgeService(ctx.conn)
    item = ks.create(
        category="general_fact",
        content="注入验证标记 KNOWLEDGE_GATE_MARKER_42",
        node_ids=[topic],
    )

    # draft
    assert not await _injected_knowledge_lines(ctx, topic, "现在讲什么？")
    # pending_review
    ks.submit(item.id)
    assert not await _injected_knowledge_lines(ctx, topic, "现在讲什么？")
    # verified — still not in the injection surface
    ks.verify(item.id, verified_by="system")
    assert not await _injected_knowledge_lines(ctx, topic, "现在讲什么？")
    # active
    ks.activate(item.id)
    lines = await _injected_knowledge_lines(ctx, topic, "现在讲什么？")
    assert any("KNOWLEDGE_GATE_MARKER_42" in ln for ln in lines), lines
    # revoked
    ks.revoke(item.id)
    assert not await _injected_knowledge_lines(ctx, topic, "现在讲什么？")


async def test_high_impact_knowledge_cannot_self_activate(ctx: AppContext):
    ks = KnowledgeService(ctx.conn)
    item = ks.create(category="user_profile", content="用户是素食者")
    ks.submit(item.id)
    with pytest.raises(PermissionError):
        ks.verify(item.id, verified_by="system")
    assert ks.get(item.id).state.value == "pending_review"
