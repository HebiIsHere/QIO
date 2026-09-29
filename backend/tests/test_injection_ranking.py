# -*- coding: utf-8 -*-
"""注入层：唯一排序入口的配置生效 + 各阶段排除原因可解释。"""

from __future__ import annotations

from agent.selector.base import IndexedDoc
from agent.selector.bm25 import BM25Backend
from agent.selector.selector import Selector
from agent.services import params
from agent.services.injection import (
    BudgetConfig,
    DROP_BELOW_MIN_SCORE,
    DROP_BUDGET,
    DROP_DUPLICATE,
    InjectionAssembler,
    InjectionBudget,
)
from agent.services.retrieval import RetrievalHit, Retriever


class _StubTopics:
    def list_with_fingerprints(self):
        return []


class _StubSource:
    """最小知识源：本组用例只关心记忆候选与预算行为。"""

    def list_active_for_node(self, node_id, limit=5):
        return []

    def recent_fragment_summaries(self, topic_id, limit=2):
        return []


def _hit(doc_id: str, score: float, *, fragment_id=None, topic_id="t1") -> RetrievalHit:
    return RetrievalHit(
        doc_id=doc_id,
        topic_id=topic_id,
        title=doc_id,
        preview="内容 " * 8,
        score=score,
        sources=("bm25",),
        token_estimate=10,
        created_at=None,
        fragment_id=fragment_id,
        relevance=score,
        relevance_term=score,
        rank=1,
        strategy="relevance",
    )


class _FakeRetriever:
    def __init__(self, hits, trace=None) -> None:
        self.hits = list(hits)
        self.last_trace = trace or {
            "strategy": "relevance",
            "policy": params.RANKING.as_dict(),
            "candidate_pool": 12,
            "return_limit": 6,
            "entity_card_hits": [],
        }
        self.calls: list[dict] = []

    def search(self, query, **kwargs):
        self.calls.append({"query": query, **kwargs})
        return list(self.hits)


def _assembler(
    retriever: _FakeRetriever, *, context_window: int = 40_000
) -> InjectionAssembler:
    budget = InjectionBudget(
        BudgetConfig(context_window=context_window, budget_ratio=0.25)
    )
    return InjectionAssembler(budget, retriever, knowledge_source=_StubSource())


def test_build_defers_return_limit_and_forwards_query_entities():
    retriever = _FakeRetriever([_hit("d1", 1.0)])
    asm = _assembler(retriever)

    asm.build("查询", topic_id="t1", query_entity_ids=["e1"])

    assert retriever.calls[0]["top_k"] is None, "返回条数必须由集中配置决定"
    assert retriever.calls[0]["entity_ids"] == ["e1"]
    assert retriever.calls[0]["anchor_topic_id"] == "t1"


def test_plan_records_effective_ranking_config():
    retriever = _FakeRetriever([_hit("d1", 1.0)])
    asm = _assembler(retriever)

    payload = asm.build("查询", topic_id="t1")

    ranking = payload.plan.ranking
    assert ranking["strategy"] == "relevance"
    assert ranking["policy"]["weights"]["affinity"] == 0.0
    assert ranking["candidate_pool"] == 12
    assert ranking["return_limit"] == 6


def test_duplicate_identity_is_dropped_with_reason():
    # 同一片段：一条是检索命中（分数高），一条是别的话题的「仅参考」摘要（分数低）
    retriever = _FakeRetriever([_hit("idx_1", 0.9, fragment_id="frag_1")])
    asm = _assembler(retriever)
    asm.knowledge_source.recent_fragment_summaries = lambda topic_id, limit=2: [
        {"id": "frag_1", "title": "旧摘要", "summary": "同一条历史"}
    ]

    payload = asm.build("查询", topic_id="t1", aux_topic_ids=["t2"])

    reasons = {d["reason"] for d in payload.plan.dropped}
    assert DROP_DUPLICATE in reasons or DROP_BELOW_MIN_SCORE in reasons
    assert len(payload.plan.memory) == 1  # 同一身份只注入一条


def test_below_min_score_and_budget_are_distinguished():
    class _EndedSource(_StubSource):
        def list_active_for_node(self, node_id, limit=5):
            return [
                {
                    "id": "k_ended",
                    "category": "general_fact",
                    "content": "与本轮完全无关的旧结论",
                    "provenance": '{"ended_at": "2026-01-01T00:00:00+00:00"}',
                }
            ]

    retriever = _FakeRetriever([_hit("d1", 0.5)])
    budget = InjectionBudget(BudgetConfig(context_window=40_000, budget_ratio=0.25))
    asm = InjectionAssembler(budget, retriever, knowledge_source=_EndedSource())

    payload = asm.build("任意查询", topic_id="t1")
    assert DROP_BELOW_MIN_SCORE in {d["reason"] for d in payload.plan.dropped}

    # 预算不够：同一条记忆在极小预算下改为 budget_insufficient
    tiny = InjectionBudget(BudgetConfig(context_window=40, budget_ratio=0.25))
    payload2 = InjectionAssembler(
        tiny, retriever, knowledge_source=_StubSource()
    ).build("任意查询", topic_id="t1")
    assert {d["reason"] for d in payload2.plan.dropped} == {DROP_BUDGET}


def test_central_config_drives_the_default_retriever_path(monkeypatch):
    """只改集中配置也必须真的生效 —— 生产装配就是 `Retriever(selector, topics, conn=...)`。"""
    docs = [IndexedDoc(doc_id=f"d{i}", text="共同 主题 内容") for i in range(20)]
    selector = Selector(recall=BM25Backend())
    selector.load(docs)

    tightened = params.RetrievalLimits(candidate_pool=2, inject_return_limit=1)
    monkeypatch.setattr(params, "LIMITS", tightened)

    retriever = Retriever(selector, _StubTopics(), conn=None)
    hits = retriever.search("共同 主题")

    assert len(hits) == 1
    assert retriever.last_trace["candidate_pool"] == 2
