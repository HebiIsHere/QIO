from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from agent.storage.archive import archive_messages, restore_message_content
from agent.storage.migrate import apply_migrations, current_version
from agent.storage.schema import SCHEMA_VERSION


def _iso(days_ago: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


def test_entity_cards_table_and_flexible_edge_types(db_conn: sqlite3.Connection):
    """实体卡表存在；edges 支持任意关系类型；embeddings 支持 entity_card。"""
    tables = {r[0] for r in db_conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "entity_cards" in tables

    now = "2026-08-13T00:00:00+00:00"
    db_conn.execute("INSERT INTO nodes VALUES ('n1','entity','鹅','{}',?,?)", (now, now))
    db_conn.execute("INSERT INTO nodes VALUES ('n2','user','用户','{}',?,?)", (now, now))
    # 任意关系类型（如「属于」）不被 CHECK 拦截
    db_conn.execute(
        "INSERT INTO edges (id,src,dst,type,weight,created_at,updated_at) "
        "VALUES ('e1','n1','n2','属于',1.0,?,?)",
        (now, now),
    )
    assert db_conn.execute("SELECT type FROM edges WHERE id='e1'").fetchone()["type"] == "属于"

    # embeddings 支持 doc_type=entity_card
    db_conn.execute(
        "INSERT INTO embeddings (id,doc_type,ref_id,model,dims,vector,created_at,updated_at) "
        "VALUES ('emb1','entity_card','c1','m',512,?,?,?)",
        (b"x", now, now),
    )
    assert db_conn.execute("SELECT doc_type FROM embeddings WHERE id='emb1'").fetchone()["doc_type"] == "entity_card"


def test_migrations_apply_and_idempotent(db_conn: sqlite3.Connection):
    assert current_version(db_conn) == SCHEMA_VERSION
    apply_migrations(db_conn)  # second run is a no-op
    assert current_version(db_conn) == SCHEMA_VERSION


def test_all_tables_present(db_conn: sqlite3.Connection):
    tables = {
        r["name"]
        for r in db_conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    expected = {
        "nodes",
        "edges",
        "fragments",
        "messages",
        "memory_index",
        "knowledge",
        "cursor",
        "credentials",
        "credential_audit",
        "schema_version",
    }
    assert expected <= tables


def test_node_type_check(db_conn: sqlite3.Connection):
    with pytest.raises(sqlite3.IntegrityError):
        db_conn.execute(
            "INSERT INTO nodes (id, type, name, created_at, updated_at) "
            "VALUES ('n1', 'bad', 'x', ?, ?)",
            (_iso(0), _iso(0)),
        )


def test_edge_unique(db_conn: sqlite3.Connection):
    now = _iso(0)
    db_conn.execute(
        "INSERT INTO nodes VALUES ('t1','topic','t', '{}', ?, ?)", (now, now)
    )
    db_conn.execute(
        "INSERT INTO nodes VALUES ('e1','entity','e', '{}', ?, ?)", (now, now)
    )
    db_conn.execute(
        "INSERT INTO edges VALUES ('x1','t1','e1','mention',1.0,?,?)", (now, now)
    )
    with pytest.raises(sqlite3.IntegrityError):
        db_conn.execute(
            "INSERT INTO edges VALUES ('x2','t1','e1','mention',1.0,?,?)", (now, now)
        )


def test_cursor_active_unique(db_conn: sqlite3.Connection):
    now = _iso(0)
    db_conn.execute(
        "INSERT INTO cursor VALUES ('active', NULL, NULL, 'active', ?)", (now,)
    )
    with pytest.raises(sqlite3.IntegrityError):
        db_conn.execute(
            "INSERT INTO cursor VALUES ('active2', NULL, NULL, 'active', ?)", (now,)
        )


def test_archive_roundtrip(db_conn: sqlite3.Connection, tmp_path):
    now = _iso(0)
    old = _iso(200)
    db_conn.execute(
        "INSERT INTO nodes VALUES ('t1','topic','t','{}',?,?)", (now, now)
    )
    db_conn.execute(
        "INSERT INTO fragments (id, topic_id, created_at) VALUES ('f1','t1',?)", (now,)
    )
    db_conn.execute(
        "INSERT INTO messages (id, fragment_id, role, content, raw, created_at, storage_tier) "
        "VALUES ('m_old','f1','user','old content','{}',?,'hot')",
        (old,),
    )
    db_conn.execute(
        "INSERT INTO messages (id, fragment_id, role, content, raw, created_at, storage_tier) "
        "VALUES ('m_new','f1','user','new content','{}',?,'hot')",
        (now,),
    )
    archived = archive_messages(db_conn, tmp_path / "archive")
    assert archived == 1
    row = db_conn.execute(
        "SELECT storage_tier, content, raw FROM messages WHERE id='m_old'"
    ).fetchone()
    assert row["storage_tier"] == "cold"
    assert row["content"] == ""
    restored = restore_message_content(db_conn, "m_old")
    assert restored == "old content"
    assert restore_message_content(db_conn, "m_new") == "new content"