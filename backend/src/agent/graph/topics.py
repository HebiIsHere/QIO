"""Topic service: fingerprints for cross-session first-hop retrieval.

A fingerprint aggregates the closed fragments of a topic (titles and
keywords from the mechanical index) so retrieval can hop to the right
topic before scanning within it.
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from dataclasses import dataclass

from agent.graph.nodes import NodeService


@dataclass(frozen=True)
class TopicFingerprint:
    topic_id: str
    title: str
    keywords: list[str]
    fragment_count: int
    last_activity: str | None
    summary_preview: str | None


class TopicService:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.nodes = NodeService(conn)

    def fingerprint(self, topic_id: str, recent_n: int = 5) -> TopicFingerprint:
        node = self.nodes.get_topic(topic_id)
        title = node.name if node is not None else topic_id
        fragments = self.conn.execute(
            "SELECT id, summary FROM fragments WHERE topic_id = ? AND closed_at IS NOT NULL "
            "ORDER BY created_at DESC LIMIT ?",
            (topic_id, recent_n),
        ).fetchall()
        keyword_counter: Counter[str] = Counter()
        preview: str | None = None
        for fragment in fragments:
            if preview is None and fragment["summary"]:
                preview = fragment["summary"][:80]
            rows = self.conn.execute(
                "SELECT keywords FROM memory_index WHERE fragment_id = ?", (fragment["id"],)
            ).fetchall()
            for row in rows:
                keyword_counter.update(json.loads(row["keywords"] or "[]"))
        keywords = [k for k, _ in keyword_counter.most_common(10)]
        last_activity = self._last_activity(topic_id) if fragments else None
        return TopicFingerprint(
            topic_id=topic_id,
            title=title,
            keywords=keywords,
            fragment_count=len(fragments),
            last_activity=last_activity,
            summary_preview=preview,
        )

    def list_with_fingerprints(self, recent_n: int = 5) -> list[TopicFingerprint]:
        return [self.fingerprint(t.id, recent_n=recent_n) for t in self.nodes.list_topics()]

    def _last_activity(self, topic_id: str) -> str | None:
        row = self.conn.execute(
            "SELECT MAX(created_at) AS m FROM fragments WHERE topic_id = ?", (topic_id,)
        ).fetchone()
        return row["m"] if row else None