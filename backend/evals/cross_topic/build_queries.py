# -*- coding: utf-8 -*-
"""跨话题检索语料：在既有 67 条记忆上生成 >= 42 条跨话题查询（确定性、可复现）。

为什么要单独做：上一轮 retrieval_ranking 里只有 10 条 cross_topic，且全是同一个模板
（「之前聊过的 X 那件事，结论是什么」），不足以支撑任何结论。

六类子问题（每条都带 note 说明为什么属于这一类），锚点话题**都不是**目标话题：

  explicit_title   查询里出现目标话题标题        「之前聊过的云南旅行那件事，结论是什么」
  keyword_only     只出现目标话题的关键词        「那个 sqlite 的事后来怎么定的」
  pronoun_only     只有指代，没有任何词面线索    「那个方案后来定了吗」（信息论上不可解）
  conclusion_review 复查上一话题的结论           「上次那个结论再确认一下」（也只有指代）
  multi_topic      同时提到两个话题，问其中一个  「数据库和界面都聊过，数据库那边最后怎么定的」
  mixed_language   中英混合的关键词              「之前说的 cache 那套方案定了吗」
  long_span_old    时间跨度很大（>=130 天）的旧话题「很久以前聊过的缓存方案，最后怎么定的」

ground truth 的口径（写进 note，避免以后有人误读）：
  期望值 = 该话题**最新一条仍然成立**的事实（被后续事实取代的旧值进 stale）；
  同时报告「话题级」指标（是否召回了目标话题的任意一条记忆），因为产品真正需要的是
  「把那个话题找回来」，不是精确到某一条。

产出：backend/evals/cross_topic/queries_cross_topic.json（引用 ../retrieval_ranking/corpus.json 的 docs，
不复制记忆本体，避免两份事实来源）。
"""

from __future__ import annotations

import json
from pathlib import Path

HERE = Path(__file__).parent
ORDINARY = HERE.parent / "retrieval_ranking" / "corpus.json"
OUT = HERE / "queries_cross_topic.json"


def load():
    corpus = json.loads(ORDINARY.read_text(encoding="utf-8"))
    docs = {d["id"]: d for d in corpus["docs"]}
    # 被取代的旧值：从普通集里带 stale 的查询收集（那里才是权威）
    superseded: dict[str, set] = {}
    for q in corpus["queries"]:
        for sid in q.get("stale") or []:
            superseded.setdefault(docs[sid]["topic"], set()).add(sid)
    topics: dict[str, list] = {}
    for d in corpus["docs"]:
        if d.get("topic"):
            topics.setdefault(d["topic"], []).append(d)
    return corpus, docs, superseded, topics


def newest_current(topic: str, topics, superseded) -> str:
    """该话题最新一条仍然成立的事实。"""
    stale = superseded.get(topic, set())
    current = [d for d in topics[topic] if d["id"] not in stale]
    return min(current, key=lambda d: d["created_days_ago"])["id"]


def other_topic(topic: str, topics) -> str:
    return next(t for t in topics if t != topic)


