"""轮次绑定与接续意图的持久服务（阶段 1 的核心状态）。

为什么要有它：在这之前，「这一轮属于哪个 Fragment」是在**写入时**临时看 Anchor 决定的，
于是后面的导航变化能把已经提交的消息搬走，历史接续也会撞上「一个话题一个开放片段」的约束。
绑定服务把这件事变成一条持久记录：轮前定好、写入照做、收尾更新。
"""

from __future__ import annotations

import pytest

from agent.services.binding import (
    BindingConflict,
    IntentNotFound,
    TurnBindingService,
)


def _seed_topic(conn, topic_id: str = "topic_a") -> None:
    ts = "2026-01-01T00:00:00+00:00"
    conn.execute(
        "INSERT INTO nodes (id, type, name, meta, created_at, updated_at) "
        "VALUES (?, 'topic', '话题', '{}', ?, ?)",
        (topic_id, ts, ts),
    )
    conn.commit()


def _seed_fragment(conn, fragment_id: str, topic_id: str = "topic_a", *, closed: bool = False) -> None:
    ts = "2026-01-01T00:00:00+00:00"
    conn.execute(
        "INSERT INTO fragments (id, topic_id, summary_version, created_at, closed_at, meta) "
        "VALUES (?, ?, 0, ?, ?, '{}')",
        (fragment_id, topic_id, ts, ts if closed else None),
    )
    conn.commit()


def test_record_and_read_binding(db_conn):
    _seed_topic(db_conn)
    _seed_fragment(db_conn, "frag_a")
    svc = TurnBindingService(db_conn)

    svc.record_binding("turn_1", "topic_a", fragment_id="frag_a")
    binding = svc.binding_for("turn_1")

    assert binding is not None
    assert (binding.turn_id, binding.topic_id, binding.fragment_id) == ("turn_1", "topic_a", "frag_a")
    assert binding.write_state == "open"
    assert binding.status is None


def test_record_binding_is_idempotent_for_same_values(db_conn):
    _seed_topic(db_conn)
    _seed_fragment(db_conn, "frag_a")
    svc = TurnBindingService(db_conn)
    svc.record_binding("turn_1", "topic_a", fragment_id="frag_a")
    svc.record_binding("turn_1", "topic_a", fragment_id="frag_a")  # 重试同一结果：不报错

    assert db_conn.execute("SELECT COUNT(*) c FROM turn_bindings").fetchone()["c"] == 1


def test_record_binding_conflict_when_values_differ(db_conn):
    _seed_topic(db_conn)
    _seed_fragment(db_conn, "frag_a")
    _seed_fragment(db_conn, "frag_b", closed=True)
    svc = TurnBindingService(db_conn)
    svc.record_binding("turn_1", "topic_a", fragment_id="frag_a")

    with pytest.raises(BindingConflict):
        svc.record_binding("turn_1", "topic_a", fragment_id="frag_b")
    # 冲突不改动已存的绑定
    assert svc.binding_for("turn_1").fragment_id == "frag_a"


def test_mark_write_closed_and_status(db_conn):
    _seed_topic(db_conn)
    _seed_fragment(db_conn, "frag_a")
    svc = TurnBindingService(db_conn)
    svc.record_binding("turn_1", "topic_a", fragment_id="frag_a")

    svc.mark_write_closed("turn_1")
    svc.mark_status("turn_1", "completed")

    binding = svc.binding_for("turn_1")
    assert binding.write_state == "closed"
    assert binding.status == "completed"


def test_fragment_write_busy(db_conn):
    _seed_topic(db_conn)
    _seed_fragment(db_conn, "frag_a")
    svc = TurnBindingService(db_conn)
    svc.record_binding("turn_1", "topic_a", fragment_id="frag_a")

    assert svc.fragment_write_busy("frag_a") is True
    svc.mark_write_closed("turn_1")
    assert svc.fragment_write_busy("frag_a") is False


def test_register_intent_replaces_previous_selection(db_conn):
    _seed_topic(db_conn)
    _seed_fragment(db_conn, "frag_old", closed=True)
    _seed_fragment(db_conn, "frag_other", closed=True)
    svc = TurnBindingService(db_conn)

    first = svc.register_intent("topic_a", "frag_old")
    assert first.state == "registered"
    assert first.version == 1

    # 重复点击同一段历史：算同一次选择，不制造新的版本
    again = svc.register_intent("topic_a", "frag_old")
    assert again.intent_id == first.intent_id
    assert again.version == first.version

    # 发送前改选另一段历史：旧选择被替换，仍然只有一条待落实意图
    second = svc.register_intent("topic_a", "frag_other")
    assert second.version == first.version + 1
    assert svc.peek_intent().intent_id == second.intent_id
    assert svc.intent_by_id(first.intent_id).state == "replaced"
    assert db_conn.execute(
        "SELECT COUNT(*) c FROM continuation_intents WHERE state = 'registered'"
    ).fetchone()["c"] == 1


def test_register_intent_same_request_id_is_idempotent(db_conn):
    _seed_topic(db_conn)
    _seed_fragment(db_conn, "frag_old", closed=True)
    svc = TurnBindingService(db_conn)

    first = svc.register_intent("topic_a", "frag_old", request_id="req_1")
    again = svc.register_intent("topic_a", "frag_old", request_id="req_1")

    assert again.intent_id == first.intent_id
    assert db_conn.execute("SELECT COUNT(*) c FROM continuation_intents").fetchone()["c"] == 1


def test_consume_intent_records_resolution(db_conn):
    _seed_topic(db_conn)
    _seed_fragment(db_conn, "frag_old", closed=True)
    _seed_fragment(db_conn, "frag_new")
    svc = TurnBindingService(db_conn)
    intent = svc.register_intent("topic_a", "frag_old")

    resolved = svc.resolve_intent(intent.intent_id, "frag_new")
    assert resolved.state == "resolved"
    assert resolved.resolved_fragment_id == "frag_new"
    assert svc.peek_intent() is None  # 已落实的不再是「待接续」

    consumed = svc.consume_intent(intent.intent_id)
    assert consumed.state == "consumed"


def test_consume_unknown_intent_raises(db_conn):
    svc = TurnBindingService(db_conn)
    with pytest.raises(IntentNotFound):
        svc.consume_intent("intent_missing")


def test_cancel_intent_leaves_no_pending(db_conn):
    _seed_topic(db_conn)
    _seed_fragment(db_conn, "frag_old", closed=True)
    svc = TurnBindingService(db_conn)
    intent = svc.register_intent("topic_a", "frag_old")

    svc.cancel_intent(intent.intent_id)

    assert svc.peek_intent() is None
    assert svc.intent_by_id(intent.intent_id).state == "cancelled"
