"""V 组独立验证 · B01：高版本存量库不能漏对象。

原缺陷（基线 `da0436b`，见 `_contracts` §6）

    `apply_migrations()` 是「`target <= 已记录版本` 就整段跳过」，并且**只看版本号**。
    基线的最高迁移是 29（因为同基线的其它分支占了 26/27/28）。于是任何
    「版本已经记到 29、但本轮需要的对象并不存在」的存量库 ——
    例如那个库是被只用了一部分 29 对象的兄弟分支写过的，或者 29 这条迁移执行到
    一半就崩过 —— 再次启动时：

        29 <= 29  →  整段跳过  →  `instances` / `record_owners` /
        `turn_journal.owner_instance_id` / `entity_cards.revision` … 永远不存在
        →  `AppContext.__init__` 里 `InstanceRegistry.start()` 直接
           `sqlite3.OperationalError: no such table: instances` —— 后端起不来。

    没有补偿迁移、没有「按对象校验」（`missing_objects` / `verify_required_objects`
    都不存在），也没有可读的 `SchemaIncompleteError`：缺对象被静默放过。

验证手段

    * 用**真实的迁移定义**（`agent.storage.schema.MIGRATIONS`）把库建到 25；
    * 再按「其它分支形状」补上 26–28 的痕迹（受控模拟：那三个分支的迁移代码
      不在本分支，它们自己的对象本轮无法验证 —— 这是诚实的边界，见 README）；
    * 主场景：把版本记录推到 29，但**不建**本轮对象；
    * 调 `apply_migrations()`，只按 `sqlite_master` / `PRAGMA table_info` 断言
      「对象齐全、版本推进、数据完好、重复幂等」。

    断言完全不 import 尚不存在的新名字（`REQUIRED_OBJECTS` / `missing_objects` /
    `verify_required_objects` / `SchemaIncompleteError`），所以基线能正常收集，
    失败是**断言失败**而不是收集错误。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest

from agent.storage.db import connect, transaction
from agent.storage.migrate import apply_migrations, current_version
from agent.storage.schema import MIGRATIONS

NOW = "2026-10-10T00:00:00+00:00"

# 本轮必需对象（冻结清单，见 _contracts §6）。
REQUIRED_TABLES = (
    "instances",
    "record_owners",
    "turn_journal",
    "derived_tasks",
    "pending_approvals",
    "entity_cards",
    "knowledge",
)

REQUIRED_COLUMNS = {
    "turn_journal": ("owner_instance_id",),
    "derived_tasks": ("owner_instance_id", "claim_generation"),
    "pending_approvals": ("owner_instance_id",),
    "knowledge": ("chain_id", "version"),
    "entity_cards": ("revision", "field_meta"),
}

REQUIRED_INDEXES = (
    "idx_instances_heartbeat",
    "idx_record_owners_instance",
    "idx_turn_journal_owner",
    "idx_knowledge_chain",
)

HIGHEST_BASELINE_VERSION = 29


# --------------------------------------------------------------------------
# 构造工具：用真实迁移定义建库
# --------------------------------------------------------------------------


def _ensure_version_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_version ("
        " id INTEGER PRIMARY KEY, version INTEGER NOT NULL, applied_at TEXT NOT NULL)"
    )


def _record_version(conn: sqlite3.Connection, version: int) -> None:
    _ensure_version_table(conn)
    conn.execute(
        "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
        (int(version), datetime.now(timezone.utc).isoformat()),
    )


def _build_through(conn: sqlite3.Connection, target: int) -> None:
    """只应用 `MIGRATIONS` 里 target 及以下的**真实**迁移定义。"""
    _ensure_version_table(conn)
    for version, statements in MIGRATIONS:
        if version > target:
            continue
        with transaction(conn):
            for stmt in statements:
                conn.execute(stmt)
            conn.execute(
                "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
                (version, NOW),
            )


def _simulate_sibling_branches(conn: sqlite3.Connection, *, through: int) -> None:
    """受控模拟「其它分支形状的 26–28」（以及它们可能已经写下的版本号）。

    这些分支的迁移代码不在本分支，所以这里只能模拟**形状**：
    它们在版本号上的痕迹 + 它们自己的一两个对象。本轮要修的不是它们的对象，
    而是「版本号被推高之后，本轮对象被整段跳过」这件事。
    """
    _ensure_version_table(conn)
    with transaction(conn):
        conn.execute(
            "CREATE TABLE IF NOT EXISTS sibling_branch_attachments ("
            " id TEXT PRIMARY KEY, message_id TEXT, created_at TEXT NOT NULL)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS sibling_branch_process_audit ("
            " id TEXT PRIMARY KEY, pid INTEGER, created_at TEXT NOT NULL)"
        )
        for version in range(26, through + 1):
            conn.execute(
                "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
                (version, NOW),
            )


def _tables(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    return {str(r["name"]) for r in rows}


def _indexes(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'").fetchall()
    return {str(r["name"]) for r in rows if r["name"]}


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(r["name"]) for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _missing_objects(conn: sqlite3.Connection) -> list[str]:
    missing: list[str] = []
    tables = _tables(conn)
    for table in REQUIRED_TABLES:
        if table not in tables:
            missing.append(f"table:{table}")
            continue
        have = _columns(conn, table)
        for column in REQUIRED_COLUMNS.get(table, ()):
            if column not in have:
                missing.append(f"column:{table}.{column}")
    indexes = _indexes(conn)
    for index in REQUIRED_INDEXES:
        if index not in indexes:
            missing.append(f"index:{index}")
    return missing


def _seed_user_data(conn: sqlite3.Connection) -> dict[str, int]:
    """写入几行用户数据（迁移不许清空数据）。返回期望的计数。"""
    conn.execute(
        "INSERT INTO nodes (id, type, name, meta, created_at, updated_at) "
        "VALUES ('node_v1', 'topic', '验证话题', '{}', ?, ?)",
        (NOW, NOW),
    )
    conn.execute(
        "INSERT INTO messages (id, fragment_id, role, content, created_at) "
        "VALUES ('msg_v1', NULL, 'user', '这是一条不能丢的历史消息', ?)",
        (NOW,),
    )
    conn.execute(
        "INSERT INTO entity_cards (id, node_id, name, aliases, kind, summary, attributes, "
        "state, created_at, updated_at) "
        "VALUES ('card_v1', 'node_v1', '我家的鹅', '[]', '动物', '人工写的摘要', '[]', "
        "'active', ?, ?)",
        (NOW, NOW),
    )
    conn.execute(
        "INSERT INTO knowledge (id, category, state, content, created_at, updated_at) "
        "VALUES ('kb_v1', 'general_fact', 'active', '人工写下的知识', ?, ?)",
        (NOW, NOW),
    )
    conn.execute(
        "INSERT INTO turn_journal (turn_id, message, topic_id, notify, status, created_at, "
        "updated_at) VALUES ('turn_v1', '不能丢的排队消息', NULL, 0, 'queued', ?, ?)",
        (NOW, NOW),
    )
    return {
        "nodes": 1,
        "messages": 1,
        "entity_cards": 1,
        "knowledge": 1,
        "turn_journal": 1,
    }


def _counts(conn: sqlite3.Connection, table: str) -> int:
    return int(conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"])


def _assert_seed_intact(conn: sqlite3.Connection) -> None:
    assert _counts(conn, "messages") == 1
    assert (
        conn.execute("SELECT content FROM messages WHERE id = 'msg_v1'").fetchone()["content"]
        == "这是一条不能丢的历史消息"
    )
    assert _counts(conn, "entity_cards") == 1
    assert (
        conn.execute("SELECT summary FROM entity_cards WHERE id = 'card_v1'").fetchone()["summary"]
        == "人工写的摘要"
    )
    assert _counts(conn, "knowledge") == 1
    assert (
        conn.execute("SELECT content FROM knowledge WHERE id = 'kb_v1'").fetchone()["content"]
        == "人工写下的知识"
    )
    assert _counts(conn, "turn_journal") == 1
    assert (
        conn.execute("SELECT message FROM turn_journal WHERE turn_id = 'turn_v1'").fetchone()[
            "message"
        ]
        == "不能丢的排队消息"
    )


@pytest.fixture()
def fresh_db(tmp_path):
    conn = connect(tmp_path / "fu-b01.db")
    yield conn
    conn.close()


# --------------------------------------------------------------------------
# 状态 1：只到 25（基线）
# --------------------------------------------------------------------------


def test_state01_only_through_25_gets_all_required_objects(fresh_db):
    conn = fresh_db
    _build_through(conn, 25)
    _seed_user_data(conn)
    assert current_version(conn) == 25

    apply_migrations(conn)

    assert _missing_objects(conn) == [], "只到 25 的存量库迁移后对象必须齐全"
    assert current_version(conn) > 25, "版本必须推进"
    _assert_seed_intact(conn)


# --------------------------------------------------------------------------
# 状态 2：25 + 其它分支形状的 26–28
# --------------------------------------------------------------------------


def test_state02_sibling_branch_26_28_still_gets_required_objects(fresh_db):
    conn = fresh_db
    _build_through(conn, 25)
    _simulate_sibling_branches(conn, through=28)
    _seed_user_data(conn)
    assert current_version(conn) == 28

    apply_migrations(conn)

    assert _missing_objects(conn) == [], "兄弟分支推进版本后，本轮对象仍必须齐全"
    assert current_version(conn) > 28
    _assert_seed_intact(conn)
    # 兄弟分支自己的对象不受影响（诚实边界：本轮不为它们做补偿）
    assert "sibling_branch_attachments" in _tables(conn)


# --------------------------------------------------------------------------
# 状态 3：版本已记到 29、但本轮对象缺失（本轮主场景）
# --------------------------------------------------------------------------


def test_state03_version_29_without_objects_is_repaired(fresh_db):
    """这是本轮要修的主场景：基线在这里静默放过，后端起不来。"""
    conn = fresh_db
    _build_through(conn, 25)
    _simulate_sibling_branches(conn, through=28)
    _seed_user_data(conn)
    # 关键一步：版本号被推到 29，但 29 的对象一个都没有（兄弟分支占用 29 号段的形状）
    _record_version(conn, HIGHEST_BASELINE_VERSION)

    assert current_version(conn) == HIGHEST_BASELINE_VERSION
    before = _missing_objects(conn)
    assert "table:instances" in before, "场景构造：instances 必须确实缺失"

    apply_migrations(conn)

    assert _missing_objects(conn) == [], f"版本已到 29 但对象缺失的库必须被补偿：缺 {before}"
    assert current_version(conn) > HIGHEST_BASELINE_VERSION, "必须有一条 >29 的补偿迁移推进版本"
    _assert_seed_intact(conn)


def test_state03_compensation_is_repeatable_and_idempotent(fresh_db):
    """补偿迁移必须可重复执行：第二次调用不再改结构、不再动数据。"""
    conn = fresh_db
    _build_through(conn, 25)
    _simulate_sibling_branches(conn, through=28)
    _seed_user_data(conn)
    _record_version(conn, HIGHEST_BASELINE_VERSION)

    apply_migrations(conn)
    version_after_first = current_version(conn)
    schema_after_first = sorted(_tables(conn))

    apply_migrations(conn)
    apply_migrations(conn)

    assert current_version(conn) == version_after_first, "重复执行不得反复推进版本"
    assert sorted(_tables(conn)) == schema_after_first, "重复执行不得反复改结构"
    assert _missing_objects(conn) == []
    _assert_seed_intact(conn)


def test_state03_compensation_does_not_empty_existing_tables(fresh_db):
    """补偿只许补对象：已有表里的行一行都不许少。"""
    conn = fresh_db
    _build_through(conn, 25)
    _seed_user_data(conn)
    _record_version(conn, HIGHEST_BASELINE_VERSION)

    apply_migrations(conn)

    _assert_seed_intact(conn)
    assert _counts(conn, "nodes") == 1


# --------------------------------------------------------------------------
# 状态 4：全新库
# --------------------------------------------------------------------------


def test_state04_fresh_database_is_complete_and_idempotent(fresh_db):
    conn = fresh_db
    version = apply_migrations(conn)

    assert _missing_objects(conn) == [], "全新库迁移后对象必须齐全"
    assert current_version(conn) == version
    assert version >= HIGHEST_BASELINE_VERSION

    # 全新库也要能容纳本轮的关键写入（列存在才写得进去）
    conn.execute(
        "INSERT INTO instances (instance_id, pid, host, started_at, last_heartbeat) "
        "VALUES ('inst_v1', 4321, 'host', ?, ?)",
        (NOW, NOW),
    )
    conn.execute(
        "INSERT INTO record_owners (record_type, record_id, instance_id) "
        "VALUES ('turn', 'turn_v1', 'inst_v1')"
    )
    conn.execute(
        "INSERT INTO entity_cards (id, node_id, name, aliases, attributes, state, "
        "created_at, updated_at, revision, field_meta) "
        "VALUES ('card_v2', NULL, '新卡', '[]', '[]', 'active', ?, ?, 1, '{}')",
        (NOW, NOW),
    )

    apply_migrations(conn)
    apply_migrations(conn)
    assert _counts(conn, "instances") == 1
    assert _counts(conn, "record_owners") == 1
    assert _counts(conn, "entity_cards") == 1


def test_required_object_list_is_not_satisfied_by_version_number_alone(fresh_db):
    """反向对照：版本号很高但对象缺失时，上面的 `_missing_objects` 必须非空。

    这条用例保护的是验证本身 —— 如果哪天 `_missing_objects` 被写成了「只看版本号」，
    状态 3 就会假绿。
    """
    conn = fresh_db
    _build_through(conn, 25)
    _record_version(conn, HIGHEST_BASELINE_VERSION)

    missing = _missing_objects(conn)
    assert "table:instances" in missing
    assert "table:record_owners" in missing
    assert "column:entity_cards.revision" in missing
    assert "column:turn_journal.owner_instance_id" in missing
