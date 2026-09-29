# -*- coding: utf-8 -*-
"""Retriever 只做编排：候选池 / 返回数 / 一次排序 / 跨类型隔离。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

# 先加载 core：`agent.tools` 包与 `agent.core.loop` 之间存在既有的导入顺序依赖
# （生产路径里 core 总是先被导入）。这里只为了让本测试可以单独运行。
import agent.core.loop  # noqa: F401

from agent.selector.base import IndexedDoc
from agent.selector.bm25 import BM25Backend
from agent.selector.selector import Selector
from agent.services import params
from agent.services.retrieval import Retriever


class _TopicsStub:
    def list_with_fingerprints(self):
        return []


class _SpyBackend(BM25Backend):
    """记录底层召回被请求了多少条。"""

    def __init__(self) -> None:
        super().__init__()
        self.requested: list[int] = []

    def search(self, query: str, top_k: int):
        self.requested.append(top_k)
        return super().search(query, top_k=top_k)


def _iso(days_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


def _retriever(docs, **kwargs) -> tuple[Retriever, _SpyBackend]:
    backend = _SpyBackend()
    selector = Selector(recall=backend)
    selector.load(docs)
    return Retriever(selector, _TopicsStub(), **kwargs), backend


def test_backend_recall_request_is_the_candidate_pool():
    docs = [IndexedDoc(doc_id=f"d{i}", text="共同 主题 内容") for i in range(40)]
    retriever, backend = _retriever(docs)

    retriever.search("共同 主题", top_k=params.LIMITS.inject_return_limit)

    # 旧链路是「返回 6 → 候选池 12 → 底层召回 36」；现在底层请求数 = 候选池
    assert backend.requested == [params.LIMITS.candidate_pool]


def test_explicit_candidate_pool_30_does_not_become_180():
    docs = [IndexedDoc(doc_id=f"d{i}", text="共同 主题 内容") for i in range(400)]
    limits = params.RetrievalLimits(candidate_pool=30, inject_return_limit=6)
    retriever, backend = _retriever(docs, limits=limits)

    retriever.search("共同 主题", top_k=6)

    assert backend.requested == [30]
    assert retriever.last_trace["candidate_pool"] == 30
    assert retriever.last_trace["return_limit"] == 6


def test_candidate_pool_grows_when_the_caller_asks_for_more_than_the_default():
    docs = [IndexedDoc(doc_id=f"d{i}", text="共同 主题 内容") for i in range(60)]
    retriever, backend = _retriever(docs)

    hits = retriever.search("共同 主题", top_k=20)

    assert backend.requested == [20], "候选池必须至少能装下最终返回条数"
    assert len(hits) <= 20


def test_older_but_more_relevant_is_not_overtaken_by_fresh_chatter():
    docs = [
        IndexedDoc(
            doc_id="old_decision",
            text="sqlite 迁移 方案 决定 记录",
            topic_id="t_other",
            created_at=_iso(500),
        ),
        IndexedDoc(
            doc_id="fresh_chatter",
            text="sqlite 迁移",
            topic_id="t_anchor",
            created_at=_iso(0),
        ),
    ]
    retriever, _ = _retriever(docs)

    # 当前话题是 t_anchor（新鲜闲聊所在话题），但排序是纯相关性
    hits = retriever.search("sqlite 迁移 方案 决定", anchor_topic_id="t_anchor", top_k=2)

    assert [h.doc_id for h in hits] == ["old_decision", "fresh_chatter"]
    assert hits[0].relevance > hits[1].relevance
    assert hits[0].factors == {} and hits[0].strategy == "relevance"


def test_relevant_memory_in_another_topic_is_not_filtered_out():
    docs = [
        IndexedDoc(doc_id="other_topic", text="sqlite 迁移 方案 决定", topic_id="t_other"),
        IndexedDoc(doc_id="anchor_noise", text="今天 天气 很好", topic_id="t_anchor"),
    ]
    retriever, _ = _retriever(docs)

    hits = retriever.search("sqlite 迁移 方案 决定", anchor_topic_id="t_anchor", top_k=2)

    assert "other_topic" in [h.doc_id for h in hits]


def test_hits_expose_rank_relevance_and_effective_config():
    docs = [IndexedDoc(doc_id=f"d{i}", text=f"共同 主题 内容 {i}") for i in range(5)]
    retriever, _ = _retriever(docs)

    hits = retriever.search("共同 主题", top_k=3)

    assert [h.rank for h in hits] == [1, 2, 3]
    assert all(h.strategy == "relevance" for h in hits)
    assert all(h.score == h.relevance for h in hits)
    trace = retriever.last_trace
    assert trace["policy"]["weights"] == {
        "affinity": 0.0,
        "recency": 0.0,
        "keyword": 0.0,
        "entity": 0.0,
    }
    assert "query" not in trace, "留痕不得落地查询原文"
    assert trace["selected"] == [h.doc_id for h in hits]


def test_effective_config_comes_from_params_limits():
    docs = [IndexedDoc(doc_id=f"d{i}", text="共同 主题 内容") for i in range(40)]
    retriever, backend = _retriever(docs)

    # 生产调用路径（不传 top_k）= 读集中配置的注入返回上限
    hits = retriever.search("共同 主题")

    assert retriever.last_trace["return_limit"] == params.LIMITS.inject_return_limit
    assert backend.requested == [params.LIMITS.candidate_pool]
    assert len(hits) <= params.LIMITS.inject_return_limit


def test_empty_result_is_deterministic():
    docs = [IndexedDoc(doc_id="d0", text="今天 天气 很好")]
    retriever, _ = _retriever(docs)

    assert retriever.search("完全不同的话题", top_k=3) == []
    assert retriever.last_trace["selected"] == []


async def test_memory_search_tool_uses_the_same_ranking_and_limit_config():
    """主动检索与自动注入走同一个 Retriever（同一套排序语义），只是条数可以不同。"""
    from agent.tools.memory_search import MemorySearchTool

    docs = [IndexedDoc(doc_id=f"d{i}", text=f"共同 主题 内容 {i}") for i in range(30)]
    retriever, backend = _retriever(docs)
    tool = MemorySearchTool(retriever)

    default_out = await tool.run(query="共同 主题")
    assert params.LIMITS.memory_search_default_k == 5
    assert backend.requested[-1] == params.LIMITS.pool_for(
        params.LIMITS.memory_search_default_k
    ), "候选池至少装得下主动检索要的条数"
    assert "relatedness=" in default_out.content  # 原始相关分对模型可见
    assert "by=relevance" in default_out.content

    # 超大 top_k 被集中配置的上限截住（不是调用方自己写死的 20）
    await tool.run(query="共同 主题", top_k=999)
    assert backend.requested[-1] == params.LIMITS.pool_for(
        params.LIMITS.memory_search_max_k
    )
