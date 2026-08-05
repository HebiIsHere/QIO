"""Lightweight ordered migrations with a schema_version table."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from agent.storage.schema import MIGRATIONS


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def current_version(conn: sqlite3.Connection) -> int:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_version ("
        " id INTEGER PRIMARY KEY, version INTEGER NOT NULL, applied_at TEXT NOT NULL)"
    )
    row = conn.execute("SELECT COALESCE(MAX(version), 0) AS v FROM schema_version").fetchone()
    return int(row["v"])


def apply_migrations(conn: sqlite3.Connection) -> int:
    """Apply pending migrations; returns the new schema version."""
    version = current_version(conn)
    for target, statements in MIGRATIONS:
        if target <= version:
            continue
        with conn:
            for stmt in statements:
                conn.execute(stmt)
            conn.execute(
                "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
                (target, _now()),
            )
        version = target
    return version