"""Selector interfaces.

The selector answers one question: which memory fragments are relevant to
the current user request. It must be fast and deterministic-first:
- rule layer: always on (anchor topic, entities, recency);
- recall layer: pluggable (BM25 / ONNX embeddings / remote API);
- rerank layer: optional, off by default.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass(frozen=True)
class IndexedDoc:
    doc_id: str
    text: str
    topic_id: str | None = None
    entity_ids: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    created_at: str | None = None


@dataclass(frozen=True)
class ScoredDoc:
    doc_id: str
    score: float
    source: str


@dataclass(frozen=True)
class MemoryCandidate:
    doc_id: str
    score: float
    sources: tuple[str, ...]
    topic_id: str | None = None
    title: str | None = None
    token_estimate: int = 0

    def merge_score(self, extra: float, source: str) -> "MemoryCandidate":
        return MemoryCandidate(
            doc_id=self.doc_id,
            score=self.score + extra,
            sources=self.sources + (source,),
            topic_id=self.topic_id,
            title=self.title,
            token_estimate=self.token_estimate,
        )


class RecallBackend(ABC):
    """A pluggable recall layer over the memory index."""

    name: str

    @abstractmethod
    def available(self) -> bool:
        """Self-check; called at startup to pick the backend tier."""

    @abstractmethod
    def index(self, docs: list[IndexedDoc]) -> None:
        """Rebuild or update the in-memory index."""

    @abstractmethod
    def search(self, query: str, top_k: int) -> list[ScoredDoc]:
        raise NotImplementedError


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()