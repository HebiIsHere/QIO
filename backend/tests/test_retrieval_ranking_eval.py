# -*- coding: utf-8 -*-
"""检索排序的标定回归测试（BM25，离线、无需模型）。

语料 backend/evals/retrieval_ranking/corpus.json 是确定性生成的 67 条记忆 /
72 条带标签查询（事实更新、决策、偏好、人物、跨话题、切换、换词、噪声抵抗）。
这里用 **BM25 召回 + 生产默认排序权重** 跑生产代码路径
（Selector.select -> Retriever.search），所以在 CI 上不需要 90MB ONNX 模型。

记录的实测值（生产默认 relevance=1.0 / recency=0 / affinity=0 / rule=0.25）：
  R@1 0.694  R@5 0.806  MRR 0.741  wrong 0.306  stale 0.056
旧的两层排序在同语料上是 R@1 0.597 / MRR 0.664 / wrong 0.403
（backend/evals/retrieval_ranking/arms_before_refactor.json）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

EVALS = Path(__file__).resolve().parents[1] / "evals" / "retrieval_ranking"
sys.path.insert(0, str(EVALS))

MIN_RECALL_AT_1 = 0.65
MIN_RECALL_AT_5 = 0.78
MIN_MRR = 0.70
MAX_WRONG = 0.33
MAX_STALE = 0.10


def _metrics():
    import run_arms

    corpus = json.loads((EVALS / "corpus.json").read_text(encoding="utf-8"))
    ranked = run_arms.rank("P_default_bm25", corpus)
    return run_arms.measure(corpus, ranked)


def test_corpus_covers_every_failure_mode():
    corpus = json.loads((EVALS / "corpus.json").read_text(encoding="utf-8"))
    assert len(corpus["docs"]) >= 60 and len(corpus["queries"]) >= 70
    cats = {q["category"] for q in corpus["queries"]}
    for required in (
        "fact_update",
        "decision",
        "preference",
        "person_entity",
        "cross_topic",
        "switch",
        "paraphrase",
    ):
        assert required in cats, f"语料缺少类别 {required}"
    assert any(q["stale"] for q in corpus["queries"]), "至少要有一条带 stale 的查询"


def test_production_default_ranking_meets_calibration():
    metrics = _metrics()
    assert metrics["recall@1"] >= MIN_RECALL_AT_1, metrics
    assert metrics["recall@5"] >= MIN_RECALL_AT_5, metrics
    assert metrics["mrr"] >= MIN_MRR, metrics
    assert metrics["wrong_memory_injection_rate"] <= MAX_WRONG, metrics
    assert metrics["stale_memory_injection_rate"] <= MAX_STALE, metrics


def test_per_category_regressions_are_visible():
    """逐类指标单独断言：整体分数会被大类稀释，重点类别必须各自站住。"""
    by_cat = _metrics()["by_category"]
    assert by_cat["fact_update"]["recall@1"] == 1.0, by_cat["fact_update"]
    assert by_cat["person_entity"]["recall@1"] >= 0.5, by_cat["person_entity"]
    assert by_cat["paraphrase"]["recall@1"] >= 0.6, by_cat["paraphrase"]
    assert by_cat["decision"]["recall@1"] >= 0.6, by_cat["decision"]
