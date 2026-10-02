# -*- coding: utf-8 -*-
"""跨话题召回缺口诊断：到底是「候选生成拿不到」，还是「排序排不上来」。

做法（全部用生产 API，不复制排序逻辑）：
  A. 生产检索结果（P0 配置）里，期望文档的排名
  B. 生产候选池（Selector.select 的 top_k 上限，与 Retriever.search 一致）里，
     期望文档是否在池内、名次多少
  C. 底层召回（放宽到 30）里，期望文档是否可见

判读：
  * 期望文档在 C 可见、在 B 之外 → **候选池截断**造成的缺口（扩池能救）
  * 期望文档在 B 内、但不在 A 的 top-5 → **排序信号**造成的缺口（扩池救不了）
  * 期望文档在 C 之外 → 底层召回就找不到（embedding/词面问题）

用法（backend 目录下）：
    uv run --frozen python evals/cross_topic/diagnose_gap.py --json evals/cross_topic/diagnosis.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "retrieval_ranking"))

from run_arms import CorpusTopics, build as build_retriever  # noqa: E402

TOP_K = 5
POOL = TOP_K * 2  # 与 Retriever.search 的候选池一致


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", default="")
    ap.add_argument("--summary-from", default="oldest", choices=("latest", "oldest"))
    args = ap.parse_args()

    docs = json.loads((HERE.parent / "retrieval_ranking" / "corpus.json").read_text(encoding="utf-8"))["docs"]
    cross = json.loads((HERE / "queries_cross_topic.json").read_text(encoding="utf-8"))["queries"]
    retriever = build_retriever(
        {"docs": docs}, "P_default_vector", topics=CorpusTopics(docs, summary_from=args.summary_from)
    )
    selector = retriever.selector

    counters = {
        "visible_in_recall": 0,      # 放宽召回里能找到
        "in_candidate_pool": 0,      # 候选池内
        "in_top5": 0,                # 最终 top-5
        "pool_cut": 0,               # 在召回里、被候选池截断
        "ranking_gap": 0,            # 在候选池里、但排不进 top-5
        "recall_gap": 0,             # 底层召回就没有
    }
    per_sub: dict = {}
    rows = []
    for query in cross:
        expected = set(query["expected"])
        anchor = query.get("topic")
        final = [h.doc_id for h in retriever.search(query["query"], anchor_topic_id=anchor, top_k=TOP_K)]
        pool = [c.doc_id for c in selector.select(query["query"], top_k=POOL, anchor_topic_id=anchor)]
        wide = [c.doc_id for c in selector.select(query["query"], top_k=30, anchor_topic_id=anchor)]

        rank_final = next((i for i, d in enumerate(final, 1) if d in expected), None)
        rank_pool = next((i for i, d in enumerate(pool, 1) if d in expected), None)
        rank_wide = next((i for i, d in enumerate(wide, 1) if d in expected), None)

        counters["visible_in_recall"] += 1 if rank_wide else 0
        counters["in_candidate_pool"] += 1 if rank_pool else 0
        counters["in_top5"] += 1 if rank_final else 0
        if rank_wide and not rank_pool:
            counters["pool_cut"] += 1
        elif rank_pool and not rank_final:
            counters["ranking_gap"] += 1
        elif not rank_wide:
            counters["recall_gap"] += 1
        sub = per_sub.setdefault(query["subtype"], {"n": 0, "pool": 0, "top5": 0, "rank_gap": 0, "recall_gap": 0})
        sub["n"] += 1
        sub["pool"] += 1 if rank_pool else 0
        sub["top5"] += 1 if rank_final else 0
        sub["rank_gap"] += 1 if (rank_pool and not rank_final) else 0
        sub["recall_gap"] += 1 if not rank_wide else 0
        rows.append({"id": query["id"], "subtype": query["subtype"],
                     "rank_in_recall30": rank_wide, "rank_in_pool": rank_pool, "rank_in_top5": rank_final})

    # 「扩池能不能救 top-5」：同一次排序、只是把候选池从 2×5 放宽到 2×20，
    # 再取前 5。排序分数对候选池大小是否敏感，一测便知。
    widened_top5 = 0
    for query in cross:
        expected = set(query["expected"])
        wide_hits = retriever.search(query["query"], anchor_topic_id=query.get("topic"), top_k=20)
        if set(h.doc_id for h in wide_hits[:TOP_K]) & expected:
            widened_top5 += 1

    n = len(cross)
    print(f"# 跨话题缺口诊断  n={n}  指纹摘要={args.summary_from}")
    print(f"  底层召回(放宽 30)里能找到期望文档 : {counters['visible_in_recall']}/{n}")
    print(f"  进入生产候选池(前 {POOL})           : {counters['in_candidate_pool']}/{n}")
    print(f"  进入最终 top-5                    : {counters['in_top5']}/{n}")
    print(f"  缺口分解：候选池截断 {counters['pool_cut']} / 排序排不上 {counters['ranking_gap']} / 召回就找不到 {counters['recall_gap']}")
    print(f"  对照：只把候选池放宽到 2×20 再取前 5 → {widened_top5}/{n}"
          f"（{'与扩池前一致，扩池无效' if widened_top5 == counters['in_top5'] else '有变化'}）")
    print()
    print("  子类：")
    for sub, st in sorted(per_sub.items()):
        print(f"    {sub:<18} n={st['n']:<3} 池内 {st['pool']:<3} top5 {st['top5']:<3} "
              f"排序缺口 {st['rank_gap']:<3} 召回缺口 {st['recall_gap']}")
    if args.json:
        Path(args.json).write_text(
            json.dumps({"n": n, "summary_from": args.summary_from, "counters": counters,
                        "widened_pool_top5": widened_top5,
                        "by_subtype": per_sub, "rows": rows}, ensure_ascii=False, indent=2),
            encoding="utf-8")
        print(f"\n结果 -> {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
