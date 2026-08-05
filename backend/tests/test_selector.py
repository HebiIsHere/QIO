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


def test_selector_combines_recall_and_rules():
    docs = [
        IndexedDoc(
            doc_id="f1", text="用户偏好清淡饮食", topic_id="t1",
            keywords=["清淡", "饮食"], entity_ids=["entity_milk"],
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

    # anchor topic + entity mention push f1 up
    result = selector.select(
        "用户 饮食 偏好 牛奶",
        anchor_topic_id="t1",
        entity_names=["牛奶"],
        top_k=2,
    )
    assert result[0].doc_id == "f1"
    assert "anchor" in result[0].sources or result[0].score > 0


def test_selector_recency_boost():
    docs = [
        IndexedDoc(doc_id="old", text="记忆 实验", created_at=_iso(200)),
        IndexedDoc(doc_id="new", text="记忆 实验", created_at=_iso(1)),
    ]
    selector = Selector()
    selector.load(docs)
    result = selector.select("记忆 实验", top_k=2)
    assert result[0].doc_id == "new"


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