"""Rule layer: deterministic, always-on signal computation.

Signals (additive weights):
- anchor topic affinity: doc belongs to the current anchor topic;
- entity match: query mentions an entity the doc references;
- keyword overlap: query tokens overlap the doc keywords;
- recency: fresher docs get a decaying bonus.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone

from agent.selector.base import IndexedDoc
from agent.selector.tokenize import tokenize

RECENCY_HALF_LIFE_DAYS = 30.0
ANCHOR_TOPIC_WEIGHT = 0.5
ENTITY_WEIGHT = 0.3
KEYWORD_WEIGHT = 0.2


@dataclass
class QueryContext:
    query: str
    anchor_topic_id: str | None = None
    entity_names: list[str] | None = None
    now: datetime | None = None


def _days_ago(iso: str | None, now: datetime) -> float:
    if not iso:
        return float("inf")
    try:
        created = datetime.fromisoformat(iso)
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        return max(0.0, (now - created).total_seconds() / 86400.0)
    except ValueError:
        return float("inf")


def compute_signals(doc: IndexedDoc, ctx: QueryContext) -> dict[str, float]:
    """Return per-signal weights for one doc (never negative)."""
    now = ctx.now or datetime.now(timezone.utc)
    signals: dict[str, float] = {}

    if ctx.anchor_topic_id and doc.topic_id == ctx.anchor_topic_id:
        signals["anchor"] = ANCHOR_TOPIC_WEIGHT

    query_entities = {e.lower() for e in (ctx.entity_names or [])}
    if query_entities and set(doc.entity_ids).intersection(query_entities):
        signals["entity"] = ENTITY_WEIGHT

    query_tokens = set(tokenize(ctx.query))
    if query_tokens and set(doc.keywords).intersection(query_tokens):
        signals["keyword"] = KEYWORD_WEIGHT

    age = _days_ago(doc.created_at, now)
    if math.isfinite(age):
        signals["recency"] = math.exp(-age / RECENCY_HALF_LIFE_DAYS) * 0.4
    return signals


def rule_score(doc: IndexedDoc, ctx: QueryContext) -> float:
    """Sum of rule signals; used to boost recall results and as a fallback ranker."""
    return sum(compute_signals(doc, ctx).values())