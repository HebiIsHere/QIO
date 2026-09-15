"""失败与取消不得推进 Anchor（spec 第 36 / 76 条）。

Turn 失败、Turn 被取消这些情况下：不能产生新的历史位置，也不能改变当前话题，
用户上一次明确选择的起点必须原封不动地留给重试。
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from agent.adapters.base import ChatMessage, Completion
from agent.api.bus import EventBus
from agent.config import Settings
from agent.credentials.store import MemoryKeyring
from agent.graph.anchors import AnchorService
from agent.services.app import AppContext
from agent.storage.db import connect
from agent.storage.migrate import apply_migrations


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@pytest.fixture()
def ctx(tmp_path) -> AppContext:
    conn = connect(tmp_path / "no_advance.db")
    apply_migrations(conn)
    app_ctx = AppContext(Settings(data_dir=tmp_path), conn, EventBus())
    app_ctx.credentials._kr = MemoryKeyring()
    return app_ctx


def _seed_fragment(ctx: AppContext, topic_id: str, *, closed: bool = True, payload: str = "PAYLOAD") -> str:
    frag_id = f"frag_{'c' if closed else 'o'}_{payload[-3:]}"
    now = _now()
    ctx.conn.execute(
        "INSERT INTO fragments (id, topic_id, summary, created_at, closed_at) VALUES (?, ?, ?, ?, ?)",
        (frag_id, topic_id, "摘要", now, now if closed else None),
    )
    ctx.conn.execute(
        "INSERT INTO messages (id, fragment_id, role, content, content_type, created_at) "
        "VALUES (?, ?, 'user', ?, 'text', ?)",
        (f"{frag_id}_m1", frag_id, payload, now),
    )
    ctx.conn.commit()
    return frag_id


class CapturingAdapter:
    mode = "native"
    model = "fake-model"

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail

    async def complete(self, messages, tools, **kwargs):  # noqa: ANN001, ANN003
        if self.fail:
            raise RuntimeError("模型不可用")
        return Completion(message=ChatMessage(role="assistant", content="好的。"))


def _install(ctx: AppContext, monkeypatch, adapter: CapturingAdapter) -> None:
    async def fake_build():
        return adapter

    monkeypatch.setattr(ctx, "build_adapter", fake_build)


def _snapshot(ctx: AppContext) -> dict:
    """只快照「历史位置」相关事实：Anchor 与片段封块状态。

    用户消息本身是真发生过的事，失败也不回滚（第一阶段已定），
    所以这里不把「多了一条开放片段」当成错误推进。
    """
    return {
        "anchor": AnchorService(ctx.conn).get_active(),
        "fragments": [
            tuple(r)
            for r in ctx.conn.execute(
                "SELECT id, topic_id, closed_at, source_fragment_id FROM fragments "
                "WHERE closed_at IS NOT NULL OR source_fragment_id IS NOT NULL ORDER BY id"
            )
        ],
    }


async def test_failed_turn_does_not_advance_anchor_or_create_history(ctx: AppContext, monkeypatch):
    topic = ctx.topics.nodes.create_topic("失败话题").id
    frag = _seed_fragment(ctx, topic, closed=True)
    AnchorService(ctx.conn).set_active(topic, frag)
    _install(ctx, monkeypatch, CapturingAdapter(fail=True))
    before = _snapshot(ctx)

    result = await ctx.run_turn("这一轮会失败", topic_id=topic)

    assert result["ok"] is False
    assert _snapshot(ctx) == before, "失败不得产生新的历史位置"


async def test_failed_turn_keeps_a_pending_suggestion_intact(ctx: AppContext, monkeypatch):
    a = ctx.topics.nodes.create_topic("甲话题").id
    b = ctx.topics.nodes.create_topic("乙话题").id
    ctx.navigation.enter_topic(a)
    ctx.navigation.request_switch(b, reason="推测")
    _install(ctx, monkeypatch, CapturingAdapter(fail=True))

    await ctx.run_turn("这一轮会失败", topic_id=a)

    pending = ctx.navigation.pending_switch()
    assert pending is not None and pending["topic_id"] == b, "失败不得吞掉待确认切换"
    assert AnchorService(ctx.conn).get_active().topic_id == a


async def test_cancelled_turn_does_not_advance_anchor_or_create_history(ctx: AppContext, monkeypatch):
    topic = ctx.topics.nodes.create_topic("取消话题").id
    frag = _seed_fragment(ctx, topic, closed=True)
    AnchorService(ctx.conn).set_active(topic, frag)
    _install(ctx, monkeypatch, CapturingAdapter())
    before = _snapshot(ctx)

    original_persist = ctx.turn_orchestrator.persist

    async def cancel_then_persist(turn_ctx, adapter, plan, result):
        turn_ctx.cancelled = True
        return await original_persist(turn_ctx, adapter, plan, result)

    monkeypatch.setattr(ctx.turn_orchestrator, "persist", cancel_then_persist)
    result = await ctx.run_turn("这一轮会被取消", topic_id=topic)

    assert result["ok"] is False and result["reason"] == "cancelled"
    assert _snapshot(ctx)["anchor"] == before["anchor"], "取消不得消费用户的历史位置"
    assert AnchorService(ctx.conn).get_active().fragment_id == frag
