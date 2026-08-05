"""Edge service: mention / related / owns edges with weight accumulation."""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

EDGE_TYPES = {"mention", "related", "owns"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class Edge:
    id: str
    src: str
    dst: str
    type: str
    weight: float
    created_at: str
    updated_at: str


class EdgeService:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def add(self, src: str, dst: str, edge_type: str, weight: float = 1.0) -> None:
        if edge_type not in EDGE_TYPES:
            raise ValueError(f"unknown edge type: {edge_type}")
        now = _now()
        existing = self.conn.execute(
            "SELECT * FROM edges WHERE src = ? AND dst = ? AND type = ?",
            (src, dst, edge_type),
        ).fetchone()
        if existing is not None:
            self.conn.execute(
                "UPDATE edges SET weight = weight + ?, updated_at = ? WHERE id = ?",
                (weight, now, existing["id"]),
            )
        else:
            self.conn.execute(
                "INSERT INTO edges (id, src, dst, type, weight, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (f"edge_{uuid.uuid4().hex[:12]}", src, dst, edge_type, weight, now, now),
            )

    def list_for(self, node_id: str) -> list[Edge]:
        rows = self.conn.execute(
            "SELECT * FROM edges WHERE src = ? OR dst = ? ORDER BY weight DESC",
            (node_id, node_id),
        ).fetchall()
        return [self._from_row(r) for r in rows]

    def neighbors(self, node_id: str, edge_type: str | None = None) -> list[str]:
        query = "SELECT src, dst FROM edges WHERE src = ? OR dst = ?"
        params = [node_id, node_id]
        if edge_type is not None:
            query += " AND type = ?"
            params.append(edge_type)
        rows = self.conn.execute(query, params).fetchall()
        result: set[str] = set()
        for row in rows:
            result.add(row["dst"] if row["src"] == node_id else row["src"])
        result.discard(node_id)
        return list(result)

    def _from_row(self, row: sqlite3.Row) -> Edge:
        return Edge(
            id=row["id"],
            src=row["src"],
            dst=row["dst"],
            type=row["type"],
            weight=row["weight"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )