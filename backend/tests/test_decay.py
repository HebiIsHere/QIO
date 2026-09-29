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
from agent.services.params import RankingPolicy
from agent.services.retrieval import Retriever


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


# ---- 排序层：默认（纯相关性）下，旧但更相关的不会被新鲜闲聊压过 ----


class _StubSelector:
    recall = None

    def __init__(self, cands: list[MemoryCandidate]) -> None:
        self._c = cands

    def select(self, query, *, candidate_pool: int = 5):
        return list(self._c)

    def created_at(self, doc_id: str) -> str | None:
        return next((c.created_at for c in self._c if c.doc_id == doc_id), None)

    def text_of(self, doc_id: str) -> str | None:
        return None


class _StubTopics:
    def list_with_fingerprints(self):
        return []


def _cands_old_vs_fresh():
    cands = [
        MemoryCandidate(
            doc_id="old_decision",
            relevance=1.0,
            sources=("bm25",),
            created_at=_iso(2_000),
        ),
        MemoryCandidate(
            doc_id="new_chatter",
            relevance=0.6,
            sources=("bm25",),
            created_at=_iso(0),
        ),
    ]
    return cands


def test_pure_relevance_keeps_older_but_more_relevant_on_top(db_conn: sqlite3.Connection):
    """默认策略只按相关度：旧的重要决策（相关 1.0）不会被新鲜闲聊（0.6）压过。"""
    r = Retriever(_StubSelector(_cands_old_vs_fresh()), _StubTopics(), conn=db_conn)
    r._preview = lambda d, t: ""
    r.kind_of = lambda d: AUTHORITATIVE if d == "old_decision" else EPHEMERAL
    hits = r.search("q", top_k=2)
    assert [h.doc_id for h in hits] == ["old_decision", "new_chatter"]
    assert hits[0].factors == {}  # 纯相关性：没有任何时效项


def test_recency_weight_is_opt_in_and_unverified(db_conn: sqlite3.Connection):
    """时效奖励默认关闭；显式开启（未验证的实验权重）才会改变顺序。

    这也是「旧的、仍有效的事实」在开启时效后可能被新鲜闲聊盖过的风险所在 ——
    所以它不能是默认行为。
    """
    policy = RankingPolicy(strategy="weighted", recency_weight=1.0)
    r = Retriever(
        _StubSelector(_cands_old_vs_fresh()),
        _StubTopics(),
        policy=policy,
        conn=db_conn,
    )
    r._preview = lambda d, t: ""
    r.kind_of = lambda d: EPHEMERAL  # 两条都当普通记忆：旧的那条衰减到 ~0
    hits = r.search("q", top_k=2)
    assert hits[0].doc_id == "new_chatter"
    # 旧的那条时效贡献已衰减到可忽略（但贡献是如实记录的，不是凭空清零）
    assert hits[1].factors.get("recency", 0.0) < 1e-9
