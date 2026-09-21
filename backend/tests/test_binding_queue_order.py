"""队列顺序是强制产品要求（阶段 1）。

规格原文：C 中提交甲且甲排队 → 选择从 A 继续 → 提交乙。甲仍归原先接续位置，乙从 A 接续。
做法：接续意图在**提交时**捕获，在**开始执行时**落实成绑定，所以后来的选择不会追溯改向。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest
from unittest.mock import AsyncMock

from agent.adapters.base import ChatMessage, Completion
from agent.api.bus import EventBus
from agent.config import Settings
from agent.credentials.store import MemoryKeyring
from agent.services.app import AppContext
from agent.storage.db import connect
from agent.storage.migrate import apply_migrations


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@pytest.fixture()
def ctx(tmp_path) -> AppContext:
    conn = connect(tmp_path / "app.db")
    apply_migrations(conn)
    app_ctx = AppContext(Settings(data_dir=tmp_path), conn, EventBus())
    app_ctx.credentials._kr = MemoryKeyring()
    return app_ctx


class GatedAdapter:
    """第一次模型调用会被闸门挡住，用来制造「甲还在跑」的时间窗。"""

    mode = "native"
    model = "fake-gated"

    def __init__(self, gate: asyncio.Event) -> None:
        self.gate = gate

    async def complete(self, messages, tools, **kwargs):
        await self.gate.wait()
        return Completion(message=ChatMessage(role="assistant", content="收到"))


def _topic(ctx: AppContext, name: str) -> str:
    return ctx.topics.nodes.create_topic(name).id


def _closed_fragment(ctx: AppContext, topic_id: str, frag_id: str, source: str | None = None) -> None:
    ts = _now()
    ctx.conn.execute(
        "INSERT INTO fragments (id, topic_id, summary_version, created_at, closed_at, meta, source_fragment_id) "
        "VALUES (?, ?, 0, ?, ?, '{}', ?)",
        (frag_id, topic_id, ts, ts, source),
    )
    ctx.conn.commit()


def _message_fragment(ctx: AppContext, content: str) -> str | None:
    row = ctx.conn.execute(
        "SELECT fragment_id FROM messages WHERE content = ? ORDER BY created_at DESC LIMIT 1",
        (content,),
    ).fetchone()
    return row["fragment_id"] if row is not None else None


async def test_queued_turn_keeps_its_position_and_later_turn_uses_the_new_selection(ctx: AppContext):
    topic = _topic(ctx, "话题C")
    _closed_fragment(ctx, topic, "frag_a")  # 已封存的历史 A
    open_frag = ctx.fragments.get_or_create_open(topic)  # 当前开放 C1
    ctx.navigation.enter_topic(topic, fragment_id=open_frag.id)

    gate = asyncio.Event()
    ctx.build_adapter = AsyncMock(return_value=GatedAdapter(gate))

    # 甲：此刻没有任何接续选择
    jia = ctx.turns.submit("甲", topic)
    for _ in range(100):  # 等它真的开始执行（占住 active）
        if ctx.turns.active is not None:
            break
        await asyncio.sleep(0.01)
    assert ctx.turns.active is not None, "甲应当已经开始执行"

    # 用户在甲还在跑的时候选择「从 A 继续」
    intent = ctx.navigation.register_continuation(topic, "frag_a")
    assert intent["intent_id"]

    # 乙：提交时捕获到这条接续选择
    yi = ctx.turns.submit("乙", topic, intent_id=intent["intent_id"])

    gate.set()
    await ctx.turns.wait(jia.turn_id, timeout=10)
    await ctx.turns.wait(yi.turn_id, timeout=10)

    jia_binding = ctx.bindings.binding_for(jia.turn_id)
    yi_binding = ctx.bindings.binding_for(yi.turn_id)

    # 甲仍归它提交时的位置（原来的开放片段），不受后来选择的影响
    assert jia_binding.fragment_id == open_frag.id
    assert _message_fragment(ctx, "甲") == open_frag.id

    # 乙沿 A 的接续路径走：新建的 D 以 A 为来源
    assert yi_binding.fragment_id != open_frag.id
    assert _message_fragment(ctx, "乙") == yi_binding.fragment_id
    d = ctx.conn.execute(
        "SELECT source_fragment_id, relation_type FROM fragments WHERE id = ?",
        (yi_binding.fragment_id,),
    ).fetchone()
    assert d["source_fragment_id"] == "frag_a"
    assert d["relation_type"] == "history_reopen"

    # 无论执行顺序如何，仍然保持「每个话题最多一个开放片段」
    open_rows = ctx.conn.execute(
        "SELECT id FROM fragments WHERE topic_id = ? AND closed_at IS NULL", (topic,)
    ).fetchall()
    assert [r["id"] for r in open_rows] == [yi_binding.fragment_id]
    # 甲用过的那个片段被原子交接封存，且记下了原因
    sealed = ctx.conn.execute(
        "SELECT boundary_reason FROM fragments WHERE id = ?", (open_frag.id,)
    ).fetchone()
    assert sealed["boundary_reason"] == "history_continuation"
