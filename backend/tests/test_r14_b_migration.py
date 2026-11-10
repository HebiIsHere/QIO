"""R14-B 迁移定向测试：79 追加 + 补偿入口（只追加、不改历史迁移）。

验证四件事：
* 全新库一次迁移到最新版本（历史轮 79/补偿 80；R15-D 追加 81/补偿 82），
  恢复记录表 / messages 追加列 / 唯一索引齐备；
* 旧库（78）升级路径：保留历史数据，升级后才具备恢复记录能力；
* 唯一逻辑答案约束真的在数据库层生效（NULL 不参与）；
* 当前补偿迁移是纯建对象语句，且「版本号已到最新、对象丢失」能被按对象复核补回来。
"""

from __future__ import annotations

import sqlite3
import types

import pytest

from agent.services.topic_recovery import (
    TOPIC_RECOVERY_TABLE,
    get_record,
    record_topic_failure,
)
from agent.storage import migrate as migrate_module
from agent.storage.db import connect, transaction
from agent.storage.migrate import (
    apply_migrations,
    current_version,
    missing_objects,
    verify_required_objects,
)
from agent.storage.schema import COMPENSATION_VERSION, MIGRATIONS, SCHEMA_VERSION

TS = "2026-11-10T00:00:00+00:00"
BASE_BEFORE_THIS_ROUND = 78
NEW_INDEXES = (
    "idx_topic_recovery_status",
    "idx_topic_recovery_claim",
    "idx_topic_recovery_answer_key",
    "idx_messages_answer_key",
    "idx_messages_late_answer",
)


def _ensure_version_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_version ("
        " id INTEGER PRIMARY KEY, version INTEGER NOT NULL, applied_at TEXT NOT NULL)"
    )


def _apply_prefix(conn: sqlite3.Connection, upto: int) -> None:
    """按真实迁移定义建到 `upto` 版（含版本行）。

    用 migrate 的幂等规则执行语句：补偿迁移会重放早期迁移已建过的列，
    裸执行会 duplicate column（与生产路径一致地跳过）。
    """
    _ensure_version_table(conn)
    for target, statements in MIGRATIONS:
        if target > upto:
            break
        with transaction(conn):
            migrate_module._apply_statements(conn, target, list(statements))
            conn.execute(
                "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
                (target, TS),
            )


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def _indexes(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")
    }


def _seed_history(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO nodes (id, type, name, meta, created_at, updated_at) "
        "VALUES ('n_r14b', 'topic', '历史话题', '{}', ?, ?)",
        (TS, TS),
    )
    conn.execute(
        "INSERT INTO fragments (id, topic_id, summary, summary_version, created_at, closed_at) "
        "VALUES ('frag_r14b', 'n_r14b', '历史摘要', 1, ?, ?)",
        (TS, TS),
    )
    conn.execute(
        "INSERT INTO messages (id, fragment_id, role, content, content_type, model, raw, "
        "created_at, storage_tier, turn_id) "
        "VALUES ('msg_r14b', 'frag_r14b', 'user', '历史问题', 'text', 'fake', '{}', ?, 'hot', 'turn_old')",
        (TS,),
    )


def _assert_history_intact(conn: sqlite3.Connection) -> None:
    row = conn.execute("SELECT content FROM messages WHERE id = 'msg_r14b'").fetchone()
    assert row is not None and row["content"] == "历史问题"
    fragment = conn.execute(
        "SELECT summary FROM fragments WHERE id = 'frag_r14b'"
    ).fetchone()
    assert fragment is not None and fragment["summary"] == "历史摘要"


# ---------------------------------------------------------------------------
# 1) 全新库
# ---------------------------------------------------------------------------


def test_fresh_database_gets_recovery_objects(tmp_path):
    conn = connect(tmp_path / "r14b_fresh.db")
    try:
        assert apply_migrations(conn) == SCHEMA_VERSION
        assert SCHEMA_VERSION == COMPENSATION_VERSION
        assert current_version(conn) == SCHEMA_VERSION
        assert TOPIC_RECOVERY_TABLE in _tables(conn)
        recovery_columns = _columns(conn, TOPIC_RECOVERY_TABLE)
        expected = {
            "record_id",
            "kind",
            "status",
            "turn_id",
            "topic_id",
            "from_topic_id",
            "fragment_id",
            "user_message_id",
            "operation_id",
            "param_version",
            "base_nav_version",
            "recovery_nav_version",
            "failed_stage",
            "message",
            "stages_json",
            "answer_key",
            "answer_id",
            "answer_text",
            "last_error",
            "claim_owner",
            "claim_until",
            "attempt_count",
            "created_at",
            "updated_at",
        }
        assert expected <= recovery_columns
        message_columns = _columns(conn, "messages")
        assert {"answer_key", "late_answer_of_message_id"} <= message_columns
        assert set(NEW_INDEXES) <= _indexes(conn)
        # 幂等：重复跑不改版本、不重复写版本行
        rows = conn.execute("SELECT COUNT(*) c FROM schema_version").fetchone()["c"]
        assert apply_migrations(conn) == SCHEMA_VERSION
        assert conn.execute("SELECT COUNT(*) c FROM schema_version").fetchone()["c"] == rows
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 2) 旧库（78）升级路径
# ---------------------------------------------------------------------------


