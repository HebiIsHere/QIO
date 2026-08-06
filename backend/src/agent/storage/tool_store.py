"""Persistent store for agent-created tool definitions.

Tools registered through the lifecycle are saved here so they survive
backend restarts. The registry is restored at startup by AppContext.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from agent.tools.spec import ToolDefinition


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ToolStore:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def save(self, definition: ToolDefinition) -> None:
        """Insert or replace the definition for the tool name."""
        now = _now()
        raw = definition.model_dump_json()
        self.conn.execute(
            "INSERT INTO tools (id, name, definition, status, created_at, updated_at) "
            "VALUES (?, ?, ?, 'active', ?, ?) "
            "ON CONFLICT(name) DO UPDATE SET definition = excluded.definition, "
            "updated_at = excluded.updated_at, status = 'active'",
            (f"tool_{definition.name}", definition.name, raw, now, now),
        )
        self.conn.commit()

    def load_all(self) -> list[ToolDefinition]:
        rows = self.conn.execute(
            "SELECT definition FROM tools WHERE status = 'active' ORDER BY created_at"
        ).fetchall()
        definitions: list[ToolDefinition] = []
        for row in rows:
            try:
                definitions.append(ToolDefinition(**json.loads(row["definition"])))
            except Exception:
                continue  # skip corrupt rows, keep the rest
        return definitions

    def remove(self, name: str) -> None:
        self.conn.execute(
            "UPDATE tools SET status = 'removed', updated_at = ? WHERE name = ?",
            (_now(), name),
        )
        self.conn.commit()

    def list_meta(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT name, status, created_at, updated_at FROM tools ORDER BY created_at"
        ).fetchall()
        return [dict(r) for r in rows]
