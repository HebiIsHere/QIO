# -*- coding: utf-8 -*-
"""分段边界：确定性规则必须保持「不误切」；语义信号暂不足以启用。

数据：backend/evals/boundary_semantic/cases.jsonl（44 条，覆盖七类边界 + 三类陷阱），
语义分数是真实 ONNX 余弦快照（scores_onnx.json），所以本测试离线、无需模型。

实测（result_onnx.json）：
  规则臂          acc 0.8182  split_recall 0.333  误切 0  漏切 8
  规则+语义 th=0.35 acc 0.8409  split_recall 0.417  误切 0
  但 th 从 0.35 挪到 0.45，误切就从 0 涨到 6 —— 增益只有一个用例，且对阈值极敏感。
「说不准就不切」是产品纪律：误切（把同一段对话切碎）比漏切更贵。因此保持 shadow。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

EVALS = Path(__file__).resolve().parents[1] / "evals" / "boundary_semantic"
sys.path.insert(0, str(EVALS))

MIN_RULES_ACCURACY = 0.80
CLEAR_IMPROVEMENT = 0.05  # 低于这个幅度不足以打开一个会改行为的新信号

# 标定值（2026-10-02 记录，真实 ONNX 快照 + 生产策略）。
# 规则臂是**当前唯一生效**的臂：它的 acc 与误切数就是长期回归的锚点。
RECORDED_N = 44
RECORDED_RULES_ACCURACY = 0.8182
RECORDED_RULES_SPLIT_RECALL = 0.3333
RECORDED_FALSE_SPLIT = 0
RECORDED_MISSED_SPLIT = 8
# 语料必须覆盖的类别（缺一类就意味着某类边界不再被观测）
REQUIRED_CATEGORIES = (
    "same_stage_continue",
    "stage_change",
    "new_goal",
    "short_ack",
    "correction",
    "side_branch",
    "return_to_task",
    "negated",
    "quoted",
    "hypothetical",
)


def _load():
    import run_boundary_arms

    cases = run_boundary_arms.load_cases()
    scores = run_boundary_arms.load_scores()
    return run_boundary_arms, cases, scores


def test_corpus_covers_every_boundary_category():
    """语料缩水/缺类 = 某类边界不再被观测，必须当场失败。"""
    _runner, cases, _scores = _load()
    assert len(cases) >= RECORDED_N, f"边界语料缩水到 {len(cases)} 条"
    categories = {c["category"] for c in cases}
    missing = [name for name in REQUIRED_CATEGORIES if name not in categories]
    assert not missing, f"边界语料缺少类别：{missing}"
    for case in cases:
        assert case.get("note", "").strip(), f"{case['id']} 缺少 note"
        assert case.get("expect") in {"split", "continue"}, case["id"]


def test_rules_arm_never_splits_by_mistake():
    runner, cases, scores = _load()
    metrics = runner.evaluate(cases, scores, semantic_threshold=None)
    assert metrics["false_split"] == 0, metrics["wrong_split_ids"]
    assert metrics["accuracy"] >= MIN_RULES_ACCURACY, metrics


def test_rules_arm_metrics_are_pinned_to_the_recorded_calibration():
    """规则臂的 acc 与误切数不许退化 —— 它们就是「不误切」这条纪律的锚点。

    改了规则、扩了语料都会让这里的数字变；那时**必须重新记录标定值并在提交里
    说明依据**（而不是把断言改松）。误切变多尤其不允许：把同一段对话切碎比漏切更贵。
    """
    runner, cases, scores = _load()
    metrics = runner.evaluate(cases, scores, semantic_threshold=None)
    assert metrics["n"] == RECORDED_N, (
        f"边界语料条数从 {RECORDED_N} 变成 {metrics['n']}：扩语料要重新记录这组标定值"
    )
    assert metrics["false_split"] == RECORDED_FALSE_SPLIT, (
        f"误切从 {RECORDED_FALSE_SPLIT} 变成 {metrics['false_split']}：{metrics['wrong_split_ids']}"
    )
    assert metrics["accuracy"] == RECORDED_RULES_ACCURACY, (
        f"规则臂 acc 从 {RECORDED_RULES_ACCURACY} 变成 {metrics['accuracy']}；"
        f"漏切 {metrics['missed_split_ids']}。要么修回归，要么重新记录标定值并说明依据"
    )
    assert metrics["split_recall"] == RECORDED_RULES_SPLIT_RECALL, metrics
    assert metrics["missed_split"] == RECORDED_MISSED_SPLIT, metrics["missed_split_ids"]


def test_semantic_signal_is_not_yet_worth_enabling():
    """打开语义信号必须先证明「明确改善」，而且不能拿误切换召回。"""
    runner, cases, scores = _load()
    rules = runner.evaluate(cases, scores, semantic_threshold=None)
    curve = [runner.evaluate(cases, scores, semantic_threshold=th) for th in (0.30, 0.35, 0.40, 0.45, 0.50)]
    best = max(curve, key=lambda m: (m["accuracy"], m["split_recall"]))
    gain = best["accuracy"] - rules["accuracy"]
    assert gain < CLEAR_IMPROVEMENT, (
        f"语义信号已经带来 {gain:.4f} 的准确率提升，超过清晰改善的门槛，"
        "可以重新评估是否把 boundary 模式从 shadow 提升为 enabled"
    )
    # 想提高召回就得接受误切：这条断言记录的就是当时的取舍
    aggressive = [m for m in curve if m["split_recall"] > rules["split_recall"] + 0.2]
    assert aggressive and all(m["false_split"] >= 5 for m in aggressive), curve


def test_boundary_policy_keeps_the_shadow_default():
    """默认仍是 shadow：只记录建议，不实际切分。

    改成 enabled 就是让确定性规则真的动片段边界 —— 那要先跑这份评测、
    在提交里写明结论，再改这个默认值。
    """
    from agent.services.turn_orchestrator import DEFAULT_BOUNDARY_MODE

    assert DEFAULT_BOUNDARY_MODE == "shadow", (
        "边界默认模式被改成 "
        f"{DEFAULT_BOUNDARY_MODE!r}：打开切分前必须重跑 backend/evals/boundary_semantic/ "
        "并重新决策（当前结论：规则臂误切 0，语义信号增益只有一个用例且对阈值极敏感）"
    )


def test_semantic_splitting_is_not_wired_into_the_production_policy():
    """语义切分只允许停在评测里：生产策略不得接收语义/向量输入。

    有人把语义信号接进生产，这个测试会失败并要求先重新决策 —— 这正是
    「shadow 是结论，不是懒」的守卫。
    """
    import inspect

    from agent.memory import boundary as boundary_module

    source = inspect.getsource(boundary_module).lower()
    for banned in ("embedding", "cosine", "similarity", "onnx", "numpy"):
        assert banned not in source, (
            f"生产边界策略里出现了 {banned!r}：语义切分必须先重新跑评测并给出结论"
        )
    params = set(inspect.signature(boundary_module.FragmentBoundaryPolicy.decide).parameters)
    forbidden = {"similarity", "sim", "semantic_score", "score", "vector"}
    assert not (params & forbidden), (
        f"decide() 新增了语义输入 {sorted(params & forbidden)}：先重新决策，再接线"
    )
