"""Injection surface: only active entries, grouped by category."""

from __future__ import annotations

import sqlite3
from typing import Any


class InjectionSource:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def list_active(
        self,
        category: str | None = None,
        exclude_ids: set[str] | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM knowledge WHERE state = 'active'"
        params: list[Any] = []
        if category is not None:
            query += " AND category = ?"
            params.append(category)
        if exclude_ids:
            placeholders = ",".join("?" for _ in exclude_ids)
            query += f" AND id NOT IN ({placeholders})"
            params.extend(sorted(exclude_ids))
        query += " ORDER BY created_at DESC"
        if limit is not None:
            query += " LIMIT ?"
            params.append(limit)
        rows = self.conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]

    def by_category(self) -> dict[str, list[dict[str, Any]]]:
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in self.list_active():
            grouped.setdefault(row["category"], []).append(row)
        return grouped