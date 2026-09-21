"""迁移 13：轮次绑定与接续意图（阶段 1 的持久基础）。

只追加迁移。旧库升级后必须满足：版本号推进、两张新表可用、**旧数据一行不动**，
以及迁移 12 建立的「每个 Topic 最多一个开放 Fragment」约束仍然健在。
"""

from __future__ import annotations

import sqlite3

from agent.storage.db import connect
from agent.storage.migrate import apply_migrations
from agent.storage.schema import MIGRATIONS

BINDING_MIGRATION = 13

BINDING_COLUMNS = {
    "turn_id",
    "topic_id",
    "fragment_id",
    "intent_id",
    "intent_version",
    "write_state",
    "status",
    "created_at",
    "updated_at",
}
INTENT_COLUMNS = {
    "intent_id",
    "topic_id",
    "source_fragment_id",
    "version",
    "state",
    "resolved_fragment_id",
    "request_id",
    "created_at",
    "updated_at",
}


def _apply_until(conn: sqlite3.Connection, limit: int) -> None:
    """把库推进到指定迁移号（用于构造「旧库」）。"""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_version ("
        " id INTEGER PRIMARY KEY, version INTEGER NOT NULL, applied_at TEXT NOT NULL)"
    )
    for target, statements in MIGRATIONS:
        if target > limit:
            break
        with conn:
            for stmt in statements:
                conn.execute(stmt)
            conn.execute(
                "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
                (target, "2026-01-01T00:00:00+00:00"),
            )


def _seed_old_data(conn: sqlite3.Connection) -> None:
    ts = "2026-01-01T00:00:00+00:00"
    conn.execute(
        "INSERT INTO nodes (id, type, name, meta, created_at, updated_at) "
        "VALUES ('topic_a', 'topic', '旧话题', '{}', ?, ?)",
        (ts, ts),
    )
    conn.execute(
        "INSERT INTO fragments (id, topic_id, summary, summary_version, created_at, meta) "
        "VALUES ('frag_a', 'topic_a', '旧摘要', 1, ?, '{}')",
        (ts,),
    )
    conn.execute(
        "INSERT INTO messages (id, fragment_id, role, content, content_type, model, raw, created_at, storage_tier) "
        "VALUES ('msg_a', 'frag_a', 'user', '旧消息', 'text', NULL, '{}', ?, 'hot')",
        (ts,),
    )
    conn.commit()


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def test_upgrade_from_v12_adds_tables_and_keeps_old_rows(tmp_path):
    conn = connect(tmp_path / "old.db")
    _apply_until(conn, 12)
    _seed_old_data(conn)

    version = apply_migrations(conn)

    assert version >= BINDING_MIGRATION
    tables = {
        row["name"]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {"turn_bindings", "continuation_intents"} <= tables
    assert BINDING_COLUMNS <= _columns(conn, "turn_bindings")
    assert INTENT_COLUMNS <= _columns(conn, "continuation_intents")

    # 旧数据一行不动
    row = conn.execute("SELECT content FROM messages WHERE id = 'msg_a'").fetchone()
    assert row is not None and row["content"] == "旧消息"
    frag = conn.execute("SELECT summary, closed_at FROM fragments WHERE id = 'frag_a'").fetchone()
    assert frag is not None and frag["summary"] == "旧摘要" and frag["closed_at"] is None


def test_one_open_fragment_constraint_survives_upgrade(tmp_path):
    conn = connect(tmp_path / "old.db")
    _apply_until(conn, 12)
    _seed_old_data(conn)
    apply_migrations(conn)

    ts = "2026-01-02T00:00:00+00:00"
    # 同话题再插一个开放片段必须被数据库拒绝（迁移 12 的约束仍然有效）
    try:
        conn.execute(
            "INSERT INTO fragments (id, topic_id, summary_version, created_at, meta) "
            "VALUES ('frag_b', 'topic_a', 0, ?, '{}')",
            (ts,),
        )
    except sqlite3.IntegrityError:
        pass
    else:  # pragma: no cover - 约束失效才会走到这里
        raise AssertionError("同一个 Topic 出现了第二个开放 Fragment，唯一约束失效")


def test_apply_migrations_is_idempotent(tmp_path):
    conn = connect(tmp_path / "twice.db")
    first = apply_migrations(conn)
    second = apply_migrations(conn)
    assert first == second


def test_binding_rows_are_unique_per_turn(tmp_path):
    conn = connect(tmp_path / "bind.db")
    apply_migrations(conn)
    _seed_old_data(conn)
    ts = "2026-01-02T00:00:00+00:00"
    conn.execute(
        "INSERT INTO turn_bindings (turn_id, topic_id, fragment_id, write_state, created_at, updated_at) "
        "VALUES ('turn_1', 'topic_a', 'frag_a', 'open', ?, ?)",
        (ts, ts),
    )
    try:
        conn.execute(
            "INSERT INTO turn_bindings (turn_id, topic_id, fragment_id, write_state, created_at, updated_at) "
            "VALUES ('turn_1', 'topic_a', 'frag_a', 'open', ?, ?)",
            (ts, ts),
        )
    except sqlite3.IntegrityError:
        pass
    else:  # pragma: no cover
        raise AssertionError("同一个 turn_id 允许了第二行绑定")
