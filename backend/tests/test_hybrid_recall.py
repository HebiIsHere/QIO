# -*- coding: utf-8 -*-
"""混合召回：RRF 只看名次，两个打分器的量纲不同也不影响。"""

from __future__ import annotations

from agent.eval.hybrid_recall import HybridRecall
from agent.selector.base import RecallBackend, ScoredDoc


class _Fake(RecallBackend):
    name = "fake"

    def __init__(self, order: list[str], scale: float = 1.0, ok: bool = True) -> None:
        self.order = order
        self.scale = scale
        self.ok = ok

    def available(self) -> bool:
        return self.ok

    def index(self, docs) -> None:
        self.indexed = list(docs)

    def search(self, query: str, top_k: int) -> list[ScoredDoc]:
        return [
            ScoredDoc(doc_id=doc_id, score=self.scale * (len(self.order) - i), source=self.name)
            for i, doc_id in enumerate(self.order[:top_k])
        ]


def test_rrf_fuses_two_rankings():
    hybrid = HybridRecall(
        _Fake(["a", "b", "c"], scale=100.0), _Fake(["b", "c", "a"], scale=0.01)
    )
    ids = [hit.doc_id for hit in hybrid.search("q", top_k=3)]
    assert ids[0] == "b", "两路都排前的位置应当融合到最前"
    assert set(ids) == {"a", "b", "c"}


def test_scale_difference_does_not_leak_into_fusion():
    big = HybridRecall(_Fake(["a", "b"], scale=999.0), _Fake(["b", "a"], scale=0.001))
    small = HybridRecall(_Fake(["a", "b"], scale=0.001), _Fake(["b", "a"], scale=999.0))
    assert [h.doc_id for h in big.search("q", 2)] == [h.doc_id for h in small.search("q", 2)]


def test_falls_back_to_the_available_side():
    hybrid = HybridRecall(_Fake([], ok=False), _Fake(["x", "y"]))
    assert [h.doc_id for h in hybrid.search("q", 2)] == ["x", "y"]


def test_weight_can_favour_the_stronger_side():
    """关键词强、向量弱时，把权重压到关键词一侧，结果应当听关键词的。"""
    lexical = _Fake(["a", "b", "c", "d"], scale=1.0)
    vector = _Fake(["d", "c", "b", "a"], scale=1.0)
    equal = HybridRecall(lexical, vector)
    weighted = HybridRecall(lexical, vector, lexical_weight=0.9, vector_weight=0.1)
    assert [h.doc_id for h in equal.search("q", 2)] == ["a", "d"]
    assert [h.doc_id for h in weighted.search("q", 2)] == ["a", "b"]
