"""Injection source: active knowledge entries, filtered by node attachment."""

from __future__ import annotations

import json
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

    def list_active_for_node(
        self, node_id: str, limit: int | None = None
    ) -> list[dict[str, Any]]:
        """Active entries attached to a specific graph node (node_ids contains node_id)."""
        rows = self.conn.execute(
            "SELECT * FROM knowledge WHERE state = 'active' AND node_ids LIKE ? "
            "ORDER BY created_at DESC",
            (f'%"{node_id}"%',),
        ).fetchall()
        items = [dict(r) for r in rows]
        if limit is not None:
            items = items[:limit]
        return items

    def recent_fragment_summaries(
        self, topic_id: str, limit: int = 2
    ) -> list[dict[str, Any]]:
        """Most recent closed fragments of a topic (summary + title)."""
        rows = self.conn.execute(
            "SELECT f.id AS fragment_id, mi.title, f.summary FROM memory_index mi "
            "JOIN fragments f ON f.id = mi.fragment_id "
            "WHERE mi.topic_id = ? AND f.closed_at IS NOT NULL "
            "AND f.summary IS NOT NULL AND f.summary != '' "
            "ORDER BY f.created_at DESC LIMIT ?",
            (topic_id, limit),
        ).fetchall()
        return [
            {"id": r["fragment_id"], "title": r["title"] or topic_id, "summary": r["summary"]}
            for r in rows
        ]

    def list_active_for_nodes(
        self, node_ids: list[str], limit_per_node: int | None = 5
    ) -> list[dict[str, Any]]:
        """Merge active entries across nodes; entries may appear once."""
        seen: dict[str, dict[str, Any]] = {}
        for node_id in node_ids:
            for item in self.list_active_for_node(node_id, limit=limit_per_node):
                seen.setdefault(item["id"], item)
        return list(seen.values())