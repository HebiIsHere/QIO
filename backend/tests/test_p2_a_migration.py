# -*- coding: utf-8 -*-
"""P2-A / 迁移 46：变化日志 / 调度状态 / 用户 turn 去重。

验收点：
* 迁移 46 只追加三张表 + 单行状态，不写 schema_version（版本收敛由 Lead 负责）；
* 语句可重复应用（半截迁移 / 补偿 48 重放），不会因为重复执行报错；
* 状态表真的只有一行（id = 1），变化日志的 (chain_id, version) 唯一约束在库层成立。
"""

from __future__ import annotations

import sqlite3

import pytest

from agent.storage import migration_p2_a
from agent.storage.migrate import current_version

from test_p2_a_support import migrated_conn, state_row


EXPECTED_STATE_COLUMNS = {
    "id",
    "turn_seq",
    "last_processed_seq",
    "batch_seq",
    "running",
    "run_started_at",
    "failed_attempts",
    "retry_blocked_until_turn",
    "last_error",
    "last_run_at",
    "updated_at",
}


def _table_names(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
    ).fetchall()
    return {str(row["name"]) for row in rows}


def test_migration_46_number_and_statement_shape():
    """迁移号是 46；语句全部是幂等的 CREATE TABLE IF NOT EXISTS / INSERT OR IGNORE。"""
    version, statements = migration_p2_a.MIGRATION
    assert version == 46
    assert len(statements) == 4
    creates = [stmt for stmt in statements if "CREATE TABLE IF NOT EXISTS" in stmt]
    assert len(creates) == 3
    assert any("INSERT OR IGNORE INTO knowledge_maintenance_state" in stmt for stmt in statements)
    assert migration_p2_a.TABLES == (
        "knowledge_change_log",
        "knowledge_maintenance_state",
        "knowledge_maintenance_turn_log",
    )


def test_migration_46_creates_tables_and_single_state_row(tmp_path):
    conn = migrated_conn(tmp_path)
    try:
        tables = _table_names(conn)
        for name in migration_p2_a.TABLES:
            assert name in tables, name
        row = state_row(conn)
        assert int(row["id"]) == 1
        assert int(row["turn_seq"]) == 0
        assert int(row["last_processed_seq"]) == 0
        assert int(row["batch_seq"]) == 0
        assert int(row["running"]) == 0
        assert row["run_started_at"] is None
        assert int(row["failed_attempts"]) == 0
        assert int(row["retry_blocked_until_turn"]) == 0
        assert row["last_error"] is None
        assert row["last_run_at"] is None
        assert row["updated_at"]  # 迁移插入的行必须有时间
    finally:
        conn.close()


def test_state_table_rejects_second_row(tmp_path):
    conn = migrated_conn(tmp_path)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO knowledge_maintenance_state (id, updated_at) VALUES (2, 'x')"
            )
    finally:
        conn.close()


def test_migration_46_columns_match_contract(tmp_path):
    conn = migrated_conn(tmp_path)
    try:
        state_columns = {
            str(row["name"]) for row in conn.execute("PRAGMA table_info(knowledge_maintenance_state)")
        }
        assert state_columns == EXPECTED_STATE_COLUMNS

        change_columns = {
            str(row["name"]) for row in conn.execute("PRAGMA table_info(knowledge_change_log)")
        }
        assert change_columns == {
            "seq",
            "knowledge_id",
            "chain_id",
            "version",
            "source",
            "occurred_at",
            "created_turn_seq",
            "consumed_batch_seq",
        }
        turn_columns = {
            str(row["name"]) for row in conn.execute("PRAGMA table_info(knowledge_maintenance_turn_log)")
        }
        assert turn_columns == {"turn_id", "created_at"}
    finally:
        conn.close()


def test_migration_46_is_idempotent_and_does_not_bump_schema_version(tmp_path):
    conn = migrated_conn(tmp_path)
    try:
        before = current_version(conn)
        # 第二次应用：全部语句可重放（补偿迁移 48 也会整体重放它们）。
        executed = migration_p2_a.apply(conn)
        assert len(executed) == len(migration_p2_a.MIGRATION[1])
        # 状态行不会被第二次应用重置。
        conn.execute(
            "UPDATE knowledge_maintenance_state SET turn_seq = 7 WHERE id = 1"
        )
        migration_p2_a.apply(conn)
        assert int(state_row(conn)["turn_seq"]) == 7
        assert current_version(conn) == before
    finally:
        conn.close()


def test_change_log_unique_chain_version_is_enforced(tmp_path):
    conn = migrated_conn(tmp_path)
    try:
        conn.execute(
            "INSERT INTO knowledge_change_log"
            " (knowledge_id, chain_id, version, source, occurred_at, created_turn_seq)"
            " VALUES ('k1', 'chain-a', 1, 'background_extraction', 'now', 0)"
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO knowledge_change_log"
                " (knowledge_id, chain_id, version, source, occurred_at, created_turn_seq)"
                " VALUES ('k2', 'chain-a', 1, 'background_extraction', 'now', 0)"
            )
        # seq 是自增处理位置，不回退。
        rows = conn.execute("SELECT seq FROM knowledge_change_log").fetchall()
        assert [int(row["seq"]) for row in rows] == [1]
    finally:
        conn.close()
