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


def test_selector_candidates_are_raw_relevance_only():
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

    # 候选阶段只按底层检索相关度：f1 与查询共享词项更多，排前面；
    # 分数是原始召回分，不含任何话题 / 实体 / 时效奖励。
    result = selector.select("用户 饮食 偏好 牛奶", candidate_pool=2)
    assert result[0].doc_id == "f1"
    assert result[0].relevance > 0
    assert result[0].sources == ("bm25",)


def test_selector_candidates_ignore_recency_topic_and_entity():
    """纯相关性候选：新鲜闲聊 / 当前话题 / 实体命中都不得偷偷改变相关分。"""
    docs = [
        IndexedDoc(
            doc_id="z_fresh_chatter", text="记忆 实验", topic_id="t_chat",
            created_at=_iso(0),
        ),
        IndexedDoc(
            doc_id="a_old_decision", text="记忆 实验", topic_id="t_work",
            entity_ids=["e1"], keywords=["记忆"], created_at=_iso(500),
        ),
    ]
    selector = Selector()
    selector.load(docs)
    result = selector.select("记忆 实验", candidate_pool=2)
    # 相关度相同 → 按稳定身份（doc_id）排序，而不是按时间 / 话题
    assert [c.doc_id for c in result] == ["a_old_decision", "z_fresh_chatter"]
    assert result[0].relevance == result[1].relevance


class _SpyBackend(BM25Backend):
    """记录底层召回被请求了多少条 —— 候选池不该被隐藏乘数放大。"""

    def __init__(self) -> None:
        super().__init__()
        self.requested: list[int] = []

    def search(self, query: str, top_k: int):
        self.requested.append(top_k)
        return super().search(query, top_k=top_k)


def test_backend_recall_request_equals_candidate_pool():
    backend = _SpyBackend()
    docs = [IndexedDoc(doc_id=f"d{i}", text=f"共同 主题 内容 {i}") for i in range(40)]
    selector = Selector(recall=backend)
    selector.load(docs)

    selector.select("共同 主题", candidate_pool=30)

    assert backend.requested == [30], "候选池 30 不得被隐藏倍增"


def test_selector_rules_only_fallback_uses_lexical_overlap():
    class BrokenRecall(BM25Backend):
        def available(self) -> bool:
            return False

    docs = [
        IndexedDoc(doc_id="f1", text="话题 甲 内容", keywords=["话题"], topic_id="t1"),
        IndexedDoc(doc_id="f2", text="话题 乙 内容", keywords=["话题", "乙"], topic_id="t2"),
    ]
    selector = Selector(recall=BrokenRecall())
    selector.load(docs)
    assert selector.backend_name == "rules-only"
    # 没有召回后端时用词项重合度当相关分：重合更多的 f2 排前面
    result = selector.select("话题 乙", candidate_pool=2)
    assert [c.doc_id for c in result] == ["f2", "f1"]
    assert result[0].relevance > result[1].relevance


def test_selector_top_k_and_titles():
    docs = [IndexedDoc(doc_id=f"d{i}", text=f"共同 主题 内容 {i}") for i in range(10)]
    selector = Selector()
    selector.load(docs, titles={f"d{i}": f"标题{i}" for i in range(10)})
    result = selector.select("共同 主题", candidate_pool=3)
    assert len(result) == 3
    assert result[0].title is not None
