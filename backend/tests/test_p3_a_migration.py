# -*- coding: utf-8 -*-
"""P3-A / 迁移 49：提交—主循环 turn 绑定 / 等待事项 / 续作恢复。

验收点：
* 只追加：board_submissions 增加 turn_id / resume_state 列；新增 board_submission_turns
  与 board_waiting；不改历史迁移、不写 schema_version（版本收敛由 Lead 负责）；
* 幂等：apply() 可重复执行（列已存在就跳过；建表 IF NOT EXISTS）；
* 旧行保留：升级后既有提交行的新列取默认值；
* board_waiting.status 只接受 waiting/resolved/cancelled；提交—轮次绑定唯一。
"""

from __future__ import annotations

import sqlite3

import pytest

from agent.storage import migration_p3_a
from agent.storage.migrate import current_version

from test_p3_a_support import add_submission, make_board, migrated_conn

EXPECTED_SUBMISSION_COLUMNS = {
    "id",
    "board_id",
    "seq",
    "status",
    "before_state",
    "after_state",
    "visible",
    "expressions",
    "baseline",
    "delivery",
    "content_hash",
    "error",
    "created_at",
    "client_request_id",
    "turn_id",
    "resume_state",
}

WAITING_COLUMNS = {
    "id",
    "board_id",
    "submission_id",
    "turn_id",
    "question",
    "missing_fields",
    "status",
    "resolved_question_ids",
    "created_at",
    "updated_at",
}


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row["name"]) for row in conn.execute("PRAGMA table_info(" + table + ")")}


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row["name"])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }


def test_migration_49_number_and_shape():
    version, statements = migration_p3_a.MIGRATION
    assert version == 49
    assert migration_p3_a.TABLES == ("board_submission_turns", "board_waiting")
    assert migration_p3_a.COLUMNS["board_submissions"] == ("turn_id", "resume_state")
    text = "\n".join(statements)
    assert "ALTER TABLE board_submissions ADD COLUMN turn_id" in text
    assert "ALTER TABLE board_submissions ADD COLUMN resume_state" in text
    assert "CREATE TABLE IF NOT EXISTS board_submission_turns" in text
    assert "CREATE TABLE IF NOT EXISTS board_waiting" in text


def test_migration_49_adds_columns_and_tables(tmp_path):
    conn = migrated_conn(tmp_path)
    try:
        columns = _columns(conn, "board_submissions")
        assert {"turn_id", "resume_state"} <= columns
        assert _columns(conn, "board_submission_turns") == {
            "submission_id",
            "turn_id",
            "resume_state",
            "created_at",
        }
        assert _columns(conn, "board_waiting") == WAITING_COLUMNS
        tables = _tables(conn)
        for name in migration_p3_a.TABLES:
            assert name in tables
        indexes = {
            str(row["name"])
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")
        }
        assert "idx_board_submission_turns_turn" in indexes
        assert "idx_board_waiting_board" in indexes
        assert "idx_board_waiting_submission" in indexes
    finally:
        conn.close()


def test_migration_49_is_idempotent_and_does_not_bump_schema_version(tmp_path):
    conn = migrated_conn(tmp_path)
    try:
        before = current_version(conn)
        executed = migration_p3_a.apply(conn)
        # 列已存在：ALTER 语句被跳过；CREATE IF NOT EXISTS 可重复执行（幂等）
        assert all("ALTER TABLE" not in statement for statement in executed)
        assert current_version(conn) == before
    finally:
        conn.close()


def test_existing_rows_keep_defaults_for_new_columns(tmp_path):
    conn = migrated_conn(tmp_path)
    try:
        make_board(conn)
        sid = add_submission(conn)
        row = conn.execute(
            "SELECT turn_id, resume_state FROM board_submissions WHERE id = ?", (sid,)
        ).fetchone()
        assert row["turn_id"] == ""
        assert row["resume_state"] == "{}"
    finally:
        conn.close()


def test_waiting_status_is_constrained(tmp_path):
    conn = migrated_conn(tmp_path)
    try:
        stamp = "2026-10-21T00:00:00+00:00"
        conn.execute(
            "INSERT INTO board_waiting (id, board_id, submission_id, question, created_at,"
            " updated_at) VALUES ('w1', 'b1', 's1', '问题？', ?, ?)",
            (stamp, stamp),
        )
        assert conn.execute("SELECT status FROM board_waiting").fetchone()["status"] == "waiting"
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO board_waiting (id, board_id, submission_id, question, status,"
                " created_at, updated_at) VALUES ('w2', 'b1', 's1', 'q', 'bogus', ?, ?)",
                (stamp, stamp),
            )
    finally:
        conn.close()


def test_submission_turn_binding_is_unique_per_pair(tmp_path):
    conn = migrated_conn(tmp_path)
    try:
        stamp = "2026-10-21T00:00:00+00:00"
        conn.execute(
            "INSERT INTO board_submission_turns (submission_id, turn_id, created_at)"
            " VALUES ('s1', 't1', ?)",
            (stamp,),
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO board_submission_turns (submission_id, turn_id, created_at)"
                " VALUES ('s1', 't1', ?)",
                (stamp,),
            )
    finally:
        conn.close()
