# -*- coding: utf-8 -*-
"""合并收尾验收（2026-10-11）：附件对象与轮次结束事实必须进结构完整性检查。

原缺陷（main `7ff5e2e`）

    `REQUIRED_OBJECTS` 只列了 A05/B01 那一批对象（instances / record_owners /
    归属列 / 版本链列 / 修订列）。补偿迁移 34 已经把 attachments 与轮次结束事实
    （reason_code / stopped_by / actions）一起补齐，但**清单里没有它们**。于是
    在已经升到 34 的库里把 `attachments` 整张删掉再执行迁移：

        * `missing_objects()` 报空；
        * 迁移报「结构完整」；
        * 表不会被补建。

    这条检查对「本线新增的那半个 schema」等于不存在。

本文件钉住四件事

    1. 清单覆盖 attachments 表 / 附件来源列 / 三个附件索引 / 结束事实三列，
       且每个对象在补偿迁移 34 里都有对应的建对象语句；
    2. 版本号已到 34、但对象分别缺失（整表 / 列 / 索引）时，检查能发现并补齐；
    3. 补偿补不齐时明确失败（`SchemaIncompleteError`），绝不返回「完整」；
    4. 四种升级起点（全新 / 旧 main 25 / main 30 / 来源流式线 28）都能升到 34，
       且**真实业务表**里的合成数据一行都不丢。

数据保护用真实业务行（messages / attachments / turn_journal / knowledge /
instances / record_owners），不用额外标记表代替。
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import pytest

from agent.storage.db import connect, transaction
from agent.storage.migrate import (
    REQUIRED_OBJECTS,
    SchemaIncompleteError,
    apply_migrations,
    current_version,
    missing_objects,
    verify_required_objects,
)
from agent.storage.schema import COMPENSATION_VERSION, MIGRATIONS, SCHEMA_VERSION

TS = "2026-10-11T00:00:00+00:00"
OLD_MAIN_VERSION = 25
MAIN_VERSION = 30          # 最新 main 的迁移号（29/30 是它的对象）
SOURCE_LINE_VERSION = 28   # 来源流式线自己的迁移号（26/27/28）
ATTACHMENT_COLUMN = "attachments.source_attachment_id"
TURN_FACT_COLUMNS = (
    "turn_journal.reason_code",
    "turn_journal.stopped_by",
    "turn_journal.actions",
)
ATTACHMENT_INDEXES = (
    "idx_attachments_turn",
    "idx_attachments_message",
    "idx_attachments_topic",
)

# 只有 SQLite >= 3.35 才有 ALTER TABLE ... DROP COLUMN。本机与 CI（uv 带的
# python-build-standalone 3.11/3.12）都远高于这个版本；真遇到老库就跳过那条
# **单列**反例（另外两条「半截迁移」构造不依赖 DROP COLUMN，照跑）。
_HAS_DROP_COLUMN = sqlite3.sqlite_version_info >= (3, 35, 0)


# ---------------------------------------------------------------------------
# 构造工具：一律用真实迁移定义建库
# ---------------------------------------------------------------------------


def _ensure_version_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_version ("
        " id INTEGER PRIMARY KEY, version INTEGER NOT NULL, applied_at TEXT NOT NULL)"
    )


_ALREADY_APPLIED = ("duplicate column name", "already exists")


def _exec_tolerating_replay(conn: sqlite3.Connection, stmt: str) -> None:
    """执行一条迁移语句；对象已存在（补偿迁移 34 的冗余重放）就跳过。

    真实的 `apply_migrations` 会在发语句之前先查列；这里只按结果容忍
    「已存在」，语义相同、装置更短。
    """
    try:
        conn.execute(stmt)
    except sqlite3.OperationalError as exc:
        if not any(token in str(exc).lower() for token in _ALREADY_APPLIED):
            raise


def _build_through(conn: sqlite3.Connection, target: int) -> None:
    """按真实迁移定义把库建到 target 版（含版本行），只到那里。"""
    _ensure_version_table(conn)
    for version, statements in MIGRATIONS:
        if version > target:
            continue
        with transaction(conn):
            for stmt in statements:
                _exec_tolerating_replay(conn, stmt)
            conn.execute(
                "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)", (version, TS)
            )


def _mark_versions(conn: sqlite3.Connection, *versions: int) -> None:
    """只记版本号、不跑 DDL：模拟「迁移半截 / 版本号被推到更高」的存量库。"""
    _ensure_version_table(conn)
    for version in versions:
        conn.execute(
            "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)", (version, TS)
        )


def _apply_source_line_shapes(conn: sqlite3.Connection) -> None:
    """把来源流式线在 26/27/28 上做过的三件事按**真实形状**做出来。

    这三条 SQL 与集成后的迁移 31/32/33 逐字一致（只是编号不同）—— 用来构造
    「流式线自己的存量库」这个升级起点。
    """
    _ensure_version_table(conn)
    with transaction(conn):
        for stmt in (
            "CREATE TABLE IF NOT EXISTS attachments ("
            " id TEXT PRIMARY KEY, message_id TEXT, turn_id TEXT, topic_id TEXT,"
            " kind TEXT NOT NULL, original_name TEXT NOT NULL, stored_path TEXT,"
            " source_path TEXT, size_bytes INTEGER NOT NULL DEFAULT 0, mtime REAL,"
            " sha256 TEXT, state TEXT NOT NULL, error TEXT, created_at TEXT NOT NULL,"
            " updated_at TEXT NOT NULL)",
            "CREATE INDEX IF NOT EXISTS idx_attachments_turn ON attachments(turn_id)",
            "CREATE INDEX IF NOT EXISTS idx_attachments_message ON attachments(message_id)",
            "CREATE INDEX IF NOT EXISTS idx_attachments_topic ON attachments(topic_id, state)",
        ):
            conn.execute(stmt)
        conn.execute("INSERT INTO schema_version (version, applied_at) VALUES (?, ?)", (26, TS))
        conn.execute("ALTER TABLE attachments ADD COLUMN source_attachment_id TEXT")
        conn.execute("INSERT INTO schema_version (version, applied_at) VALUES (?, ?)", (27, TS))
        for column in ("reason_code", "stopped_by", "actions"):
            conn.execute(f"ALTER TABLE turn_journal ADD COLUMN {column} TEXT")
        conn.execute("INSERT INTO schema_version (version, applied_at) VALUES (?, ?)", (28, TS))


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {str(r[0]) for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _indexes(conn: sqlite3.Connection) -> set[str]:
    return {
        str(r[0])
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name IS NOT NULL"
        )
    }


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(r[1]) for r in conn.execute(f"PRAGMA table_info({table})")}


def _count(conn: sqlite3.Connection, table: str) -> int:
    return int(conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"])


def _version_rows(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) AS n FROM schema_version").fetchone()["n"])


# ---------------------------------------------------------------------------
# 合成业务数据（真实表，不是标记表）
# ---------------------------------------------------------------------------


def _seed_history(conn: sqlite3.Connection, *, with_facts: bool) -> None:
    """用户消息 / 知识 / 轮次台账。`with_facts` 只在结束事实三列存在时可为真。"""
    conn.execute(
        "INSERT INTO messages (id, fragment_id, role, content, created_at) "
        "VALUES ('msg_closeout_1', NULL, 'user', '用户消息：这条不能被迁移弄丢', ?)",
        (TS,),
    )
    conn.execute(
        "INSERT INTO messages (id, fragment_id, role, content, created_at) "
        "VALUES ('msg_closeout_2', NULL, 'assistant', '助手消息：同样不能丢', ?)",
        (TS,),
    )
    conn.execute(
        "INSERT INTO knowledge (id, category, state, content, node_ids, created_at, updated_at) "
        "VALUES ('kn_closeout', 'general_fact', 'active', '人工写下的知识', '[]', ?, ?)",
        (TS, TS),
    )
    if with_facts:
        conn.execute(
            "INSERT INTO turn_journal (turn_id, message, topic_id, notify, status, created_at, "
            "updated_at, reason_code, stopped_by, actions) "
            "VALUES ('turn_closeout_1', '收尾验收轮', NULL, 0, 'interrupted', ?, ?, "
            "'interrupted', 'user', '[\"retry\"]')",
            (TS, TS),
        )
    else:
        conn.execute(
            "INSERT INTO turn_journal (turn_id, message, topic_id, notify, status, created_at, "
            "updated_at) VALUES ('turn_closeout_1', '收尾验收轮', NULL, 0, 'interrupted', ?, ?)",
            (TS, TS),
        )


def _seed_ownership(conn: sqlite3.Connection) -> None:
    """实例归属数据（main 迁移 29 的对象）。"""
    conn.execute(
        "INSERT INTO instances (instance_id, pid, host, started_at, last_heartbeat) "
        "VALUES ('inst_closeout', 4242, 'host-under-test', ?, ?)",
        (TS, TS),
    )
    conn.execute(
        "INSERT INTO record_owners (record_type, record_id, instance_id) "
        "VALUES ('turn', 'turn_closeout_1', 'inst_closeout')"
    )


def _seed_attachments(conn: sqlite3.Connection, *, with_source: bool) -> None:
    """附件记录（含复用来源列）：证明升级/补偿保留已登记的附件。"""
    source = "'att_closeout_1'" if with_source else "NULL"
    conn.execute(
        "INSERT INTO attachments (id, message_id, turn_id, topic_id, kind, original_name, "
        "stored_path, source_path, size_bytes, mtime, sha256, state, error, created_at, "
        "updated_at, source_attachment_id) VALUES "
        "('att_closeout_1', 'msg_closeout_2', 'turn_closeout_1', NULL, 'copy', '报告.txt', "
        "'D:/qio-data/attachments/2026/10/report.bin', NULL, 4096, 17.5, 'sha-合成', 'ready', "
        f"NULL, ?, ?, {source})",
        (TS, TS),
    )
    conn.execute(
        "INSERT INTO attachments (id, message_id, turn_id, topic_id, kind, original_name, "
        "stored_path, source_path, size_bytes, mtime, sha256, state, error, created_at, "
        "updated_at, source_attachment_id) VALUES "
        "('att_closeout_2', 'msg_closeout_2', 'turn_closeout_1', NULL, 'reference', '大文件.bin', "
        "NULL, 'D:/users/big-file.bin', 268435456, 3.25, 'sha-引用', 'ready', NULL, ?, ?, "
        f"{source})",
        (TS, TS),
    )


def _assert_history_intact(conn: sqlite3.Connection, *, facts: bool) -> None:
    assert _count(conn, "messages") == 2
    assert (
        conn.execute("SELECT content FROM messages WHERE id='msg_closeout_1'").fetchone()["content"]
        == "用户消息：这条不能被迁移弄丢"
    )
    assert (
        conn.execute("SELECT content FROM messages WHERE id='msg_closeout_2'").fetchone()["content"]
        == "助手消息：同样不能丢"
    )
    assert _count(conn, "knowledge") == 1
    assert (
        conn.execute("SELECT content FROM knowledge WHERE id='kn_closeout'").fetchone()["content"]
        == "人工写下的知识"
    )
    turn = conn.execute(
        "SELECT message, status, reason_code, stopped_by, actions "
        "FROM turn_journal WHERE turn_id='turn_closeout_1'"
    ).fetchone()
    assert turn["message"] == "收尾验收轮" and turn["status"] == "interrupted"
    if facts:
        assert turn["reason_code"] == "interrupted" and turn["stopped_by"] == "user"
        assert turn["actions"] == '[\"retry\"]'
    else:
        # 升级前这些列不存在：补列不许给旧行编造事实
        assert turn["reason_code"] is None and turn["stopped_by"] is None
        assert turn["actions"] is None


def _assert_ownership_intact(conn: sqlite3.Connection) -> None:
    assert _count(conn, "instances") == 1 and _count(conn, "record_owners") == 1
    row = conn.execute(
        "SELECT instance_id, pid, host FROM instances WHERE instance_id='inst_closeout'"
    ).fetchone()
    assert row["pid"] == 4242 and row["host"] == "host-under-test"
    owner = conn.execute(
        "SELECT instance_id FROM record_owners WHERE record_type='turn' "
        "AND record_id='turn_closeout_1'"
    ).fetchone()
    assert owner["instance_id"] == "inst_closeout"


def _assert_attachments_intact(conn: sqlite3.Connection) -> None:
    assert _count(conn, "attachments") == 2
    row = conn.execute(
        "SELECT original_name, state, size_bytes, source_attachment_id, stored_path "
        "FROM attachments WHERE id='att_closeout_1'"
    ).fetchone()
    assert row["original_name"] == "报告.txt" and row["state"] == "ready"
    assert int(row["size_bytes"]) == 4096
    assert row["stored_path"] == "D:/qio-data/attachments/2026/10/report.bin"
    assert row["source_attachment_id"] == "att_closeout_1", "附件复用来源列的数据不许被改"
    ref = conn.execute(
        "SELECT kind, source_path, source_attachment_id FROM attachments WHERE id='att_closeout_2'"
    ).fetchone()
    assert ref["kind"] == "reference" and ref["source_path"] == "D:/users/big-file.bin"
    assert ref["source_attachment_id"] == "att_closeout_1"


def _assert_all_required_present(conn: sqlite3.Connection) -> None:
    missing = missing_objects(conn)
    assert not any(missing.values()), missing
    assert "attachments" in _tables(conn)
    for spec in (ATTACHMENT_COLUMN,) + TURN_FACT_COLUMNS:
        table, _, column = spec.partition(".")
        assert column in _columns(conn, table), spec
    for index in ATTACHMENT_INDEXES:
        assert index in _indexes(conn), index


@pytest.fixture()
def conn(tmp_path: Path):
    connection = connect(tmp_path / "closeout_objects.db")
    yield connection
    connection.close()


# ---------------------------------------------------------------------------
# 1. 清单本身：覆盖集成对象，且与补偿迁移一一对应
# ---------------------------------------------------------------------------


def test_required_objects_cover_attachments_and_turn_facts():
    assert "attachments" in REQUIRED_OBJECTS["tables"]
    for spec in (ATTACHMENT_COLUMN,) + TURN_FACT_COLUMNS:
        assert spec in REQUIRED_OBJECTS["columns"], spec
    for index in ATTACHMENT_INDEXES:
        assert index in REQUIRED_OBJECTS["indexes"], index


def test_required_objects_each_have_a_compensation_statement():
    """清单里的每一项都必须能被补偿迁移 34 建出来（不接受只能发现、补不了的项）。"""
    statements = [
        str(stmt)
        for version, stmts in MIGRATIONS
        if version == COMPENSATION_VERSION
        for stmt in stmts
    ]
    blob = "\n".join(statements)
    for name in REQUIRED_OBJECTS["tables"]:
        assert f"CREATE TABLE IF NOT EXISTS {name}" in blob, name
    for spec in REQUIRED_OBJECTS["columns"]:
        table, _, column = spec.partition(".")
        assert f"ADD COLUMN {column}" in blob, spec
        assert re.search(rf"CREATE TABLE IF NOT EXISTS {table}\b", blob), spec
    for name in REQUIRED_OBJECTS["indexes"]:
        assert name in blob, name


def test_missing_objects_reports_attachments_when_version_is_low(conn):
    """反向对照：只到 25 的库必须把 attachments 那批对象报成「缺失」。"""
    _build_through(conn, OLD_MAIN_VERSION)
    missing = missing_objects(conn)
    assert "attachments" in missing["tables"], missing
    assert ATTACHMENT_COLUMN in missing["columns"], missing
    assert set(ATTACHMENT_INDEXES) <= set(missing["indexes"]), missing
    # 纯检查不许改库
    assert verify_required_objects(conn, compensate=False) == missing
    assert "attachments" not in _tables(conn)


# ---------------------------------------------------------------------------
# 2. 四种升级起点 + 数据保护
# ---------------------------------------------------------------------------


def test_fresh_database_is_complete_and_idempotent(conn):
    assert apply_migrations(conn) == SCHEMA_VERSION
    _assert_all_required_present(conn)
    _seed_history(conn, with_facts=True)
    _seed_ownership(conn)
    _seed_attachments(conn, with_source=True)

    rows, tables = _version_rows(conn), sorted(_tables(conn))
    assert apply_migrations(conn) == SCHEMA_VERSION
    assert _version_rows(conn) == rows and sorted(_tables(conn)) == tables
    _assert_history_intact(conn, facts=True)
    _assert_ownership_intact(conn)
    _assert_attachments_intact(conn)


def test_upgrade_from_old_main_25_keeps_rows_and_builds_attachments(conn):
    _build_through(conn, OLD_MAIN_VERSION)
    _seed_history(conn, with_facts=False)
    assert current_version(conn) == OLD_MAIN_VERSION

    assert apply_migrations(conn) == SCHEMA_VERSION
    _assert_all_required_present(conn)
    _assert_history_intact(conn, facts=False)

    # 升级之后再登记实例归属与附件，再跑一次：幂等且不动这些行
    _seed_ownership(conn)
    _seed_attachments(conn, with_source=True)
    rows = _version_rows(conn)
    assert apply_migrations(conn) == SCHEMA_VERSION
    assert _version_rows(conn) == rows
    _assert_history_intact(conn, facts=False)
    _assert_ownership_intact(conn)
    _assert_attachments_intact(conn)


def test_upgrade_from_main_30_keeps_rows_and_builds_attachments(conn):
    _build_through(conn, MAIN_VERSION)
    _seed_history(conn, with_facts=False)
    _seed_ownership(conn)
    assert current_version(conn) == MAIN_VERSION
    assert "attachments" not in _tables(conn), "场景前提：最新 main 的库里没有附件表"

    assert apply_migrations(conn) == SCHEMA_VERSION
    _assert_all_required_present(conn)
    _assert_history_intact(conn, facts=False)
    _assert_ownership_intact(conn)

    _seed_attachments(conn, with_source=True)
    assert apply_migrations(conn) == SCHEMA_VERSION
    _assert_attachments_intact(conn)
    _assert_history_intact(conn, facts=False)


def test_upgrade_from_source_streaming_28_keeps_existing_attachments(conn):
    """来源流式线自己的库（attachments 与结束事实列已在，缺 main 的 29/30/34 对象）。"""
    _build_through(conn, OLD_MAIN_VERSION)
    _apply_source_line_shapes(conn)
    _seed_history(conn, with_facts=True)
    _seed_attachments(conn, with_source=True)
    assert current_version(conn) == SOURCE_LINE_VERSION

    assert apply_migrations(conn) == SCHEMA_VERSION
    _assert_all_required_present(conn)
    # 升级前的数据一行都不许被动
    _assert_history_intact(conn, facts=True)
    _assert_attachments_intact(conn)
    assert current_version(conn) == SCHEMA_VERSION > SOURCE_LINE_VERSION

    # 29/30 的归属对象这次才被继承：补上数据后再跑，同样幂等
    _seed_ownership(conn)
    rows = _version_rows(conn)
    assert apply_migrations(conn) == SCHEMA_VERSION
    assert _version_rows(conn) == rows
    _assert_ownership_intact(conn)
    _assert_attachments_intact(conn)


# ---------------------------------------------------------------------------
# 3. 版本已到 34、对象分别缺失：发现 + 补齐
# ---------------------------------------------------------------------------


def test_version_34_with_attachments_table_dropped_is_repaired(conn):
    assert apply_migrations(conn) == SCHEMA_VERSION
    _seed_history(conn, with_facts=True)
    _seed_ownership(conn)
    conn.execute("DROP TABLE attachments")
    assert "attachments" not in _tables(conn)

    missing = missing_objects(conn)
    assert "attachments" in missing["tables"], missing
    assert set(ATTACHMENT_INDEXES) <= set(missing["indexes"]), missing

    rows = _version_rows(conn)
    assert apply_migrations(conn) == COMPENSATION_VERSION
    _assert_all_required_present(conn)
    assert _version_rows(conn) == rows, "补偿只补对象，不改版本行"
    # 被删掉的表里的行当然回不来（那是一次真实删除）；其它业务行一行不少
    _assert_history_intact(conn, facts=True)
    _assert_ownership_intact(conn)

    # 补建出来的表能正常承载写入
    _seed_attachments(conn, with_source=True)
    _assert_attachments_intact(conn)


def test_version_34_with_attachment_source_column_missing_is_repaired(conn):
    """半截迁移形状：31 已应用、32/33/34 只记了版本号（与 SQLite 版本无关）。"""
    _build_through(conn, 31)
    _mark_versions(conn, 32, 33, 34)
    _seed_history(conn, with_facts=False)
    assert current_version(conn) == COMPENSATION_VERSION
    assert "attachments" in _tables(conn)
    assert "source_attachment_id" not in _columns(conn, "attachments")

    missing = missing_objects(conn)
    assert ATTACHMENT_COLUMN in missing["columns"], missing
    assert set(TURN_FACT_COLUMNS) <= set(missing["columns"]), missing

    assert apply_migrations(conn) == COMPENSATION_VERSION
    _assert_all_required_present(conn)
    _assert_history_intact(conn, facts=False)


def test_version_34_with_turn_fact_columns_missing_is_repaired(conn):
    """33 只记了版本号：结束事实三列缺失，检查必须发现并补齐。"""
    _build_through(conn, 32)
    _mark_versions(conn, 33, 34)
    _seed_history(conn, with_facts=False)
    assert "source_attachment_id" in _columns(conn, "attachments")

    missing = missing_objects(conn)
    assert set(TURN_FACT_COLUMNS) <= set(missing["columns"]), missing

    assert apply_migrations(conn) == COMPENSATION_VERSION
    _assert_all_required_present(conn)
    _assert_history_intact(conn, facts=False)


@pytest.mark.skipif(not _HAS_DROP_COLUMN, reason="SQLite < 3.35 不支持 DROP COLUMN")
def test_version_34_with_only_the_source_column_dropped_is_repaired(conn):
    """只有附件来源列丢了（表在、索引在）：检查必须精确报出这一列并补回。"""
    assert apply_migrations(conn) == SCHEMA_VERSION
    _seed_history(conn, with_facts=True)
    _seed_attachments(conn, with_source=False)
    conn.execute("ALTER TABLE attachments DROP COLUMN source_attachment_id")
    assert "source_attachment_id" not in _columns(conn, "attachments")

    missing = missing_objects(conn)
    assert missing["columns"] == [ATTACHMENT_COLUMN], missing
    assert missing["tables"] == [] and missing["indexes"] == [], missing

    assert apply_migrations(conn) == COMPENSATION_VERSION
    _assert_all_required_present(conn)
    _assert_history_intact(conn, facts=True)
    # 附件行本身（除新补的列为 NULL 外）不许被动
    assert _count(conn, "attachments") == 2
    row = conn.execute(
        "SELECT original_name, state, size_bytes, stored_path, source_attachment_id "
        "FROM attachments WHERE id='att_closeout_1'"
    ).fetchone()
    assert row["original_name"] == "报告.txt" and row["state"] == "ready"
    assert int(row["size_bytes"]) == 4096
    assert row["stored_path"] == "D:/qio-data/attachments/2026/10/report.bin"
    assert row["source_attachment_id"] is None


def test_version_34_with_attachment_index_missing_is_repaired(conn):
    assert apply_migrations(conn) == SCHEMA_VERSION
    _seed_history(conn, with_facts=True)
    _seed_attachments(conn, with_source=True)
    conn.execute("DROP INDEX idx_attachments_message")
    assert "idx_attachments_message" not in _indexes(conn)

    missing = missing_objects(conn)
    assert missing["indexes"] == ["idx_attachments_message"], missing

    assert apply_migrations(conn) == COMPENSATION_VERSION
    _assert_all_required_present(conn)
    _assert_attachments_intact(conn)
    _assert_history_intact(conn, facts=True)

    # 幂等：补完之后再跑不再改任何东西
    rows, indexes = _version_rows(conn), sorted(_indexes(conn))
    assert apply_migrations(conn) == COMPENSATION_VERSION
    assert _version_rows(conn) == rows and sorted(_indexes(conn)) == indexes


# ---------------------------------------------------------------------------
# 4. 补偿补不齐 → 明确失败（不许返回「完整」）
# ---------------------------------------------------------------------------


def test_compensation_that_cannot_rebuild_fails_loudly(conn):
    """attachments 表在、但形状完全不对：补偿建不了索引 → 必须抛可读错误。"""
    assert apply_migrations(conn) == SCHEMA_VERSION
    _seed_history(conn, with_facts=True)
    _seed_ownership(conn)
    conn.execute("DROP TABLE attachments")
    conn.execute("CREATE TABLE attachments (id TEXT PRIMARY KEY)")

    with pytest.raises(SchemaIncompleteError) as excinfo:
        apply_migrations(conn)

    text = str(excinfo.value)
    assert "索引" in text and "idx_attachments" in text, text
    assert set(excinfo.value.missing["indexes"]) == set(ATTACHMENT_INDEXES), excinfo.value.missing
    # 失败之后复核仍然如实报缺 —— 不返回「完整」
    assert set(missing_objects(conn)["indexes"]) == set(ATTACHMENT_INDEXES)
    # 补偿没有破坏已有数据；那张形状不对的表也还在（不猜、不删）
    _assert_history_intact(conn, facts=True)
    _assert_ownership_intact(conn)
    assert "attachments" in _tables(conn)
