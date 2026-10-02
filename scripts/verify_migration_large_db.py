"""F3：migration 24 / 25 在大库上的验证（小 / 中 / 大三档 synthetic DB）。

只用**真实的 migration 代码路径**（agent.storage.migrate.apply_migrations），不另写一套。
验证：迁移耗时 / 内存峰值 / 事务原子性 / 失败回滚 / 重启幂等 / 行数 / 关键字段不变。

用法（backend 目录）：
    $env:QIO_DATA_DIR=''; uv run --frozen python ../scripts/verify_migration_large_db.py --scale medium
    （--scale small|medium|large，默认 medium；--keep 保留临时库）

注意：**不使用任何用户真实数据库**，全部在临时目录里跑（可用 --dir 指定目录）。
"""

from __future__ import annotations

import argparse
import json
import pathlib
import shutil
import sqlite3
import sys
import tempfile
import time
import tracemalloc

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "backend" / "src"))

from agent.storage.db import connect  # noqa: E402
from agent.storage.migrate import apply_migrations, current_version  # noqa: E402
from agent.storage import migrate as migrate_module  # noqa: E402
from agent.storage.schema import MIGRATIONS  # noqa: E402

# 迁移 24/25 之前的最大版本（这两条是第三阶段要验证的对象）
TARGET_BEFORE = 23
SCALES = {
    "small": {"turn_traces": 200, "messages": 1_000, "tool_records": 200},
    "medium": {"turn_traces": 20_000, "messages": 100_000, "tool_records": 5_000},
    "large": {"turn_traces": 100_000, "messages": 500_000, "tool_records": 20_000},
}


def build_v23_database(db_path: pathlib.Path, scale: dict) -> sqlite3.Connection:
    """用**真实迁移列表**建到 23 版，再灌入 synthetic 数据。"""
    conn = connect(db_path)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_version ("
        " id INTEGER PRIMARY KEY, version INTEGER NOT NULL, applied_at TEXT NOT NULL)"
    )
    for target, statements in MIGRATIONS:
        if target > TARGET_BEFORE:
            break
        for statement in statements:
            conn.execute(statement)
        conn.execute(
            "INSERT INTO schema_version (version, applied_at) VALUES (?, '2026-01-01T00:00:00+00:00')",
            (target,),
        )
    conn.execute("DELETE FROM schema_version")
    for target, _statements in MIGRATIONS:
        if target > TARGET_BEFORE:
            break
        conn.execute(
            "INSERT INTO schema_version (version, applied_at) VALUES (?, '2026-01-01T00:00:00+00:00')",
            (target,),
        )
    return conn


