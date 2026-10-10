# -*- coding: utf-8 -*-
"""B01 验收：高版本存量库不能漏对象（W3）。

四种库状态**全部用真实迁移定义构造**（不是把版本号改高就完事）：

1. 只到 25（基线 main 的版本）；
2. 25 + 「其它分支形状的 26–28」；
3. 版本已记到 29、但本轮必需对象缺失（本轮要修的**主场景**）；
4. 全新库。

四种都要断言：迁移后对象齐全、版本推进、数据完好、重复跑幂等。

**诚实边界**：那三个 26–28 分支**没有集成到本分支**，它们的真实迁移定义不在
这里；状态 2 只能造出「版本号已到 28 + 它们各自新增的对象」这种形状，
它们自己的对象本轮无法补偿。该组合**未验证真实分支的实际形状**；
它们合并前必须把迁移号抬到 30 以上，否则会被本轮的 30 整段跳过。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from agent.storage import migrate as migrate_module
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

TS = "2026-10-10T00:00:00+00:00"
BASELINE_VERSION = 25
SIBLING_MIN, SIBLING_MAX = 26, 28


# ---------------------------------------------------------------------------
# 用真实迁移定义构造库状态
# ---------------------------------------------------------------------------


def _ensure_version_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_version ("
        " id INTEGER PRIMARY KEY, version INTEGER NOT NULL, applied_at TEXT NOT NULL)"
    )


def _apply_prefix(conn: sqlite3.Connection, upto: int) -> None:
    """按**真实迁移定义**把库建到 `upto` 版（含版本行），且只到那里。"""
    _ensure_version_table(conn)
    for target, statements in MIGRATIONS:
        if target > upto:
            break
        with transaction(conn):
            for stmt in statements:
                conn.execute(stmt)
            conn.execute(
                "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)", (target, TS)
            )


def _mark_versions(conn: sqlite3.Connection, *versions: int) -> None:
    """只记版本号、不执行 DDL —— 模拟「版本号被别的分支推到更高」的存量库。"""
    _ensure_version_table(conn)
    for version in versions:
        conn.execute(
            "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)", (version, TS)
        )


def _apply_sibling_shape(conn: sqlite3.Connection) -> None:
    """模拟同基线兄弟分支 26–28 的**形状**（它们的真实定义不在本分支）。

    这里只造「它们各自新增了对象」这一件事，用来验证「版本号高 + 缺本轮对象」
    时本轮迁移仍会跑到。**不是它们的真实形状**，也不能据此宣称兼容。
    """
    # 注意：**不要**用 `attachments` 这个名字。集成后 `attachments` 已经是本线自己的表
    # （来源线原迁移 26，集成时改号为 31），存量库里带着一张**形状不同**的 `attachments`
    # 不是任何真实分支会产生的情形，也不是本轮要兼容的对象；而这里要模拟的只是
    # 「版本号已经很高、但本轮必需对象缺失」这一件事。
    conn.execute(
        "CREATE TABLE IF NOT EXISTS sibling_documents ("
        " id TEXT PRIMARY KEY, message_id TEXT, created_at TEXT NOT NULL)"
    )
    conn.execute("ALTER TABLE messages ADD COLUMN attachment_ids TEXT NOT NULL DEFAULT '[]'")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS process_audit ("
        " id TEXT PRIMARY KEY, pid INTEGER, created_at TEXT NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS process_streams ("
        " id TEXT PRIMARY KEY, ref_id TEXT, created_at TEXT NOT NULL)"
    )
    _mark_versions(conn, *range(SIBLING_MIN, SIBLING_MAX + 1))


# ---------------------------------------------------------------------------
# 独立的对象断言（不依赖被测的 missing_objects，避免自证）
# ---------------------------------------------------------------------------


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def _indexes(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")
    }


def _assert_required_objects_present(conn: sqlite3.Connection) -> None:
    tables, indexes = _tables(conn), _indexes(conn)
    for name in REQUIRED_OBJECTS["tables"]:
        assert name in tables, f"迁移后仍缺表 {name}"
    for spec in REQUIRED_OBJECTS["columns"]:
        table, _, column = spec.partition(".")
        assert table in tables, f"列 {spec} 所在的表 {table} 不存在"
        assert column in _columns(conn, table), f"迁移后 {table} 仍缺列 {column}"
    for name in REQUIRED_OBJECTS["indexes"]:
        assert name in indexes, f"迁移后仍缺索引 {name}"


def _seed_history(conn: sqlite3.Connection) -> None:
    """存量数据：迁移不许把它弄丢，也不许为了补列清空。"""
    conn.execute(
        "INSERT INTO nodes (id, type, name, meta, created_at, updated_at) "
        "VALUES ('n_w3', 'topic', '历史话题', '{}', ?, ?)",
        (TS, TS),
    )
    conn.execute(
        "INSERT INTO knowledge (id, category, state, content, node_ids, created_at, updated_at) "
        "VALUES ('kn_w3', 'general_fact', 'active', '历史知识', '[]', ?, ?)",
        (TS, TS),
    )
    conn.execute(
        "INSERT INTO entity_cards (id, node_id, name, aliases, attributes, state, created_at, "
        "updated_at) VALUES ('ec_w3', 'n_w3', '历史实体', '[]', '[]', 'active', ?, ?)",
        (TS, TS),
    )
    conn.execute(
        "INSERT INTO turn_journal (turn_id, message, status, created_at, updated_at) "
        "VALUES ('tj_w3', '历史消息', 'interrupted', ?, ?)",
        (TS, TS),
    )


def _assert_history_intact(conn: sqlite3.Connection) -> None:
    assert (
        conn.execute("SELECT name FROM nodes WHERE id='n_w3'").fetchone()["name"] == "历史话题"
    )
    row = conn.execute(
        "SELECT content, state, chain_id, version FROM knowledge WHERE id='kn_w3'"
    ).fetchone()
    assert row["content"] == "历史知识" and row["state"] == "active"
    assert row["version"] == 1, "补列给存量行的默认值必须可用（不是 NULL）"
    card = conn.execute(
        "SELECT name, revision, field_meta FROM entity_cards WHERE id='ec_w3'"
    ).fetchone()
    assert card["name"] == "历史实体"
    assert card["revision"] == 0 and card["field_meta"] == "{}"
    turn = conn.execute(
        "SELECT message, status FROM turn_journal WHERE turn_id='tj_w3'"
    ).fetchone()
    assert turn["message"] == "历史消息" and turn["status"] == "interrupted"


def _version_rows(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) AS n FROM schema_version").fetchone()["n"])


# ---------------------------------------------------------------------------
# 状态 1：只到 25
# ---------------------------------------------------------------------------


def test_state_baseline_25_gets_this_rounds_objects(tmp_path: Path):
    conn = connect(tmp_path / "b01_25.db")
    _apply_prefix(conn, BASELINE_VERSION)
    assert current_version(conn) == BASELINE_VERSION
    missing = missing_objects(conn)
    assert missing["tables"] == ["instances", "record_owners"], missing
    assert "turn_journal.owner_instance_id" in missing["columns"], missing
    # 纯检查不许改库（compensate=False 只读）
    assert verify_required_objects(conn, compensate=False) == missing
    assert current_version(conn) == BASELINE_VERSION
    assert "instances" not in _tables(conn)

    _seed_history(conn)
    assert apply_migrations(conn) == SCHEMA_VERSION
    _assert_required_objects_present(conn)
    assert missing_objects(conn) == {"tables": [], "columns": [], "indexes": []}
    _assert_history_intact(conn)

    # 幂等：再来一遍不改版本、不重复写版本行、数据仍在
    rows = _version_rows(conn)
    assert apply_migrations(conn) == SCHEMA_VERSION
    assert _version_rows(conn) == rows
    _assert_history_intact(conn)
    conn.close()


# ---------------------------------------------------------------------------
# 状态 2：25 + 其它分支形状的 26–28
# ---------------------------------------------------------------------------


def test_state_sibling_26_28_shape_gets_this_rounds_objects(tmp_path: Path):
    conn = connect(tmp_path / "b01_sibling.db")
    _apply_prefix(conn, BASELINE_VERSION)
    _apply_sibling_shape(conn)
    _seed_history(conn)
    assert current_version(conn) == SIBLING_MAX
    assert "instances" not in _tables(conn), "前提：本轮对象还没建"

    assert apply_migrations(conn) == SCHEMA_VERSION
    _assert_required_objects_present(conn)
    _assert_history_intact(conn)
    # 兄弟分支的对象与版本行不许被动
    assert {"attachments", "process_audit", "process_streams"} <= _tables(conn)
    assert "attachment_ids" in _columns(conn, "messages")
    recorded = {
        int(row["version"]) for row in conn.execute("SELECT version FROM schema_version")
    }
    assert set(range(SIBLING_MIN, SIBLING_MAX + 1)) <= recorded

    rows = _version_rows(conn)
    assert apply_migrations(conn) == SCHEMA_VERSION
    assert _version_rows(conn) == rows
    _assert_history_intact(conn)
    conn.close()


# ---------------------------------------------------------------------------
# 状态 3：版本已记到 29、但本轮对象缺失（本轮主场景）
# ---------------------------------------------------------------------------


def test_state_version_29_without_objects_is_repaired_by_compensation(tmp_path: Path):
    conn = connect(tmp_path / "b01_v29.db")
    _apply_prefix(conn, BASELINE_VERSION)
    # 版本号被推到 29（例如被兄弟分支碰过、或迁移 29 的 DDL 半截丢失），
    # 但 29 的对象一个都没有 —— 旧逻辑会「整段跳过」，后端起不来。
    _mark_versions(conn, *range(SIBLING_MIN, 29 + 1))
    _seed_history(conn)
    assert current_version(conn) == 29
    assert "instances" not in _tables(conn)
    missing = missing_objects(conn)
    assert missing["tables"] == ["instances", "record_owners"], missing
    assert set(missing["columns"]) == {
        "turn_journal.owner_instance_id",
        "derived_tasks.owner_instance_id",
        "derived_tasks.claim_generation",
        "pending_approvals.owner_instance_id",
        "knowledge.chain_id",
        "knowledge.version",
        "entity_cards.revision",
        "entity_cards.field_meta",
    }, missing

    assert apply_migrations(conn) == SCHEMA_VERSION
    _assert_required_objects_present(conn)
    assert current_version(conn) == SCHEMA_VERSION > 29, "版本必须推到补偿迁移之上"
    _assert_history_intact(conn)

    rows = _version_rows(conn)
    assert apply_migrations(conn) == SCHEMA_VERSION
    assert _version_rows(conn) == rows, "重复跑不得重复写版本行"
    _assert_history_intact(conn)
    conn.close()


def test_version_30_recorded_but_objects_lost_is_compensated(tmp_path: Path):
    """版本已经到 30、对象却丢了：`verify_required_objects` 必须补偿（不靠版本号）。"""
    conn = connect(tmp_path / "b01_v30_lost.db")
    _apply_prefix(conn, BASELINE_VERSION)
    _mark_versions(conn, *range(SIBLING_MIN, COMPENSATION_VERSION + 1))
    _seed_history(conn)
    assert current_version(conn) == COMPENSATION_VERSION
    assert missing_objects(conn)["tables"] == ["instances", "record_owners"]

    rows = _version_rows(conn)
    assert apply_migrations(conn) == COMPENSATION_VERSION
    _assert_required_objects_present(conn)
    assert _version_rows(conn) == rows, "补偿不改版本行（对象补齐就够了）"
    _assert_history_intact(conn)
    conn.close()


# ---------------------------------------------------------------------------
# 状态 4：全新库
# ---------------------------------------------------------------------------


def test_state_fresh_database_gets_everything_in_one_run(tmp_path: Path):
    conn = connect(tmp_path / "b01_fresh.db")
    assert apply_migrations(conn) == SCHEMA_VERSION
    _assert_required_objects_present(conn)
    assert missing_objects(conn) == {"tables": [], "columns": [], "indexes": []}
    rows = _version_rows(conn)
    assert apply_migrations(conn) == SCHEMA_VERSION
    assert _version_rows(conn) == rows
    conn.close()


# ---------------------------------------------------------------------------
# 「不再静默放过」与迁移纪律
# ---------------------------------------------------------------------------


def test_missing_object_after_compensation_raises_readable_error(tmp_path: Path, monkeypatch):
    conn = connect(tmp_path / "b01_incomplete.db")
    monkeypatch.setitem(
        migrate_module.REQUIRED_OBJECTS,
        "tables",
        tuple(REQUIRED_OBJECTS["tables"]) + ("w3_object_that_no_migration_creates",),
    )
    with pytest.raises(SchemaIncompleteError) as excinfo:
        apply_migrations(conn)
    text = str(excinfo.value)
    assert "w3_object_that_no_migration_creates" in text, text
    assert "表" in text and str(COMPENSATION_VERSION) in text, text
    assert excinfo.value.missing["tables"] == ["w3_object_that_no_migration_creates"]
    conn.close()


def test_compensation_migration_is_additive_and_ordered():
    targets = [target for target, _ in MIGRATIONS]
    assert targets == sorted(set(targets)), f"迁移号必须唯一且递增：{targets}"
    assert COMPENSATION_VERSION == max(targets) == SCHEMA_VERSION > 29
    statements = [str(s) for target, stmts in MIGRATIONS if target == COMPENSATION_VERSION for s in stmts]
    forbidden = ("DROP ", "DELETE ", "UPDATE ", "TRUNCATE")
    for stmt in statements:
        head = stmt.strip().upper()
        assert not head.startswith(forbidden), f"补偿迁移不得改/删数据：{stmt[:60]}"
    # 每个必需对象都要有对应的补偿语句（表 / 列 / 索引）——清单与迁移一一对应
    blob = "\n".join(statements)
    for spec in REQUIRED_OBJECTS["columns"]:
        table, _, column = spec.partition(".")
        assert f"ADD COLUMN {column}" in blob and table in blob, spec
    for name in REQUIRED_OBJECTS["indexes"]:
        assert name in blob, name
    for name in REQUIRED_OBJECTS["tables"]:
        assert f"CREATE TABLE IF NOT EXISTS {name}" in blob, name


def test_history_migrations_are_untouched_by_the_compensation_number():
    """补偿迁移只允许追加：历史迁移号递增、停在 29，补偿号严格大于它们。"""
    history = [target for target, _ in MIGRATIONS if target <= 29]
    assert history == sorted(history) and history[-1] == 29
    assert BASELINE_VERSION in history, "基线 main 的 25 必须在序列里"
    # 26–28 被同基线的兄弟分支占用：本分支**没有**它们（所以它们自己的对象
    # 本轮无法补偿，合并前必须把号抬到 30 以上）。
    assert not ({SIBLING_MIN, 27, SIBLING_MAX} & set(history))
    assert COMPENSATION_VERSION > history[-1]
