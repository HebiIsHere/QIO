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


def _load():
    import run_boundary_arms

    cases = run_boundary_arms.load_cases()
    scores = run_boundary_arms.load_scores()
    return run_boundary_arms, cases, scores


def test_rules_arm_never_splits_by_mistake():
    runner, cases, scores = _load()
    metrics = runner.evaluate(cases, scores, semantic_threshold=None)
    assert metrics["false_split"] == 0, metrics["wrong_split_ids"]
    assert metrics["accuracy"] >= MIN_RULES_ACCURACY, metrics


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
    """默认仍是 shadow：只记录建议，不实际切分。"""
    from agent.services.turn_orchestrator import DEFAULT_BOUNDARY_MODE

    assert DEFAULT_BOUNDARY_MODE == "shadow"
