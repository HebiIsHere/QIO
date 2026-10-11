# -*- coding: utf-8 -*-
"""互动线并入 main 的迁移针对性验收（2026-10-11）。

本次整合对数据库做的唯一改动：**互动对象的迁移号从来源线的 26 改为 35**，
并追加补偿入口 36（= 迁移 34 的全部语句 + 互动对象），`REQUIRED_OBJECTS` 同步
补上 7 张互动表与 3 个索引。

为什么必须改号：`apply_migrations` 只按版本号前进（`target <= 已记录版本`
就整段跳过）。main 的历史迁移已到 34，沿用 26 的互动建表语句在**任何 main
存量库上都不会执行**，结果是互动接口缺表。

本文件只验这五件事（不跑全量）：
    1. 空数据库：一次升到 36，互动对象齐全，能真的写入互动数据；
    2. main 已有库（记到 34）：升级后互动表建出，**聊天 / 附件 / 记忆数据一行不丢**；
    3. 互动来源线已有的库（记到 26 的互动形状）：升级后互动数据一行不丢、不重复建；
    4. 版本号已推进到 36 但互动对象缺失：按对象复核 → 重放 36 → 补齐，版本行不变；
    5. 重复启动：结构与内容都不再变化（幂等）。

运行：cd backend; uv run --frozen pytest tests/test_interactive_migration_integration.py -q
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from agent.storage.db import connect, transaction
from agent.storage.migrate import (
    REQUIRED_OBJECTS,
    apply_migrations,
    current_version,
    missing_objects,
)
from agent.storage.schema import (
    COMPENSATION_VERSION,
    MIGRATIONS,
    SCHEMA_VERSION,
    _INTERACTIVE_DDL,
)

TS = "2026-10-11T00:00:00+00:00"
MAIN_VERSION = 34          # 并入前 main 的实际最大版本
SOURCE_VERSION = 26        # 来源线给互动对象用的编号
INTERACTIVE_TABLES = (
    "boards",
    "board_states",
    "board_state_snapshots",
    "board_drafts",
    "board_submissions",
    "board_pending",
    "board_intents",
)
INTERACTIVE_INDEXES = (
    "idx_board_snapshots_board",
    "idx_board_submissions_board",
    "idx_board_intents_board",
)
_ALREADY = ("duplicate column name", "already exists")


def _build_through(conn: sqlite3.Connection, target: int) -> None:
    """按真实迁移定义把库建到 target 版（含版本行），容忍补偿迁移的冗余重放。"""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_version ("
        " id INTEGER PRIMARY KEY, version INTEGER NOT NULL, applied_at TEXT NOT NULL)"
    )
    for version, statements in MIGRATIONS:
        if version > target:
            continue
        with transaction(conn):
            for stmt in statements:
                try:
                    conn.execute(stmt)
                except sqlite3.OperationalError as exc:
                    if not any(tok in str(exc).lower() for tok in _ALREADY):
                        raise
            conn.execute(
                "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)", (version, TS)
            )


def _mark_versions(conn: sqlite3.Connection, *versions: int) -> None:
    for version in versions:
        conn.execute(
            "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)", (version, TS)
        )


def _build_interactive_shape(conn: sqlite3.Connection) -> None:
    """来源线自己的库：互动建表跑在 26 号（SQL 与 _INTERACTIVE_DDL 逐字同源）。"""
    with transaction(conn):
        for stmt in _INTERACTIVE_DDL:
            conn.execute(stmt)
    _mark_versions(conn, SOURCE_VERSION)


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {str(r[0]) for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _indexes(conn: sqlite3.Connection) -> set[str]:
    return {
        str(r[0])
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name IS NOT NULL"
        )
    }


def _count(conn: sqlite3.Connection, table: str) -> int:
    return int(conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"])


def _version_rows(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) AS n FROM schema_version").fetchone()["n"])


def _seed_chat_and_memory(conn: sqlite3.Connection) -> None:
    """聊天 / 记忆（25 号起就有的表）：升级与补偿都不许动它。"""
    conn.execute(
        "INSERT INTO messages (id, fragment_id, role, content, created_at) "
        "VALUES ('msg_int_1', NULL, 'user', '互动整合前的聊天原文', ?)",
        (TS,),
    )
    conn.execute(
        "INSERT INTO knowledge (id, category, state, content, node_ids, created_at, updated_at) "
        "VALUES ('kn_int_1', 'general_fact', 'active', '记忆：不能丢的知识', '[]', ?, ?)",
        (TS, TS),
    )


def _seed_attachments(conn: sqlite3.Connection) -> None:
    """附件（31 号起才有表）：升级路径上补种。"""
    conn.execute(
        "INSERT INTO attachments (id, message_id, turn_id, topic_id, kind, original_name, "
        "stored_path, source_path, size_bytes, mtime, sha256, state, error, created_at, "
        "updated_at, source_attachment_id) VALUES "
        "('att_int_1', 'msg_int_1', NULL, NULL, 'copy', '材料.txt', "
        "'D:/qio-data/attachments/材料.bin', NULL, 1024, 1.0, 'sha-int', 'ready', NULL, ?, ?, NULL)",
        (TS, TS),
    )


def _seed_interactive(conn: sqlite3.Connection) -> None:
    """互动数据：板面 / 最新状态 / 快照 / 草稿 / 提交 / 待提交 / 意图。"""
    conn.execute(
        "INSERT INTO boards (id, title, created_at, updated_at, meta) "
        "VALUES ('board_int_1', '整合验收板', ?, ?, '{}')",
        (TS, TS),
    )
    conn.execute(
        "INSERT INTO board_states (board_id, seq, state, reason, updated_at) "
        "VALUES ('board_int_1', 3, '{\"cards\":[]}', 'op', ?)",
        (TS,),
    )
    conn.execute(
        "INSERT INTO board_state_snapshots (id, board_id, seq, state, reason, created_at) "
        "VALUES ('snap_int_1', 'board_int_1', 2, '{\"cards\":[]}', 'op', ?)",
        (TS,),
    )
    conn.execute(
        "INSERT INTO board_drafts (board_id, drafts, updated_at) "
        "VALUES ('board_int_1', '{\"card:1\":\"草稿原文\"}', ?)",
        (TS,),
    )
    conn.execute(
        "INSERT INTO board_submissions (id, board_id, seq, status, before_state, after_state, "
        "visible, expressions, baseline, delivery, content_hash, error, created_at) "
        "VALUES ('sub_int_1', 'board_int_1', 1, 'applied', '{}', '{}', '{}', '[]', '{}', '{}', "
        "'hash-int', NULL, ?)",
        (TS,),
    )
    conn.execute(
        "INSERT INTO board_pending (board_id, baseline_seq, expressions, updated_at) "
        "VALUES ('board_int_1', 3, '[]', ?)",
        (TS,),
    )
    conn.execute(
        "INSERT INTO board_intents (id, board_id, submission_id, title, summary, status, preview, "
        "impact, depends_on, conflicts_with, conflict_key, material_refs, progress, applied, "
        "revert, reason, demo, created_at, updated_at) "
        "VALUES ('intent_int_1', 'board_int_1', 'sub_int_1', '整理材料', '', 'applied', '{}', "
        "'{}', '[]', '[]', '', '[]', '{}', '{}', '{}', '', 0, ?, ?)",
        (TS, TS),
    )


def _assert_interactive_intact(conn: sqlite3.Connection) -> None:
    assert _count(conn, "boards") == 1
    row = conn.execute("SELECT title FROM boards WHERE id='board_int_1'").fetchone()
    assert row["title"] == "整合验收板"
    assert conn.execute(
        "SELECT state FROM board_states WHERE board_id='board_int_1'"
    ).fetchone()["state"] == '{"cards":[]}'
    assert _count(conn, "board_state_snapshots") == 1
    assert _count(conn, "board_drafts") == 1
    assert _count(conn, "board_submissions") == 1
    assert _count(conn, "board_pending") == 1
    assert _count(conn, "board_intents") == 1


def _assert_chat_and_memory_intact(conn: sqlite3.Connection) -> None:
    assert conn.execute(
        "SELECT content FROM messages WHERE id='msg_int_1'"
    ).fetchone()["content"] == "互动整合前的聊天原文"
    assert conn.execute(
        "SELECT content FROM knowledge WHERE id='kn_int_1'"
    ).fetchone()["content"] == "记忆：不能丢的知识"


def _assert_interactive_objects_present(conn: sqlite3.Connection) -> None:
    missing = missing_objects(conn)
    assert not any(missing.values()), missing
    tables, indexes = _tables(conn), _indexes(conn)
    for name in INTERACTIVE_TABLES:
        assert name in tables, name
    for name in INTERACTIVE_INDEXES:
        assert name in indexes, name


@pytest.fixture()
def conn(tmp_path: Path):
    connection = connect(tmp_path / "interactive_integration.db")
    yield connection
    connection.close()


# ---------------------------------------------------------------------------
# 1. 空数据库
# ---------------------------------------------------------------------------


def test_fresh_database_reaches_36_with_interactive_objects(conn):
    assert apply_migrations(conn) == SCHEMA_VERSION == COMPENSATION_VERSION == 36
    _assert_interactive_objects_present(conn)
    # 建出来的互动表能真的承载写入
    _seed_interactive(conn)
    _assert_interactive_intact(conn)


# ---------------------------------------------------------------------------
# 2. main 已有库（记到 34）
# ---------------------------------------------------------------------------


def test_main_database_upgrades_and_keeps_chat_attachment_memory(conn):
    _build_through(conn, MAIN_VERSION)
    assert current_version(conn) == MAIN_VERSION
    assert "boards" not in _tables(conn), "场景前提：并入前 main 的库里没有互动表"
    _seed_chat_and_memory(conn)
    _seed_attachments(conn)

    assert apply_migrations(conn) == 36
    _assert_interactive_objects_present(conn)
    _assert_chat_and_memory_intact(conn)
    assert _count(conn, "attachments") == 1
    assert conn.execute(
        "SELECT original_name FROM attachments WHERE id='att_int_1'"
    ).fetchone()["original_name"] == "材料.txt"

    # 升级后再写互动数据：幂等重跑不动它
    _seed_interactive(conn)
    rows = _version_rows(conn)
    assert apply_migrations(conn) == 36
    assert _version_rows(conn) == rows
    _assert_interactive_intact(conn)
    _assert_chat_and_memory_intact(conn)


# ---------------------------------------------------------------------------
# 3. 互动来源线已有的库（互动建表记在 26）
# ---------------------------------------------------------------------------


def test_source_line_database_keeps_interactive_rows(conn):
    _build_through(conn, 25)
    _build_interactive_shape(conn)
    _seed_chat_and_memory(conn)
    _seed_interactive(conn)
    assert current_version(conn) == SOURCE_VERSION
    assert "boards" in _tables(conn)

    assert apply_migrations(conn) == 36
    _assert_interactive_objects_present(conn)
    # 互动线自己的数据一行不丢；聊天 / 记忆同样不动
    _assert_interactive_intact(conn)
    _assert_chat_and_memory_intact(conn)
    # 附件表是 main 那条线带来的：升级路径上照样建出来
    assert "attachments" in _tables(conn)

    rows = _version_rows(conn)
    assert apply_migrations(conn) == 36
    assert _version_rows(conn) == rows, "重复启动不得重复写版本行"
    _assert_interactive_intact(conn)


# ---------------------------------------------------------------------------
# 4. 版本号已推进到 36、互动对象却缺失
# ---------------------------------------------------------------------------


def test_version_36_recorded_but_interactive_objects_lost_is_compensated(conn):
    assert apply_migrations(conn) == 36
    _seed_interactive(conn)
    conn.execute("DROP TABLE board_intents")
    conn.execute("DROP INDEX idx_board_snapshots_board")
    missing = missing_objects(conn)
    assert "board_intents" in missing["tables"], missing
    assert "idx_board_snapshots_board" in missing["indexes"], missing

    rows = _version_rows(conn)
    assert apply_migrations(conn) == COMPENSATION_VERSION == 36
    _assert_interactive_objects_present(conn)
    assert _version_rows(conn) == rows, "补偿只补对象，不改版本行"
    # 其它互动数据不受影响（只补被删掉的那张表与索引）
    assert _count(conn, "boards") == 1 and _count(conn, "board_states") == 1

    # 补出来的表可用
    conn.execute(
        "INSERT INTO board_intents (id, board_id, title, status, created_at, updated_at) "
        "VALUES ('intent_int_2', 'board_int_1', '补齐后的意图', 'pending', ?, ?)",
        (TS, TS),
    )
    assert _count(conn, "board_intents") == 1


# ---------------------------------------------------------------------------
# 5. 重复启动幂等（结构与内容都不动）
# ---------------------------------------------------------------------------


def test_repeated_start_is_idempotent(conn):
    _build_through(conn, MAIN_VERSION)
    _seed_chat_and_memory(conn)
    _seed_attachments(conn)
    apply_migrations(conn)
    _seed_interactive(conn)

    version = current_version(conn)
    tables = sorted(_tables(conn))
    indexes = sorted(_indexes(conn))
    rows = _version_rows(conn)
    for _ in range(3):
        assert apply_migrations(conn) == version
    assert sorted(_tables(conn)) == tables
    assert sorted(_indexes(conn)) == indexes
    assert _version_rows(conn) == rows
    _assert_interactive_intact(conn)
    _assert_chat_and_memory_intact(conn)
    assert _count(conn, "attachments") == 1
