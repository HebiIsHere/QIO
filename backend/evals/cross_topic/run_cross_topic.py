# -*- coding: utf-8 -*-
"""跨话题召回评测：跑生产检索（单层排序），报告跨话题 + 普通集两组指标。

指标口径：
  R@1 / R@5 / MRR            严格口径：期望值 = 目标话题最新一条仍然成立的事实
  topic_hit@1 / topic_hit@5  话题级口径：是否召回了**目标话题的任意一条**记忆
                             （产品真正需要的是「把那个话题找回来」）
  wrong 注入 / stale 注入     与普通集同口径

方案开关（都走生产参数，默认关；见 services/params.RetrievalPolicy）：
  P0            现状
  A_rewrite     查询改写：用话题指纹给查询补关键词（确定性，无模型）
  B_expand      指纹候选扩充：额外用话题指纹文本再取一批候选，合并后仍只排一次
  C_relation    关系感知：沿 fragment 来源链把同话题历史记忆纳入候选
  O_oracle      上界诊断：直接用 oracle_topics 过滤候选（**不是方案**，只用于量化
                「意图已知时排序还能不能救」）

用法（backend 目录下）：
    uv run --frozen python evals/cross_topic/run_cross_topic.py --arm P0
    uv run --frozen python evals/cross_topic/run_cross_topic.py --all --json evals/cross_topic/results.json
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

ORDINARY = HERE.parent / "retrieval_ranking" / "corpus.json"
CROSS = HERE / "queries_cross_topic.json"
TOP_K = 5

ARMS = ("P0", "A_rewrite", "B_expand", "C_relation", "O_oracle", "P0_bm25")


def load():
    ordinary = json.loads(ORDINARY.read_text(encoding="utf-8"))
    cross = json.loads(CROSS.read_text(encoding="utf-8"))
    docs = ordinary["docs"]
    ordinary_queries = ordinary["queries"]
    cross_queries = cross["queries"]
    return docs, ordinary_queries, cross_queries


def _retriever(arm: str, docs: list, *, summary_from: str = "latest"):
    """建检索实例；方案开关通过**生产策略对象**生效（不改排序公式）。"""
    from dataclasses import replace

    from agent.services.params import CROSS_TOPIC

    # 话题指纹用评测语料**同构构造**（见 run_arms.CorpusTopics）——
    # 没有指纹的话 A/B/C 三个开关全是空转，这一点第一版就踩过。
    recall_arm = "P_default_bm25" if arm == "P0_bm25" else "P_default_vector"
    retriever = build_retriever(
        {"docs": docs}, recall_arm, topics=CorpusTopics(docs, summary_from=summary_from)
    )
    if arm in ("P0", "P0_bm25"):
        policy = CROSS_TOPIC
    elif arm == "A_rewrite":
        policy = replace(CROSS_TOPIC, rewrite_enabled=True)
    elif arm == "B_expand":
        policy = replace(CROSS_TOPIC, expand_enabled=True)
    elif arm == "C_relation":
        policy = replace(CROSS_TOPIC, relation_enabled=True, expand_enabled=True)
    elif arm == "O_oracle":
        # 上界诊断：候选话题来自 ground truth（不是方案），机制与 B 完全相同
        policy = replace(CROSS_TOPIC, expand_enabled=True)
    else:
        raise SystemExit(f"未知方案 {arm}")
    retriever.cross_topic = policy
    return retriever


def seed_relation_graph(conn, docs: list):
    """给 C 臂准备真实可用的会话状态：每个话题一条片段 + 一条更早的来源片段。

    没有这段状态，relation 扩展就是空转（position_fragment 拿不到片段）——
    第一版就踩过这个坑，评测必须真的走到那条代码路径。
    """
    from datetime import datetime, timezone

    from agent.graph.anchors import AnchorService

    now = datetime.now(timezone.utc).isoformat()
    topics = sorted({d["topic"] for d in docs if d.get("topic")})
    for topic in topics:
        # fragments.topic_id 有外键指向 nodes —— 先把话题节点建出来
        conn.execute(
            "INSERT OR IGNORE INTO nodes (id, type, name, meta, created_at, updated_at) "
            "VALUES (?, 'topic', ?, '{}', ?, ?)",
            (topic, topic, now, now),
        )
    conn.commit()
    for topic in topics:
        for frag_id, source in ((f"frag_{topic}_old", None), (f"frag_{topic}", f"frag_{topic}_old")):
            conn.execute(
                "INSERT OR REPLACE INTO fragments (id, topic_id, summary, summary_version, "
                "created_at, closed_at, meta, source_fragment_id, relation_type, content_version) "
                "VALUES (?, ?, ?, 1, ?, ?, '{}', ?, 'normal', 1)",
                (frag_id, topic, f"{topic} 的片段摘要", now, now, source),
            )
    conn.commit()
    return AnchorService(conn), set(topics)


def rank(arm: str, docs: list, queries: list, *, summary_from: str = "latest") -> dict:
    retriever = _retriever(arm, docs, summary_from=summary_from)
    anchors = None
    relation_seen: dict = {}
    if arm == "C_relation":
        anchors, _topics = seed_relation_graph(retriever.conn, docs)
    out = {}
    for query in queries:
        anchor = query.get("topic")
        if anchors is not None and anchor:
            anchors.set_active(anchor, f"frag_{anchor}")  # 会话状态：用户此刻在锚点话题里
            relation_seen[query["id"]] = retriever._relation_topics(anchor)
        hits = retriever.search(
            query["query"],
            anchor_topic_id=anchor,
            top_k=TOP_K,
            topic_hints=query.get("oracle_topics") if arm == "O_oracle" else None,
        )
        out[query["id"]] = [h.doc_id for h in hits]
    if arm == "C_relation":
        same = sum(
            1 for qid, topics in relation_seen.items()
            if topics and all(
                t == next(q["topic"] for q in queries if q["id"] == qid) for t in topics
            )
        )
        with_topics = sum(1 for topics in relation_seen.values() if topics)
        print(f"   [C 诊断] 来源链给出话题的查询 {with_topics}/{len(queries)}；"
              f"其中**全部等于锚点话题**（跨不出话题边界）{same}/{with_topics}")
    return out


def measure(queries: list, docs_by_id: dict, ranked: dict) -> dict:
    n = len(queries) or 1
    r1 = r5 = 0
    rr = 0.0
    wrong = stale = 0
    t1 = t5 = 0
    per_sub: dict = {}
    rows = []
    for query in queries:
        expected = set(query["expected"])
        stale_ids = set(query.get("stale") or [])
        got = ranked[query["id"]]
        hit1 = bool(set(got[:1]) & expected)
        hit5 = bool(set(got[:TOP_K]) & expected)
        rr_q = 0.0
        for i, doc_id in enumerate(got, start=1):
            if doc_id in expected:
                rr_q = 1.0 / i
                break
        target = query.get("target_topic")
        topic1 = bool(target) and any(docs_by_id.get(d, {}).get("topic") == target for d in got[:1])
        topic5 = bool(target) and any(docs_by_id.get(d, {}).get("topic") == target for d in got[:TOP_K])
        first_expected = next((i for i, d in enumerate(got) if d in expected), None)
        stale_above = any(
            d in stale_ids and (first_expected is None or i < first_expected) for i, d in enumerate(got)
        )
        r1 += hit1
        r5 += hit5
        rr += rr_q
        wrong += 0 if hit1 else 1
        stale += 1 if stale_above else 0
        t1 += topic1
        t5 += topic5
        key = query.get("subtype") or query["category"]
        slot = per_sub.setdefault(key, {"n": 0, "r1": 0, "r5": 0, "t1": 0, "t5": 0, "rr": 0.0})
        slot["n"] += 1
        slot["r1"] += hit1
        slot["r5"] += hit5
        slot["t1"] += topic1
        slot["t5"] += topic5
        slot["rr"] += rr_q
        rows.append({"id": query["id"], "subtype": key, "ranked": got,
                     "expected": sorted(expected), "hit@1": hit1, "topic_hit@1": topic1})

    def rate(num, den):
        return round(num / den, 4) if den else 0.0

    return {
        "n": len(queries),
        "recall@1": rate(r1, n),
        "recall@5": rate(r5, n),
        "mrr": round(rr / n, 4),
        "wrong_memory_injection_rate": rate(wrong, n),
        "stale_memory_injection_rate": rate(stale, n),
        "topic_hit@1": rate(t1, n),
        "topic_hit@5": rate(t5, n),
        "by_subtype": {
            k: {"n": v["n"], "recall@1": rate(v["r1"], v["n"]), "recall@5": rate(v["r5"], v["n"]),
                "topic_hit@1": rate(v["t1"], v["n"]), "topic_hit@5": rate(v["t5"], v["n"]),
                "mrr": round(v["rr"] / (v["n"] or 1), 4)}
            for k, v in sorted(per_sub.items())
        },
        "rows": rows,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--arm", default="P0")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--json", default="")
    ap.add_argument(
        "--summary-from",
        default="latest",
        choices=("latest", "oldest"),
        help="话题指纹的摘要预览取最新/最早一条记忆（oldest = 无泄漏变体）",
    )
    ap.add_argument("--arms", default="", help="逗号分隔的臂名，覆盖 --all")
    with_rows = "--rows" in sys.argv
    args = ap.parse_args()

    docs, ordinary_queries, cross_queries = load()
    docs_by_id = {d["id"]: d for d in docs}
    arms = [a for a in args.arms.split(",") if a] or (list(ARMS) if args.all else [args.arm])
    print(f"# 跨话题召回评测  docs={len(docs)}  普通集={len(ordinary_queries)}  跨话题={len(cross_queries)}"
          f"  指纹摘要={args.summary_from}")
    payload = {"arms": {}}
    for arm in arms:
        ranked = rank(arm, docs, ordinary_queries + cross_queries, summary_from=args.summary_from)
        ordinary = measure(ordinary_queries, docs_by_id, ranked)
        cross = measure(cross_queries, docs_by_id, ranked)
        payload["arms"][arm] = {"ordinary": ordinary, "cross_topic": cross}
        print()
        print(f"## 方案 {arm}")
        print(f"   跨话题  n={cross['n']:<3} R@1={cross['recall@1']:.3f} R@5={cross['recall@5']:.3f} "
              f"MRR={cross['mrr']:.3f} topic@1={cross['topic_hit@1']:.3f} topic@5={cross['topic_hit@5']:.3f} "
              f"wrong={cross['wrong_memory_injection_rate']:.3f} stale={cross['stale_memory_injection_rate']:.3f}")
        print(f"   普通集  n={ordinary['n']:<3} R@1={ordinary['recall@1']:.3f} R@5={ordinary['recall@5']:.3f} "
              f"MRR={ordinary['mrr']:.3f} wrong={ordinary['wrong_memory_injection_rate']:.3f} "
              f"stale={ordinary['stale_memory_injection_rate']:.3f}")
        print("   子类：")
        for sub, st in cross["by_subtype"].items():
            print(f"     {sub:<18} n={st['n']:<3} R@1={st['recall@1']:.3f} R@5={st['recall@5']:.3f} "
                  f"topic@1={st['topic_hit@1']:.3f} topic@5={st['topic_hit@5']:.3f}")
    if not with_rows:
        for arm in payload["arms"]:
            payload["arms"][arm]["ordinary"].pop("rows", None)
            payload["arms"][arm]["cross_topic"].pop("rows", None)
    if args.json:
        Path(args.json).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n结果 -> {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
