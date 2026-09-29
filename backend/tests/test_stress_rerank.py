# -*- coding: utf-8 -*-
"""重排三件套：原序、本地向量、Jev。全部离线，用假后端与假客户端。"""

from __future__ import annotations

import numpy as np

from agent.eval.jev_client import JevAnswer
from agent.eval.stress_rerank import (
    Candidate,
    ConditionalRerank,
    IdentityRerank,
    JevRerank,
    LocalVectorRerank,
)


class _FakeBackend:
    """查询与"含对字"的候选都是 [1,0]，其余是 [0,1]：于是正确候选更接近查询。"""

    def embed_texts(self, texts):
        rows = []
        for t in texts:
            rows.append([1.0, 0.0] if ("对" in t or t == "query") else [0.0, 1.0])
        return np.array(rows, dtype=np.float32)


class _FakeClient:
    def __init__(self, probs: dict) -> None:
        self.probs = probs
        self.calls = 0

    def ask(self, state, questions):
        self.calls += 1
        return JevAnswer(
            answers={k: {"type": "noul", "noul": v} for k, v in self.probs.items()},
            latency_ms=7.0,
        )


def _candidates():
    return [
        Candidate(doc_id="wrong", score=0.9, text="差不多的问题但不是它"),
        Candidate(doc_id="right", score=0.8, text="这条才对"),
    ]


def test_identity_keeps_order():
    assert IdentityRerank().rerank("q", _candidates()) == ["wrong", "right"]


def test_local_vector_rerank_promotes_semantically_closer_candidate():
    ranked = LocalVectorRerank(_FakeBackend()).rerank("query", _candidates())
    assert ranked == ["right", "wrong"]


def test_jev_rerank_sorts_by_noul_and_keeps_tail():
    client = _FakeClient({"c0": 0.1, "c1": 0.9})
    reranker = JevRerank(client)
    ranked = reranker.rerank("query", _candidates())
    assert ranked == ["right", "wrong"]
    assert client.calls == 1
    assert reranker.latencies_ms == [7.0]


def test_jev_rerank_degrades_to_input_order_on_failure():

    class _Boom:
        def ask(self, state, questions):
            raise RuntimeError("boom")

    reranker = JevRerank(_Boom(), max_candidates=1)
    cands = _candidates() + [Candidate(doc_id="tail", score=0.1, text="更后面的候选")]
    assert reranker.rerank("query", cands) == ["wrong", "right", "tail"]
    assert reranker.failures == 1


def test_conditional_rerank_skips_when_top_is_clear():
    calls = []

    class _Spy:
        def rerank(self, query, candidates):
            calls.append(query)
            return [c.doc_id for c in reversed(candidates)]

    reranker = ConditionalRerank(_Spy(), relative_gap=0.2)
    clear = [Candidate("a", 1.0, ""), Candidate("b", 0.5, "")]
    fuzzy = [Candidate("a", 1.0, ""), Candidate("b", 0.98, "")]
    assert reranker.rerank("q1", clear) == ["a", "b"]
    assert reranker.rerank("q2", fuzzy) == ["b", "a"]
    assert calls == ["q2"]
    assert (reranker.triggered, reranker.skipped) == (1, 1)
