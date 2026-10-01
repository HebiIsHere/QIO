from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from agent.selector.base import MemoryCandidate
from agent.services.decay import (
    AUTHORITATIVE,
    EPHEMERAL,
    PROJECT_DECISION,
    DecayPolicy,
)
from agent.services.retrieval import RetrievalConfig, Retriever


def _iso(days_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


def test_authoritative_never_decays():
    p = DecayPolicy()
    assert p.weight(5_000, AUTHORITATIVE) == 1.0
    assert p.weight(5_000, EPHEMERAL) < 0.01


def test_project_decision_decays_slower_than_ephemeral():
    p = DecayPolicy()
    age = 365.0
    assert p.weight(age, PROJECT_DECISION) > p.weight(age, EPHEMERAL)
    assert p.weight(age, PROJECT_DECISION) > 0.3


def test_knowledge_category_mapping():
    p = DecayPolicy()
    assert p.kind_for_knowledge_category("general_fact") == AUTHORITATIVE
    assert p.kind_for_knowledge_category("user_profile") == "preference"
    assert p.kind_for_knowledge_category(None) == AUTHORITATIVE


# ---- 排序层：旧但重要 ≠ 被新但无关压过 ----


class _StubSelector:
    recall = None

    def __init__(self, cands: list[MemoryCandidate]) -> None:
        self._c = cands

    def select(self, query, *, top_k=5, anchor_topic_id=None, entity_names=None):
        return list(self._c)


class _StubTopics:
    def list_with_fingerprints(self):
        return []


#: 时效层现在是**可选**的（生产默认 recency_weight=0，见 params.RETRIEVAL 的数据注释）。
#: 下面两个用例测的是「时效层本身正确」——所以显式打开它，
#: 这样默认值怎么调都不会让机制失去覆盖。
RECENCY_ON = RetrievalConfig(recency_weight=1.0)


def test_old_authoritative_beats_fresh_ephemeral(db_conn: sqlite3.Connection):
    # 旧的重要决策（相关度高）vs 新的低相关闲聊（更新但无关）
    cands = [
        MemoryCandidate(doc_id="old_decision", score=1.0, sources=("bm25",)),
        MemoryCandidate(doc_id="new_chatter", score=0.6, sources=("bm25",)),
    ]
    r = Retriever(_StubSelector(cands), _StubTopics(), config=RECENCY_ON, conn=db_conn)
    r._created_at = lambda d: _iso(2_000 if d == "old_decision" else 0.0)
    r._preview = lambda d, t: ""
    r.kind_of = lambda d: AUTHORITATIVE if d == "old_decision" else EPHEMERAL

    hits = r.search("q", top_k=2)
    assert hits[0].doc_id == "old_decision"  # 同等相关度下，旧的重要信息仍胜出

    # 反证：若把旧决策也当 ephemeral，衰减到 ~0 → 会被新闲聊压过
    r2 = Retriever(_StubSelector(cands), _StubTopics(), config=RECENCY_ON, conn=db_conn)
    r2._created_at = r._created_at
    r2._preview = lambda d, t: ""
    r2.kind_of = lambda d: EPHEMERAL
    assert r2.search("q", top_k=2)[0].doc_id == "new_chatter"


def test_default_kind_preserves_previous_behaviour(db_conn: sqlite3.Connection):
    """无 kind 元数据时（memory_index 现状）默认落叶是 ephemeral：新的更靠前。"""
    cands = [
        MemoryCandidate(doc_id="old", score=1.0, sources=("bm25",)),
        MemoryCandidate(doc_id="new", score=1.0, sources=("bm25",)),
    ]
    r = Retriever(_StubSelector(cands), _StubTopics(), config=RECENCY_ON, conn=db_conn)
    r._created_at = lambda d: _iso(60 if d == "old" else 0.0)
    r._preview = lambda d, t: ""
    hits = r.search("q", top_k=2)
    assert hits[0].doc_id == "new"


def test_retrieval_config_defaults_come_from_params():
    """排序权重只有一个权威来源：services.params.RETRIEVAL。

    旧版 RetrievalConfig 自己写了一份 0.4/0.25/0.35，生产又不读 params，
    结果「改配置」改的是没人用的常量。
    """
    from agent.services import params

    config = RetrievalConfig()
    assert config.relevance_weight == params.RETRIEVAL.relevance_weight
    assert config.recency_weight == params.RETRIEVAL.recency_weight
    assert config.affinity_weight == params.RETRIEVAL.affinity_weight
    assert config.rule_weight == params.RETRIEVAL.rule_weight


def test_default_ranking_is_pure_relevance_plus_rule_signals(db_conn: sqlite3.Connection):
    """生产默认：相关度为主，时效/话题亲和关闭，规则分项按 params 的权重加一次。"""
    from agent.services import params

    cands = [
        MemoryCandidate(doc_id="old_relevant", score=1.0, sources=("bm25",)),
        MemoryCandidate(doc_id="fresh_irrelevant", score=0.6, sources=("bm25",)),
    ]
    r = Retriever(_StubSelector(cands), _StubTopics(), conn=db_conn)
    r._created_at = lambda d: _iso(400 if d == "old_relevant" else 0.0)
    r._preview = lambda d, t: ""
    hits = r.search("q", top_k=2)
    assert params.RETRIEVAL.recency_weight == 0.0
    assert hits[0].doc_id == "old_relevant", "时效默认关闭：相关度高的旧记忆不该被无关的新消息压过"
