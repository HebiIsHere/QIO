"""SQLite connection management."""

from __future__ import annotations

import sqlite3
from pathlib import Path


def connect(db_path: Path) -> sqlite3.Connection:
    """Open a SQLite connection with WAL, foreign keys, and busy timeout.

    check_same_thread=False: FastAPI serves requests on a thread pool while
    the agent loop may also touch storage; SQLite serializes access through
    WAL + busy_timeout.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    # autocommit: every write persists immediately (no implicit transactions)
    conn.isolation_level = None
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def close(conn: sqlite3.Connection) -> None:
    conn.close()