def seed(conn: sqlite3.Connection, scale: dict) -> None:
    """灌 synthetic 数据：话题 → 片段 → 消息；另有 trace 与工具历史。"""
    topics = max(1, scale["turn_traces"] // 50)
    conn.executemany(
        "INSERT OR IGNORE INTO nodes (id, type, name, meta, created_at, updated_at) "
        "VALUES (?, 'topic', ?, '{}', ?, ?)",
        [
            (f"topic_{i:06d}", f"话题 {i}", "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00")
            for i in range(topics)
        ],
    )
    fragments = max(1, scale["messages"] // 20)
    # 每个话题只能有一个「开放」片段（迁移 12 的部分唯一索引）：其余必须带 closed_at，
    # 否则 INSERT OR IGNORE 会静默跳过，后面的消息就挂不上外键。
    conn.executemany(
        "INSERT OR IGNORE INTO fragments (id, topic_id, created_at, closed_at) VALUES (?, ?, ?, ?)",
        [
            (
                f"frag_{i:06d}",
                f"topic_{i % topics:06d}",
                "2026-01-01T00:00:00+00:00",
                None if i < topics else "2026-01-01T00:00:00+00:00",
            )
            for i in range(fragments)
        ],
    )
    # 分批写入：大档不要一次 materialize 50 万行
    batch: list[tuple] = []
    for i in range(scale["messages"]):
        batch.append(
            (
                f"msg_{i:08d}",
                f"frag_{i % fragments:06d}",
                "user" if i % 2 == 0 else "assistant",
                f"第 {i} 条消息：这是一段用于迁移压测的 synthetic 内容。",
                "2026-01-01T00:00:00+00:00",
            )
        )
        if len(batch) >= 20_000:
            conn.executemany(
                "INSERT INTO messages (id, fragment_id, role, content, content_type, created_at) "
                "VALUES (?, ?, ?, ?, 'text', ?)",
                batch,
            )
            batch = []
    if batch:
        conn.executemany(
            "INSERT INTO messages (id, fragment_id, role, content, content_type, created_at) "
            "VALUES (?, ?, ?, ?, 'text', ?)",
            batch,
        )
    conn.executemany(
        "INSERT INTO turn_traces (turn_id, status, started_at, ended_at, duration_ms, "
        " initial_topic, final_topic, topic, injection, model_calls, tool_runs, writes, "
        " warnings, final_preview) "
        "VALUES (?, 'done', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                f"turn_{i:08d}",
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:00:02+00:00",
                2000 + (i % 500),
                f"topic_{i % topics:06d}",
                f"topic_{i % topics:06d}",
                json.dumps({"initial": f"topic_{i % topics:06d}"}),
                json.dumps({"total_tokens": i % 800}),
                json.dumps([{"seq": 1, "latency_ms": 100 + (i % 400)}]),
                json.dumps([{"tool": "memory_search", "duration_ms": i % 50}]),
                json.dumps({"messages": [f"msg_{i:08d}"]}),
                "[]",
                f"第 {i} 轮回答预览",
            )
            for i in range(scale["turn_traces"])
        ],
    )
    conn.executemany(
        "INSERT INTO tool_records (id, turn_id, tool_name, arguments, output, status, created_at) "
        "VALUES (?, ?, 'fs_read', '{}', '内容', 'success', ?)",
        [
            (f"tr_{i:08d}", f"turn_{i:08d}", "2026-01-01T00:00:00+00:00")
            for i in range(scale["tool_records"])
        ],
    )


def counts(conn: sqlite3.Connection) -> dict:
    tables = ["nodes", "fragments", "messages", "turn_traces", "tool_records", "settings"]
    return {
        table: int(conn.execute("SELECT COUNT(*) FROM " + table).fetchone()[0])
        for table in tables
    }


def sample_fields(conn: sqlite3.Connection) -> dict:
    row = conn.execute(
        "SELECT turn_id, status, duration_ms, final_preview FROM turn_traces "
        "ORDER BY turn_id DESC LIMIT 1"
    ).fetchone()
    message = conn.execute(
        "SELECT content FROM messages ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return {
        "turn_id": row[0], "status": row[1], "duration_ms": row[2], "final_preview": row[3],
        "message_content": message[0],
    }


