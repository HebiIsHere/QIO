"""唯一的业务排序入口：把「可选奖励」与「可选重排」作用到候选上一次。

为什么只有这一处：历史上同一批信号算了两遍 —— `Selector` 给召回分叠加
anchor / entity / keyword / recency 奖励，`Retriever` 又按
「相关性 × 时效 × 话题亲和」加权一次，两层之间还夹着一次截断。结果既能重复
计分，又让「原始相关性」名不副实。

现在的分工：

    Selector（召回阶段）   只按底层检索相关程度取候选，分数即原始相关分；
    ranking.rank()（本模块）唯一一处把可选奖励 / 重排作用到候选上；
    Retriever              只做编排（话题指纹、实体卡隔离合并、截断返回）。

默认策略 `RankingPolicy(strategy="relevance")`：所有奖励权重为 0，
`score == relevance`，顺序就是原始相关度顺序；分数相同时按稳定身份
（`doc_id`）确定顺序。任何奖励都必须显式选择 `strategy="weighted"` 并给出
非零权重（那些权重尚未在生产验证，只用于对照实验）。

可解释性：每条结果都带 `relevance`（原始相关分）、`relevance_term`（实际进入
求和的项）、`factors`（各附加因素的实际贡献 = 权重 × 因素值）与 `rank`；
恒等式 `score == relevance_term + sum(factors.values())` 始终成立。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Mapping, Sequence

from agent.selector.base import MemoryCandidate
from agent.selector.tokenize import tokenize
from agent.services.decay import EPHEMERAL, DecayPolicy
from agent.services.params import RankingPolicy, RankingStrategy

FACTOR_TOPIC_AFFINITY = "topic_affinity"
FACTOR_RECENCY = "recency"
FACTOR_KEYWORD = "keyword"
FACTOR_ENTITY = "entity"

#: 重排钩子签名：`(query, 候选) -> 重排后的候选`。只在排序入口内被调用一次，
#: 且必须保持「同一批候选成员」不变（重排只改顺序，不改候选集）。
RerankFn = Callable[[str, Sequence["RankedCandidate"]], Sequence["RankedCandidate"]]


@dataclass(frozen=True)
class RankingContext:
    """排序所需的上下文（话题、实体、时间都由调用方提供，便于钉住做确定性对照）。"""

    query: str = ""
    anchor_topic_id: str | None = None
    #: 查询命中的实体标识（与 `IndexedDoc.entity_ids` 同一标识空间）
    entity_ids: tuple[str, ...] = ()
    #: 话题指纹重合度：topic_id → 0..1（第一跳的联想强度）
    fingerprint_scores: Mapping[str, float] = field(default_factory=dict)
    now: datetime | None = None


@dataclass(frozen=True)
class RankedCandidate:
    doc_id: str
    relevance: float
    score: float
    rank: int
    relevance_term: float = 0.0
    factors: dict[str, float] = field(default_factory=dict)
    sources: tuple[str, ...] = ()
    topic_id: str | None = None
    title: str | None = None
    token_estimate: int = 0
    created_at: str | None = None


def rank(
    candidates: Sequence[MemoryCandidate],
    *,
    ctx: RankingContext,
    policy: RankingPolicy,
    decay: DecayPolicy | None = None,
    kind_of: Callable[[str], str] | None = None,
    rerank: RerankFn | None = None,
) -> list[RankedCandidate]:
    """候选 → 最终顺序。纯相关性策略下不引入任何业务奖励。"""
    items = list(candidates)
    if not items:
        return []

    strategy = RankingStrategy(policy.strategy)
    keyword_weight = policy.keyword_weight
    entity_weight = policy.entity_weight
    affinity_weight = policy.affinity_weight
    recency_weight = policy.recency_weight

    now = ctx.now or datetime.now(timezone.utc)
    decay_policy = decay or DecayPolicy()
    kind_lookup = kind_of or (lambda _doc_id: EPHEMERAL)
    query_tokens = set(tokenize(ctx.query)) if keyword_weight else set()
    wanted_entities = {e.lower() for e in ctx.entity_ids} if entity_weight else set()

    # 加权策略下把相关度归一到 [0,1]，让奖励与相关度处在可比量纲；
    # 纯相关性策略保留原始分（不归一化也不会改变顺序，但原始分要如实可见）。
    max_relevance = max((c.relevance for c in items), default=0.0) or 1.0

    scored: list[RankedCandidate] = []
    for cand in items:
        factors: dict[str, float] = {}
        if strategy is RankingStrategy.WEIGHTED:
            relevance_term = cand.relevance / max_relevance
            if affinity_weight:
                factors[FACTOR_TOPIC_AFFINITY] = affinity_weight * _topic_affinity(cand, ctx)
            if recency_weight:
                factors[FACTOR_RECENCY] = recency_weight * _recency(
                    cand, now, decay_policy, kind_lookup
                )
            if keyword_weight:
                factors[FACTOR_KEYWORD] = keyword_weight * _keyword_overlap(
                    cand, query_tokens
                )
            if entity_weight:
                factors[FACTOR_ENTITY] = entity_weight * _entity_hit(cand, wanted_entities)
            factors = {k: v for k, v in factors.items() if v}
        else:
            relevance_term = cand.relevance
        scored.append(
            RankedCandidate(
                doc_id=cand.doc_id,
                relevance=cand.relevance,
                score=relevance_term + sum(factors.values()),
                rank=0,
                relevance_term=relevance_term,
                factors=factors,
                sources=cand.sources,
                topic_id=cand.topic_id,
                title=cand.title,
                token_estimate=cand.token_estimate,
                created_at=cand.created_at,
            )
        )

    scored.sort(key=lambda c: (-c.score, c.doc_id))
    if rerank is not None:
        head = scored[: policy.rerank_candidate_cap]
        tail = scored[policy.rerank_candidate_cap :]
        rescored = list(rerank(ctx.query, head))
        # 重排只允许改顺序：拿回来的必须还是同一批头部候选（不多、不少、不重复）
        if len(rescored) != len(head) or {c.doc_id for c in rescored} != {
            c.doc_id for c in head
        }:
            raise ValueError("重排必须返回同一批候选（只改顺序，不改候选集）")
        scored = rescored + tail
    return [
        RankedCandidate(
            doc_id=item.doc_id,
            relevance=item.relevance,
            score=item.score,
            rank=index,
            relevance_term=item.relevance_term,
            factors=item.factors,
            sources=item.sources,
            topic_id=item.topic_id,
            title=item.title,
            token_estimate=item.token_estimate,
            created_at=item.created_at,
        )
        for index, item in enumerate(scored, start=1)
    ]


# -- 各因素的实际取值（0..1 量纲；权重与「是否启用」由策略决定） --------------


def _topic_affinity(cand: MemoryCandidate, ctx: RankingContext) -> float:
    if cand.topic_id is None:
        return 0.0
    if ctx.anchor_topic_id and cand.topic_id == ctx.anchor_topic_id:
        return 1.0
    return float(ctx.fingerprint_scores.get(cand.topic_id, 0.0))


def _recency(
    cand: MemoryCandidate,
    now: datetime,
    decay: DecayPolicy,
    kind_of: Callable[[str], str],
) -> float:
    age_days = _age_days(cand.created_at, now)
    if age_days is None:
        return 0.0
    return decay.weight(age_days, kind_of(cand.doc_id))


def _age_days(iso: str | None, now: datetime) -> float | None:
    if not iso:
        return None
    try:
        created = datetime.fromisoformat(iso)
    except ValueError:
        return None
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    days = (now - created).total_seconds() / 86400.0
    return max(0.0, days) if math.isfinite(days) else None


def _keyword_overlap(cand: MemoryCandidate, query_tokens: set[str]) -> float:
    if not query_tokens or not cand.keywords:
        return 0.0
    return len(query_tokens & set(cand.keywords)) / len(query_tokens)


def _entity_hit(cand: MemoryCandidate, wanted: set[str]) -> float:
    if not wanted or not cand.entity_ids:
        return 0.0
    return 1.0 if wanted & {e.lower() for e in cand.entity_ids} else 0.0
