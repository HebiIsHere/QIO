"""数据库不变量：同一个 Topic 最多一个开放 Fragment。

产品规则只能靠「先 SELECT 再 INSERT」维持时，并发或中途失败会破掉它。
这里把规则交给数据库（部分唯一索引），并且必须保证**已有数据库仍能启动**：
历史数据里可能已经有重复的开放片段，迁移要先归一化再建约束，且不得删数据。
"""

from __future__ import annotations

import sqlite3

import pytest

from agent.storage.db import connect
from agent.storage.migrate import apply_migrations, current_version
from agent.storage.schema import MIGRATIONS

INVARIANT_VERSION = max(v for v, _ in MIGRATIONS)


def _open_fragments(conn: sqlite3.Connection, topic_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM fragments WHERE topic_id = ? AND closed_at IS NULL "
        "ORDER BY created_at, id",
        (topic_id,),
    ).fetchall()


def _make_topic(conn: sqlite3.Connection, topic_id: str, name: str = "话题") -> None:
    conn.execute(
        "INSERT INTO nodes (id, type, name, meta, created_at, updated_at) "
        "VALUES (?, 'topic', ?, '{}', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')",
        (topic_id, name),
    )


def _insert_fragment(
    conn: sqlite3.Connection, fragment_id: str, topic_id: str, created_at: str, closed_at=None, summary=None
) -> None:
    conn.execute(
        "INSERT INTO fragments (id, topic_id, summary, summary_version, created_at, closed_at, meta) "
        "VALUES (?, ?, ?, 0, ?, ?, '{}')",
        (fragment_id, topic_id, summary, created_at, closed_at),
    )


def _apply_up_to(conn: sqlite3.Connection, version: int) -> None:
    """只应用 version 及更早的迁移（用来构造「旧库」状态）。"""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_version ("
        " id INTEGER PRIMARY KEY, version INTEGER NOT NULL, applied_at TEXT NOT NULL)"
    )
    for target, statements in MIGRATIONS:
        if target > version:
            break
        for stmt in statements:
            conn.execute(stmt)
        conn.execute(
            "INSERT INTO schema_version (version, applied_at) VALUES (?, '2026-01-01T00:00:00+00:00')",
            (target,),
        )


def test_partial_unique_index_blocks_second_open_fragment(tmp_path):
    conn = connect(tmp_path / "fresh.db")
    apply_migrations(conn)
    _make_topic(conn, "t1")
    _insert_fragment(conn, "f1", "t1", "2026-01-01T00:00:00+00:00")

    with pytest.raises(sqlite3.IntegrityError):
        _insert_fragment(conn, "f2", "t1", "2026-01-02T00:00:00+00:00")
    conn.close()


def test_closing_then_opening_again_is_allowed(tmp_path):
    """约束只管「开放」的那一个：关闭旧的、再开新的必须正常。"""
    conn = connect(tmp_path / "fresh.db")
    apply_migrations(conn)
    _make_topic(conn, "t1")
    _insert_fragment(conn, "f1", "t1", "2026-01-01T00:00:00+00:00")
    conn.execute("UPDATE fragments SET closed_at = '2026-01-03T00:00:00+00:00' WHERE id = 'f1'")
    _insert_fragment(conn, "f2", "t1", "2026-01-04T00:00:00+00:00")
    assert len(_open_fragments(conn, "t1")) == 1
    conn.close()


def test_migration_normalizes_existing_duplicates_without_data_loss(tmp_path):
    conn = connect(tmp_path / "legacy.db")
    # 1) 造出一个「旧版本」的库：还没有这条不变量
    _apply_up_to(conn, INVARIANT_VERSION - 1)
    _make_topic(conn, "t1")
    _make_topic(conn, "t2", "另一个话题")
    _insert_fragment(conn, "f_old", "t1", "2026-01-01T00:00:00+00:00", summary="最早")
    _insert_fragment(conn, "f_mid", "t1", "2026-01-02T00:00:00+00:00", summary="中间")
    _insert_fragment(conn, "f_new", "t1", "2026-01-03T00:00:00+00:00", summary="最新")
    _insert_fragment(conn, "f_other", "t2", "2026-01-05T00:00:00+00:00", summary="别的")
    before = conn.execute("SELECT COUNT(*) c FROM fragments").fetchone()["c"]

    # 2) 迁移：必须归一化而不是启动失败
    apply_migrations(conn)
    assert current_version(conn) == INVARIANT_VERSION

    assert conn.execute("SELECT COUNT(*) c FROM fragments").fetchone()["c"] == before
    open_t1 = _open_fragments(conn, "t1")
    assert [r["id"] for r in open_t1] == ["f_new"], "只保留最新的一个开放片段"
    assert [r["id"] for r in _open_fragments(conn, "t2")] == ["f_other"]
    # 被关闭的行保留原有字段（不丢数据、不静默改写摘要）
    rows = {r["id"]: r for r in conn.execute("SELECT * FROM fragments").fetchall()}
    assert rows["f_old"]["summary"] == "最早"
    assert rows["f_mid"]["summary"] == "中间"
    assert rows["f_old"]["closed_at"] is not None
    conn.close()


def test_migration_is_idempotent(tmp_path):
    conn = connect(tmp_path / "legacy.db")
    _apply_up_to(conn, INVARIANT_VERSION - 1)
    _make_topic(conn, "t1")
    _insert_fragment(conn, "f1", "t1", "2026-01-01T00:00:00+00:00")
    _insert_fragment(conn, "f2", "t1", "2026-01-02T00:00:00+00:00")

    apply_migrations(conn)
    snapshot = [dict(r) for r in conn.execute("SELECT * FROM fragments ORDER BY id").fetchall()]
    apply_migrations(conn)  # 第二次：不能再报错、不能改动数据
    assert [dict(r) for r in conn.execute("SELECT * FROM fragments ORDER BY id").fetchall()] == snapshot
    conn.close()
