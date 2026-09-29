"""跨会话检索：编排 + 一次业务排序。

Pipeline:
1. first hop: topic fingerprints（话题级元摘要）与查询的重合度 —— 只是候选，
   是否加分由排序策略决定；
2. global recall: M5 selector 按**原始检索相关程度**取候选池（无业务奖励）；
3. ranking: 交给唯一排序入口 `agent/services/ranking.py` 执行一次；
4. entity cards: 实体卡是**另一种类型**的命中（名称/向量命中，不是同量纲的相关分），
   这里显式隔离合并 —— 不得把整体结果宣称为「纯相关性」，见
   `_entity_card_hits` 的注释与 docs/status.md 的限制说明。

历史缺陷（本次消除）：这里曾再算一遍
`0.4 × 相关 + 0.25 × 时效 + 0.35 × 话题亲和`，而「相关」已经是 Selector 里
叠加过奖励的分数 —— 同一批信号算两遍，配置还有两份默认值。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from agent.graph.topics import TopicService
from agent.selector.selector import Selector
from agent.selector.tokenize import tokenize
from agent.services import params
from agent.services.decay import EPHEMERAL, DecayPolicy
from agent.services.ranking import RankingContext, rank

# 实体卡命中的固定分（名称命中的强信号）。它与 BM25 / 余弦分**不是同一量纲**，
# 跨类型合并只是为了「交流锚点」的历史行为；同类型对照（纯相关性实验）只看
# memory 来源的命中。
ENTITY_NAME_HIT_SCORE = 1.0


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
    # -- 可解释性（排序入口给出的中间量） --------------------------------
    #: 原始检索相关分（未被任何业务奖励修改）
    relevance: float = 0.0
    #: 实际进入求和的项（纯相关性策略下等于 relevance；加权策略下是归一化后的相关度）
    relevance_term: float = 0.0
    #: 各附加因素的实际贡献（权重 × 因素值），默认策略下为空
    factors: dict = field(default_factory=dict)
    #: 在排序入口里的名次（1 起）
    rank: int = 0
    #: 生效的排序策略名
    strategy: str = "relevance"

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
        *,
        policy: params.RankingPolicy | None = None,
        limits: params.RetrievalLimits | None = None,
        conn=None,
        decay: DecayPolicy | None = None,
    ) -> None:
        self.selector = selector
        self.topics = topics
        # 唯一的配置来源：services/params.py（默认 = 纯相关性 + 候选池 12 / 返回 6）。
        self.policy = policy or params.RANKING
        self.limits = limits or params.LIMITS
        self.conn = conn
        self.decay = decay or DecayPolicy(
            {EPHEMERAL: self.policy.recency_half_life_days}
        )
        self._fragment_cache: dict[str, str | None] = {}
        #: 最近一次 search 的排序留痕（候选池 / 入选 / 生效配置），供 Trace 与评测读取。
        self.last_trace: dict = {}

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
        entity_ids: tuple[str, ...] | list[str] | None = None,
        top_k: int | None = None,
        now: datetime | None = None,
        rerank=None,
    ) -> list[RetrievalHit]:
        limit = max(
            1, int(top_k if top_k is not None else self.limits.inject_return_limit)
        )
        # 候选池单独表达：底层召回只被请求「候选池」这么多条（无隐藏倍增）。
        pool = self.limits.pool_for(limit)
        candidates = self.selector.select(query, candidate_pool=pool)
        if not candidates:
            # 无记忆候选时仍允许实体卡命中（交流锚点）
            entity_hits = self._entity_card_hits(query, top_k=limit)
            self.last_trace = self._trace(pool, limit, [], entity_hits, [])
            return [hit for _, hit in entity_hits[:limit]]

        fingerprint_scores = self._fingerprint_scores(query)
        # 唯一的一次业务排序：可选奖励与重排都只在这里发生。
        ranked = rank(
            candidates,
            ctx=RankingContext(
                query=query,
                anchor_topic_id=anchor_topic_id,
                entity_ids=tuple(entity_ids or ()),
                fingerprint_scores=fingerprint_scores,
                now=now,
            ),
            policy=self.policy,
            decay=self.decay,
            kind_of=self.kind_of,
            rerank=rerank,
        )
        ranked_hits: list[tuple[float, RetrievalHit]] = [
            (
                item.score,
                RetrievalHit(
                    doc_id=item.doc_id,
                    topic_id=item.topic_id,
                    title=item.title,
                    preview=self._preview(item.doc_id, item.title),
                    score=item.score,
                    sources=item.sources,
                    token_estimate=item.token_estimate,
                    created_at=self._created_at(item.doc_id),
                    fragment_id=self.fragment_of(item.doc_id),
                    relevance=item.relevance,
                    relevance_term=item.relevance_term,
                    factors=dict(item.factors),
                    rank=item.rank,
                    strategy=self.policy.strategy.value,
                ),
            )
            for item in ranked
        ]
        # 跨类型合并（实体卡）：与记忆命中不是同一量纲，这里显式隔离 —— 保留历史行为，
        # 本轮不顺带重写实体匹配，也不把它算进「纯相关性」的结论里。
        entity_hits = self._entity_card_hits(query, top_k=limit)
        merged = ranked_hits + entity_hits
        merged.sort(key=lambda pair: (-pair[0], pair[1].doc_id))
        selected = [hit for _, hit in merged[:limit]]
        self.last_trace = self._trace(pool, limit, ranked, entity_hits, selected)
        return selected

    def _trace(self, pool: int, limit: int, ranked, entity_hits, selected) -> dict:
        """本次检索的留痕：生效配置 + 候选池 + 入选 + 跨类型命中。

        只记录稳定身份与分数，不记录查询原文与记忆正文（生产 Trace 不落
        完整私人对话）。评测侧需要逐条输入时由 eval 自己记录。
        """
        return {
            "strategy": self.policy.strategy.value,
            "policy": self.policy.as_dict(),
            "limits": self.limits.as_dict(),
            "candidate_pool": pool,
            "return_limit": limit,
            "pool": [
                {
                    "doc_id": item.doc_id,
                    "relevance": round(item.relevance, 6),
                    "sources": list(item.sources),
                }
                for item in ranked
            ],
            "ranked": [
                {
                    "doc_id": item.doc_id,
                    "relevance": round(item.relevance, 6),
                    "factors": {k: round(v, 6) for k, v in item.factors.items()},
                    "score": round(item.score, 6),
                    "rank": item.rank,
                }
                for item in ranked
            ],
            "entity_card_hits": [
                {"doc_id": hit.doc_id, "score": round(score, 6)}
                for score, hit in entity_hits
            ],
            "selected": [hit.doc_id for hit in selected],
        }

    def _entity_card_hits(self, query: str, top_k: int) -> list[tuple[float, RetrievalHit]]:
        """消息命中实体卡（名称/别名，或向量相似）→ 返回卡内容作为检索命中。

        **类型隔离**：实体卡的分数是「名称命中的固定强信号」或「实体卡向量的
        余弦相似度」，与记忆片段的 BM25 分不是同一量纲。这里的合并是历史行为
        （实体卡是「交流锚点」，需要优先出现），保留但显式标注 `sources=("entity_card",)`；
        纯相关性对照只在 `sources=("bm25",)` / 同一向量后端这同类候选上做。
        """
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
                    ENTITY_NAME_HIT_SCORE,
                    RetrievalHit(
                        doc_id=card.id,
                        topic_id=None,
                        title=card.name,
                        preview=card.summary or "",
                        score=ENTITY_NAME_HIT_SCORE,
                        sources=("entity_card",),
                        token_estimate=0,
                        created_at=None,
                        relevance=ENTITY_NAME_HIT_SCORE,
                        relevance_term=ENTITY_NAME_HIT_SCORE,
                        strategy="entity_card",
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
                            relevance=score,
                            relevance_term=score,
                            strategy="entity_card",
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
