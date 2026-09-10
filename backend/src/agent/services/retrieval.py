"""Cross-session retrieval with the three-weight ranking.

Pipeline:
1. first hop: topic fingerprints (session-level meta summaries) match the
   query — topics that score get an affinity bonus;
2. global recall: the M5 selector over memory_index fragments (relevance);
3. ranking: relevance (BM25, normalized) x relevance_weight +
   recency decay x recency_weight + topic affinity x affinity_weight.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone

from agent.graph.topics import TopicService
from agent.services.decay import EPHEMERAL, DecayPolicy
from agent.selector.base import IndexedDoc
from agent.selector.selector import Selector
from agent.selector.tokenize import tokenize

DEFAULT_RECENCY_HALF_LIFE_DAYS = 30.0


@dataclass
class RetrievalConfig:
    relevance_weight: float = 0.4
    recency_weight: float = 0.25
    affinity_weight: float = 0.35
    recency_half_life_days: float = DEFAULT_RECENCY_HALF_LIFE_DAYS
    fingerprint_top: int = 3


@dataclass
class RetrievalHit:
    doc_id: str
    topic_id: str | None
    title: str | None
    preview: str
    score: float
    sources: tuple[str, ...]
    token_estimate: int
    created_at: str | None

    @property
    def age_days(self) -> float:
        if not self.created_at:
            return float("inf")
        try:
            created = datetime.fromisoformat(self.created_at)
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            return max(0.0, (datetime.now(timezone.utc) - created).total_seconds() / 86400.0)
        except ValueError:
            return float("inf")


class Retriever:
    def __init__(
        self,
        selector: Selector,
        topics: TopicService,
        config: RetrievalConfig | None = None,
        conn=None,
        decay: DecayPolicy | None = None,
    ) -> None:
        self.selector = selector
        self.topics = topics
        self.config = config or RetrievalConfig()
        self.conn = conn
        # 默认策略与旧行为一致（ephemeral half-life == recency_half_life_days）
        self.decay = decay or DecayPolicy({EPHEMERAL: self.config.recency_half_life_days})

    def kind_of(self, doc_id: str) -> str:
        """信息种类（供差异化衰减）。当前 memory_index 无可信分类 → ephemeral。

        未来若有可靠 category metadata，可在此按 doc_id 返回对应 kind，
        无需改动排序主逻辑（见 agent/services/decay.py）。
        """
        return EPHEMERAL

    # -- first hop: topic fingerprints ------------------------------------

    def _fingerprint_scores(self, query: str) -> dict[str, float]:
        query_tokens = set(tokenize(query))
        scores: dict[str, float] = {}
        if not query_tokens:
            return scores
        for fingerprint in self.topics.list_with_fingerprints():
            overlap = query_tokens & set(fingerprint.keywords)
            if overlap:
                scores[fingerprint.topic_id] = len(overlap) / len(query_tokens)
        return scores

    # -- main search ------------------------------------------------------

    def search(
        self,
        query: str,
        *,
        anchor_topic_id: str | None = None,
        top_k: int = 6,
    ) -> list[RetrievalHit]:
        candidates = self.selector.select(
            query,
            top_k=top_k * 2,
            anchor_topic_id=anchor_topic_id,
        )
        if not candidates:
            # 无记忆候选时仍允许实体卡命中（交流锚点）
            entity_hits = self._entity_card_hits(query, top_k=top_k)
            return [hit for _, hit in entity_hits[:top_k]]
        fingerprint_scores = self._fingerprint_scores(query)
        max_relevance = max((c.score for c in candidates), default=1.0) or 1.0

        ranked: list[tuple[float, RetrievalHit]] = []
        for candidate in candidates:
            relevance_norm = candidate.score / max_relevance
            recency = 0.0
            created_at = self._created_at(candidate.doc_id)
            if created_at is not None:
                age_days = self._age_days(created_at)
                if math.isfinite(age_days):
                    recency = self.decay.weight(age_days, self.kind_of(candidate.doc_id))
            affinity = 0.0
            if candidate.topic_id is not None:
                if anchor_topic_id and candidate.topic_id == anchor_topic_id:
                    affinity = 1.0
                elif candidate.topic_id in fingerprint_scores:
                    affinity = fingerprint_scores[candidate.topic_id]
            score = (
                self.config.relevance_weight * relevance_norm
                + self.config.recency_weight * recency
                + self.config.affinity_weight * affinity
            )
            ranked.append(
                (
                    score,
                    RetrievalHit(
                        doc_id=candidate.doc_id,
                        topic_id=candidate.topic_id,
                        title=candidate.title,
                        preview=self._preview(candidate.doc_id, candidate.title),
                        score=score,
                        sources=candidate.sources,
                        token_estimate=candidate.token_estimate,
                        created_at=created_at,
                    ),
                )
            )
        # 实体卡检索（交流锚点）：名称命中（强）+ 向量命中（增强）
        entity_hits = self._entity_card_hits(query, top_k=top_k)
        ranked = ranked + entity_hits
        ranked.sort(key=lambda pair: (-pair[0], pair[1].doc_id))
        return [hit for _, hit in ranked[:top_k]]

    def _entity_card_hits(self, query: str, top_k: int) -> list[tuple[float, RetrievalHit]]:
        """消息命中实体卡（名称/别名，或向量相似）→ 返回卡内容作为检索命中。"""
        if self.conn is None:
            return []
        from agent.entities.cards import EntityCardService

        svc = EntityCardService(self.conn)
        hits: list[tuple[float, RetrievalHit]] = []
        seen: set[str] = set()
        # 名称命中（强信号）
        for card in svc.match_cards(query):
            seen.add(card.id)
            hits.append(
                (
                    1.0,
                    RetrievalHit(
                        doc_id=card.id,
                        topic_id=None,
                        title=card.name,
                        preview=card.summary or "",
                        score=1.0,
                        sources=("entity_card",),
                        token_estimate=0,
                        created_at=None,
                    ),
                )
            )
        # 向量命中（增强；无向量/embedding 时跳过）
        recall = self.selector.recall
        if recall is not None and hasattr(recall, "entity_card_search") and recall.available():
            for card_id, score in recall.entity_card_search(query, top_k=top_k):
                if card_id in seen:
                    continue
                card = svc.get(card_id)
                if card is None:
                    continue
                seen.add(card_id)
                hits.append(
                    (
                        score,
                        RetrievalHit(
                            doc_id=card.id,
                            topic_id=None,
                            title=card.name,
                            preview=card.summary or "",
                            score=score,
                            sources=("entity_card",),
                            token_estimate=0,
                            created_at=None,
                        ),
                    )
                )
        return hits[:top_k]

    # -- internals --------------------------------------------------------

    def _created_at(self, doc_id: str) -> str | None:
        return self.selector.created_at(doc_id)

    def _preview(self, doc_id: str, title: str | None) -> str:
        # inject summary content (index doc text) instead of just the title
        text = self.selector.text_of(doc_id)
        if text and text.strip():
            return text
        return title or doc_id

    def _age_days(self, iso: str) -> float:
        try:
            created = datetime.fromisoformat(iso)
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            return max(0.0, (datetime.now(timezone.utc) - created).total_seconds() / 86400.0)
        except ValueError:
            return float("inf")
