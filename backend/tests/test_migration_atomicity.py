"""F3 的失败回滚验收：migration 运行器必须**原子**，半截迁移的存量库必须能自愈。

缺陷（第三阶段 F3 用真实 apply_migrations 复现）：

* storage/db.py 把连接设成 autocommit（isolation_level = None），此时 `with conn:`
  **不会隐式开事务** —— 迁移的每条语句各自落盘。
* 于是一次中途失败（或两步之间进程死掉）会留下**半截 DDL**，而 schema_version 还停在
  旧版本。migration 24 是单条 `ALTER TABLE ... ADD COLUMN phases`：ALTER 生效、版本没写，
  下次启动重放它就以 "duplicate column name: phases" 永远起不来。

这里的三条断言分别对应修复要求：原子性（失败什么都不留）、自愈（半截库能起来）、
常规升级回归（数据不丢、重启幂等）。
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

import pytest

from agent.storage import migrate as migrate_module
from agent.storage.db import connect
from agent.storage.migrate import apply_migrations, current_version
from agent.storage.schema import MIGRATIONS

# 迁移 24/25 之前的最大版本
BEFORE = 23
FINAL = MIGRATIONS[-1][0]


def _build_database_at_23(path: Path) -> sqlite3.Connection:
    """用**真实迁移列表**建到 23 版（和 scripts/verify_migration_large_db.py 同一套口径）。"""
    conn = connect(path)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_version ("
        " id INTEGER PRIMARY KEY, version INTEGER NOT NULL, applied_at TEXT NOT NULL)"
    )
    for target, statements in MIGRATIONS:
        if target > BEFORE:
            break
        for statement in statements:
            conn.execute(statement)
        conn.execute(
            "INSERT INTO schema_version (version, applied_at) VALUES (?, '2026-01-01T00:00:00+00:00')",
            (target,),
        )
    return conn


def _seed_minimal_rows(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO nodes (id, type, name, meta, created_at, updated_at) "
        "VALUES ('topic_1', 'topic', '话题', '{}', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')"
    )
    conn.execute(
        "INSERT INTO fragments (id, topic_id, created_at) VALUES ('frag_1', 'topic_1', '2026-01-01T00:00:00+00:00')"
    )
    conn.execute(
        "INSERT INTO messages (id, fragment_id, role, content, content_type, created_at) "
        "VALUES ('msg_1', 'frag_1', 'user', '原文', 'text', '2026-01-01T00:00:00+00:00')"
    )
    conn.execute(
        "INSERT INTO turn_traces (turn_id, status, started_at, duration_ms, final_preview) "
        "VALUES ('turn_1', 'done', '2026-01-01T00:00:00+00:00', 1234, '回答预览')"
    )


def _counts(conn: sqlite3.Connection) -> dict:
    return {
        table: int(conn.execute("SELECT COUNT(*) FROM " + table).fetchone()[0])
        for table in ("nodes", "fragments", "messages", "turn_traces")
    }


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [row[1] for row in conn.execute("PRAGMA table_info(" + table + ")").fetchall()]


def test_a_failed_migration_leaves_no_partial_schema(tmp_path: Path):
    """第 2 条语句失败 → 这条迁移既不留 DDL、也不留版本号（前面的迁移照常生效）。"""
    conn = _build_database_at_23(tmp_path / "atomic.db")
    assert current_version(conn) == BEFORE
    original = list(migrate_module.MIGRATIONS)
    broken = max(target for target, _ in original) + 1
    try:
        migrate_module.MIGRATIONS.append(
            (
                broken,
                [
                    "ALTER TABLE settings ADD COLUMN atomicity_probe TEXT",
                    # 真失败（不是「已存在」信号）：自愈规则不许把它吞掉
                    "ALTER TABLE no_such_table_here ADD COLUMN x TEXT",
                ],
            )
        )
        with pytest.raises(sqlite3.Error):
            apply_migrations(conn)
    finally:
        migrate_module.MIGRATIONS[:] = original

    # 24 / 25 正常生效；失败的那条（26）什么都没留下
    assert current_version(conn) == FINAL
    assert "atomicity_probe" not in _columns(conn, "settings")


def test_a_half_applied_migration_is_self_healed(tmp_path: Path, caplog):
    """旧代码的崩溃窗口：ALTER 生效了、schema_version 还停在 23。重启必须能起来。"""
    conn = _build_database_at_23(tmp_path / "half.db")
    # 模拟 migration 24 已经执行、版本行还没写
    conn.execute("ALTER TABLE turn_traces ADD COLUMN phases TEXT NOT NULL DEFAULT '{}'")
    assert current_version(conn) == BEFORE

    with caplog.at_level(logging.WARNING):
        version = apply_migrations(conn)

    assert version == FINAL
    assert "phases" in _columns(conn, "turn_traces")
    assert conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='turn_journal'"
    ).fetchone() is not None
    # 自愈不是静默的：日志里要说清哪条语句已经应用过
    assert any("已应用" in record.message for record in caplog.records), [r.message for r in caplog.records]


def test_normal_upgrade_preserves_data_and_restart_is_idempotent(tmp_path: Path):
    conn = _build_database_at_23(tmp_path / "normal.db")
    _seed_minimal_rows(conn)
    before = _counts(conn)

    assert apply_migrations(conn) == FINAL
    assert _counts(conn) == before
    row = conn.execute(
        "SELECT status, duration_ms, final_preview FROM turn_traces WHERE turn_id = 'turn_1'"
    ).fetchone()
    assert tuple(row) == ("done", 1234, "回答预览")
    assert conn.execute("SELECT phases FROM turn_traces WHERE turn_id = 'turn_1'").fetchone()[0] == "{}"

    # 重启：再跑一次真实的迁移入口，什么都不该变
    assert apply_migrations(conn) == FINAL
    assert _counts(conn) == before


def test_only_already_exists_errors_are_treated_as_applied():
    assert migrate_module._already_applied(
        sqlite3.OperationalError("duplicate column name: phases")
    )
    assert migrate_module._already_applied(
        sqlite3.OperationalError("table turn_journal already exists")
    )
    assert not migrate_module._already_applied(sqlite3.OperationalError("no such table: foo"))
    assert not migrate_module._already_applied(
        sqlite3.IntegrityError("CHECK constraint failed: nodes")
    )