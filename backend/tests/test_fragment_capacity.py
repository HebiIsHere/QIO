"""容量兜底：长度参与、按真实轮次统计、单轮不拆（阶段 4）。

规格要求：
* 长度是兜底条件，不等于任务完成；
* 用户轮数按**真实 Turn 绑定**统计，系统通知与工具消息不计轮；
* 单轮可以超过容量目标，保留完整后再封存（不拆轮、不截断原文）。
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from agent.api.bus import EventBus
from agent.config import Settings
from agent.credentials.store import MemoryKeyring
from agent.memory.fragment import FragmentManager, resolve_max_tokens
from agent.services.app import AppContext
from agent.storage.db import connect
from agent.storage.migrate import apply_migrations


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@pytest.fixture()
def ctx(tmp_path) -> AppContext:
    conn = connect(tmp_path / "cap.db")
    apply_migrations(conn)
    app_ctx = AppContext(Settings(data_dir=tmp_path), conn, EventBus())
    app_ctx.credentials._kr = MemoryKeyring()
    return app_ctx


def _topic(ctx: AppContext, name: str = "话题") -> str:
    return ctx.topics.nodes.create_topic(name).id


def test_content_length_alone_can_trigger_closing(ctx: AppContext):
    """轮数没到，内容长度到了也要分块（长度是兜底手段）。"""
    topic = _topic(ctx)
    fragments = FragmentManager(ctx.conn, max_turns=10, max_tokens=30)

    # 一轮完整对话，但内容很长
    ctx.memory.append_message(topic_id=topic, role="user", content="很长的问题" * 30)
    ctx.memory.append_message(topic_id=topic, role="assistant", content="很长的回答" * 30)
    fragment = fragments.get_or_create_open(topic)

    assert fragments.content_tokens(fragment.id) >= 30
    assert fragments.should_close(fragment) is True


def test_single_over_capacity_turn_is_kept_whole(ctx: AppContext):
    """单轮超容量：停在半轮时不得封存（不拆轮、不截断原文）。"""
    topic = _topic(ctx)
    fragments = FragmentManager(ctx.conn, max_turns=1, max_tokens=10)
    ctx.memory.append_message(topic_id=topic, role="user", content="这一轮特别长" * 20)
    fragment = fragments.get_or_create_open(topic)

    assert fragments.should_close(fragment) is False, "最后一条是 user：这一轮还没结束"

    ctx.memory.append_message(topic_id=topic, role="assistant", content="回答")
    assert fragments.should_close(fragments.get(fragment.id)) is True, "完整轮次之后才封块"


def test_system_turns_do_not_consume_capacity(ctx: AppContext):
    topic = _topic(ctx)
    fragments = FragmentManager(ctx.conn, max_turns=2, max_tokens=0)
    ctx.bindings.record_binding("turn_sys", topic, system=True)
    ctx.memory.append_message(
        topic_id=topic, role="user", content="系统驱动的一条", turn_id="turn_sys"
    )

    fragment = fragments.get_or_create_open(topic)
    assert fragments.turn_count(fragment.id) == 0, "系统通知不该占用用户轮次"


def test_same_turn_counts_once(ctx: AppContext):
    topic = _topic(ctx)
    fragments = FragmentManager(ctx.conn, max_turns=5, max_tokens=0)
    ctx.bindings.record_binding("turn_1", topic)
    ctx.memory.append_message(topic_id=topic, role="user", content="第一句", turn_id="turn_1")
    ctx.memory.append_message(topic_id=topic, role="user", content="补充一句", turn_id="turn_1")

    fragment = fragments.get_or_create_open(topic)
    assert fragments.turn_count(fragment.id) == 1, "同一轮的多条消息只算一轮"


def test_legacy_messages_without_turn_id_still_count(ctx: AppContext):
    """旧数据没有 turn_id：按每条算一轮，保持旧行为（不制造突然的变化）。"""
    topic = _topic(ctx)
    fragments = FragmentManager(ctx.conn, max_turns=5, max_tokens=0)
    frag_id = fragments.get_or_create_open(topic).id
    for i in range(3):
        ctx.conn.execute(
            "INSERT INTO messages (id, fragment_id, role, content, content_type, created_at) "
            "VALUES (?, ?, 'user', ?, 'text', ?)",
            (f"legacy_{i}", frag_id, f"旧消息 {i}", _now()),
        )
    ctx.conn.commit()

    assert fragments.turn_count(frag_id) == 3


def test_resolve_max_tokens_reads_setting_and_clamps():
    class _Store:
        def __init__(self, value):
            self.value = value

        def get(self, key):
            return self.value

    assert resolve_max_tokens(_Store(None)) == 4096  # 未配置 → 默认
    assert resolve_max_tokens(_Store("12000")) == 12000
    assert resolve_max_tokens(_Store("10")) == 2000  # 太小收敛到下界
    assert resolve_max_tokens(_Store("999999")) == 200_000  # 太大收敛到上界
    assert resolve_max_tokens(_Store("abc")) == 4096  # 坏值回落默认
