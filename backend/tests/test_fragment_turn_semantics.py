"""Fragment 封块语义：一「轮」= 用户 + 助手，而不是一条消息。

背景（第三阶段 spec 第 57~60 条）：设置页一直写「标准（10 轮）」，但后端实际按
消息条数封块（`fragment.max_messages`）。一轮对话 = user + assistant 两条消息，
所以「10 轮」实际只有 5 轮。这里固定修复后的语义：

* `max_turns` 数的是完整对话轮；
* 只有 user 消息开启一轮，工具消息不计入；
* 一轮还没结束（最后落下的还是 user 消息）时不封块，否则第 N 轮的助手回答
  会被写进下一个片段，把一轮对话拆到两个片段里。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest

from agent.memory.fragment import FragmentManager
from agent.memory.ingest import MemoryWriter


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@pytest.fixture()
def topic(db_conn: sqlite3.Connection) -> str:
    db_conn.execute(
        "INSERT INTO nodes (id, type, name, created_at, updated_at) "
        "VALUES ('t1', 'topic', '饮食', ?, ?)",
        (_now(), _now()),
    )
    return "t1"


def _append_turn(writer: MemoryWriter, topic: str, index: int):
    """写一轮完整对话，返回最后一次 append 的 close 信号。"""
    writer.append_message(topic_id=topic, role="user", content=f"问题 {index}")
    _, closed = writer.append_message(
        topic_id=topic, role="assistant", content=f"回答 {index}"
    )
    return closed


def test_turn_count_counts_user_messages_only(db_conn: sqlite3.Connection, topic: str):
    fm = FragmentManager(db_conn, max_turns=10)
    writer = MemoryWriter(db_conn, fm)
    _append_turn(writer, topic, 1)
    _append_turn(writer, topic, 2)
    for i in range(5):
        writer.append_message(topic_id=topic, role="tool", content=f"工具结果 {i}")
    frag = fm.get_or_create_open(topic)
    assert fm.message_count(frag.id) == 9
    assert fm.turn_count(frag.id) == 2


def test_fragment_closes_after_max_turns_not_after_max_messages(
    db_conn: sqlite3.Connection, topic: str
):
    """10 轮 = 20 条消息（含助手回答）才封块，不再是 10 条消息。"""
    fm = FragmentManager(db_conn, max_turns=10)
    writer = MemoryWriter(db_conn, fm)
    for i in range(9):
        closed = _append_turn(writer, topic, i)
        assert closed is None, f"第 {i + 1} 轮就封块了，语义仍然按消息数"
    closed = _append_turn(writer, topic, 10)
    assert closed is not None
    assert fm.turn_count(closed.id) == 10
    assert fm.message_count(closed.id) == 20


def test_fragment_does_not_close_mid_turn(db_conn: sqlite3.Connection, topic: str):
    """一轮还没结束就封块，会把这条 user 消息和它的回答拆到两个片段里。"""
    fm = FragmentManager(db_conn, max_turns=2)
    writer = MemoryWriter(db_conn, fm)
    _append_turn(writer, topic, 1)
    _, closed = writer.append_message(topic_id=topic, role="user", content="第二轮问题")
    assert closed is None, "最后一条是 user 消息时不得封块"
    _, closed = writer.append_message(topic_id=topic, role="assistant", content="第二轮回答")
    assert closed is not None


def test_should_close_uses_configured_turn_budget(
    db_conn: sqlite3.Connection, topic: str
):
    fm = FragmentManager(db_conn, max_turns=2)
    writer = MemoryWriter(db_conn, fm)
    assert _append_turn(writer, topic, 1) is None
    closed = _append_turn(writer, topic, 2)
    assert closed is not None
    writer.close_open_fragment(topic, lambda f, m: "摘要")
    writer.append_message(topic_id=topic, role="user", content="下一轮")
    assert fm.get_or_create_open(topic).id != closed.id
