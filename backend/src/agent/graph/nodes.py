"""Node service: user root (singleton), topics, lazy entities.

Only three node types exist: user (root), entity, topic.
Entities are created lazily: after enough mentions (threshold, default 2)
or on explicit user annotation (force). Merging is manual.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

DEFAULT_ENTITY_THRESHOLD = 2

_ENTITY_TYPES = {"user", "entity", "topic"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


@dataclass(frozen=True)
class Node:
    id: str
    type: str
    name: str
    meta: dict
    created_at: str
    updated_at: str


class NodeService:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    # -- user root --------------------------------------------------------

    def get_or_create_user_root(self, name: str = "用户") -> Node:
        row = self.conn.execute(
            "SELECT * FROM nodes WHERE type = 'user' ORDER BY created_at LIMIT 1"
        ).fetchone()
        if row is not None:
            return self._from_row(row)
        node_id = new_id("user")
        now = _now()
        self.conn.execute(
            "INSERT INTO nodes (id, type, name, meta, created_at, updated_at) "
            "VALUES (?, 'user', ?, '{}', ?, ?)",
            (node_id, name, now, now),
        )
        return Node(node_id, "user", name, {}, now, now)

    # -- topics -----------------------------------------------------------

    def create_topic(self, name: str) -> Node:
        node_id = new_id("topic")
        now = _now()
        self.conn.execute(
            "INSERT INTO nodes (id, type, name, meta, created_at, updated_at) "
            "VALUES (?, 'topic', ?, '{}', ?, ?)",
            (node_id, name, now, now),
        )
        return Node(node_id, "topic", name, {}, now, now)

    def get(self, node_id: str) -> Node | None:
        row = self.conn.execute("SELECT * FROM nodes WHERE id = ?", (node_id,)).fetchone()
        return self._from_row(row) if row else None

    def get_topic(self, topic_id: str) -> Node | None:
        node = self.get(topic_id)
        return node if node is not None and node.type == "topic" else None

    def list_topics(self) -> list[Node]:
        rows = self.conn.execute(
            "SELECT * FROM nodes WHERE type = 'topic' ORDER BY updated_at DESC"
        ).fetchall()
        return [self._from_row(r) for r in rows]

    # -- entities (lazy) --------------------------------------------------

    def get_entity_by_name(self, name: str) -> Node | None:
        row = self.conn.execute(
            "SELECT * FROM nodes WHERE type = 'entity' AND name = ? ORDER BY created_at LIMIT 1",
            (name,),
        ).fetchone()
        return self._from_row(row) if row else None

    def create_entity(self, name: str) -> Node:
        node_id = new_id("ent")
        now = _now()
        self.conn.execute(
            "INSERT INTO nodes (id, type, name, meta, created_at, updated_at) "
            "VALUES (?, 'entity', ?, '{}', ?, ?)",
            (node_id, name, now, now),
        )
        return Node(node_id, "entity", name, {}, now, now)

    def mention(
        self,
        name: str,
        topic_id: str | None = None,
        *,
        threshold: int = DEFAULT_ENTITY_THRESHOLD,
        force: bool = False,
    ) -> tuple[Node | None, bool]:
        """Record a mention; returns (entity node or None, created?).

        If the entity node already exists, returns it. Otherwise counts the
        mention and creates the node once the threshold is reached or
        force=True (user annotation).
        """
        existing = self.get_entity_by_name(name)
        if existing is not None:
            return existing, False
        now = _now()
        if topic_id is not None:
            self.conn.execute(
                "INSERT INTO entity_mentions (id, entity_name, topic_id, mention_count, "
                "created_at, updated_at) VALUES (?, ?, ?, 1, ?, ?) "
                "ON CONFLICT(entity_name, topic_id) DO UPDATE SET "
                "mention_count = mention_count + 1, updated_at = ?",
                (new_id("em"), name, topic_id, now, now, now),
            )
            count = self.conn.execute(
                "SELECT mention_count AS c FROM entity_mentions "
                "WHERE entity_name = ? AND topic_id = ?",
                (name, topic_id),
            ).fetchone()["c"]
        else:
            count = 1
        if force or count >= threshold:
            entity = self.create_entity(name)
            return entity, True
        return None, False

    def merge_entities(self, target_id: str, source_id: str) -> None:
        """Manual merge: redirect edges, mark the source as merged."""
        target = self.get(target_id)
        source = self.get(source_id)
        if target is None or source is None:
            raise KeyError("merge target/source not found")
        if target.type != "entity" or source.type != "entity":
            raise ValueError("merge only applies to entity nodes")
        if target_id == source_id:
            return
        now = _now()
        # redirect edges pointing at the source; merge weights when the
        # (src, target, type) edge already exists
        for direction in ("dst", "src"):
            other = "src" if direction == "dst" else "dst"
            rows = self.conn.execute(
                f"SELECT * FROM edges WHERE {direction} = ? AND {other} != ?",
                (source_id, target_id),
            ).fetchall()
            for row in rows:
                existing = self.conn.execute(
                    f"SELECT * FROM edges WHERE src = ? AND dst = ? AND type = ?",
                    (
                        row["dst"] if direction == "src" else row["src"],
                        row["src"] if direction == "src" else row["dst"],
                        row["type"],
                    ),
                ).fetchone()
                if existing is not None:
                    self.conn.execute(
                        "UPDATE edges SET weight = weight + ?, updated_at = ? WHERE id = ?",
                        (row["weight"], now, existing["id"]),
                    )
                    self.conn.execute("DELETE FROM edges WHERE id = ?", (row["id"],))
                else:
                    self.conn.execute(
                        f"UPDATE edges SET {direction} = ?, updated_at = ? WHERE id = ?",
                        (target_id, now, row["id"]),
                    )
        meta = dict(source.meta)
        meta["merged_into"] = target_id
        meta["merged_at"] = now
        self.conn.execute(
            "UPDATE nodes SET meta = ?, updated_at = ? WHERE id = ?",
            (json.dumps(meta, ensure_ascii=False), now, source_id),
        )

    def _from_row(self, row: sqlite3.Row) -> Node:
        return Node(
            id=row["id"],
            type=row["type"],
            name=row["name"],
            meta=json.loads(row["meta"] or "{}"),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )