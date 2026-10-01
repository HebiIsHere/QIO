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


def test_recording_overhead_is_small(db_conn: sqlite3.Connection):
    """轨迹写入的**相对**开销：与同进程原生 sqlite 基线比倍数，而不是比绝对秒数。

    旧写法是 `assert elapsed < 1.0`（200 次写入的绝对墙钟）。这条插入路径是
    「读 JSON 列 → append → 写回」，在本机安静环境下**原生 sqlite 做同样的事**
    就要 0.93–1.01s；7 个 agent 并行时会到 1.2s+。于是这个断言变成了负载计：
    main 的 store.py 与本分支在这一段逐字节同路径，负载高时一样会红
    （2026-10-02 交错测 6 组：main 中位数 1.065s / 本分支 1.054s，两组各自的
    单次波动 ±0.25s —— 波动比版本差大一个数量级）。

    所以自校准：先用**同样的 SQL 形状**量一个原生基线（干净列 → 200 次读改写），
    再量 TraceStore.record_model_call，断言倍数上限 3.0x。
    实测（200 次/组，安静环境）：worktree/native ≈ 1.0–1.3x，main/native ≈ 1.1–1.2x；
    3.0x 拦的是「每次写入多了一次 commit / fsync / 全表重写」这类数量级退化，
    而不是磁盘快慢。正确性断言（真的落下 200 条）保持不变。
    """
    import json
    import time

    calls = 200
    s = TraceStore(db_conn)
    s.begin("turn_perf")

    def run(*, native: bool) -> float:
        db_conn.execute(
            "UPDATE turn_traces SET model_calls = '[]' WHERE turn_id = 'turn_perf'"
        )
        started = time.perf_counter()
        for i in range(calls):
            if native:
                row = db_conn.execute(
                    "SELECT model_calls FROM turn_traces WHERE turn_id = 'turn_perf'"
                ).fetchone()
                data = json.loads(row["model_calls"]) if row["model_calls"] else []
                data.append({"seq": i, "output_tokens": i})
                db_conn.execute(
                    "UPDATE turn_traces SET model_calls = ? WHERE turn_id = 'turn_perf'",
                    (json.dumps(data),),
                )
            else:
                s.record_model_call("turn_perf", {"seq": i, "output_tokens": i})
        return time.perf_counter() - started

    baseline = min(run(native=True), run(native=True))  # 取两次里更快的一次，抵消抖动
    elapsed = run(native=False)
    ratio = elapsed / max(baseline, 1e-6)

    # 正确性：200 条必须真的写下去（与旧断言相同的那一条）
    stored = s.get("turn_perf")["model_calls"]
    assert len(stored) == calls
    assert stored[0]["seq"] == 0 and stored[-1]["seq"] == calls - 1

    assert ratio <= 3.0, (
        f"轨迹每次写入的开销相对原生 sqlite 基线 {ratio:.2f}x（上限 3.0x）："
        f"trace={elapsed:.3f}s baseline={baseline:.3f}s"
    )
