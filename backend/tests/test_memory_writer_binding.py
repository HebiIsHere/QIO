"""写入按显式绑定落库（阶段 1）。

关键行为：一轮对话绑定到某个 Fragment 之后，写入必须落在那一个。
不能再出现「绑定的是 A，实际写进了此刻恰好开放着的 B」——
那正是「回复在跑、用户改了导航」时消息被搬走的根因。
"""

from __future__ import annotations

import pytest

from agent.memory.fragment import FragmentManager
from agent.memory.ingest import BindingMismatch, MemoryWriter
from agent.services.binding import TurnBindingService


def _seed_topic(conn, topic_id: str, name: str = "话题") -> None:
    ts = "2026-01-01T00:00:00+00:00"
    conn.execute(
        "INSERT INTO nodes (id, type, name, meta, created_at, updated_at) "
        "VALUES (?, 'topic', ?, '{}', ?, ?)",
        (topic_id, name, ts, ts),
    )
    conn.commit()


def _seed_fragment(conn, fragment_id: str, topic_id: str, *, closed: bool = False) -> None:
    ts = "2026-01-01T00:00:00+00:00"
    conn.execute(
        "INSERT INTO fragments (id, topic_id, summary_version, created_at, closed_at, meta) "
        "VALUES (?, ?, 0, ?, ?, '{}')",
        (fragment_id, topic_id, ts, ts if closed else None),
    )
    conn.commit()


def _writer(conn) -> tuple[MemoryWriter, FragmentManager, TurnBindingService]:
    fragments = FragmentManager(conn)
    bindings = TurnBindingService(conn)
    return MemoryWriter(conn, fragments, bindings=bindings), fragments, bindings


def _messages_of(conn, fragment_id: str) -> list[str]:
    return [
        row["content"]
        for row in conn.execute(
            "SELECT content FROM messages WHERE fragment_id = ? ORDER BY created_at", (fragment_id,)
        )
    ]


def test_append_uses_bound_fragment(db_conn):
    _seed_topic(db_conn, "topic_a")
    _seed_fragment(db_conn, "frag_a", "topic_a")
    writer, _, bindings = _writer(db_conn)
    bindings.record_binding("turn_1", "topic_a", fragment_id="frag_a")

    writer.append_message(topic_id="topic_a", role="user", content="第一句", turn_id="turn_1")

    assert _messages_of(db_conn, "frag_a") == ["第一句"]


def test_append_rejects_when_bound_fragment_was_sealed(db_conn):
    """绑定之后片段被封存：必须报错，不能把内容写进新开的那一个。"""
    _seed_topic(db_conn, "topic_a")
    _seed_fragment(db_conn, "frag_a", "topic_a")
    writer, fragments, bindings = _writer(db_conn)
    bindings.record_binding("turn_1", "topic_a", fragment_id="frag_a")

    fragments.close("frag_a", "旧摘要")
    _seed_fragment(db_conn, "frag_b", "topic_a")  # 同话题的下一个开放片段

    with pytest.raises(BindingMismatch):
        writer.append_message(topic_id="topic_a", role="assistant", content="不该落库", turn_id="turn_1")

    assert _messages_of(db_conn, "frag_a") == []
    assert _messages_of(db_conn, "frag_b") == []


def test_append_rejects_fragment_from_other_topic(db_conn):
    _seed_topic(db_conn, "topic_a")
    _seed_topic(db_conn, "topic_b", "另一个话题")
    _seed_fragment(db_conn, "frag_a", "topic_a")
    writer, _, _ = _writer(db_conn)

    with pytest.raises(BindingMismatch):
        writer.append_message(
            topic_id="topic_b", role="user", content="越界", fragment_id="frag_a"
        )
    assert _messages_of(db_conn, "frag_a") == []


def test_append_rejects_explicit_fragment_conflicting_with_binding(db_conn):
    _seed_topic(db_conn, "topic_a")
    _seed_fragment(db_conn, "frag_a", "topic_a")
    _seed_fragment(db_conn, "frag_b", "topic_a", closed=True)
    writer, _, bindings = _writer(db_conn)
    bindings.record_binding("turn_1", "topic_a", fragment_id="frag_a")

    with pytest.raises(BindingMismatch):
        writer.append_message(
            topic_id="topic_a", role="user", content="冲突", fragment_id="frag_b", turn_id="turn_1"
        )


def test_append_without_binding_keeps_open_fragment_behaviour(db_conn):
    """没有轮次绑定的写入（系统通知等）沿用「写进该话题的开放片段」。"""
    _seed_topic(db_conn, "topic_a")
    writer, _, _ = _writer(db_conn)

    writer.append_message(topic_id="topic_a", role="system", content="系统通知")

    rows = db_conn.execute(
        "SELECT m.content, f.topic_id FROM messages m JOIN fragments f ON f.id = m.fragment_id"
    ).fetchall()
    assert [(r["topic_id"], r["content"]) for r in rows] == [("topic_a", "系统通知")]


def test_legacy_constructor_without_bindings_still_works(db_conn):
    """不传 bindings 的旧构造方式仍可用（兼容既有测试与脚本）。"""
    _seed_topic(db_conn, "topic_a")
    fragments = FragmentManager(db_conn)
    writer = MemoryWriter(db_conn, fragments)

    writer.append_message(topic_id="topic_a", role="user", content="兼容")

    row = db_conn.execute("SELECT COUNT(*) c FROM messages").fetchone()
    assert row["c"] == 1