def test_old_database_78_upgrades_and_keeps_history(tmp_path):
    conn = connect(tmp_path / "r14b_old.db")
    try:
        _apply_prefix(conn, BASE_BEFORE_THIS_ROUND)
        _seed_history(conn)
        assert current_version(conn) == BASE_BEFORE_THIS_ROUND
        assert TOPIC_RECOVERY_TABLE not in _tables(conn)
        assert "answer_key" not in _columns(conn, "messages")

        assert apply_migrations(conn) == SCHEMA_VERSION
        _assert_history_intact(conn)
        assert current_version(conn) == SCHEMA_VERSION
        assert TOPIC_RECOVERY_TABLE in _tables(conn)
        assert "answer_key" in _columns(conn, "messages")

        # 升级后才具备恢复记录能力（旧行两列为 NULL，不回溯猜测）
        old = conn.execute(
            "SELECT answer_key, late_answer_of_message_id FROM messages WHERE id='msg_r14b'"
        ).fetchone()
        assert old["answer_key"] is None and old["late_answer_of_message_id"] is None
        scope = types.SimpleNamespace(conn=conn)
        record = record_topic_failure(
            scope,
            turn_id="turn_old",
            topic_id="n_r14b",
            user_message_id="msg_r14b",
            failed_stage="answer",
            message="旧库升级后的恢复记录",
        )
        assert get_record(scope, record["record_id"])["status"] == "pending"
        assert record["fragment_id"] == "frag_r14b", "从原用户消息解析固定片段"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 3) 唯一逻辑答案约束（数据库层）
# ---------------------------------------------------------------------------


def test_answer_key_unique_constraint_in_database(tmp_path):
    conn = connect(tmp_path / "r14b_unique.db")
    try:
        apply_migrations(conn)
        _seed_history(conn)
        insert = (
            "INSERT INTO messages (id, fragment_id, role, content, content_type, model, raw, "
            "created_at, storage_tier, turn_id, answer_key) "
            "VALUES (?, 'frag_r14b', 'assistant', '答案', 'text', 'fake', '{}', ?, 'hot', "
            "'turn_old', ?)"
        )
        conn.execute(insert, ("msg_a1", TS, "key_same"))
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(insert, ("msg_a2", TS, "key_same"))
        # NULL 不参与唯一约束（普通消息可以有任意多行）
        conn.execute(insert, ("msg_a3", TS, None))
        conn.execute(insert, ("msg_a4", TS, None))
        assert (
            conn.execute("SELECT COUNT(*) c FROM messages").fetchone()["c"] == 4
        )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 4) 当前补偿迁移：纯建对象 + 可在对象丢失后补回
# ---------------------------------------------------------------------------


def test_compensation_migration_is_additive_and_can_restore_lost_objects(tmp_path):
    targets = [target for target, _ in MIGRATIONS]
    assert targets == sorted(set(targets)), f"迁移号必须唯一且递增：{targets[-4:]}"
    assert targets[-2:] == [SCHEMA_VERSION - 1, SCHEMA_VERSION]
    assert COMPENSATION_VERSION == max(targets) == SCHEMA_VERSION >= 80

    statements = [
        str(statement)
        for target, items in MIGRATIONS
        if target == COMPENSATION_VERSION
        for statement in items
    ]
    forbidden = ("DROP ", "DELETE ", "UPDATE ", "TRUNCATE")
    for statement in statements:
        assert not statement.strip().upper().startswith(forbidden), statement[:60]
    blob = "\n".join(statements)
    assert f"CREATE TABLE IF NOT EXISTS {TOPIC_RECOVERY_TABLE}" in blob
    assert "ADD COLUMN answer_key" in blob
    assert "ADD COLUMN late_answer_of_message_id" in blob
    for index in NEW_INDEXES:
        assert index in blob, index

    # 版本已到最新、对象却被删掉：补偿语句能补回对象，且不动历史数据
    conn = connect(tmp_path / "r14b_lost.db")
    try:
        apply_migrations(conn)
        _seed_history(conn)
        conn.execute(f"DROP TABLE {TOPIC_RECOVERY_TABLE}")
        conn.execute("DROP INDEX idx_messages_answer_key")
        conn.execute("DROP INDEX idx_messages_late_answer")
        assert TOPIC_RECOVERY_TABLE not in _tables(conn)

        with transaction(conn):
            for statement in statements:
                migrate_module._apply_statements(  # 补偿的同一幂等规则
                    conn, COMPENSATION_VERSION, [statement]
                )
        assert TOPIC_RECOVERY_TABLE in _tables(conn)
        assert {"idx_messages_answer_key", "idx_messages_late_answer"} <= _indexes(conn)
        _assert_history_intact(conn)
    finally:
        conn.close()


def test_version_recorded_but_objects_lost_is_detected_and_compensated(tmp_path):
    """按对象复核（B01 口径）：版本已到最新但对象丢失 ⇒ 重放当前补偿迁移补回。"""
    conn = connect(tmp_path / "r14b_b01.db")
    try:
        apply_migrations(conn)
        _seed_history(conn)
        conn.execute(f"DROP TABLE {TOPIC_RECOVERY_TABLE}")
        conn.execute("DROP INDEX idx_messages_answer_key")
        conn.execute("DROP INDEX idx_messages_late_answer")
        missing = missing_objects(conn)
        assert TOPIC_RECOVERY_TABLE in missing["tables"], missing
        assert set(NEW_INDEXES) & set(missing["indexes"]), missing

        still_missing = verify_required_objects(conn)
        assert not any(still_missing.values()), still_missing
        assert TOPIC_RECOVERY_TABLE in _tables(conn)
        assert {"idx_messages_answer_key", "idx_messages_late_answer"} <= _indexes(conn)
        _assert_history_intact(conn)
    finally:
        conn.close()
