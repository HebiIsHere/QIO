"""迁移纪律回归：存量库上的迁移必须真的补上本轮对象（Lead 复验）。

为什么单独钉这一条（真实踩到过）：
`apply_migrations` 的语义是「`target <= 已记录版本` 就整条跳过」。本轮迁移最初
编号 26，而**同基线的其它修复分支**已经用 26 / 27 / 28 做过各自的迁移（基线
`6e073e9` 自己停在 25）。于是任何被那些分支碰过的存量库（例如长期数据目录
`D:\\QIO-data`）都已经记着 26，本轮迁移被整段跳过，`instances` 表不存在，
后端在 `AppContext.__init__` 里直接崩：

    sqlite3.OperationalError: no such table: instances

修法是把本轮迁移改到 29（已知最大号 + 1），并在这里用「历史状态样例」把它钉住：
构造一个 `schema_version` 已经到 26~28、但缺少本轮对象（`instances` /
`record_owners` / `turn_journal.owner_instance_id` / `knowledge.chain_id` /
`entity_cards.revision`）的库，跑一次 `apply_migrations` 后必须全部补齐，
且**不丢已有数据**、重复跑幂等。
"""

from __future__ import annotations

import sqlite3

from agent.storage.db import connect
from agent.storage.migrate import apply_migrations, current_version
from agent.storage.schema import MIGRATIONS, SCHEMA_VERSION

# 同基线兄弟分支已经占到 28（附件发送 / 进程审计 / 统一进程流等）。
SIBLING_MAX_VERSION = 28

# 本轮迁移必须补上的对象。
REQUIRED_TABLES = ("instances", "record_owners")
REQUIRED_COLUMNS = {
    "turn_journal": "owner_instance_id",
    "derived_tasks": "owner_instance_id",
    "pending_approvals": "owner_instance_id",
    "knowledge": "chain_id",
    "knowledge": "version",
    "entity_cards": "revision",
    "entity_cards": "field_meta",
}


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(r[1]) for r in conn.execute(f"PRAGMA table_info({table})")}


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {
        str(r[0])
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }


def _build_sibling_state(conn: sqlite3.Connection) -> None:
    """造一个「被同基线兄弟分支的迁移 26~28 碰过」的存量库。

    做法：按基线 main 的顺序把 1..25 全部应用（它们不含本轮对象），
    再把 schema_version 补到 28 —— 这正是长期数据目录的真实形态。
    """
    baseline = [(t, s) for t, s in MIGRATIONS if t <= 25]
    # schema_version 表由 current_version() 建立，迁移 1 之前它还不存在
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_version ("
        " id INTEGER PRIMARY KEY, version INTEGER NOT NULL, applied_at TEXT NOT NULL)"
    )
    for target, statements in baseline:
        for stmt in statements:
            conn.execute(stmt)
        conn.execute(
            "INSERT INTO schema_version (version, applied_at) VALUES (?, '2026-10-09T00:00:00+00:00')",
            (target,),
        )
    for extra in (26, 27, 28):
        conn.execute(
            "INSERT INTO schema_version (version, applied_at) VALUES (?, '2026-10-09T00:00:00+00:00')",
            (extra,),
        )


def test_migration_number_is_above_every_known_sibling(tmp_path):
    """本轮迁移号必须大于同基线兄弟分支用过的最大号，否则会被整段跳过。"""
    assert SCHEMA_VERSION > SIBLING_MAX_VERSION, (
        f"本轮迁移号 {SCHEMA_VERSION} 不大于兄弟分支已用到的 {SIBLING_MAX_VERSION}："
        "存量库会把这条迁移整段跳过，后端将因缺表起不来"
    )
    # 迁移号不得重复（重复会让后一条永远不生效）
    targets = [t for t, _ in MIGRATIONS]
    assert len(targets) == len(set(targets)), f"迁移号重复：{targets}"
    assert targets == sorted(targets), "迁移必须按号递增排列"


def test_sibling_versioned_database_still_gets_this_rounds_objects(tmp_path):
    """存量库（版本已到 28、缺本轮对象）跑一次迁移后必须补齐，且不丢数据。"""
    db = tmp_path / "sibling.db"
    conn = connect(db)
    _build_sibling_state(conn)
    assert current_version(conn) == SIBLING_MAX_VERSION
    assert "instances" not in _tables(conn)
    assert "owner_instance_id" not in _columns(conn, "turn_journal")

    # 存量数据：迁移不能把它弄丢
    conn.execute(
        "INSERT INTO nodes (id, type, name, meta, created_at, updated_at) "
        "VALUES ('n_keep', 'topic', '历史话题', '{}', '2026-01-01T00:00:00+00:00', "
        "'2026-01-01T00:00:00+00:00')"
    )
    conn.execute(
        "INSERT INTO knowledge (id, category, state, content, node_ids, created_at, updated_at) "
        "VALUES ('kn_keep', 'general_fact', 'active', '历史知识', '[]', "
        "'2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')"
    )

    new_version = apply_migrations(conn)
    assert new_version == SCHEMA_VERSION

    tables = _tables(conn)
    for name in REQUIRED_TABLES:
        assert name in tables, f"迁移后仍缺表 {name}（存量库会因此起不来）"
    for table, column in REQUIRED_COLUMNS.items():
        assert column in _columns(conn, table), f"迁移后 {table} 仍缺列 {column}"

    # 数据完好：内容、归属与版本链的容器都还在
    row = conn.execute("SELECT content, state FROM knowledge WHERE id = 'kn_keep'").fetchone()
    assert row["content"] == "历史知识" and row["state"] == "active"
    assert conn.execute("SELECT name FROM nodes WHERE id = 'n_keep'").fetchone()["name"] == "历史话题"
    # 历史知识的版本列被回填成可用的默认值（不是 NULL 导致链判定失效）
    chain = conn.execute(
        "SELECT chain_id, version FROM knowledge WHERE id = 'kn_keep'"
    ).fetchone()
    assert chain["version"] == 1

    # 幂等：重复跑不再改版本、不重复插入
    before_rows = conn.execute("SELECT COUNT(*) AS n FROM schema_version").fetchone()["n"]
    assert apply_migrations(conn) == SCHEMA_VERSION
    assert conn.execute("SELECT COUNT(*) AS n FROM schema_version").fetchone()["n"] == before_rows
    conn.close()


def test_fresh_database_reaches_current_version(tmp_path):
    """全新库直接到最新版本（同一条迁移在两种库上都成立）。"""
    conn = connect(tmp_path / "fresh.db")
    assert apply_migrations(conn) == SCHEMA_VERSION
    tables = _tables(conn)
    for name in REQUIRED_TABLES:
        assert name in tables
    conn.close()
