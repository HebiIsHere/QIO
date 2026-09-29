# -*- coding: utf-8 -*-
"""指标计算属于纯函数，数字必须能手工核对。

这些断言是全部实验数字的地基：指标算错的话，三条臂的对照结论就没有意义。
"""

from __future__ import annotations

from agent.eval.stress_corpus import RecallCase
from agent.eval.stress_metrics import recall_metrics, stratify_recall


def _case(cid: str, expected: tuple, stale: tuple = (), hard: bool = False):
    return RecallCase(
        id=cid,
        category="fact_update" if stale else "same_topic",
        query="q",
        expected=expected,
        stale=stale,
        keyword_answerable=not hard,
    )


def test_recall_metrics_counts_first_and_top5_hits():
    cases = [_case("a", ("g1",)), _case("b", ("g2",)), _case("c", ("g3",))]
    ranked = [["g1", "x"], ["x", "g2"], ["x", "y"]]
    m = recall_metrics(ranked, cases, k=2)
    assert m["recall@1"] == 0.3333
    assert m["recall@2"] == 0.6667
    assert m["mrr"] == 0.5
    assert m["no_gold_in_top2_rate"] == 0.3333
    assert m["precision@2"] == 0.3333
    assert m["unanswerable_false_recall"].startswith("未评估")


def test_stale_knowledge_rate_counts_stale_above_gold():
    cases = [
        _case("a", ("new",), stale=("old",)),
        _case("b", ("new2",), stale=("old2",)),
    ]
    ranked = [["old", "new"], ["new2", "old2"]]
    m = recall_metrics(ranked, cases, k=2)
    assert m["stale_knowledge_injection_rate"] == 0.5


def test_stratify_recall_splits_hard_and_easy_subset():
    cases = [
        _case("a", ("g1",)),
        _case("b", ("g2",), hard=True),
        _case("c", ("g3",), hard=True),
    ]
    ranked = [["g1"], ["x"], ["g3"]]
    out = stratify_recall(ranked, cases, k=1)
    assert out["all"]["recall@1"] == 0.6667
    assert out["keyword_answerable"]["recall@1"] == 1.0
    assert out["keyword_unanswerable"]["recall@1"] == 0.5
