"""Sphere layout with positional inertia.

Positions persist once assigned (nodes.meta["layout"]). New topics take
the next Fibonacci-sphere slot; existing positions never move. The layout
is computed server-side so every frontend instance sees identical
coordinates.
"""

from __future__ import annotations

import json
import math
import sqlite3
from typing import Any

RADIUS = 1.0


def fibonacci_sphere(count: int) -> list[tuple[float, float, float]]:
    points = []
    golden = math.pi * (3 - math.sqrt(5))
    for i in range(count):
        y = 1 - (i / max(1, count - 1)) * 2
        radius = math.sqrt(max(0.0, 1 - y * y))
        theta = golden * i
        points.append((math.cos(theta) * radius, y, math.sin(theta) * radius))
    return points


def assign_positions(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Read or assign sphere positions for all topic nodes.

    Returns [{topic_id, name, position: [x, y, z], activity}].
    """
    rows = conn.execute(
        "SELECT id, name, meta, updated_at FROM nodes WHERE type = 'topic' "
        "ORDER BY created_at"
    ).fetchall()
    assigned: list[dict[str, Any]] = []
    pending: list[sqlite3.Row] = []
    for row in rows:
        meta = json.loads(row["meta"] or "{}")
        if "layout" in meta:
            assigned.append(
                {
                    "topic_id": row["id"],
                    "name": row["name"],
                    "position": meta["layout"],
                    "activity": meta.get("activity", 0.5),
                    "updated_at": row["updated_at"],
                }
            )
        else:
            pending.append(row)

    if pending:
        slots = fibonacci_sphere(len(rows))
        used = {tuple(a["position"]) for a in assigned}
        slot_idx = 0
        for row in pending:
            while slot_idx < len(slots) and slots[slot_idx] in used:
                slot_idx += 1
            if slot_idx >= len(slots):
                # fallback: pick any free-ish slot
                position = slots[(slot_idx + len(rows)) % len(slots)]
            else:
                position = slots[slot_idx]
            used.add(position)
            slot_idx += 1
            meta = json.loads(row["meta"] or "{}")
            meta["layout"] = list(position)
            now = row["updated_at"]
            conn.execute(
                "UPDATE nodes SET meta = ? WHERE id = ?",
                (json.dumps(meta, ensure_ascii=False), row["id"]),
            )
            assigned.append(
                {
                    "topic_id": row["id"],
                    "name": row["name"],
                    "position": list(position),
                    "activity": meta.get("activity", 0.5),
                    "updated_at": now,
                }
            )
    # stable order by creation (already ordered by created_at via first query order)
    return assigned