"""Runtime settings store: simple key/value persisted in SQLite.

Used for user-tunable preferences (e.g. fragment chunk thresholds)
that the frontend can read and update via the settings API.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class SettingsStore:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def get(self, key: str, default: str | None = None) -> str | None:
        row = self.conn.execute(
            "SELECT value FROM settings WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row is not None else default

    def get_int(self, key: str, default: int) -> int:
        raw = self.get(key)
        if raw is None:
            return default
        try:
            return int(raw)
        except (TypeError, ValueError):
            return default

    def get_bool(self, key: str, default: bool) -> bool:
        """布尔设置：存 "0"/"1"（也容忍 true/false/yes/no 这类写法）。"""
        raw = self.get(key)
        if raw is None:
            return default
        return str(raw).strip().lower() in ("1", "true", "yes", "on")

    def set(self, key: str, value: str) -> None:
        now = _now()
        self.conn.execute(
            "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (key, value, now),
        )

    def all(self) -> dict[str, str]:
        rows = self.conn.execute("SELECT key, value FROM settings").fetchall()
        return {r["key"]: r["value"] for r in rows}
