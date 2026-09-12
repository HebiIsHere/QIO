"""Anchor Continuation Eval：把「要不要加 Anchor 距离偏置」变成可复核的评测结论。"""

from __future__ import annotations

from pathlib import Path

from agent.eval.anchor_eval import (
    SCHEMES,
    delivered_ids,
    evaluate,
    load_cases,
    rank_case,
)

EVALS = Path(__file__).resolve().parents[1] / "evals"
CASES = EVALS / "anchor_continuation" / "cases.jsonl"


def _cases():
    return load_cases(CASES)


def test_cases_cover_the_required_scenarios():
    ids = {c["id"] for c in _cases()}
    assert {
        "A_start_here",          # 用户明确 StartHere
        "B_far_but_relevant",    # 真正相关的离 anchor 很远
        "C_neighbor_irrelevant", # 相邻片段内容无关
        "D_spread_discussion",   # 同一讨论跨多个不连续片段
        "E_no_anchor",           # 无 anchor
    } <= ids


def test_eval_is_deterministic():
    a = evaluate(_cases())
    b = evaluate(_cases())
    for scheme in SCHEMES:
        assert a[scheme] == b[scheme]
    assert a["decision"] == b["decision"]


def test_start_here_case_hits_anchor_focus():
    case = next(c for c in _cases() if c["id"] == "A_start_here")
    # anchor 片段本身即期望内容：Focus 直接交付（检索不必再选它 → 也就不重复注入）
    delivered = delivered_ids(case, scheme="baseline")
    assert case["expected"][0] == delivered[0]
    assert case["anchor"] not in rank_case(case, scheme="baseline")


def test_distance_bias_does_not_beat_production_baseline():
    """决策守卫：只要距离偏置没有明确收益（且带来干扰），生产就不实现它。

    若未来 case 数据变化导致偏置真的有收益，这个测试会失败，从而强制重新决策
    （而不是让一个「顺手加的偏置」悄悄留在生产里）。
    """
    metrics = evaluate(_cases())
    base = metrics["baseline"]
    bias = metrics["anchor_distance"]
    assert bias["recall@5"] <= base["recall@5"], "距离偏置不应提升 recall@5"
    assert bias["wrong_memory_injection_rate"] >= base["wrong_memory_injection_rate"]
    assert bias["anchor_distraction_rate"] > 0 or metrics["decision"]["verdict"] == "skip"
    assert metrics["decision"]["verdict"] in {"skip", "implement"}
    if metrics["decision"]["verdict"] == "skip":
        # 明确记录：当前 Focus + 语义检索已覆盖主要需求
        assert bias["recall@5"] - base["recall@5"] <= 0


def test_baseline_has_no_duplicate_injection():
    metrics = evaluate(_cases())
    assert metrics["baseline"]["duplicate_injection_rate"] == 0.0
    assert metrics["baseline"]["recall@5"] > 0


def test_neighbor_fragments_are_not_boosted_by_relevance_alone():
    """相邻但无关的片段不能因为「挨着 anchor」进入 top-1。"""
    case = next(c for c in _cases() if c["id"] == "C_neighbor_irrelevant")
    ranked = rank_case(case, scheme="baseline")
    assert ranked, "普通语义检索必须能选出东西"
    assert ranked[0] in case["expected"] or ranked[0] == case["anchor"]
    assert ranked[0] not in {"f12", "f14"}, "相邻的无关片段不得排第一"
