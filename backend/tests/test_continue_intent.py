"""历史接续：点击只登记、执行时才落实（阶段 1 的核心修复）。

基线行为（a664900）：`continue_from_history` 对已关闭来源直接 INSERT 新片段，
目标话题已有开放片段时撞上 `idx_fragments_one_open_per_topic`。
现在的行为：登记意图 → 真正有消息要执行时，在同一个事务里封存已有开放片段再建接续片段。
"""

from __future__ import annotations

import pytest

from agent.services.binding import TurnBindingService
from agent.services.navigation import (
    FragmentNotInTopic,
    TopicNavigationService,
    TopicNotFound,
)


def _topic(conn, topic_id: str = "topic_a", name: str = "话题") -> None:
    ts = "2026-01-01T00:00:00+00:00"
    conn.execute(
        "INSERT INTO nodes (id, type, name, meta, created_at, updated_at) "
        "VALUES (?, 'topic', ?, '{}', ?, ?)",
        (topic_id, name, ts, ts),
    )
    conn.commit()


def _fragment(conn, fragment_id: str, topic_id: str, *, closed: bool = False, source=None, created=None) -> None:
    ts = created or "2026-01-01T00:00:00+00:00"
    conn.execute(
        "INSERT INTO fragments (id, topic_id, summary_version, created_at, closed_at, meta, source_fragment_id) "
        "VALUES (?, ?, 0, ?, ?, '{}', ?)",
        (fragment_id, topic_id, ts, ts if closed else None, source),
    )
    conn.commit()


def _open_fragments(conn, topic_id: str) -> list[str]:
    return [
        row["id"]
        for row in conn.execute(
            "SELECT id FROM fragments WHERE topic_id = ? AND closed_at IS NULL", (topic_id,)
        )
    ]


def test_sealed_source_with_existing_open_fragment_does_not_raise(db_conn):
    """规格验收第一行：已关闭 A + 同话题开放 C，从 A 继续 → 交接到 D，无唯一约束错误。"""
    _topic(db_conn)
    _fragment(db_conn, "frag_a", "topic_a", closed=True)
    _fragment(db_conn, "frag_c", "topic_a", created="2026-01-02T00:00:00+00:00")
    nav = TopicNavigationService(db_conn)

    registered = nav.register_continuation("topic_a", "frag_a")  # 点击历史：只登记
    assert registered["historic"] is True
    assert _open_fragments(db_conn, "topic_a") == ["frag_c"]  # 点击不产生新片段

    result = nav.apply_continuation("topic_a", registered["intent_id"])  # 有消息执行时才落实

    assert result.created_fragment_id is not None
    assert result.fragment_id == result.created_fragment_id
    assert result.sealed_fragment_id == "frag_c"
    assert _open_fragments(db_conn, "topic_a") == [result.created_fragment_id]
    row = db_conn.execute(
        "SELECT source_fragment_id, relation_type, boundary_reason FROM fragments WHERE id = ?",
        (result.created_fragment_id,),
    ).fetchone()
    assert row["source_fragment_id"] == "frag_a"
    assert row["relation_type"] == "history_reopen"
    # 被封存的 C 记录分段原因，便于之后解释「为什么这里断开」
    sealed = db_conn.execute(
        "SELECT boundary_reason FROM fragments WHERE id = 'frag_c'"
    ).fetchone()
    assert sealed["boundary_reason"] == "history_continuation"


def test_register_only_creates_no_fragment(db_conn):
    _topic(db_conn)
    _fragment(db_conn, "frag_a", "topic_a", closed=True)
    nav = TopicNavigationService(db_conn)

    nav.register_continuation("topic_a", "frag_a")
    nav.register_continuation("topic_a", "frag_a")  # 重复点击同一段历史
    nav.register_continuation("topic_a", "frag_a", request_id="req_1")
    nav.register_continuation("topic_a", "frag_a", request_id="req_1")  # 传输重试

    assert db_conn.execute("SELECT COUNT(*) c FROM fragments").fetchone()["c"] == 1
    assert db_conn.execute(
        "SELECT COUNT(*) c FROM continuation_intents WHERE state = 'registered'"
    ).fetchone()["c"] == 1


def test_clicking_open_fragment_is_plain_continue(db_conn):
    _topic(db_conn)
    _fragment(db_conn, "frag_open", "topic_a")
    nav = TopicNavigationService(db_conn)

    registered = nav.register_continuation("topic_a", "frag_open")

    assert registered["opens_current"] is True
    assert registered["intent_id"] is None
    assert db_conn.execute("SELECT COUNT(*) c FROM continuation_intents").fetchone()["c"] == 0
    assert db_conn.execute("SELECT COUNT(*) c FROM fragments").fetchone()["c"] == 1


def test_apply_is_idempotent(db_conn):
    _topic(db_conn)
    _fragment(db_conn, "frag_a", "topic_a", closed=True)
    nav = TopicNavigationService(db_conn)
    intent = nav.register_continuation("topic_a", "frag_a")

    first = nav.apply_continuation("topic_a", intent["intent_id"])
    second = nav.apply_continuation("topic_a", intent["intent_id"])

    assert first.fragment_id == second.fragment_id
    assert db_conn.execute("SELECT COUNT(*) c FROM fragments").fetchone()["c"] == 2
    assert _open_fragments(db_conn, "topic_a") == [first.fragment_id]


def test_topic_without_open_fragment_creates_continuation(db_conn):
    _topic(db_conn)
    _fragment(db_conn, "frag_a", "topic_a", closed=True)
    nav = TopicNavigationService(db_conn)
    intent = nav.register_continuation("topic_a", "frag_a")

    result = nav.apply_continuation("topic_a", intent["intent_id"])

    assert result.sealed_fragment_id is None
    assert _open_fragments(db_conn, "topic_a") == [result.created_fragment_id]


def test_cancel_leaves_no_trace(db_conn):
    _topic(db_conn)
    _fragment(db_conn, "frag_a", "topic_a", closed=True)
    nav = TopicNavigationService(db_conn)
    bindings = TurnBindingService(db_conn)
    intent = nav.register_continuation("topic_a", "frag_a")

    bindings.cancel_intent(intent["intent_id"])

    assert bindings.peek_intent() is None
    assert db_conn.execute("SELECT COUNT(*) c FROM fragments").fetchone()["c"] == 1


def test_register_rejects_foreign_fragment_and_unknown_topic(db_conn):
    _topic(db_conn, "topic_a")
    _topic(db_conn, "topic_b", "另一个话题")
    _fragment(db_conn, "frag_a", "topic_a", closed=True)
    nav = TopicNavigationService(db_conn)

    with pytest.raises(FragmentNotInTopic):
        nav.register_continuation("topic_b", "frag_a")
    with pytest.raises(TopicNotFound):
        nav.register_continuation("topic_ghost", "frag_a")


def test_continue_from_history_shim_still_works(db_conn):
    """旧的组合入口保留：登记 + 立即落实，行为与旧版一致（既有调用方不受影响）。"""
    _topic(db_conn)
    _fragment(db_conn, "frag_a", "topic_a", closed=True)
    nav = TopicNavigationService(db_conn)

    result = nav.continue_from_history("topic_a", "frag_a")

    assert result.created_fragment_id is not None
    assert result.source_fragment_id == "frag_a"