def run(scale_name: str, directory: pathlib.Path | None, keep: bool) -> dict:
    scale = SCALES[scale_name]
    base = pathlib.Path(directory) if directory else pathlib.Path(tempfile.mkdtemp())
    base.mkdir(parents=True, exist_ok=True)
    db_path = base / f"migration_{scale_name}.db"
    if db_path.exists():
        db_path.unlink()

    report: dict = {"scale": scale_name, "rows": scale, "db": str(db_path)}
    conn = build_v23_database(db_path, scale)
    seed(conn, scale)
    report["version_before"] = current_version(conn)
    report["counts_before"] = counts(conn)
    report["sample_before"] = sample_fields(conn)
    report["db_size_before_bytes"] = db_path.stat().st_size
    conn.close()

    # --- 迁移（真实代码路径） ---
    conn = connect(db_path)
    tracemalloc.start()
    started = time.perf_counter()
    version_after = apply_migrations(conn)
    elapsed = time.perf_counter() - started
    _current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    report["version_after"] = version_after
    report["migration_seconds"] = round(elapsed, 4)
    report["python_peak_bytes"] = peak
    report["counts_after"] = counts(conn)
    report["sample_after"] = sample_fields(conn)
    report["data_preserved"] = report["counts_before"] == report["counts_after"] and \
        report["sample_before"] == report["sample_after"]

    # --- 结构检查 ---
    columns = [row[1] for row in conn.execute("PRAGMA table_info(turn_traces)").fetchall()]
    report["has_phases_column"] = "phases" in columns
    report["legacy_phases_default"] = conn.execute(
        "SELECT phases FROM turn_traces ORDER BY turn_id DESC LIMIT 1"
    ).fetchone()[0]
    report["turn_journal_exists"] = bool(conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='turn_journal'"
    ).fetchone())
    report["turn_journal_indexes"] = [
        row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='turn_journal'"
        ).fetchall()
    ]
    report["db_size_after_bytes"] = db_path.stat().st_size
    conn.close()

    # --- 重启幂等：再跑一次真实迁移入口 ---
    conn = connect(db_path)
    started = time.perf_counter()
    version_restart = apply_migrations(conn)
    report["restart_seconds"] = round(time.perf_counter() - started, 4)
    report["version_after_restart"] = version_restart
    report["counts_after_restart"] = counts(conn)
    report["restart_idempotent"] = (
        version_restart == version_after and report["counts_after_restart"] == report["counts_after"]
    )
    conn.close()

    # --- 失败回滚：真实的 apply_migrations 路径 + 一条会失败的迁移 ---
    failure_conn = connect(db_path)
    original = list(migrate_module.MIGRATIONS)
    broken_version = max(target for target, _ in original) + 1
    try:
        migrate_module.MIGRATIONS.append((broken_version, [
            "ALTER TABLE turn_traces ADD COLUMN rollback_probe TEXT",
            "ALTER TABLE turn_traces ADD COLUMN rollback_probe TEXT",  # 故意重复 → 必然失败
        ]))
        try:
            apply_migrations(failure_conn)
            report["failure_raised"] = False
        except sqlite3.Error as exc:
            report["failure_raised"] = True
            report["failure_error"] = str(exc)[:120]
    finally:
        migrate_module.MIGRATIONS[:] = original
    columns_after_failure = [
        row[1] for row in failure_conn.execute("PRAGMA table_info(turn_traces)").fetchall()
    ]
    report["version_after_failure"] = current_version(failure_conn)
    report["partial_ddl_left_behind"] = "rollback_probe" in columns_after_failure
    report["counts_after_failure"] = counts(failure_conn)

    # 真实后果：迁移 24 的 ALTER 生效了、版本号没推进（进程在两步之间死掉），
    # 下次启动会再跑一遍这条 ALTER —— 这是一条**不可自愈**的路径，看它到底会不会炸。
    try:
        apply_migrations(failure_conn)
        report["rerun_after_partial_failure"] = "ok"
    except sqlite3.Error as exc:
        report["rerun_after_partial_failure"] = "raises: " + str(exc)[:80]
    failure_conn.close()

    # --- 崩溃窗口：真实迁移 24 的 ALTER 已经生效、版本号还没写 ---
    crash_path = base / f"crash_{scale_name}.db"
    if crash_path.exists():
        crash_path.unlink()
    crash_conn = build_v23_database(crash_path, {"turn_traces": 0, "messages": 0, "tool_records": 0})
    crash_conn.execute("ALTER TABLE turn_traces ADD COLUMN phases TEXT NOT NULL DEFAULT '{}'")
    try:
        apply_migrations(crash_conn)
        report["crash_window_restart"] = "ok"
    except sqlite3.Error as exc:
        report["crash_window_restart"] = "raises: " + str(exc)[:80]
    crash_conn.close()
    if not keep and directory is None:
        crash_path.unlink(missing_ok=True)

    if not keep and directory is None:
        shutil.rmtree(base, ignore_errors=True)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="migration 24/25 大库验证")
    parser.add_argument("--scale", choices=sorted(SCALES), default="medium")
    parser.add_argument("--dir", help="数据库目录（默认临时目录；给了就不删）")
    parser.add_argument("--keep", action="store_true", help="保留临时数据库")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    report = run(args.scale, pathlib.Path(args.dir) if args.dir else None, args.keep)
    print(json.dumps(report, ensure_ascii=False, indent=1))
    ok = (
        report["version_after"] == 25
        and report["data_preserved"]
        and report["has_phases_column"]
        and report["legacy_phases_default"] == "{}"
        and report["turn_journal_exists"]
        and report["restart_idempotent"]
        # 原子性/失败回滚是 F3 的验收项之一：不满足就不能算 ok
        and not report["partial_ddl_left_behind"]
        and report["crash_window_restart"] == "ok"
    )
    print("VERDICT " + ("ok" if ok else "PROBLEM"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())