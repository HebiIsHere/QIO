from __future__ import annotations

import sqlite3

from agent.trace.store import TraceStore


def test_begin_record_finish_roundtrip(db_conn: sqlite3.Connection):
    s = TraceStore(db_conn)
    s.begin("turn_1", initial_topic="topic_a")
    s.set_topic("turn_1", {"predictor_backend": "rules", "final": "topic_b", "operation": "switch"})
    s.set_injection("turn_1", {"total_tokens": 120, "items": [{"surface": "topic_short", "tokens": 120}]})
    s.record_model_call("turn_1", {"seq": 1, "model": "m", "output_tokens": 10})
    s.record_tool_run("turn_1", {"call_id": "c1", "tool": "echo", "ok": True})
    s.record_write("turn_1", "messages", "msg_1")
    s.add_warning("turn_1", "budget_exhausted", "hit limit")
    s.finish("turn_1", "done", final_topic="topic_b", final_preview="hello")

    t = s.get("turn_1")
    assert t["status"] == "done"
    assert t["final_topic"] == "topic_b"
    assert t["topic"]["operation"] == "switch"
    assert t["injection"]["total_tokens"] == 120
    assert t["model_calls"][0]["model"] == "m"
    assert t["tool_runs"][0]["tool"] == "echo"
    assert t["writes"]["messages"] == ["msg_1"]
    assert t["warnings"][0]["code"] == "budget_exhausted"
    assert t["duration_ms"] is not None


def test_pagination_and_count(db_conn: sqlite3.Connection):
    s = TraceStore(db_conn)
    for i in range(5):
        s.begin(f"turn_{i}")
        s.finish(f"turn_{i}", "done")
    assert s.count() == 5
    page1 = s.list(limit=2, offset=0)
    page2 = s.list(limit=2, offset=2)
    assert len(page1) == 2 and len(page2) == 2
    ids = {t["turn_id"] for t in page1} | {t["turn_id"] for t in page2}
    assert len(ids) == 4  # 不重叠


def test_corrupted_trace_reads_without_raising(db_conn: sqlite3.Connection):
    s = TraceStore(db_conn)
    s.begin("turn_bad")
    # 手工写入损坏 JSON
    db_conn.execute(
        "UPDATE turn_traces SET model_calls = '{not json', topic = '' WHERE turn_id = ?",
        ("turn_bad",),
    )
    t = s.get("turn_bad")
    assert t is not None
    assert t["model_calls"] is None  # 损坏 → 容忍为 None，不抛
    assert t["topic"] is None


def test_disabled_store_is_noop(db_conn: sqlite3.Connection):
    s = TraceStore(db_conn, enabled=False)
    s.begin("turn_x")
    s.record_tool_run("turn_x", {"call_id": "c", "tool": "t"})
    assert s.get("turn_x") is None
    assert s.count() == 0
