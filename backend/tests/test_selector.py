from __future__ import annotations

from datetime import datetime, timedelta, timezone

from agent.selector.base import IndexedDoc
from agent.selector.bm25 import BM25Backend
from agent.selector.selector import Selector
from agent.selector.tokenize import tokenize


def _iso(days_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


def test_tokenize_mixed():
    tokens = tokenize("用户喜欢吃辣 food and 川菜")
    assert "food" in tokens
    assert "吃" in tokens
    assert "川菜" in tokens  # bigram covers the compound term


def test_bm25_ranks_relevant_first():
    docs = [
        IndexedDoc(doc_id="a", text="用户喜欢吃辣，不爱吃甜食", created_at=_iso(10)),
        IndexedDoc(doc_id="b", text="Python agent loop 状态机与预算控制", created_at=_iso(10)),
        IndexedDoc(doc_id="c", text="用户最近去了广东，饮食习惯偏清淡", created_at=_iso(10)),
    ]
    backend = BM25Backend()
    backend.index(docs)
    hits = backend.search("用户 广东 清淡 饮食", top_k=3)
    assert hits[0].doc_id == "c"
    assert {h.doc_id for h in hits} == {"a", "c"}  # b 与查询无共享词，不返回


def test_bm25_empty_index():
    backend = BM25Backend()
    backend.index([])
    assert backend.search("anything", top_k=5) == []


def test_selector_ranks_by_relevance_and_exposes_rule_signals():
    """候选阶段只按底层相关度排序；规则分项作为**信号**带出去，不参与这里的排序。

    业务奖励（anchor / entity / keyword / recency）由 agent/services/retrieval.py
    的统一排序按权重使用一次 —— 2026-10-02 之前这里会把奖励加进分数并据此截断，
    与下游再加权一次，形成两层排序（见 backend/evals/retrieval_ranking/）。
    """
    docs = [
        IndexedDoc(
            doc_id="f1", text="用户偏好清淡饮食", topic_id="t1",
            keywords=["清淡", "饮食"], entity_ids=["milk"],
            created_at=_iso(2),
        ),
        IndexedDoc(
            doc_id="f2", text="讨论了 SQLite schema 设计", topic_id="t2",
            keywords=["sqlite"], created_at=_iso(60),
        ),
    ]
    selector = Selector()
    selector.load(docs, titles={"f1": "饮食偏好", "f2": "存储设计"})
    assert selector.backend_name == "bm25"

    result = selector.select(
        "用户 饮食 偏好 牛奶",
        anchor_topic_id="t1",
        entity_names=["Milk"],  # 大小写不敏感：与 doc.entity_ids 的 "milk" 对上
        top_k=2,
    )
    assert result[0].doc_id == "f1"
    assert result[0].score > 0, "相关度来自召回后端"
    # 规则分项必须完整带出来，下游那一个排序入口才有得用
    signals = result[0].signals
    assert signals.get("anchor") and signals.get("keyword") and signals.get("entity")
    assert "recency" in signals


def test_selector_does_not_apply_business_rewards_itself():
    """同样的文本、不同的时间/话题，候选阶段给出的相关度必须相同。"""
    docs = [
        IndexedDoc(doc_id="old", text="记忆 实验", created_at=_iso(200), topic_id="t_a"),
        IndexedDoc(doc_id="new", text="记忆 实验", created_at=_iso(1), topic_id="t_b"),
    ]
    selector = Selector()
    selector.load(docs)
    result = selector.select("记忆 实验", top_k=2, anchor_topic_id="t_b")
    scores = {c.doc_id: c.score for c in result}
    assert scores["old"] == scores["new"], "候选阶段不得把时效/话题奖励加进相关度"
    # 奖励仍以信号形式可见，供统一排序使用
    assert result[0].signals["anchor"] > 0 or result[1].signals["anchor"] > 0


def test_selector_rules_only_when_recall_unavailable():
    class BrokenRecall(BM25Backend):
        def available(self) -> bool:
            return False

    docs = [
        IndexedDoc(doc_id="f1", text="话题 甲 内容", keywords=["话题", "甲"], topic_id="t1"),
        IndexedDoc(doc_id="f2", text="话题 乙 内容", keywords=["话题", "乙"], topic_id="t2"),
    ]
    selector = Selector(recall=BrokenRecall())
    selector.load(docs)
    assert selector.backend_name == "rules-only"
    result = selector.select("话题 乙", anchor_topic_id="t2", top_k=2)
    assert result[0].doc_id == "f2"


def test_selector_top_k_and_titles():
    docs = [IndexedDoc(doc_id=f"d{i}", text=f"共同 主题 内容 {i}") for i in range(10)]
    selector = Selector()
    selector.load(docs, titles={f"d{i}": f"标题{i}" for i in range(10)})
    result = selector.select("共同 主题", top_k=3)
    assert len(result) == 3
    assert result[0].title is not None