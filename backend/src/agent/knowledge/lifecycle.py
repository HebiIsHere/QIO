"""Knowledge lifecycle state machine.

States: draft -> pending_review -> verified -> active -> expired | revoked.
Only active entries enter the injection surface.

Version chain: supersedes_id points at the entry this one replaces; when a
new version is activated, the superseded entry is revoked automatically.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any

CATEGORIES = {
    "user_profile",
    "agent_self",
    "goal",
    "general_fact",
    "tool_experience",
}
HIGH_IMPACT_CATEGORIES = {"user_profile", "agent_self", "goal"}
LOW_IMPACT_CATEGORIES = CATEGORIES - HIGH_IMPACT_CATEGORIES


class KnowledgeState(str, Enum):
    DRAFT = "draft"
    PENDING_REVIEW = "pending_review"
    VERIFIED = "verified"
    ACTIVE = "active"
    EXPIRED = "expired"
    REVOKED = "revoked"


_TRANSITIONS: dict[KnowledgeState, set[KnowledgeState]] = {
    KnowledgeState.DRAFT: {KnowledgeState.PENDING_REVIEW, KnowledgeState.REVOKED},
    KnowledgeState.PENDING_REVIEW: {KnowledgeState.VERIFIED, KnowledgeState.DRAFT, KnowledgeState.REVOKED},
    KnowledgeState.VERIFIED: {KnowledgeState.ACTIVE, KnowledgeState.REVOKED},
    KnowledgeState.ACTIVE: {KnowledgeState.EXPIRED, KnowledgeState.REVOKED},
    KnowledgeState.EXPIRED: set(),
    KnowledgeState.REVOKED: set(),
}


def impact_of(category: str) -> str:
    return "high" if category in HIGH_IMPACT_CATEGORIES else "low"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id() -> str:
    return f"kn_{uuid.uuid4().hex[:12]}"


@dataclass(frozen=True)
class KnowledgeItem:
    id: str
    category: str
    state: KnowledgeState
    content: str
    supersedes_id: str | None
    topic_id: str | None
    node_ids: list[str]
    entity_ids: list[str]
    provenance: dict
    confidence: float | None
    created_at: str
    updated_at: str
    activated_at: str | None
    expired_at: str | None
    export: bool


class KnowledgeService:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    # -- lifecycle --------------------------------------------------------

    def create(
        self,
        *,
        category: str,
        content: str,
        topic_id: str | None = None,
        node_ids: list[str] | None = None,
        entity_ids: list[str] | None = None,
        provenance: dict | None = None,
        export: bool = False,
        supersedes_id: str | None = None,
    ) -> KnowledgeItem:
        if category not in CATEGORIES:
            raise ValueError(f"unknown category: {category}")
        if not content.strip():
            raise ValueError("knowledge content must not be empty")
        if supersedes_id is not None and self.get(supersedes_id) is None:
            raise ValueError(f"supersedes target not found: {supersedes_id}")
        now = _now()
        knowledge_id = new_id()
        # node_ids is the source of truth; topic_id remains for compatibility
        resolved_nodes = list(node_ids or [])
        if topic_id is not None and topic_id not in resolved_nodes:
            resolved_nodes.append(topic_id)
        self.conn.execute(
            "INSERT INTO knowledge (id, category, state, content, supersedes_id, "
            "topic_id, node_ids, entity_ids, provenance, created_at, updated_at, export) "
            "VALUES (?, ?, 'draft', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                knowledge_id,
                category,
                content,
                supersedes_id,
                topic_id,
                json.dumps(resolved_nodes, ensure_ascii=False),
                json.dumps(entity_ids or [], ensure_ascii=False),
                json.dumps(provenance or {}, ensure_ascii=False),
                now,
                now,
                1 if export else 0,
            ),
        )
        item = self.get(knowledge_id)
        assert item is not None
        return item

    def submit(self, knowledge_id: str) -> KnowledgeItem:
        return self._transition(knowledge_id, KnowledgeState.PENDING_REVIEW)

    def reject(self, knowledge_id: str) -> KnowledgeItem:
        """pending_review -> draft (sent back for revision)."""
        return self._transition(knowledge_id, KnowledgeState.DRAFT)

    def verify(
        self, knowledge_id: str, *, verified_by: str = "system"
    ) -> KnowledgeItem:
        """pending_review -> verified.

        High-impact categories (user_profile / agent_self / goal) require an
        explicit user confirmation; low-impact categories may be
        auto-verified by the system.
        """
        item = self.get(knowledge_id)
        if item is None:
            raise KeyError(knowledge_id)
        if item.state != KnowledgeState.PENDING_REVIEW:
            raise ValueError(f"cannot verify from state {item.state.value}")
        if impact_of(item.category) == "high" and verified_by != "user":
            raise PermissionError(
                f"high-impact category '{item.category}' requires user confirmation"
            )
        return self._transition(
            knowledge_id, KnowledgeState.VERIFIED, extra={"confidence": 0.9}
        )

    def activate(self, knowledge_id: str) -> KnowledgeItem:
        """verified -> active; revokes the superseded entry (version chain)."""
        item = self.get(knowledge_id)
        if item is None:
            raise KeyError(knowledge_id)
        if item.state != KnowledgeState.VERIFIED:
            raise ValueError(f"cannot activate from state {item.state.value}")
        if item.supersedes_id:
            superseded = self.get(item.supersedes_id)
            if superseded is not None and superseded.state not in {
                KnowledgeState.EXPIRED,
                KnowledgeState.REVOKED,
            }:
                self._set_state(superseded.id, KnowledgeState.REVOKED)
        return self._transition(
            knowledge_id, KnowledgeState.ACTIVE, extra={"activated_at": _now()}
        )

    def revoke(self, knowledge_id: str) -> KnowledgeItem:
        item = self.get(knowledge_id)
        if item is None:
            raise KeyError(knowledge_id)
        if item.state in {KnowledgeState.EXPIRED, KnowledgeState.REVOKED}:
            return item
        return self._transition(knowledge_id, KnowledgeState.REVOKED)

    def expire(self, knowledge_id: str) -> KnowledgeItem:
        """active -> expired."""
        return self._transition(
            knowledge_id, KnowledgeState.EXPIRED, extra={"expired_at": _now()}
        )

    # -- reading ----------------------------------------------------------

    def get(self, knowledge_id: str) -> KnowledgeItem | None:
        row = self.conn.execute(
            "SELECT * FROM knowledge WHERE id = ?", (knowledge_id,)
        ).fetchone()
        return self._from_row(row) if row else None

    def history(self, knowledge_id: str) -> list[KnowledgeItem]:
        """Walk the supersedes chain (oldest first)."""
        chain: list[KnowledgeItem] = []
        seen: set[str] = set()
        current_id: str | None = knowledge_id
        while current_id and current_id not in seen:
            seen.add(current_id)
            item = self.get(current_id)
            if item is None:
                break
            chain.append(item)
            current_id = item.supersedes_id
        chain.reverse()
        return chain

    def list_items(
        self,
        category: str | None = None,
        state: str | None = None,
        q: str | None = None,
    ) -> list[KnowledgeItem]:
        """列出知识条目，支持分类/状态/关键词过滤，按 updated_at 倒序。"""
        sql = "SELECT * FROM knowledge WHERE 1=1"
        params: list[Any] = []
        if category:
            sql += " AND category = ?"
            params.append(category)
        if state:
            sql += " AND state = ?"
            params.append(state)
        if q:
            sql += " AND content LIKE ?"
            params.append(f"%{q}%")
        sql += " ORDER BY updated_at DESC"
        rows = self.conn.execute(sql, params).fetchall()
        return [self._from_row(r) for r in rows]

    # -- internals --------------------------------------------------------

    def _transition(
        self,
        knowledge_id: str,
        target: KnowledgeState,
        extra: dict[str, Any] | None = None,
    ) -> KnowledgeItem:
        item = self.get(knowledge_id)
        if item is None:
            raise KeyError(knowledge_id)
        allowed = _TRANSITIONS[item.state]
        if target not in allowed:
            raise ValueError(
                f"invalid transition {item.state.value} -> {target.value}"
            )
        self._set_state(knowledge_id, target, extra=extra)
        updated = self.get(knowledge_id)
        assert updated is not None
        return updated

    def _set_state(
        self,
        knowledge_id: str,
        state: KnowledgeState,
        confidence: float | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        sets = ["state = ?", "updated_at = ?"]
        params: list[Any] = [state.value, _now()]
        if confidence is not None:
            sets.append("confidence = ?")
            params.append(confidence)
        for column, value in (extra or {}).items():
            sets.append(f"{column} = ?")
            params.append(value)
        params.append(knowledge_id)
        self.conn.execute(
            f"UPDATE knowledge SET {', '.join(sets)} WHERE id = ?", params
        )

    def _from_row(self, row: sqlite3.Row) -> KnowledgeItem:
        return KnowledgeItem(
            id=row["id"],
            category=row["category"],
            state=KnowledgeState(row["state"]),
            content=row["content"],
            supersedes_id=row["supersedes_id"],
            topic_id=row["topic_id"],
            node_ids=json.loads(row["node_ids"] or "[]"),
            entity_ids=json.loads(row["entity_ids"] or "[]"),
            provenance=json.loads(row["provenance"] or "{}"),
            confidence=row["confidence"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            activated_at=row["activated_at"],
            expired_at=row["expired_at"],
            export=bool(row["export"]),
        )