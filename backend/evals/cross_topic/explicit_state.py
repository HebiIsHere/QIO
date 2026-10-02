# -*- coding: utf-8 -*-
"""显式 / 隐式跨话题：P0~P3 实验（第三阶段 E2/E3/E4/E5/E6）。

分类（E2，**分开报指标**，不混在一起）：
  explicit = 查询里含目标话题的区分性词面（explicit_title / keyword_only /
             mixed_language / multi_topic / long_span_old）
  implicit = 只有指代或复查措辞（pronoun_only / conclusion_review）

臂（E5）：
  P0  当前生产行为（query only）
  P1  已知目标 Topic 但不注入 summary（用生产 topic_hints 候选扩充）
  P2  目标 Topic current state 作为检索上下文（query + state 合成一次检索，
      **单一查询、单一打分来源** —— 上一阶段踩过「用另一段文本召回再混排」的坑）
  P3  Topic state 直接作为 context，不做额外 memory retrieval

状态文本来源（E4：必须是真实数据，不能人工写一个含答案的 summary）：
  latest  该话题最新一条仍然成立的记忆正文（≈ 生产「最近封存片段摘要」）
  others  该话题其余仍然成立的记忆正文（**构造上不含期望值**，无泄漏对照）

答案泄漏检查（E4/E7）：状态文本覆盖期望记忆正文的词元比例 ≥ 60% 记为泄漏样本；
只有**未泄漏**样本上的改善才算证据。

用法（backend 目录下；先设 TEMP）：
    uv run --frozen python evals/cross_topic/explicit_state.py --json evals/cross_topic/explicit_state.json
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
from run_cross_topic import (  # noqa: E402
    load as load_corpora,
    measure as measure_ranked,
)

TOP_K = 5
EXPLICIT = {"explicit_title", "keyword_only", "mixed_language", "multi_topic", "long_span_old"}
IMPLICIT = {"pronoun_only", "conclusion_review"}
LEAK_OVERLAP = 0.6
ARMS = ("P0", "P1", "P2", "P3")


def tokens(text: str) -> set:
    from agent.selector.tokenize import tokenize

    return {t for t in tokenize(text) if len(t) >= 2}


def topic_state(docs_by_topic: dict, target: str, expected: set, *, source: str) -> str:
    """目标话题的 current state 文本（只用真实记忆正文，不人工编写）。"""
    current = docs_by_topic.get(target, [])
    if source == "latest":
        return current[0]["text"] if current else ""
    parts = [d["text"] for d in current if d["id"] not in expected]
    return " ".join(parts)


def leaks(state: str, expected_texts: list[str]) -> bool:
    """答案泄漏：状态文本已经覆盖期望记忆正文的大部分词元。"""
    state_tokens = tokens(state)
    if not state_tokens:
        return False
    for text in expected_texts:
        expected_tokens = tokens(text)
        if not expected_tokens:
            continue
        covered = len(expected_tokens & state_tokens) / len(expected_tokens)
        if covered >= LEAK_OVERLAP:
            return True
        if text and text in state:
            return True
    return False


def current_docs_by_topic(docs: list, stale_by_topic: dict) -> dict:
    """每个话题「仍然成立」的记忆，按时间从新到旧（与 ground truth 口径一致）。"""
    grouped: dict = {}
    for doc in docs:
        topic = doc.get("topic")
        if not topic:
            continue
        if doc["id"] in stale_by_topic.get(topic, set()):
            continue
        grouped.setdefault(topic, []).append(doc)
    for topic in grouped:
        grouped[topic].sort(key=lambda d: d.get("created_days_ago", 0.0))
    return grouped


def build_state_rows(cross_queries, docs, *, state_source: str):
    """给每条跨话题查询算好：目标话题状态文本 + 是否泄漏 + P3 是否命中。"""
    stale_by_topic: dict = {}
    for doc in docs:
        pass
    # stale 定义来自普通集查询（权威），与 build_queries.py 同源
    ordinary = json.loads((HERE.parent / "retrieval_ranking" / "corpus.json").read_text(encoding="utf-8"))
    for query in ordinary["queries"]:
        for stale_id in query.get("stale") or []:
            topic = next(d["topic"] for d in docs if d["id"] == stale_id)
            stale_by_topic.setdefault(topic, set()).add(stale_id)
    docs_by_topic = current_docs_by_topic(docs, stale_by_topic)
    docs_by_id = {d["id"]: d for d in docs}
    rows = []
    for query in cross_queries:
        target = query["target_topic"]
        expected = set(query["expected"])
        expected_texts = [docs_by_id[e]["text"] for e in expected if e in docs_by_id]
        state = topic_state(docs_by_topic, target, expected, source=state_source)
        rows.append({
            "id": query["id"],
            "query": query["query"],
            "anchor": query.get("topic"),
            "target": target,
            "subtype": query["subtype"],
            "group": "explicit" if query["subtype"] in EXPLICIT else "implicit",
            "expected": sorted(expected),
            "state": state,
            "leaked": leaks(state, expected_texts),
            "state_contains_expected": bool(state) and leaks(state, expected_texts),
        })
    return rows


def run(arm: str, rows, docs, *, policy_expand: bool, top_k: int = TOP_K):
    from dataclasses import replace

    from agent.services.params import CROSS_TOPIC

    policy = replace(CROSS_TOPIC, expand_enabled=policy_expand)
    retriever = build_retriever(
        {"docs": docs}, "P_default_vector", topics=CorpusTopics(docs, summary_from="oldest")
    )
    retriever.cross_topic = policy
    ranked = {}
    for row in rows:
        if arm == "P2":
            query_text = f"{row['query']} {row['state']}"
        else:
            query_text = row["query"]
        hits = retriever.search(
            query_text,
            anchor_topic_id=row["anchor"],
            top_k=top_k,
            topic_hints=[row["target"]] if arm == "P1" else None,
        )
        ranked[row["id"]] = [h.doc_id for h in hits]
    return ranked


def summarize(rows, ranked, *, group: str | None = None, skip_leaked: bool = False, top_k: int = TOP_K) -> dict:
    selected = [
        r for r in rows
        if (group is None or r["group"] == group) and (not skip_leaked or not r["leaked"])
    ]
    n = len(selected) or 1
    r1 = r5 = 0
    rr = 0.0
    wrong = stale_hits = 0
    for row in selected:
        expected = set(row["expected"])
        got = ranked[row["id"]]
        hit1 = bool(set(got[:1]) & expected)
        hit5 = bool(set(got[:top_k]) & expected)
        r1 += hit1
        r5 += hit5
        for i, doc_id in enumerate(got, start=1):
            if doc_id in expected:
                rr += 1.0 / i
                break
        wrong += 0 if hit1 else 1
    return {
        "n": len(selected),
        "recall@1": round(r1 / n, 4),
        "recall@5": round(r5 / n, 4),
        "mrr": round(rr / n, 4),
        "wrong_memory_injection_rate": round(wrong / n, 4),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", default="")
    ap.add_argument("--state-source", default="both", choices=("latest", "others", "both"))
    args = ap.parse_args()

    docs, ordinary_queries, cross_queries = load_corpora()
    sources = ("latest", "others") if args.state_source == "both" else (args.state_source,)
    payload: dict = {"arms": {}, "states": {}}
    print(f"# 显式/隐式跨话题 P0~P3  docs={len(docs)}  跨话题={len(cross_queries)}  普通集={len(ordinary_queries)}")

    for source in sources:
        rows = build_state_rows(cross_queries, docs, state_source=source)
        leaked = sum(1 for r in rows if r["leaked"])
        print()
        print(f"## 状态来源 = {source}（answer leakage: {leaked}/{len(rows)}"
              f"，其中 explicit {sum(1 for r in rows if r['group'] == 'explicit' and r['leaked'])} /"
              f" implicit {sum(1 for r in rows if r['group'] == 'implicit' and r['leaked'])}）")
        payload["states"][source] = {"leaked": leaked, "n": len(rows)}
        arm_rows = {}
        for arm in ARMS:
            if arm == "P3":
                # 不做检索：状态文本本身就是 context
                metrics = {}
                for group in ("explicit", "implicit"):
                    sel = [r for r in rows if r["group"] == group]
                    hit = sum(1 for r in sel if r["state_contains_expected"])
                    non_leak = [r for r in sel if not r["leaked"]]
                    hit_clean = sum(1 for r in non_leak if r["state_contains_expected"])
                    metrics[group] = {
                        "n": len(sel),
                        "state_contains_expected_rate": round(hit / (len(sel) or 1), 4),
                        "n_non_leaked": len(non_leak),
                        "clean_hit_rate": round(hit_clean / (len(non_leak) or 1), 4),
                    }
                arm_rows[arm] = {"p3": metrics}
            else:
                ranked = run(arm, rows, docs, policy_expand=(arm == "P1"))
                entry = {}
                for group in ("explicit", "implicit"):
                    entry[group] = summarize(rows, ranked, group=group)
                    # 「未泄漏」子集才是能证明 retrieval 改善的样本（E4/E7）
                    entry[f"{group}_clean"] = summarize(rows, ranked, group=group, skip_leaked=True)
                if entry["explicit_clean"]["n"] == 0 and entry["implicit_clean"]["n"] == 0:
                    entry["note"] = "所有样本都被判定为答案泄漏：该状态来源不能用于证明检索改善"
                arm_rows[arm] = entry
            payload["arms"].setdefault(source, {})[arm] = arm_rows[arm]

        for arm in ("P0", "P1", "P2"):
            e = payload["arms"][source][arm]
            print(f"  {arm}: explicit R@1={e['explicit']['recall@1']:.3f} R@5={e['explicit']['recall@5']:.3f} "
                  f"| implicit R@1={e['implicit']['recall@1']:.3f} R@5={e['implicit']['recall@5']:.3f} "
                  f"| explicit(未泄漏 n={e['explicit_clean']['n']}) R@1={e['explicit_clean']['recall@1']:.3f} "
                  f"R@5={e['explicit_clean']['recall@5']:.3f}")
        p3 = payload["arms"][source]["P3"]["p3"]
        print(f"  P3: explicit 状态含期望 {p3['explicit']['state_contains_expected_rate']:.3f} "
              f"（未泄漏子集 {p3['explicit']['n_non_leaked']} 条命中率 {p3['explicit']['clean_hit_rate']:.3f}）"
              f" | implicit {p3['implicit']['state_contains_expected_rate']:.3f}")

    # 普通检索回归：普通集 72 条，P0 配置
    rows_ordinary = [
        {"id": q["id"], "query": q["query"], "anchor": q.get("topic"), "target": q.get("target_topic"),
         "subtype": q["category"], "group": "ordinary", "expected": q["expected"], "state": "",
         "leaked": False, "state_contains_expected": False}
        for q in ordinary_queries
    ]
    ranked = run("P0", rows_ordinary, docs, policy_expand=False)
    docs_by_id = {d["id"]: d for d in docs}
    ordinary_metrics = measure_ranked(ordinary_queries, docs_by_id, ranked)
    payload["ordinary"] = ordinary_metrics
    print()
    print(f"## 普通集回归（P0）R@1={ordinary_metrics['recall@1']:.3f} R@5={ordinary_metrics['recall@5']:.3f} "
          f"MRR={ordinary_metrics['mrr']:.3f} wrong={ordinary_metrics['wrong_memory_injection_rate']:.3f}")

    if args.json:
        Path(args.json).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n结果 -> {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
