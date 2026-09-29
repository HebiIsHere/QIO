"""压力实验的指标计算：纯函数，不碰模型也不碰数据库。

召回类指标与既有 `retrieval_eval` 保持同一口径（前 1 命中、前 k 命中、MRR、
错误记忆注入、陈旧知识注入），另外加了分层：把"关键词路径字面答不出"的子集
单独算一遍 —— 模型的增量价值主要落在那上面。
"""

from __future__ import annotations

from typing import Any, Callable, Sequence

from agent.eval.stress_corpus import RecallCase


def _rate(num: float, den: int) -> float:
    return round(num / den, 4) if den else 0.0


def recall_metrics(
    ranked: Sequence[Sequence[str]], cases: Sequence[RecallCase], k: int = 5
) -> dict[str, Any]:
    """`ranked[i]` 是第 i 条用例的召回结果（按相关性降序的 doc_id）。"""
    hit1 = hitk = stale_above = 0
    rr_sum = 0.0
    for ids, case in zip(ranked, cases):
        expected = set(case.expected)
        stale = set(case.stale)
        top1 = set(ids[:1])
        if top1 & expected:
            hit1 += 1
        if set(ids[:k]) & expected:
            hitk += 1
        for i, doc_id in enumerate(ids, start=1):
            if doc_id in expected:
                rr_sum += 1.0 / i
                break
        first_expected = next((i for i, d in enumerate(ids) if d in expected), None)
        if any(
            d in stale and (first_expected is None or i < first_expected)
            for i, d in enumerate(ids)
        ):
            stale_above += 1
    n = len(cases)
    return {
        "n": n,
        "recall@1": _rate(hit1, n),
        f"recall@{k}": _rate(hitk, n),
        "mrr": round(rr_sum / n, 4) if n else 0.0,
        # 名字说清它到底是什么：前 k 名里没有标准答案的比例（不是"注入了错误记忆"）。
        f"no_gold_in_top{k}_rate": _rate(n - hitk, n),
        # 精确率：返回的 k 条里有多少条确实是标准答案（本语料里非标准答案的片段
        # 按构造都与问题无关，所以这个口径成立）。
        f"precision@{k}": round(
            sum(len(set(ids[:k]) & set(c.expected)) for ids, c in zip(ranked, cases)) / (n * k), 4
        ) if n and k else 0.0,
        "stale_knowledge_injection_rate": _rate(stale_above, n),
        # 未构造的指标如实标注，不用 0 冒充"没问题"。
        "unanswerable_false_recall": "未评估（语料没有「库中无答案」的用例）",
        "expired_fact_usage": "未评估（真实语料未标注被更新的旧事实）",
    }


def stratify_recall(
    ranked: Sequence[Sequence[str]], cases: Sequence[RecallCase], k: int = 5
) -> dict[str, Any]:
    """整体 + 两个子集（关键词能答 / 答不出）+ 按类别的细表。"""
    def subset(pred: Callable[[RecallCase], bool]) -> dict[str, Any]:
        idx = [i for i, c in enumerate(cases) if pred(c)]
        if not idx:
            return {"n": 0}
        return recall_metrics([ranked[i] for i in idx], [cases[i] for i in idx], k=k)

    out: dict[str, Any] = {
        "all": recall_metrics(ranked, cases, k=k),
        "keyword_answerable": subset(lambda c: c.keyword_answerable),
        "keyword_unanswerable": subset(lambda c: not c.keyword_answerable),
        "by_category": {},
        "by_tier": {},
    }
    for category in sorted({c.category for c in cases}):
        out["by_category"][category] = subset(lambda c, cat=category: c.category == cat)
    for tier in ("literal", "partial", "disjoint"):
        out["by_tier"][tier] = subset(lambda c, want=tier: c.tier == want)
    return out


def classification_metrics(
    rows: Sequence[dict[str, Any]], *, key: str = "predicted", expected_key: str = "expected"
) -> dict[str, Any]:
    """通用分类指标：正确率 + 混淆计数（话题判定、实体匹配、去重都用它）。"""
    n = len(rows)
    correct = sum(1 for r in rows if r.get(key) == r.get(expected_key))
    counts: dict[str, int] = {}
    for r in rows:
        pair = f"{r.get(expected_key)}→{r.get(key)}"
        counts[pair] = counts.get(pair, 0) + 1
    return {"n": n, "accuracy": _rate(correct, n), "confusions": counts}


def binary_metrics(
    rows: Sequence[dict[str, Any]], *, key: str = "predicted", expected_key: str = "expected"
) -> dict[str, Any]:
    """二分类指标：真阳/假阳/真阴/假阴，用于"是否重复话题"这类判定。"""
    tp = fp = tn = fn = 0
    for r in rows:
        want, got = bool(r.get(expected_key)), bool(r.get(key))
        if want and got:
            tp += 1
        elif want and not got:
            fn += 1
        elif not want and got:
            fp += 1
        else:
            tn += 1
    return {
        "n": len(rows),
        "accuracy": _rate(tp + tn, len(rows)),
        "recall": _rate(tp, tp + fn),
        "false_positive_rate": _rate(fp, fp + tn),
        "confusions": {"tp": tp, "fp": fp, "tn": tn, "fn": fn},
    }


def ranking_metrics(
    rows: Sequence[dict[str, Any]], *, k: int = 5
) -> dict[str, Any]:
    """"目标是否进入候选 + 平均位次"：工具路由用。"""
    n = len(rows)
    if not n:
        return {"n": 0}
    hit = sum(1 for r in rows if r.get("expected") in (r.get("ranked") or [])[:k])
    positions = [
        (r["ranked"].index(r["expected"]) + 1)
        for r in rows
        if r.get("expected") in (r.get("ranked") or [])
    ]
    return {
        "n": n,
        f"hit@{k}": _rate(hit, n),
        "mean_position": round(sum(positions) / len(positions), 2) if positions else None,
    }


def latency_summary(samples_ms: Sequence[float]) -> dict[str, Any]:
    if not samples_ms:
        return {"n": 0}
    ordered = sorted(samples_ms)

    def pick(p: float) -> float:
        idx = min(len(ordered) - 1, int(round(p * (len(ordered) - 1))))
        return round(ordered[idx], 2)

    return {
        "n": len(ordered),
        "mean_ms": round(sum(ordered) / len(ordered), 2),
        "p50_ms": pick(0.5),
        "p90_ms": pick(0.9),
        "max_ms": round(ordered[-1], 2),
    }