def build() -> dict:
    corpus, docs, superseded, topics = load()
    ids = sorted(topics)
    todo = min(6, 3)

    def safe(tid: str) -> str:
        return tid.replace("t_", "")

    queries: list[dict] = []

    def add(qid, subtype, text, target, anchor, note, oracle=None):
        queries.append({
            "id": qid,
            "query": text,
            "topic": anchor,                  # 锚点话题（检索时的 anchor_topic_id）
            "target_topic": target,           # ground truth：目标话题（只用于指标，不作为输入）
            "expected": [newest_current(target, topics, superseded)],
            "stale": sorted(superseded.get(target, set())),
            "category": "cross_topic",
            "subtype": subtype,
            "oracle_topics": oracle if oracle is not None else [target],
            "note": note,
        })

    # 1) 明确提到标题（10 条）
    for tid in ids:
        title = topics[tid][0]["title"]
        add(f"ct_title_{safe(tid)}", "explicit_title",
            f"之前聊过的{title}那件事，结论是什么", tid, other_topic(tid, topics),
            f"查询里出现目标话题标题「{title}」；期望=该话题最新仍然成立的事实")

    # 2) 只提到关键词、不提标题（6 条）
    for tid in ids[:6]:
        kw = [k for k in topics[tid][0]["keywords"] if k][:2]
        add(f"ct_kw_{safe(tid)}", "keyword_only",
            f"那个 {kw[0]} 的事后来怎么定的", tid, other_topic(tid, topics),
            f"只出现目标话题关键词「{kw[0]}」，不含标题；期望=最新仍然成立的事实")

    # 3) 只有指代，没有任何词面线索（8 条）—— 信息论上单靠查询不可解
    pronoun_texts = [
        "那个方案后来定了吗",
        "刚才说的那件事最后怎么处理的",
        "这个我们之前是不是聊过，最后结论是啥",
        "那个东西到底选哪个了",
        "前面提到的那个问题解决了吗",
        "你还有印象吗，我们最后怎么定的",
        "那件事后来有变化吗",
        "之前那个选择现在还算数吗",
    ]
    for i, tid in enumerate(ids[:8]):
        add(f"ct_pro_{safe(tid)}", "pronoun_only",
            pronoun_texts[i], tid, other_topic(tid, topics),
            "只有指代（那个/那件事），查询本身没有任何目标话题的词面线索；"
            "单靠检索不可解，需要会话状态或意图记录（oracle_topics 就是那个状态）")

    # 4) 复查上一话题的结论（8 条）
    review_texts = [
        "上次那个结论再确认一下",
        "我们最后决定的是什么来着",
        "把上次定下来的再念一遍",
        "结论有没有改过",
        "最终版本是什么",
        "之前敲定的那个还算数吗",
        "再复述一下最后的决定",
        "有没有后来又推翻",
    ]
    for i, tid in enumerate(ids[:8]):
        add(f"ct_rev_{safe(tid)}", "conclusion_review",
            review_texts[i], tid, other_topic(tid, topics),
            "复查结论类：句式指向「上一话题的结论」，同样没有目标话题的词面线索")

    # 5) 多话题交错，问其中一个（5 条）
    for tid in ids[:5]:
        other = other_topic(tid, topics)
        add(f"ct_multi_{safe(tid)}", "multi_topic",
            f"{topics[other][0]['title']}和{topics[tid][0]['title']}都聊过，"
            f"{topics[tid][0]['title']}那边最后怎么定的",
            tid, other,
            "一句话里出现两个话题，问的是后一个；考验的是「别被先出现的词带走」")

    # 6) 中英混合关键词（5 条）
    mixed = {
        "t_arch": ("cache", "之前说的 cache 那套方案定了吗"),
        "t_deploy": ("rollback", "deploy 那块的 rollback 流程最后怎么定的"),
        "t_db": ("migration", "migration 那件事最后结论是啥"),
        "t_perf": ("latency", "latency 那个问题后来解决了吗"),
        "t_work": ("token", "接口那个 token 字段最后怎么统一的"),
    }
    for i, (tid, (kw, text)) in enumerate(mixed.items()):
        add(f"ct_mix_{safe(tid)}", "mixed_language", text, tid, ids[-(i + 1)],
            f"中英混合：用英文词「{kw}」指代目标话题，中文检索词面命中不到")

    # 7) 时间跨度很大的旧记忆（4 条）
    old_topics = ["t_arch", "t_food", "t_people", "t_ui"]
    for tid in old_topics:
        oldest = max(topics[tid], key=lambda d: d["created_days_ago"])
        add(f"ct_old_{safe(tid)}", "long_span_old",
            f"很久以前聊过的{topics[tid][0]['title']}，最后怎么定的", tid, other_topic(tid, topics),
            f"目标话题最早一条记忆是 {oldest['created_days_ago']:.0f} 天前；检验旧记忆不会被时效压掉")

    return {
        "name": "cross-topic-queries",
        "provenance": "确定性生成（build_queries.py）；docs 复用 ../retrieval_ranking/corpus.json",
        "docs_source": "../retrieval_ranking/corpus.json",
        "ground_truth": "expected = 目标话题最新一条仍然成立的事实；stale = 被取代的旧值",
        "queries": queries,
    }


def main() -> int:
    payload = build()
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    subs: dict = {}
    for q in payload["queries"]:
        subs[q["subtype"]] = subs.get(q["subtype"], 0) + 1
    print(f"queries={len(payload['queries'])} -> {OUT}")
    print("subtypes:", json.dumps(subs, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
