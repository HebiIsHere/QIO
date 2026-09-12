"""Anchor Continuation Eval：Anchor 距离偏置到底有没有收益（离线、确定性）。

背景：QIO 已有「Focus Block（用户从这里开始 / Agent 显式 continue）+ 语义检索
（相关性 / 时间衰减 / 话题亲和）+ memory_search」。在决定是否给检索加
「离 Anchor Fragment 越近权重越高」之前，先用离线评测回答值不值得。

三个方案（同一批 case、同一份生产排序参数）：
    baseline        Focus（不进检索排序）+ 生产语义检索 + 身份去重（= 当前生产行为）
    focus_only      只把 Anchor 自身作为 Focus，检索完全保持语义（与 baseline 等价，
                    单独列出是为了让「不引入物理距离」这个选择可以被显式对比）
    anchor_distance 在 baseline 之上叠加「序数距离」偏置：分数 +
                    ANCHOR_DISTANCE_WEIGHT * max(0, 1 - 距离/ANCHOR_DISTANCE_SPAN)

指标：
    recall@1 / recall@5 / mrr        期望片段是否被排进前列
    wrong_memory_injection_rate      top-1 不是期望片段
    stale_memory_injection_rate      过期/被替代条目排在正确条目之前
    anchor_redundancy_rate           检索又把 Focus 那个片段选回来（Focus 已经给了）
    duplicate_injection_rate         身份去重后仍然重复注入（应恒为 0）
    anchor_distraction_rate          本来能进 top-5 的正确远端片段，被距离偏置挤出 top-5

实验参数（ANCHOR_DISTANCE_*）只属于评测：**生产代码不引用它们**。若哪天评测
证明偏置有效，再把权重搬进 services/params.py 并接进 Retriever（见 docs/status.md）。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from agent.selector.base import IndexedDoc
from agent.selector.selector import Selector
from agent.services import params
from agent.services.decay import DecayPolicy
from agent.services.injection import Candidate, dedupe_candidates
from agent.services.retrieval import RetrievalConfig, Retriever

# 评测专用实验参数（刻意不放进 services/params.py：生产代码不使用）
ANCHOR_DISTANCE_WEIGHT = 0.35
ANCHOR_DISTANCE_SPAN = 5.0

SCHEMES = ("baseline", "focus_only", "anchor_distance")


class _StubTopics:
    def list_with_fingerprints(self):
        return []


def load_cases(path: str | Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


def _iso(days_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


def rank_case(case: dict[str, Any], *, scheme: str = "baseline", top_k: int = 5) -> list[str]:
    """返回该方案下最终会注入的片段顺序（已做身份去重）。"""
    docs = [
        IndexedDoc(
            doc_id=d["id"],
            text=d["text"],
            topic_id=d.get("topic"),
            keywords=d.get("keywords", []),
        )
        for d in case["docs"]
    ]
    selector = Selector()
    selector.load(docs)
    rp = params.RETRIEVAL
    retriever = Retriever(
        selector,
        _StubTopics(),
        config=RetrievalConfig(
            relevance_weight=rp.relevance_weight,
            recency_weight=rp.recency_weight,
            affinity_weight=rp.affinity_weight,
            recency_half_life_days=rp.recency_half_life_days,
        ),
        conn=None,
        decay=DecayPolicy(),
    )
    ages = {d["id"]: d.get("created_days_ago", 0.0) for d in case["docs"]}
    kinds = {d["id"]: d.get("kind", "ephemeral") for d in case["docs"]}
    retriever._created_at = lambda doc_id: _iso(ages.get(doc_id, 0.0))  # type: ignore[assignment]
    retriever._preview = lambda doc_id, title: ""  # type: ignore[assignment]
    retriever.kind_of = lambda doc_id: kinds.get(doc_id, "ephemeral")  # type: ignore[assignment]
    # eval 的 doc 本身就是「片段」（doc_id == fragment_id），显式声明这个前提
    retriever.fragment_of = lambda doc_id: doc_id  # type: ignore[assignment]

    hits = retriever.search(case["query"], top_k=top_k * 2)
    anchor = case.get("anchor")
    orders = {d["id"]: float(d.get("order", 0)) for d in case["docs"]}

    scored: list[tuple[float, str]] = []
    for hit in hits:
        score = hit.score
        if scheme == "anchor_distance" and anchor and anchor in orders:
            distance = abs(orders.get(hit.doc_id, 0.0) - orders[anchor])
            proximity = max(0.0, 1.0 - distance / ANCHOR_DISTANCE_SPAN)
            score += ANCHOR_DISTANCE_WEIGHT * proximity
        scored.append((score, hit.doc_id))
    scored.sort(key=lambda pair: (-pair[0], pair[1]))

    candidates = [
        Candidate(
            source="memory",
            surface="memory",
            item_id=f"{doc_id}",
            text="",
            score=score,
            identity=doc_id,
        )
        for score, doc_id in scored
    ]
    # focus_only 与 baseline 的打分完全一致：差别只在「有没有距离偏置」，
    # 因此这里不需要分支；身份去重（生产规则）统一作用在最后一步。
    deduped = dedupe_candidates(candidates, blocked={anchor} if anchor else set())
    return [c.item_id for c in deduped[:top_k]]


def delivered_ids(case: dict[str, Any], *, scheme: str = "baseline", top_k: int = 5) -> list[str]:
    """模型在本轮真正看到的有序片段：Focus（anchor 片段）在最前，其余是检索结果。

    Focus 已经直接提供了 anchor 片段的内容，所以它在 Context 里算「已交付」，
    不该因为「没被检索再次选中」而被当成缺失（anchor_redundancy_rate 另算）。
    """
    ranked = rank_case(case, scheme=scheme, top_k=top_k)
    anchor = case.get("anchor")
    return ([anchor] if anchor else []) + ranked


def evaluate(cases: list[dict[str, Any]], top_k: int = 5) -> dict[str, Any]:
    metrics: dict[str, Any] = {"n": len(cases)}
    rankings: dict[str, dict[str, list[str]]] = {}
    for scheme in SCHEMES:
        rankings[scheme] = {case["id"]: rank_case(case, scheme=scheme, top_k=top_k) for case in cases}

    for scheme in SCHEMES:
        recall1 = recall5 = 0
        rr_sum = 0.0
        wrong = stale = redundancy = duplicate = 0
        distraction_total = distraction = 0
        rows: list[dict[str, Any]] = []
        for case in cases:
            expected = set(case["expected"])
            stale_ids = set(case.get("stale", []))
            ranked = rankings[scheme][case["id"]]
            anchor = case.get("anchor")
            delivered = ([anchor] if anchor else []) + ranked
            top1, top5 = set(delivered[:1]), set(delivered[:top_k])
            if top1 & expected:
                recall1 += 1
            if top5 & expected:
                recall5 += 1
            rr = next((1.0 / i for i, d in enumerate(delivered, start=1) if d in expected), 0.0)
            rr_sum += rr
            # 检索层面的错误注入：检索把不相关的东西排在最前面（Focus 不算检索结果）
            if ranked and not (set(ranked[:1]) & expected):
                wrong += 1
            first_expected = next((i for i, d in enumerate(ranked) if d in expected), None)
            if any(
                d in stale_ids and (first_expected is None or i < first_expected)
                for i, d in enumerate(ranked)
            ):
                stale += 1
            # Focus 的片段又被检索选回来 → 冗余（生产用身份去重消掉）
            if anchor and anchor in ranked:
                redundancy += 1
            # 应用生产去重规则后，若 Focus 片段仍在结果里 → 说明真的会重复注入
            blocked = {anchor} if anchor else set()
            after = [
                c.item_id
                for c in dedupe_candidates(
                    [
                        Candidate(
                            source="memory",
                            surface="memory",
                            item_id=d,
                            text="",
                            score=0.0,
                            identity=d,
                        )
                        for d in ranked
                    ],
                    blocked=blocked,
                )
            ]
            if anchor and anchor in after:
                duplicate += 1
            # Anchor 干扰：正确片段在 baseline 的 top-5 里，却被本方案挤出 top-5
            # Anchor 干扰：正确片段本身离 anchor 较远时，有没有「因为离 anchor 近」
            # 的无关片段被排到它前面（这正是物理距离偏置的失败模式）
            orders = {d["id"]: float(d.get("order", 0)) for d in case["docs"]}
            if anchor and anchor in orders and expected:
                expected_distance = min(
                    abs(orders[e] - orders[anchor]) for e in expected if e in orders
                )
                bias_sensitive = any(
                    d["id"] not in expected
                    and d["id"] != anchor
                    and abs(float(d.get("order", 0)) - orders[anchor]) < expected_distance
                    for d in case["docs"]
                )
                if bias_sensitive:
                    distraction_total += 1
                    first_expected = next(
                        (i for i, d in enumerate(ranked) if d in expected), None
                    )
                    if any(
                        d != anchor
                        and d not in expected
                        and abs(orders.get(d, 0.0) - orders[anchor]) < expected_distance
                        for d in (ranked if first_expected is None else ranked[:first_expected])
                    ):
                        distraction += 1
            rows.append(
                {
                    "id": case["id"],
                    "ranked": ranked,
                    "delivered": delivered,
                    "expected": sorted(expected),
                    "hit@1": bool(top1 & expected),
                }
            )
        n = len(cases) or 1
        metrics[scheme] = {
            "recall@1": round(recall1 / n, 4),
            "recall@5": round(recall5 / n, 4),
            "mrr": round(rr_sum / n, 4),
            "wrong_memory_injection_rate": round(wrong / n, 4),
            "stale_memory_injection_rate": round(stale / n, 4),
            "anchor_redundancy_rate": round(redundancy / n, 4),
            "duplicate_injection_rate": round(duplicate / n, 4),
            "anchor_distraction_rate": round(distraction / distraction_total, 4)
            if distraction_total
            else 0.0,
            "rows": rows,
        }
    # 决策需要的对比结论（写进 docs/status.md 的依据）
    base = metrics["baseline"]
    bias = metrics["anchor_distance"]
    metrics["decision"] = {
        "recall5_delta": round(bias["recall@5"] - base["recall@5"], 4),
        "wrong_rate_delta": round(
            bias["wrong_memory_injection_rate"] - base["wrong_memory_injection_rate"], 4
        ),
        "anchor_distraction_rate": bias["anchor_distraction_rate"],
        "verdict": (
            "implement"
            if bias["recall@5"] > base["recall@5"] and bias["anchor_distraction_rate"] <= 0.0
            else "skip"
        ),
    }
    return metrics


def public_metrics(metrics: dict[str, Any]) -> dict[str, Any]:
    """去掉逐 case 明细（baseline 文件与回归测试用）。"""
    out = {k: v for k, v in metrics.items() if k != "rows"}
    for scheme in SCHEMES:
        out[scheme] = {k: v for k, v in out[scheme].items() if k != "rows"}
    return out
