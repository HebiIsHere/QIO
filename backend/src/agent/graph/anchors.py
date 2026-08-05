"""Anchor service: current topic + fragment position.

Rules (agreed design): single active anchor; topic switches are debounced
through a pending row (confirm within the debounce window or the pending
target is discarded); moving does not discard the previous position — it
is preserved as history per topic.
"""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class Anchor:
    topic_id: str | None
    fragment_id: str | None
    anchor_type: str  # active / pending / history
    updated_at: str


class AnchorService:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def get_active(self) -> Anchor | None:
        return self._get_type("active")

    def get_pending(self) -> Anchor | None:
        return self._get_type("pending")

    def set_active(self, topic_id: str, fragment_id: str | None = None) -> Anchor:
        """Move the current active anchor into history, then set a new one."""
        current = self.get_active()
        now = _now()
        if current is not None and current.topic_id != topic_id:
            self._insert("history", current.topic_id, current.fragment_id, now)
        self._upsert_singleton("active", topic_id, fragment_id, now)
        return self.get_active()  # type: ignore[return-value]

    def request_pending(self, topic_id: str, fragment_id: str | None = None) -> Anchor:
        """Debounce entry: candidate anchor before switching."""
        now = _now()
        self._upsert_singleton("pending", topic_id, fragment_id, now)
        pending = self.get_pending()
        assert pending is not None
        return pending

    def confirm_pending(self) -> Anchor | None:
        """pending -> active (the switch is confirmed)."""
        pending = self.get_pending()
        if pending is None or pending.topic_id is None:
            return None
        self.set_active(pending.topic_id, pending.fragment_id)
        self.conn.execute("DELETE FROM cursor WHERE anchor_type = 'pending'")
        return self.get_active()

    def discard_pending(self) -> None:
        """The pending switch did not settle; keep the current anchor."""
        self.conn.execute("DELETE FROM cursor WHERE anchor_type = 'pending'")

    def get_position(self, topic_id: str) -> Anchor | None:
        """Most recent known position for a topic (active or history)."""
        active = self.get_active()
        if active is not None and active.topic_id == topic_id:
            return active
        row = self.conn.execute(
            "SELECT * FROM cursor WHERE anchor_type = 'history' AND topic_id = ? "
            "ORDER BY updated_at DESC LIMIT 1",
            (topic_id,),
        ).fetchone()
        return self._from_row(row) if row else None

    def history(self, topic_id: str) -> list[Anchor]:
        rows = self.conn.execute(
            "SELECT * FROM cursor WHERE anchor_type = 'history' AND topic_id = ? "
            "ORDER BY updated_at DESC",
            (topic_id,),
        ).fetchall()
        return [self._from_row(r) for r in rows]

    # -- internals --------------------------------------------------------

    def _get_type(self, anchor_type: str) -> Anchor | None:
        row = self.conn.execute(
            "SELECT * FROM cursor WHERE anchor_type = ?", (anchor_type,)
        ).fetchone()
        return self._from_row(row) if row else None

    def _upsert_singleton(self, anchor_type: str, topic_id: str | None, fragment_id: str | None, now: str) -> None:
        existing = self._get_type(anchor_type)
        if existing is not None:
            self.conn.execute(
                "UPDATE cursor SET topic_id = ?, fragment_id = ?, updated_at = ? "
                "WHERE anchor_type = ?",
                (topic_id, fragment_id, now, anchor_type),
            )
        else:
            self.conn.execute(
                "INSERT INTO cursor (id, topic_id, fragment_id, anchor_type, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (anchor_type, topic_id, fragment_id, anchor_type, now),
            )

    def _insert(self, anchor_type: str, topic_id: str | None, fragment_id: str | None, now: str) -> None:
        self.conn.execute(
            "INSERT INTO cursor (id, topic_id, fragment_id, anchor_type, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (f"hist_{uuid.uuid4().hex[:12]}", topic_id, fragment_id, anchor_type, now),
        )

    def _from_row(self, row: sqlite3.Row) -> Anchor:
        return Anchor(
            topic_id=row["topic_id"],
            fragment_id=row["fragment_id"],
            anchor_type=row["anchor_type"],
            updated_at=row["updated_at"],
        )