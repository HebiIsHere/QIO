"""Mechanical memory index: keywords, entity links, token estimates.

The index is code-generated; the model never writes here. It feeds the
selector (M5) and the injection budget (M9).
"""

from __future__ import annotations

import sqlite3
from collections import Counter
from datetime import datetime, timezone

from agent.selector.tokenize import tokenize

try:
    import tiktoken

    _ENCODER = tiktoken.get_encoding("cl100k_base")
except Exception:  # pragma: no cover - fallback when tiktoken unavailable
    _ENCODER = None


def estimate_tokens(text: str) -> int:
    if _ENCODER is not None:
        return len(_ENCODER.encode(text))
    return max(1, len(text) // 4)


def extract_keywords(texts: list[str], top_n: int = 10) -> list[str]:
    counts: Counter[str] = Counter()
    for text in texts:
        counts.update(tokenize(text))
    stop = {
        "用户", "一个", "这个", "那个", "什么", "可以", "进行", "需要",
        "然后", "如果", "但是", "以及", "我们", "你们", "他们", "因为",
        "所以", "没有", "就是", "觉得", "可能", "应该", "怎么", "多少",
        "the", "and", "for", "with", "from", "that", "this", "are",
    }
    ranked = [(count, token) for token, count in counts.items() if token not in stop]
    ranked.sort(reverse=True)
    return [token for _, token in ranked[:top_n]]


def match_entity_ids(conn: sqlite3.Connection, names: list[str]) -> list[str]:
    """Map entity names to existing entity node ids (lazy creation is M8)."""
    if not names:
        return []
    placeholders = ",".join("?" for _ in names)
    rows = conn.execute(
        f"SELECT id FROM nodes WHERE type = 'entity' AND name IN ({placeholders})",
        names,
    ).fetchall()
    return [row["id"] for row in rows]


class IndexBuilder:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def build(
        self,
        *,
        fragment_id: str,
        topic_id: str,
        title: str | None,
        summary_text: str,
        entities: list[str],
        keywords: list[str],
        message_texts: list[str],
    ) -> dict:
        """Write one memory_index row; returns the serialized index entry."""
        from agent.memory.fragment import new_id

        all_keywords = keywords or extract_keywords([summary_text, *message_texts])
        token_estimate = estimate_tokens(summary_text) + sum(
            estimate_tokens(t) for t in message_texts
        )
        entity_ids = match_entity_ids(self.conn, entities)
        index_id = new_id("idx")
        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute(
            "INSERT INTO memory_index (id, fragment_id, topic_id, entity_ids, "
            "keywords, title, token_estimate, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                index_id,
                fragment_id,
                topic_id,
                _json(entity_ids),
                _json(all_keywords),
                title,
                token_estimate,
                now,
            ),
        )
        return {
            "index_id": index_id,
            "fragment_id": fragment_id,
            "topic_id": topic_id,
            "entity_ids": entity_ids,
            "keywords": all_keywords,
            "title": title,
            "token_estimate": token_estimate,
        }


def _json(value: list[str]) -> str:
    import json

    return json.dumps(value, ensure_ascii=False)