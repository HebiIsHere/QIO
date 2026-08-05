"""Memory writer: append-only transcript ingestion.

Discipline: the main model is the only writer of summaries; this module
only records what happened (append-only) and triggers chunk closing when
the fragment thresholds are hit. The summarizer callback is injected so
callers control the (async) model call.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Callable

from agent.memory.fragment import Fragment, FragmentManager, new_id


class MemoryWriter:
    def __init__(
        self,
        conn: sqlite3.Connection,
        fragments: FragmentManager,
    ) -> None:
        self.conn = conn
        self.fragments = fragments

    def append_message(
        self,
        *,
        topic_id: str,
        role: str,
        content: str,
        content_type: str = "text",
        model: str | None = None,
        raw: dict | None = None,
    ) -> tuple[str, Fragment | None]:
        """Append one message; returns (message_id, closed_fragment).

        closed_fragment is non-None when this append crossed the chunk
        threshold and the fragment was closed. Closing itself does not
        summarize — the caller invokes the summarizer via
        close_open_fragment().
        """
        fragment = self.fragments.get_or_create_open(topic_id)
        message_id = new_id("msg")
        now = self._now()
        self.conn.execute(
            "INSERT INTO messages (id, fragment_id, role, content, content_type, "
            "model, raw, created_at, storage_tier) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'hot')",
            (
                message_id,
                fragment.id,
                role,
                content,
                content_type,
                model,
                json.dumps(raw or {}, ensure_ascii=False),
                now,
            ),
        )
        if fragment.start_message_id is None:
            self.conn.execute(
                "UPDATE fragments SET start_message_id = ? WHERE id = ?",
                (message_id, fragment.id),
            )
        self.conn.execute(
            "UPDATE fragments SET end_message_id = ? WHERE id = ?",
            (message_id, fragment.id),
        )
        closed: Fragment | None = None
        if self.fragments.should_close(fragment):
            closed = self.fragments.get(fragment.id)
        return message_id, closed

    def close_open_fragment(
        self,
        topic_id: str,
        summarizer: Callable[[Fragment, list[sqlite3.Row]], str | None],
        *,
        summary_model: str | None = None,
    ) -> Fragment | None:
        """Close the open fragment for a topic using the injected summarizer.

        summarizer returns the summary text, or None on failure (degraded:
        the fragment is closed with an empty summary; the index falls back
        to direct citation of the raw text).
        """
        fragment = self.fragments.get_or_create_open(topic_id)
        if fragment.start_message_id is None:
            return None  # nothing to summarize
        messages = self.fragments.messages(fragment.id)
        summary = summarizer(fragment, messages)
        if summary:
            self.fragments.close(fragment.id, summary, summary_model=summary_model)
        else:
            self.fragments.close(fragment.id, "", summary_model=None, summary_version=0)
        return self.fragments.get(fragment.id)

    def _now(self) -> str:
        from datetime import datetime, timezone

        return datetime.now(timezone.utc).isoformat()