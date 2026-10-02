"""F2：TraceStore 写放大 benchmark（不改任何产品代码）。

背景：TraceStore 的 append 是「读出整列 JSON → 追加 → 整列写回」，于是单次写入成本
随这一轮已经记录的事件数增长 —— 需要先量化，再决定要不要动存储结构。

用法（backend 目录）：
    $env:QIO_DATA_DIR=''; uv run --frozen python ../scripts/bench_trace_store.py
四种规模：short(3) / medium(30) / long(100) / heavy(400) 次「模型或工具」事件，
每种规模都从空 trace 开始写，记录总耗时、单次成本、以及相对裸写（同连接、同样 autocommit、
单行 UPDATE，值不变的基线）的倍数。
"""

from __future__ import annotations

import json
import pathlib
import statistics
import sys
import tempfile
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "backend" / "src"))

from agent.storage.db import connect  # noqa: E402
from agent.storage.migrate import apply_migrations  # noqa: E402
from agent.trace.store import TraceStore  # noqa: E402

SCALES = [("short", 3, 1), ("medium", 30, 5), ("long", 100, 20), ("heavy", 400, 60)]
TRIALS = 3


def fresh_store():
    directory = pathlib.Path(tempfile.mkdtemp())
    conn = connect(directory / "app.db")
    apply_migrations(conn)
    store = TraceStore(conn)
    store.begin("turn_bench")
    return conn, store


def bare_baseline(conn, iterations):
    """同连接 / 同样 autocommit 的裸写基线：每次改一个值（不是不变的 no-op）。"""
    conn.execute("CREATE TABLE IF NOT EXISTS bench (id INTEGER PRIMARY KEY, v TEXT NOT NULL DEFAULT '')")
    conn.execute("INSERT OR IGNORE INTO bench (id, v) VALUES (1, '')")
    started = time.perf_counter()
    for index in range(iterations):
        conn.execute("UPDATE bench SET v = ? WHERE id = 1", (str(index),))
    return time.perf_counter() - started


def write_events(store, model_events, tool_events):
    for index in range(model_events):
        store.record_model_call("turn_bench", {
            "seq": index + 1, "model": "deepseek-v4-flash", "adapter_mode": "native",
            "input_tokens": 8404, "output_tokens": 979, "latency_ms": 4806, "tool_calls": 1,
        })
        if index % 2 == 0 and tool_events:
            store.record_tool_run("turn_bench", {
                "call_id": f"call_{index}", "tool": "memory_search",
                "args_preview": "{\"query\": \"原神爆料查询工具 提交审批 工作区\"}",
                "ok": True, "error": None, "duration_ms": 9, "policy": None,
                "result_preview": "未找到相关记忆",
            })


def main() -> int:
    print(f"{chr(61) * 86}")
    print(f"{'规模':<8}{'事件数':>7}{'总耗时(s)':>11}{'单次(ms)':>11}{'裸写总(s)':>11}{'倍数':>9}  说明")
    print(f"{chr(61) * 86}")
    results = []
    for name, model_events, tool_events in SCALES:
        totals = []
        per_event = []
        ratios = []
        bares = []
        for _ in range(TRIALS):
            conn, store = fresh_store()
            events = model_events + (len(range(0, model_events, 2)) if tool_events else 0)
            started = time.perf_counter()
            write_events(store, model_events, tool_events)
            elapsed = time.perf_counter() - started
            bare = bare_baseline(conn, events)
            bares.append(bare)
            totals.append(elapsed)
            per_event.append(elapsed / events * 1000)
            ratios.append(elapsed / max(bare, 1e-6))
            conn.close()
        row = {
            "scale": name,
            "events": events,
            "total_s": round(statistics.median(totals), 4),
            "per_event_ms": round(statistics.median(per_event), 3),
            "bare_s": round(statistics.median(bares), 4),
            "ratio_vs_bare": round(statistics.median(ratios), 1),
        }
        results.append(row)
        print(f"{name:<8}{events:>7}{row['total_s']:>11.4f}{row['per_event_ms']:>11.3f}"
              f"{row['bare_s']:>11.4f}{row['ratio_vs_bare']:>9.1f}  {model_events} model + {tool_events} tool")
    print(chr(61) * 86)
    first = results[0]["per_event_ms"]
    last = results[-1]["per_event_ms"]
    print(f"单次成本从 {first:.3f} ms（{results[0]['events']} 事件）涨到 {last:.3f} ms（{results[-1]['events']} 事件）"
          f" = {last / max(first, 1e-9):.1f}x")
    print("JSON " + json.dumps(results, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())