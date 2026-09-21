"""边界策略的三个模式：off / shadow / enabled（阶段 4）。

规格要求「实现 off/shadow/enabled 或同等可回退模式，并明确哪些边界实际生效」。
默认是 shadow：确定性规则先只记录建议，不实际切分 —— 这样上线前能看到
「如果生效会切在哪里」，而不会先把对话切开。
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from agent.adapters.base import ChatMessage, Completion
from agent.api.bus import EventBus
from agent.config import Settings
from agent.credentials.store import MemoryKeyring
from agent.services.app import AppContext
from agent.services.turn_orchestrator import BOUNDARY_MODE_KEY
from agent.storage.db import connect
from agent.storage.migrate import apply_migrations


class _Adapter:
    mode = "native"
    model = "fake"

    async def complete(self, messages, tools, **kwargs):
        return Completion(message=ChatMessage(role="assistant", content="收到"))


@pytest.fixture()
def ctx(tmp_path) -> AppContext:
    conn = connect(tmp_path / "boundary.db")
    apply_migrations(conn)
    app_ctx = AppContext(Settings(data_dir=tmp_path), conn, EventBus())
    app_ctx.credentials._kr = MemoryKeyring()
    app_ctx.build_adapter = AsyncMock(return_value=_Adapter())
    return app_ctx


def _seed_turn(ctx: AppContext, topic_id: str, text: str) -> None:
    """先放一段已有对话，让当前片段「有内容、不在半轮」。"""
    ctx.memory.append_message(topic_id=topic_id, role="user", content="先做方案设计")
    ctx.memory.append_message(topic_id=topic_id, role="assistant", content="方案如下……")


def _fragments(ctx: AppContext, topic_id: str) -> list:
    return ctx.conn.execute(
        "SELECT * FROM fragments WHERE topic_id = ? ORDER BY created_at, id", (topic_id,)
    ).fetchall()


def _boundary_traces(ctx: AppContext, turn_id: str) -> list[str]:
    detail = ctx.trace_store.get(turn_id) or {}
    writes = detail.get("writes") or {}
    return list(writes.get("boundary") or []) + list(writes.get("boundary_split") or [])


async def _run(ctx: AppContext, text: str, topic_id: str) -> str:
    """提交并等一轮结束，返回 turn_id（run_turn 的返回值里没有 turn_id，
    而这条测试要按轮次查 trace 与绑定）。"""
    pending = ctx.bindings.peek_intent()
    tctx = ctx.turns.submit(text, topic_id, intent_id=pending.intent_id if pending else None)
    result = await ctx.turns.wait(tctx.turn_id, timeout=10)
    assert result and result.get("ok"), result
    return tctx.turn_id


async def test_off_mode_does_not_evaluate(ctx: AppContext):
    ctx.settings_store.set(BOUNDARY_MODE_KEY, "off")
    topic = ctx.topics.nodes.create_topic("话题").id
    _seed_turn(ctx, topic, "")

    turn_id = await _run(ctx, "设计确定了，开始实现", topic)

    assert len(_fragments(ctx, topic)) == 1
    assert _boundary_traces(ctx, turn_id) == []


async def test_shadow_mode_records_a_suggestion_without_splitting(ctx: AppContext):
    ctx.settings_store.set(BOUNDARY_MODE_KEY, "shadow")
    topic = ctx.topics.nodes.create_topic("话题").id
    _seed_turn(ctx, topic, "")

    turn_id = await _run(ctx, "设计确定了，开始实现", topic)

    assert len(_fragments(ctx, topic)) == 1, "shadow 模式不得实际切分"
    traces = _boundary_traces(ctx, turn_id)
    assert any("split:phase_change" in t for t in traces), traces


async def test_enabled_mode_splits_and_the_turn_binds_to_the_new_fragment(ctx: AppContext):
    ctx.settings_store.set(BOUNDARY_MODE_KEY, "enabled")
    topic = ctx.topics.nodes.create_topic("话题").id
    _seed_turn(ctx, topic, "")
    before = ctx.fragments.open_fragment(topic)

    turn_id = await _run(ctx, "设计确定了，开始实现", topic)

    fragments = _fragments(ctx, topic)
    assert len(fragments) == 2, "enabled 模式下高置信规则应当实际切分"
    sealed = ctx.fragments.get(before.id)
    child = ctx.fragments.open_fragment(topic)
    assert sealed.closed_at is not None, "旧片段被原子交接封存"
    assert sealed.boundary_reason == "stage_change"
    assert child.id != before.id
    row = ctx.conn.execute(
        "SELECT source_fragment_id, boundary_reason, same_stage FROM fragments WHERE id = ?",
        (child.id,),
    ).fetchone()
    assert row["source_fragment_id"] == before.id, "新片段要记下直接来源"
    assert row["same_stage"] == 0, "阶段变化不是容量延续"
    # 本轮绑定到新片段：消息写进新的一段
    binding = ctx.bindings.binding_for(turn_id)
    assert binding.fragment_id == child.id
    written = ctx.conn.execute(
        "SELECT fragment_id FROM messages WHERE turn_id = ? AND role = 'user'", (turn_id,)
    ).fetchall()
    assert [w["fragment_id"] for w in written] == [child.id]


async def test_negated_instruction_does_not_split_even_when_enabled(ctx: AppContext):
    ctx.settings_store.set(BOUNDARY_MODE_KEY, "enabled")
    topic = ctx.topics.nodes.create_topic("话题").id
    _seed_turn(ctx, topic, "")

    turn_id = await _run(ctx, "不要开始写代码，继续分析", topic)

    assert len(_fragments(ctx, topic)) == 1
    traces = _boundary_traces(ctx, turn_id)
    assert any("negated_instruction" in t for t in traces), traces
