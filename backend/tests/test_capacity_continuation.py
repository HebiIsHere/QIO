"""容量分段的延续关系（阶段 4）。

规格要求「容量延续保留同阶段」并且「来源必须来自实际绑定路径」。
之前的行为是：封存之后新片段由下一条消息 lazy 创建，**没有来源、没有同阶段**，
路径在容量边界断掉。现在容量封存会留下一条「下一段接这一段」的待用信息，
由下一条消息创建片段时消费掉。
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from agent.api.bus import EventBus
from agent.config import Settings
from agent.credentials.store import MemoryKeyring
from agent.memory.fragment import FragmentManager
from agent.services.app import AppContext
from agent.storage.db import connect
from agent.storage.migrate import apply_migrations


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@pytest.fixture()
def ctx(tmp_path) -> AppContext:
    conn = connect(tmp_path / "cap2.db")
    apply_migrations(conn)
    app_ctx = AppContext(Settings(data_dir=tmp_path), conn, EventBus())
    app_ctx.credentials._kr = MemoryKeyring()
    return app_ctx


def _topic(ctx: AppContext, name: str = "话题") -> str:
    return ctx.topics.nodes.create_topic(name).id


def test_capacity_seal_records_continuation_for_the_next_fragment(ctx: AppContext):
    topic = _topic(ctx)
    ctx.memory.append_message(topic_id=topic, role="user", content="第一轮问题")
    ctx.memory.append_message(topic_id=topic, role="assistant", content="第一轮回答")
    sealed = ctx.fragments.get_or_create_open(topic)

    sealed_now = ctx.memory_lifecycle.seal_fragment(topic, reason="capacity", continue_same_stage=True)
    assert sealed_now is not None and sealed_now.closed_at is not None

    # 还没有新消息：不创建空片段，只留待用信息
    assert ctx.fragments.open_fragment(topic) is None
    pending = ctx.fragments.peek_continuation(topic)
    assert pending == {"source_fragment_id": sealed.id, "same_stage": 1, "reason": "capacity"}

    # 下一条消息来了才建新片段，并且带上来源 / 关系 / 同阶段标记
    ctx.memory.append_message(topic_id=topic, role="user", content="第二轮问题")
    child = ctx.fragments.open_fragment(topic)
    assert child is not None
    assert child.source_fragment_id == sealed.id
    assert child.relation_type == "normal"
    assert child.boundary_reason == "capacity"
    assert child.same_stage == 1, "容量延续必须标成同阶段"
    # 待用信息只能用一次
    assert ctx.fragments.peek_continuation(topic) is None


def test_continuation_is_consumed_once(ctx: AppContext):
    topic = _topic(ctx)
    ctx.memory.append_message(topic_id=topic, role="user", content="问题")
    ctx.memory.append_message(topic_id=topic, role="assistant", content="回答")
    first = ctx.fragments.get_or_create_open(topic)
    ctx.memory_lifecycle.seal_fragment(topic, reason="capacity", continue_same_stage=True)

    ctx.memory.append_message(topic_id=topic, role="user", content="第二条")
    child_id = ctx.fragments.open_fragment(topic).id
    # 再封存一次（这次不延续），随后创建的片段不该继承更早的来源
    ctx.memory_lifecycle.seal_fragment(topic, reason="stage_change", continue_same_stage=False)
    ctx.memory.append_message(topic_id=topic, role="user", content="第三条")
    later = ctx.fragments.open_fragment(topic)

    assert later.source_fragment_id is None, "没有延续信息时不应凭空继承来源"
    assert child_id != first.id


def test_explicit_child_creation_clears_pending_continuation(ctx: AppContext):
    """阶段变化显式建了子片段时，容量留下的待用信息必须作废（否则会串）。"""
    topic = _topic(ctx)
    ctx.memory.append_message(topic_id=topic, role="user", content="问题")
    ctx.memory.append_message(topic_id=topic, role="assistant", content="回答")
    first = ctx.fragments.get_or_create_open(topic)
    ctx.fragments.mark_continuation(topic, first.id, same_stage=True, reason="capacity")

    # 显式建子片段之前必须先封存（一个话题同时只能有一个开放片段）
    ctx.memory_lifecycle.seal_fragment(topic, reason="stage_change", continue_same_stage=False)
    child = ctx.fragments.create_child(
        topic, source_fragment_id=first.id, relation_type="normal",
        boundary_reason="stage_change", same_stage=False,
    )

    assert ctx.fragments.peek_continuation(topic) is None
    assert ctx.fragments.open_fragment(topic).id == child


def test_first_fragment_has_no_source(ctx: AppContext):
    """话题的第一段没有来源：不能靠「时间上最近的那一段」猜。"""
    topic = _topic(ctx)
    ctx.memory.append_message(topic_id=topic, role="user", content="开头")
    first = ctx.fragments.open_fragment(topic)

    assert first.source_fragment_id is None
    assert first.relation_type is None
