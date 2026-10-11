# -*- coding: utf-8 -*-
"""迁移 39（提交身份）+ 补偿 40 的定向验收（子智能体 B，整合轮 2026-10-11）。

契约：docs/integration-contract-20261011.md §1.2 / §4。只验这一批对象：

    迁移 39 给 board_submissions 追加 client_request_id TEXT NOT NULL DEFAULT ''，
    并加**部分唯一索引** idx_board_submissions_client_request(board_id, client_request_id)
    （client_request_id <> '' 才参与，空串 = 旧客户端不参与幂等）；
    补偿入口切到 40（= 38 的全部语句 + 39 的新对象），REQUIRED_OBJECTS 同步列新列与新索引。

本文件只验这五件事（不跑全量）：
    1. 空库：一次跑到 40，新列与新索引都在、列可写、索引能真的挡住重复身份；
    2. main 历史库（记到 34）升级：互动表建出、新列在、聊天 / 附件 / 记忆一行不丢；
    3. 版本号已到 39、新对象却丢了：按对象复核 → 重放补偿 40 → 补齐，版本行不变，
       历史提交行与用户内容（草稿 / 板面 / 提交事实）一行不丢；
    4. 补偿只补对象：重放之后重复身份仍然被唯一索引挡住（唯一性不因补偿丢失）；
    5. 重复启动幂等：结构与内容都不再变化。

运行：cd backend; uv run --frozen pytest tests/test_irm_b_migration_39.py -q
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
    verify_required_objects,
)
from agent.storage.schema import (
    COMPENSATION_VERSION,
    MIGRATIONS,
    SCHEMA_VERSION,
    _INTERACTIVE_DDL,
    _INTERACTIVE_REAL_MODEL_DDL,
    _SUBMISSION_IDENTITY_DDL,
)

TS = "2026-10-11T00:00:00+00:00"
MAIN_VERSION = 34          # 并入前 main 的实际最大版本
PREVIOUS_VERSION = 38      # 本轮之前的最新迁移号（真实模型线）
NEW_VERSION = 39           # 本轮新增：提交身份
IDENTITY_COLUMN = "client_request_id"
IDENTITY_INDEX = "idx_board_submissions_client_request"
BOARD = "board_m39"
_ALREADY = ("duplicate column name", "already exists")


# --- 工具 -----------------------------------------------------------------


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


def _indexes(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index' AND name IS NOT NULL")
    }


def _index_sql(conn: sqlite3.Connection, name: str) -> str:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' AND name = ?", (name,)
    ).fetchone()
    return str(row[0]) if row is not None else ""


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")]


def _version_rows(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) AS n FROM schema_version").fetchone()["n"])


def _count(conn: sqlite3.Connection, table: str) -> int:
    return int(conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"])


def _seed_user_content(conn: sqlite3.Connection) -> None:
    """聊天 / 记忆 / 附件：升级与补偿都不许动它。"""
    conn.execute(
        "INSERT INTO messages (id, fragment_id, role, content, created_at) "
        "VALUES ('msg_m39', NULL, 'user', '迁移前的聊天原文', ?)",
        (TS,),
    )
    conn.execute(
        "INSERT INTO knowledge (id, category, state, content, node_ids, created_at, updated_at) "
        "VALUES ('kn_m39', 'general_fact', 'active', '迁移前的知识', '[]', ?, ?)",
        (TS, TS),
    )
    conn.execute(
        "INSERT INTO attachments (id, message_id, turn_id, topic_id, kind, original_name, "
        "stored_path, source_path, size_bytes, mtime, sha256, state, error, created_at, "
        "updated_at, source_attachment_id) VALUES "
        "('att_m39', 'msg_m39', NULL, NULL, 'copy', '材料.txt', "
        "'D:/qio-data/attachments/x.bin', NULL, 8, 1.0, 'sha-m39', 'ready', NULL, ?, ?, NULL)",
        (TS, TS),
    )


def _seed_interactive(conn: sqlite3.Connection, *, client_request_id: str | None = "crid-old-1") -> None:
    """互动数据：板面 / 快照 / 草稿 / 提交 / 意图。

    client_request_id=None 表示这个库还没跑迁移 39（列不存在）：按 38 版的列集写。
    """
    conn.execute(
        "INSERT INTO boards (id, title, created_at, updated_at, meta) "
        "VALUES (?, '迁移 39 验收板', ?, ?, '{}')",
        (BOARD, TS, TS),
    )
    conn.execute(
        "INSERT INTO board_states (board_id, seq, state, reason, updated_at) "
        "VALUES (?, 4, '{\"cards\":[{\"id\":\"c1\"}]}', 'op', ?)",
        (BOARD, TS),
    )
    conn.execute(
        "INSERT INTO board_state_snapshots (id, board_id, seq, state, reason, created_at) "
        "VALUES ('snap_m39', ?, 4, '{\"cards\":[]}', 'op', ?)",
        (BOARD, TS),
    )
    conn.execute(
        "INSERT INTO board_drafts (board_id, drafts, updated_at) VALUES (?, '{\"c1\":\"草稿原文\"}', ?)",
        (BOARD, TS),
    )
    columns = (
        "id, board_id, seq, status, before_state, after_state, visible, expressions, baseline,"
        " delivery, content_hash, error, created_at"
    )
    values: tuple = (BOARD, TS)
    if client_request_id is not None:
        columns += ", " + IDENTITY_COLUMN
        values = (BOARD, TS, client_request_id)
    conn.execute(
        f"INSERT INTO board_submissions ({columns}) VALUES "
        "('sub_m39_1', ?, 1, 'succeeded', '{}', '{\"seq\":3}', '{}', '[]', '{}',"
        " '{\"delivered\":true}', 'hash-m39', NULL, ?"
        + (", ?" if client_request_id is not None else "")
        + ")",
        values,
    )
    conn.execute(
        "INSERT INTO board_intents (id, board_id, submission_id, title, summary, status, preview, "
        "impact, depends_on, conflicts_with, conflict_key, material_refs, progress, applied, "
        "revert, reason, demo, created_at, updated_at) VALUES "
        "('intent_m39', ?, 'sub_m39_1', '整理材料', '', 'applied', '{}', '{}', '[]', '[]', '', "
        "'[]', '{}', '{}', '{}', '', 0, ?, ?)",
        (BOARD, TS, TS),
    )


def _insert_identity(conn: sqlite3.Connection, submission_id: str, client_request_id: str, seq: int):
    conn.execute(
        "INSERT INTO board_submissions (id, board_id, seq, status, before_state, after_state, "
        "visible, expressions, baseline, delivery, content_hash, error, created_at, "
        + IDENTITY_COLUMN
        + ") VALUES (?, ?, ?, 'succeeded', '{}', '{}', '{}', '[]', '{}', '{}', '', NULL, ?, ?)",
        (submission_id, BOARD, seq, TS, client_request_id),
    )


def _assert_user_content_intact(conn: sqlite3.Connection) -> None:
    assert conn.execute("SELECT content FROM messages WHERE id='msg_m39'").fetchone()["content"] == "迁移前的聊天原文"
    assert conn.execute("SELECT content FROM knowledge WHERE id='kn_m39'").fetchone()["content"] == "迁移前的知识"
    assert conn.execute(
        "SELECT original_name FROM attachments WHERE id='att_m39'"
    ).fetchone()["original_name"] == "材料.txt"
    assert conn.execute(
        "SELECT title FROM boards WHERE id=?", (BOARD,)
    ).fetchone()["title"] == "迁移 39 验收板"


def _assert_interactive_intact(conn: sqlite3.Connection) -> None:
    assert _count(conn, "board_submissions") == 1
    row = conn.execute(
        "SELECT status, delivery, client_request_id FROM board_submissions WHERE id='sub_m39_1'"
    ).fetchone()
    assert row["status"] == "succeeded"
    assert "delivered" in str(row["delivery"])
    assert conn.execute("SELECT drafts FROM board_drafts WHERE board_id=?", (BOARD,)).fetchone()[
        "drafts"
    ] == '{"c1":"草稿原文"}'
    assert _count(conn, "board_state_snapshots") == 1
    assert _count(conn, "board_intents") == 1


@pytest.fixture()
def conn(tmp_path: Path):
    connection = connect(tmp_path / "irm_b_m39.db")
    yield connection
    connection.close()


# --- 1. 迁移定义本身 -------------------------------------------------------


def test_identity_migration_is_numbered_39_and_compensation_is_40():
    numbers = [version for version, _ in MIGRATIONS]
    assert numbers == sorted(numbers), "迁移必须按编号有序追加"
    assert len(numbers) == len(set(numbers)), "迁移号不得重复"
    assert NEW_VERSION == 39, "契约 §4：本轮新增迁移号固定 39"
    assert COMPENSATION_VERSION == 40, "契约 §4：补偿入口固定 40"
    assert SCHEMA_VERSION == COMPENSATION_VERSION
    identity_sql = " ".join(_SUBMISSION_IDENTITY_DDL)
    assert "client_request_id" in identity_sql
    assert IDENTITY_INDEX in identity_sql
    assert "client_submissions" not in identity_sql


def test_compensation_40_is_38_plus_39_objects():
    by_number = dict(MIGRATIONS)
    assert 38 in by_number and 40 in by_number
    expected = list(by_number[38]) + list(_SUBMISSION_IDENTITY_DDL)
    assert by_number[40] == expected, "补偿 40 = 38 全部语句 + 39 新对象"


def test_historical_migrations_37_38_are_untouched():
    by_number = dict(MIGRATIONS)
    assert by_number[37] == list(_INTERACTIVE_REAL_MODEL_DDL)
    interactive_sql = " ".join(by_number[35])
    assert "client_request_id" not in interactive_sql, "历史迁移 35 原样保留"
    assert 35 in by_number and 36 in by_number


# --- 2. 空库 ---------------------------------------------------------------


def test_fresh_database_reaches_39_with_identity_objects(conn):
    assert apply_migrations(conn) == SCHEMA_VERSION == COMPENSATION_VERSION == 40
    assert IDENTITY_COLUMN in _columns(conn, "board_submissions")
    assert IDENTITY_INDEX in _indexes(conn)
    assert not any(missing_objects(conn).values())
    assert ("board_submissions." + IDENTITY_COLUMN) in REQUIRED_OBJECTS["columns"]
    assert IDENTITY_INDEX in REQUIRED_OBJECTS["indexes"]

    _seed_user_content(conn)
    _seed_interactive(conn)
    _assert_interactive_intact(conn)
    _assert_user_content_intact(conn)


def test_unique_index_blocks_duplicate_identity_but_allows_empty(conn):
    apply_migrations(conn)
    sql = _index_sql(conn, IDENTITY_INDEX)
    assert "UNIQUE" in sql.upper()
    assert IDENTITY_COLUMN in sql
    assert "board_id" in sql
    assert "<>" in sql or "!=" in sql, "必须是部分索引：空串不参与"
    conn.execute(
        "INSERT INTO boards (id, title, created_at, updated_at, meta) VALUES (?, '板', ?, ?, '{}')",
        (BOARD, TS, TS),
    )
    _insert_identity(conn, "sub_a", "crid-1", 1)
    with pytest.raises(sqlite3.IntegrityError):
        _insert_identity(conn, "sub_b", "crid-1", 2)
    # 同一块板面、空串身份可以有多行（旧客户端不参与幂等）
    _insert_identity(conn, "sub_c", "", 2)
    _insert_identity(conn, "sub_d", "", 3)
    # 另一块板面的同一身份互不干扰
    conn.execute(
        "INSERT INTO boards (id, title, created_at, updated_at, meta) VALUES ('board_m39_other', '板2', ?, ?, '{}')",
        (TS, TS),
    )
    conn.execute(
        "INSERT INTO board_submissions (id, board_id, seq, status, before_state, after_state,"
        " visible, expressions, baseline, delivery, content_hash, error, created_at,"
        " client_request_id) VALUES ('sub_e', 'board_m39_other', 1, 'succeeded', '{}', '{}',"
        " '{}', '[]', '{}', '{}', '', NULL, ?, 'crid-1')",
        (TS,),
    )
    assert _count(conn, "board_submissions") == 4


# --- 3. main 历史库升级 ----------------------------------------------------


def test_main_database_upgrades_to_39_and_keeps_user_content(conn):
    _build_through(conn, MAIN_VERSION)
    assert current_version(conn) == MAIN_VERSION
    tables = {str(r[0]) for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "board_submissions" not in tables, "场景前提：main 的 34 版还没有互动表"
    _seed_user_content(conn)

    assert apply_migrations(conn) == 40
    assert current_version(conn) == 40
    assert IDENTITY_COLUMN in _columns(conn, "board_submissions")
    assert IDENTITY_INDEX in _indexes(conn)
    # 互动表是升级路径上才建出来的：先种互动数据，再一起核对内容
    _seed_interactive(conn)
    _assert_interactive_intact(conn)
    _assert_user_content_intact(conn)


def test_upgrade_from_38_adds_column_with_empty_default(conn):
    _build_through(conn, PREVIOUS_VERSION)
    assert current_version(conn) == PREVIOUS_VERSION
    _seed_user_content(conn)
    _seed_interactive(conn, client_request_id=None)  # 38 版：还没有这一列
    assert IDENTITY_COLUMN not in _columns(conn, "board_submissions")

    assert apply_migrations(conn) == 40
    assert IDENTITY_COLUMN in _columns(conn, "board_submissions")
    row = conn.execute(
        "SELECT client_request_id FROM board_submissions WHERE id='sub_m39_1'"
    ).fetchone()
    assert row[IDENTITY_COLUMN] == "", "历史行的新列必须是空串（旧客户端语义）"
    _assert_interactive_intact(conn)
    _assert_user_content_intact(conn)


# --- 4. 缺对象补偿 ---------------------------------------------------------


def test_missing_index_is_detected_and_compensated(conn):
    apply_migrations(conn)
    _seed_interactive(conn)
    conn.execute(f"DROP INDEX {IDENTITY_INDEX}")
    missing = missing_objects(conn)
    assert IDENTITY_INDEX in missing["indexes"], missing

    rows = _version_rows(conn)
    assert apply_migrations(conn) == 40
    assert IDENTITY_INDEX in _indexes(conn)
    assert _version_rows(conn) == rows, "补偿只补对象，不改版本行"
    _assert_interactive_intact(conn)
    # 补出来的索引仍然挡住重复身份
    with pytest.raises(sqlite3.IntegrityError):
        _insert_identity(conn, "sub_dup", "crid-old-1", 2)


def test_missing_column_is_detected_and_compensated(conn):
    apply_migrations(conn)
    _seed_interactive(conn)
    conn.execute(f"DROP INDEX {IDENTITY_INDEX}")
    conn.execute("ALTER TABLE board_submissions DROP COLUMN " + IDENTITY_COLUMN)
    assert IDENTITY_COLUMN not in _columns(conn, "board_submissions")
    missing = missing_objects(conn)
    assert ("board_submissions." + IDENTITY_COLUMN) in missing["columns"], missing

    rows = _version_rows(conn)
    remaining = verify_required_objects(conn)  # 缺 → 重放补偿 40
    assert not any(remaining.values()), remaining
    assert IDENTITY_COLUMN in _columns(conn, "board_submissions")
    assert IDENTITY_INDEX in _indexes(conn)
    assert _version_rows(conn) == rows
    _assert_interactive_intact(conn)
    assert conn.execute(
        "SELECT client_request_id FROM board_submissions WHERE id='sub_m39_1'"
    ).fetchone()[IDENTITY_COLUMN] == "", "补偿不得动历史行：补出来的列是空串"


def test_high_version_database_missing_identity_objects_is_compensated(conn):
    """版本号已到 39（甚至更高）但新对象全丢：按对象复核必须补回来。"""
    _build_through(conn, NEW_VERSION)
    _seed_interactive(conn)
    conn.execute(f"DROP INDEX {IDENTITY_INDEX}")
    conn.execute("ALTER TABLE board_submissions DROP COLUMN " + IDENTITY_COLUMN)
    assert current_version(conn) == NEW_VERSION
    missing = missing_objects(conn)
    assert ("board_submissions." + IDENTITY_COLUMN) in missing["columns"], missing
    assert IDENTITY_INDEX in missing["indexes"], missing

    # 版本号已到 39：迁移 39 本身不会再跑；缺的对象由补偿 40 补回（40 也是 MIGRATIONS 里的一条）
    remaining = verify_required_objects(conn)
    assert not any(remaining.values()), remaining
    assert IDENTITY_COLUMN in _columns(conn, "board_submissions")
    assert IDENTITY_INDEX in _indexes(conn)
    assert apply_migrations(conn) == COMPENSATION_VERSION, "缺对象时应由补偿路径补齐"
    assert not any(missing_objects(conn).values())
    version = current_version(conn)
    rows = _version_rows(conn)
    assert apply_migrations(conn) == version
    assert _version_rows(conn) == rows, "重复启动不得重复写版本行"
    _assert_interactive_intact(conn)


# --- 5. 重复启动幂等 -------------------------------------------------------


def test_repeated_start_is_idempotent(conn):
    _build_through(conn, MAIN_VERSION)
    _seed_user_content(conn)
    apply_migrations(conn)
    _seed_interactive(conn)

    version = current_version(conn)
    indexes = sorted(_indexes(conn))
    columns = sorted(_columns(conn, "board_submissions"))
    rows = _version_rows(conn)
    for _ in range(3):
        assert apply_migrations(conn) == version
    assert sorted(_indexes(conn)) == indexes
    assert sorted(_columns(conn, "board_submissions")) == columns
    assert _version_rows(conn) == rows
    _assert_interactive_intact(conn)
    _assert_user_content_intact(conn)
