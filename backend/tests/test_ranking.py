# -*- coding: utf-8 -*-
"""唯一业务排序入口：默认只按相关度；可选奖励与重排都在这里生效一次。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from agent.selector.base import MemoryCandidate
from agent.services.params import RankingPolicy, RankingStrategy
from agent.services.ranking import RankingContext, rank

NOW = datetime(2026, 9, 29, 12, 0, 0, tzinfo=timezone.utc)


def _iso(days_ago: float) -> str:
    return (NOW - timedelta(days=days_ago)).isoformat()


def _cand(doc_id: str, relevance: float, **kwargs) -> MemoryCandidate:
    return MemoryCandidate(
        doc_id=doc_id, relevance=relevance, sources=("bm25",), **kwargs
    )


def test_relevance_strategy_keeps_raw_relevance_and_adds_nothing():
    cands = [_cand("a", 0.4), _cand("b", 0.9), _cand("c", 0.9)]
    ranked = rank(cands, ctx=RankingContext(query="q"), policy=RankingPolicy())

    assert [r.doc_id for r in ranked] == ["b", "c", "a"]  # 同分 → 稳定身份序
    assert [r.relevance for r in ranked] == [0.9, 0.9, 0.4]
    assert [r.score for r in ranked] == [0.9, 0.9, 0.4]  # 未叠加任何奖励
    assert all(r.factors == {} for r in ranked)
    assert [r.rank for r in ranked] == [1, 2, 3]


def test_relevance_strategy_refuses_nonzero_bonus_weights():
    with pytest.raises(ValueError):
        RankingPolicy(recency_weight=0.3)
    with pytest.raises(ValueError):
        RankingPolicy(strategy="weighted", affinity_weight=-0.1)


def test_switching_current_topic_does_not_change_relevance_scores():
    cands = [
        _cand("other_topic", 0.9, topic_id="t2"),
        _cand("anchor_topic", 0.5, topic_id="t1"),
    ]
    ctx = RankingContext(query="q", anchor_topic_id="t1")
    ranked = rank(cands, ctx=ctx, policy=RankingPolicy())
    assert [r.doc_id for r in ranked] == ["other_topic", "anchor_topic"]
    assert ranked[0].score == ranked[0].relevance


def test_weighted_strategy_records_factor_contributions_and_stays_exact():
    policy = RankingPolicy(strategy=RankingStrategy.WEIGHTED, recency_weight=1.0)
    cands = [
        _cand("old_but_relevant", 1.0, created_at=_iso(400)),
        _cand("fresh_chatter", 0.5, created_at=_iso(0)),
    ]
    ranked = rank(cands, ctx=RankingContext(query="q", now=NOW), policy=policy)
    assert ranked[0].doc_id == "fresh_chatter"  # 显式开启时效后，新闲聊才能压过旧的
    assert ranked[0].factors["recency"] == pytest.approx(1.0, abs=1e-6)
    assert ranked[0].score == pytest.approx(
        ranked[0].relevance_term + sum(ranked[0].factors.values())
    )
    # 未开启的奖励不得出现（关键词 / 实体仍为 0 权重）
    assert set(ranked[0].factors) == {"recency"}


def test_topic_affinity_only_applies_when_explicitly_enabled():
    cands = [
        _cand("other_topic", 0.6, topic_id="t2"),
        _cand("anchor_topic", 0.5, topic_id="t1"),
        _cand("fingerprinted", 0.4, topic_id="t3"),
    ]
    ctx = RankingContext(
        query="q",
        anchor_topic_id="t1",
        fingerprint_scores={"t3": 0.5},
    )
    weighted = rank(
        cands,
        ctx=ctx,
        policy=RankingPolicy(strategy="weighted", affinity_weight=1.0),
    )
    assert weighted[0].doc_id == "anchor_topic"  # 锚点话题 1.0
    assert weighted[1].doc_id == "fingerprinted"  # 指纹重合度 0.5
    assert weighted[0].factors["topic_affinity"] == pytest.approx(1.0)
    assert weighted[1].factors["topic_affinity"] == pytest.approx(0.5)
    assert weighted[2].factors == {}  # 与查询话题无关的候选不加分


def test_keyword_and_entity_factors_stay_off_by_default():
    cands = [
        _cand("kw", 0.2, keywords=("饮食", "清淡")),
        _cand("ent", 0.3, entity_ids=("e_milk",)),
    ]
    ctx = RankingContext(query="饮食 清淡", entity_ids=("e_milk",))
    assert [r.doc_id for r in rank(cands, ctx=ctx, policy=RankingPolicy())] == ["ent", "kw"]

    weighted = rank(
        cands,
        ctx=ctx,
        policy=RankingPolicy(strategy="weighted", keyword_weight=1.0, entity_weight=1.0),
    )
    by_id = {r.doc_id: r for r in weighted}
    # 关键词因素 = 查询词与记忆关键词的重合比例（与旧 rules.py 同一口径）
    assert by_id["kw"].factors["keyword"] == pytest.approx(2 / 6)
    assert by_id["ent"].factors["entity"] == pytest.approx(1.0)


def test_rerank_runs_once_inside_the_entry_and_is_not_overridden():
    calls: list[str] = []

    def fake_rerank(query, ranked):
        calls.append(query)
        return list(reversed(ranked))

    cands = [_cand("a", 0.9), _cand("b", 0.5), _cand("c", 0.1)]
    ranked = rank(
        cands,
        ctx=RankingContext(query="q"),
        policy=RankingPolicy(),
        rerank=fake_rerank,
    )
    assert calls == ["q"]  # 恰好执行一次
    assert [r.doc_id for r in ranked] == ["c", "b", "a"]
    assert [r.rank for r in ranked] == [1, 2, 3]  # 名次跟着重排结果走
    assert [r.relevance for r in ranked] == [0.1, 0.5, 0.9]  # 原始相关分不被改写


def test_empty_candidates_are_returned_unchanged():
    assert rank([], ctx=RankingContext(query="q"), policy=RankingPolicy()) == []
