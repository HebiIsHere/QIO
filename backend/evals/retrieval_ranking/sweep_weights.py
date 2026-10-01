# -*- coding: utf-8 -*-
"""排序权重扫描 + 分层 K 折（C3：权重必须来自实验）。

做法：检索实例只建一次（embedding 只算一次），每条查询反复用不同权重重排 ——
排序仍然走生产的 Retriever.search（权重从 RetrievalConfig 注入），
评测里没有第二套排序实现。K 折按「查询类别」分层：折内选权重、折外计分，
给出的才是「这套选参流程」的诚实成绩，而不是在同一批查询上挑出来的最好看数字。

用法（backend 目录下）：
    uv run --frozen python evals/retrieval_ranking/sweep_weights.py
    uv run --frozen python evals/retrieval_ranking/sweep_weights.py --json evals/retrieval_ranking/weight_sweep.json
"""

from __future__ import annotations

import argparse
import itertools
import json
import sys
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[1] / "src"))

from run_arms import CORPUS, measure, rank  # noqa: E402


def build_retriever(corpus, recall_kind="vector"):
    """建一次检索实例（含真实 embedding），之后只换权重。"""
    import run_arms

    run_arms.ARMS["__sweep__"] = (recall_kind, (1.0, 0.0, 0.0, 0.0), "sweep")
    return run_arms.build(corpus, "__sweep__")


def rerank(retriever, corpus, weights, top_k=5):
    from agent.services.retrieval import RetrievalConfig

    retriever.config = RetrievalConfig(
        relevance_weight=weights[0],
        recency_weight=weights[1],
        affinity_weight=weights[2],
        rule_weight=weights[3],
    )
    out = {}
    for query in corpus["queries"]:
        hits = retriever.search(query["query"], anchor_topic_id=query.get("topic"), top_k=top_k)
        out[query["id"]] = [h.doc_id for h in hits]
    return out


def metrics_for(retriever, corpus, weights, top_k=5):
    return measure(corpus, rerank(retriever, corpus, weights, top_k=top_k), top_k=top_k)


def stratified_folds(corpus, k):
    buckets: dict = {}
    for query in corpus["queries"]:
        buckets.setdefault(query["category"], []).append(query)
    folds = [[] for _ in range(k)]
    for idx, (_, rows) in enumerate(sorted(buckets.items())):
        for i, row in enumerate(rows):
            folds[(i + idx) % k].append(row)
    return folds


def subset(corpus, queries):
    return {"docs": corpus["docs"], "queries": queries}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", default="")
    ap.add_argument("--k", type=int, default=5)
    args = ap.parse_args()

    corpus = json.loads(CORPUS.read_text(encoding="utf-8"))
    retriever = build_retriever(corpus)

    grid = {
        "rule": [0.0, 0.1, 0.15, 0.25, 0.4, 0.6],
        "recency": [0.0, 0.05, 0.1, 0.15],
        "affinity": [0.0, 0.1, 0.2, 0.4],
    }
    combos = [
        (1.0, rec, aff, rule)
        for rule, rec, aff in itertools.product(grid["rule"], grid["recency"], grid["affinity"])
    ]

    print(f"# 权重扫描  queries={len(corpus['queries'])}  组合={len(combos)}")
    print(f"{'rel':>5} {'rec':>5} {'aff':>5} {'rule':>5} {'R@1':>6} {'R@5':>6} {'MRR':>6} {'wrong':>7} {'stale':>7}")
    table = []
    for weights in combos:
        m = metrics_for(retriever, corpus, weights)
        table.append({"weights": weights, "metrics": m})
        print(f"{weights[0]:>5.2f} {weights[1]:>5.2f} {weights[2]:>5.2f} {weights[3]:>5.2f} "
              f"{m['recall@1']:>6.3f} {m['recall@5']:>6.3f} {m['mrr']:>6.3f} "
              f"{m['wrong_memory_injection_rate']:>7.3f} {m['stale_memory_injection_rate']:>7.3f}")

    ranked = sorted(table, key=lambda row: (-row["metrics"]["recall@1"], -row["metrics"]["mrr"]))
    print("\n## 按 R@1 排序 top-8")
    for row in ranked[:8]:
        w = row["weights"]
        m = row["metrics"]
        print(f"  rel={w[0]} rec={w[1]} aff={w[2]} rule={w[3]} → R@1={m['recall@1']:.3f} "
              f"R@5={m['recall@5']:.3f} MRR={m['mrr']:.3f} wrong={m['wrong_memory_injection_rate']:.3f}")

    # 分层 K 折：折内选权重、折外计分
    folds = stratified_folds(corpus, args.k)
    pool_ok = pool_n = 0
    pool_wrong = 0
    fold_rows = []
    for i, test_queries in enumerate(folds):
        test_ids = {q["id"] for q in test_queries}
        train = subset(corpus, [q for q in corpus["queries"] if q["id"] not in test_ids])
        best = None
        for weights in combos:
            m = metrics_for(retriever, train, weights)
            key = (m["recall@1"], m["mrr"])
            if best is None or key > best[0]:
                best = (key, weights)
        held = metrics_for(retriever, subset(corpus, test_queries), best[1])
        pool_ok += held["recall@1"] * held["n"]
        pool_wrong += held["wrong_memory_injection_rate"] * held["n"]
        pool_n += held["n"]
        fold_rows.append({"fold": i, "weights": best[1], "recall@1": held["recall@1"],
                          "mrr": held["mrr"], "n": held["n"]})
        print(f"  折 {i}: 折内选中 rel={best[1][0]} rec={best[1][1]} aff={best[1][2]} rule={best[1][3]} "
              f"→ 折外 R@1={held['recall@1']:.3f} MRR={held['mrr']:.3f} (n={held['n']})")
    print(f"  分层 {args.k} 折汇总（折内选权的诚实估计）：R@1={pool_ok / (pool_n or 1):.3f} "
          f"wrong={pool_wrong / (pool_n or 1):.3f}")

    if args.json:
        Path(args.json).write_text(
            json.dumps({"grid": grid, "table": table, "top": ranked[:8],
                        "cv": {"k": args.k, "pooled_recall@1": round(pool_ok / (pool_n or 1), 4),
                               "folds": fold_rows}},
                       ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"\n结果 -> {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
