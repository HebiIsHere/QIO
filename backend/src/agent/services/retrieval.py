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
    # 片段身份：memory_index 的 doc_id 是索引行 id，Agent 需要的是它对应的
    # fragment_id（才能 continue_from_fragment / 去重）。实体卡等非片段命中为 None。
    fragment_id: str | None = None

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


@dataclass
class MessageHit:
    """一条**保存的对话原文**的命中（不经过摘要、不依赖索引）。"""

    message_id: str
    fragment_id: str | None
    topic_id: str | None
    topic_name: str | None
    role: str
    created_at: str | None
    content: str
    score: float
    truncated: bool = False


class Retriever:
    # 原文检索一次最多扫多少条候选：它是「把说过的话找回来」，不是全库导出。
    # 先按时间取最近的一批，再按命中覆盖率排序取前几条。
    MESSAGE_SCAN_LIMIT = 200
    # 单条原文最多回多少字：够看清「当时到底说了什么」，又不至于把上下文塞满。
    MESSAGE_TEXT_LIMIT = 600

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
        self._fragment_cache: dict[str, str | None] = {}

    def search_messages(
        self,
        query: str,
        *,
        topic_id: str | None = None,
        top_k: int = 3,
    ) -> list[MessageHit]:
        """在**保存的对话原文**里检索（`messages` 表本身，不经过摘要）。

        为什么需要这条路：`search()` 只看 `memory_index`，也就是「封存并摘要成功」
        的片段。还没封存的片段（以及摘要失败的内容）在那儿根本不存在 ——
        用户明明说过，Agent 却回「未找到相关记忆」。

        中文靠 CJK 1/2-gram 分词（见 `selector/tokenize.py`）：单字不参与匹配
        （否则「的」这种字会命中一切），二字词足够认出一句话。
        `topic_id` 是**过滤**而不是偏向：指定了话题就只在这个话题里找。

        这里**故意不缓存**：`Retriever` 与进程同寿，缓存会把「已经删掉的消息」
        一直答出来。每次都读当前的行，删除与修改自然一致。
        """
        if self.conn is None:
            return []
        terms = sorted({t for t in tokenize(query) if len(t) >= 2})
        if not terms:
            return []
        sql = (
            "SELECT m.id AS message_id, m.fragment_id, m.role, m.content, m.created_at, "
            "f.topic_id AS topic_id, n.name AS topic_name "
            "FROM messages m "
            "LEFT JOIN fragments f ON f.id = m.fragment_id "
            "LEFT JOIN nodes n ON n.id = f.topic_id "
            "WHERE m.content <> '' AND ("
            + " OR ".join("m.content LIKE ?" for _ in terms)
            + ")"
        )
        params: list[object] = [f"%{term}%" for term in terms]
        if topic_id:
            sql += " AND f.topic_id = ?"
            params.append(topic_id)
        sql += " ORDER BY m.created_at DESC LIMIT ?"
        params.append(self.MESSAGE_SCAN_LIMIT)
        rows = self.conn.execute(sql, params).fetchall()

        hits: list[MessageHit] = []
        for row in rows:
            content = row["content"] or ""
            low = content.lower()
            matched = sum(1 for term in terms if term in low)
            if not matched:
                continue
            text = content[: self.MESSAGE_TEXT_LIMIT]
            hits.append(
                MessageHit(
                    message_id=row["message_id"],
                    fragment_id=row["fragment_id"],
                    topic_id=row["topic_id"],
                    topic_name=row["topic_name"],
                    role=row["role"],
                    created_at=row["created_at"],
                    content=text,
                    score=matched / len(terms),
                    truncated=len(content) > self.MESSAGE_TEXT_LIMIT,
                )
            )
        # rows 已按时间倒序；稳定排序让「命中一样多」时新的排在前面
        hits.sort(key=lambda hit: -hit.score)
        return hits[:top_k]

    def fragment_of(self, doc_id: str) -> str | None:
        """索引行 → 片段 id（没有 DB 连接时返回 None，例如离线 eval）。"""
        if self.conn is None:
            return None
        if doc_id in self._fragment_cache:
            return self._fragment_cache[doc_id]
        row = self.conn.execute(
            "SELECT fragment_id FROM memory_index WHERE id = ?", (doc_id,)
        ).fetchone()
        value = row["fragment_id"] if row is not None else None
        self._fragment_cache[doc_id] = value
        return value

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
            # 已结束的话题不再参与联想（不给 affinity 加分）；
            # 它的片段仍然留在记忆里，照样能被检索到。
            node = self.topics.nodes.get_topic(fingerprint.topic_id)
            if node is not None and node.meta.get("ended_at"):
                continue
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
                        fragment_id=self.fragment_of(candidate.doc_id),
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
