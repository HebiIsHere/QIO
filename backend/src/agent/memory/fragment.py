"""Fragment management: open fragment per topic, close on thresholds.

A fragment is the chunk boundary of the memory domain. The open fragment
is where new messages are appended; closing writes the model summary and
freezes the chunk (append-only).
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

DEFAULT_MAX_MESSAGES = 30
DEFAULT_MAX_TOKENS = 4096


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


@dataclass(frozen=True)
class Fragment:
    id: str
    topic_id: str
    start_message_id: str | None
    end_message_id: str | None
    summary: str | None
    summary_model: str | None
    summary_version: int
    created_at: str
    closed_at: str | None
    meta: dict


class FragmentManager:
    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        max_messages: int = DEFAULT_MAX_MESSAGES,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> None:
        self.conn = conn
        self.max_messages = max_messages
        self.max_tokens = max_tokens

    def get_or_create_open(self, topic_id: str) -> Fragment:
        row = self.conn.execute(
            "SELECT * FROM fragments WHERE topic_id = ? AND closed_at IS NULL "
            "ORDER BY created_at DESC LIMIT 1",
            (topic_id,),
        ).fetchone()
        if row is not None:
            return self._from_row(row)
        fragment_id = new_id("frag")
        now = _now()
        self.conn.execute(
            "INSERT INTO fragments (id, topic_id, created_at, summary_version, meta) "
            "VALUES (?, ?, ?, 0, '{}')",
            (fragment_id, topic_id, now),
        )
        return Fragment(
            id=fragment_id,
            topic_id=topic_id,
            start_message_id=None,
            end_message_id=None,
            summary=None,
            summary_model=None,
            summary_version=0,
            created_at=now,
            closed_at=None,
            meta={},
        )

    def open_fragment(self, topic_id: str) -> Fragment | None:
        """当前开放片段（只读；不存在时返回 None，不创建）。"""
        row = self.conn.execute(
            "SELECT * FROM fragments WHERE topic_id = ? AND closed_at IS NULL "
            "ORDER BY created_at DESC LIMIT 1",
            (topic_id,),
        ).fetchone()
        return self._from_row(row) if row is not None else None

    def get(self, fragment_id: str) -> Fragment | None:
        row = self.conn.execute(
            "SELECT * FROM fragments WHERE id = ?", (fragment_id,)
        ).fetchone()
        return self._from_row(row) if row else None

    def close(
        self,
        fragment_id: str,
        summary: str,
        summary_model: str | None = None,
        summary_version: int = 1,
    ) -> None:
        self.conn.execute(
            "UPDATE fragments SET summary = ?, summary_model = ?, summary_version = ?, "
            "closed_at = ? WHERE id = ?",
            (summary, summary_model, summary_version, _now(), fragment_id),
        )

    def should_close(self, fragment: Fragment) -> bool:
        """Chunk-boundary policy: message count or token estimate threshold."""
        count = self.conn.execute(
            "SELECT COUNT(*) AS c FROM messages WHERE fragment_id = ?", (fragment.id,)
        ).fetchone()["c"]
        if count >= self.max_messages:
            return True
        return False

    def message_count(self, fragment_id: str) -> int:
        return int(
            self.conn.execute(
                "SELECT COUNT(*) AS c FROM messages WHERE fragment_id = ?",
                (fragment_id,),
            ).fetchone()["c"]
        )

    def messages(self, fragment_id: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM messages WHERE fragment_id = ? ORDER BY created_at",
            (fragment_id,),
        ).fetchall()

    def _from_row(self, row: sqlite3.Row) -> Fragment:
        return Fragment(
            id=row["id"],
            topic_id=row["topic_id"],
            start_message_id=row["start_message_id"],
            end_message_id=row["end_message_id"],
            summary=row["summary"],
            summary_model=row["summary_model"],
            summary_version=row["summary_version"],
            created_at=row["created_at"],
            closed_at=row["closed_at"],
            meta=json.loads(row["meta"] or "{}"),
        )
