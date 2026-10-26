# -*- coding: utf-8 -*-
"""FU3-D：子任务通知轮必须回到**原任务话题**，不得被界面当前话题覆盖（先红后绿）。

基线缺陷（dae32828）：
`_pump_subtask_notifications` 已经按 origin 原话题 A 提交通知轮
（`turns.submit(..., topic, notify=True)`，ctx.initial_topic=A），但
`_execute_notify_turn` 无条件 `topic = self.current_topic()`（用户此刻正在看的
另一个话题 B），并把它写回 ctx.current_topic / ctx.bound_topic / record_binding /
trace。于是结果被记进 B：绑定、上下文、trace、后续记忆保存全部错位。

修复口径：优先 ctx.initial_topic（原任务话题），只有缺失时才回退 self.current_topic()；
trace_store.ensure_started / set_initial_topic、ctx.current_topic、ctx.bound_topic、
record_binding 全部使用同一个解析结果；通知轮其他语义不变。
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


class _FinalAdapter:
    """只要一条最终回复：把通知轮跑完（不触发工具、不需要真实模型）。"""

    mode = "native"
    model = "fake-notify-model"

    def __init__(self) -> None:
        self.seen: list[str] = []

    async def complete(self, messages, tools, **kwargs):
        self.seen.append("\n".join(message.content or "" for message in messages))
        return Completion(message=ChatMessage(role="assistant", content="通知轮最终回复"))


def _topic(ctx: AppContext, name: str) -> str:
    ctx.topics.nodes.get_or_create_user_root()
    return ctx.topics.nodes.create_topic(name).id


def _activate(ctx: AppContext, topic_id: str) -> None:
    from agent.graph.anchors import AnchorService

    AnchorService(ctx.conn).set_active(topic_id)


def _assistant_topics(ctx: AppContext, turn_id: str) -> list[str]:
    """本轮写下的 assistant 消息落在哪些话题（跨 fragments 反查 topic_id）。"""
    rows = ctx.conn.execute(
        "SELECT DISTINCT f.topic_id AS topic_id FROM messages m"
        " JOIN fragments f ON f.id = m.fragment_id"
        " WHERE m.role = 'assistant' AND m.turn_id = ?",
        (turn_id,),
    ).fetchall()
    return [str(row["topic_id"]) for row in rows]


async def test_notify_turn_binds_original_topic_not_current(ctx: AppContext, monkeypatch):
    """initial_topic=A（原任务话题）+ 界面当前话题=B → 绑定 / 上下文 / trace / 保存都用 A。"""
    from agent.core.turn import TurnContext

    topic_a = _topic(ctx, "原任务话题A")
    topic_b = _topic(ctx, "界面当前话题B")
    _activate(ctx, topic_b)
    assert ctx.current_topic() == topic_b, "前提：界面当前话题是 B"

    adapter = _FinalAdapter()
    monkeypatch.setattr(ctx, "build_adapter", AsyncMock(return_value=adapter))

    turn = TurnContext(
        turn_id="turn_notify_topic_a",
        message="【子任务执行结果】task_id: task_x",
        initial_topic=topic_a,
        current_topic=topic_a,
        notify=True,
    )
    await ctx._execute_notify_turn(turn)

    assert turn.current_topic == topic_a, (
        f"通知轮执行上下文必须留在原任务话题 A（实际 {turn.current_topic}）"
    )
    assert turn.bound_topic == topic_a, (
        f"通知轮绑定必须指向原任务话题 A（实际 {getattr(turn, 'bound_topic', None)}）"
    )
    binding = ctx.bindings.binding_for(turn.turn_id)
    assert binding is not None, "通知轮必须写入自己的绑定"
    assert binding.topic_id == topic_a, (
        f"record_binding 必须用原任务话题 A（实际 {binding.topic_id}）"
    )
    trace = ctx.trace_store.get(turn.turn_id)
    assert trace is not None
    assert trace["initial_topic"] == topic_a, (
        f"trace 初始话题必须是 A（实际 {trace['initial_topic']}）"
    )
    assert trace["final_topic"] == topic_a, (
        f"trace 结束话题必须是 A（实际 {trace['final_topic']}）"
    )
    saved = _assistant_topics(ctx, turn.turn_id)
    assert saved == [topic_a], f"结果保存必须落在 A（实际 {saved}）"


async def test_notify_turn_falls_back_to_current_topic_when_missing(
    ctx: AppContext, monkeypatch
):
    """没有 initial_topic（旧调用点/缺失）时才回退界面当前话题 B。"""
    from agent.core.turn import TurnContext

    topic_b = _topic(ctx, "界面当前话题B")
    _activate(ctx, topic_b)

    adapter = _FinalAdapter()
    monkeypatch.setattr(ctx, "build_adapter", AsyncMock(return_value=adapter))

    turn = TurnContext(
        turn_id="turn_notify_topic_fallback",
        message="【子任务执行结果】task_id: task_y",
        initial_topic=None,
        current_topic=None,
        notify=True,
    )
    await ctx._execute_notify_turn(turn)

    assert turn.current_topic == topic_b
    assert turn.bound_topic == topic_b
    binding = ctx.bindings.binding_for(turn.turn_id)
    assert binding is not None and binding.topic_id == topic_b
    trace = ctx.trace_store.get(turn.turn_id)
    assert trace is not None and trace["initial_topic"] == topic_b
