from __future__ import annotations

from agent.storage.settings import SettingsStore


def test_migration_v4_tables_exist(db_conn):
    rows = db_conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name IN ('embeddings', 'settings')"
    ).fetchall()
    assert {r["name"] for r in rows} == {"embeddings", "settings"}
    # embeddings 唯一约束：同一 (doc_type, ref_id) 只有一行
    db_conn.execute(
        "INSERT INTO embeddings (id, doc_type, ref_id, model, dims, vector, created_at, updated_at) "
        "VALUES ('e1', 'topic', 't1', 'bge', 512, X'00', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')"
    )
    import pytest
    with pytest.raises(Exception):
        db_conn.execute(
            "INSERT INTO embeddings (id, doc_type, ref_id, model, dims, vector, created_at, updated_at) "
            "VALUES ('e2', 'topic', 't1', 'bge', 512, X'00', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')"
        )


def test_settings_roundtrip(db_conn):
    store = SettingsStore(db_conn)
    assert store.get_int("fragment.max_messages", 10) == 10
    store.set("fragment.max_messages", "15")
    assert store.get_int("fragment.max_messages", 10) == 15
    store.set("fragment.max_messages", "5")
    assert store.get_int("fragment.max_messages", 10) == 5


def test_settings_invalid_int_falls_back(db_conn):
    store = SettingsStore(db_conn)
    store.set("fragment.max_messages", "abc")
    assert store.get_int("fragment.max_messages", 10) == 10
    assert store.get("fragment.max_messages") == "abc"
