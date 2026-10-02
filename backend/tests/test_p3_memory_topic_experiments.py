# -*- coding: utf-8 -*-
"""第三阶段 E：跨话题 explicit/implicit 拆分 + embedding 不可用时的 fallback（护栏）。

数据与结论：
  backend/evals/cross_topic/queries_cross_topic.json（46 条，explicit 30 / implicit 16）
  backend/evals/cross_topic/explicit_state.json（P0~P3 + 泄漏检查）
  backend/evals/topic_fallback/results.json（A/B/C/D + 扫描 + 分层 5 折）
  backend/evals/EXPERIMENTS-P3-E.md

本文件不依赖 ONNX 模型：fallback 路径本来就没有 embedding。
"""

from __future__ import annotations

import json
import sys
from dataclasses import replace
from pathlib import Path

EVALS = Path(__file__).resolve().parents[1] / "evals"
sys.path.insert(0, str(EVALS / "cross_topic"))
sys.path.insert(0, str(EVALS / "topic_fallback"))

EXPLICIT = {"explicit_title", "keyword_only", "mixed_language", "multi_topic", "long_span_old"}
IMPLICIT = {"pronoun_only", "conclusion_review"}


# ---------------------------------------------------------------- 数据集与拆分


def test_cross_topic_split_is_explicit_and_implicit():
    cross = json.loads((EVALS / "cross_topic" / "queries_cross_topic.json").read_text(encoding="utf-8"))
    queries = cross["queries"]
    explicit = [q for q in queries if q["subtype"] in EXPLICIT]
    implicit = [q for q in queries if q["subtype"] in IMPLICIT]
    assert len(explicit) >= 25, f"explicit 样本太少：{len(explicit)}"
    assert len(implicit) >= 12, f"implicit 样本太少：{len(implicit)}"
    assert len(explicit) + len(implicit) == len(queries)
    for query in queries:
        assert query["subtype"] in (EXPLICIT | IMPLICIT)
        assert query["note"] and query["target_topic"] != query["topic"]


def test_answer_leakage_check_flags_a_summary_that_contains_the_answer():
    """E4 的硬要求：summary 已经包含答案的样本不能用来证明检索改善。"""
    import explicit_state

    answer = "最终决定数据库改用 SQLite，理由是单机部署维护成本低"
    leaky = f"话题摘要：{answer}"
    clean = "话题摘要：讨论了部署成本与并发问题，尚未给出结论"
    assert explicit_state.leaks(leaky, [answer]) is True
    assert explicit_state.leaks(clean, [answer]) is False
    # 覆盖大部分词元（≥60%）也算泄漏，哪怕不是逐字复制
    paraphrased = "最终决定数据库改用 SQLite，理由是部署维护成本"
    assert explicit_state.leaks(paraphrased, [answer]) is True


def test_explicit_state_results_are_recorded_and_show_the_leakage_trap():
    payload = json.loads((EVALS / "cross_topic" / "explicit_state.json").read_text(encoding="utf-8"))
    latest = payload["states"]["latest"]
    others = payload["states"]["others"]
    # 用「最新一条记忆」当状态：46/46 全部泄漏 → 不能作为检索改善的证据
    assert latest["leaked"] == latest["n"] == 46
    # 无泄漏对照：0 泄漏，P2 反而比 P0 差
    assert others["leaked"] == 0
    p0 = payload["arms"]["others"]["P0"]["explicit"]
    p2 = payload["arms"]["others"]["P2"]["explicit"]
    assert p2["recall@5"] <= p0["recall@5"], "无泄漏状态下 P2 不应优于 P0（否则结论要重写）"
    p1 = payload["arms"]["others"]["P1"]["explicit"]
    assert p1["recall@1"] == p0["recall@1"], "已知话题但不注入状态（P1）在上一阶段已证无收益"


# ---------------------------------------------------------------- fallback（E8）


def test_fallback_corpus_is_available():
    corpus = EVALS / "topic_threshold" / "cases.jsonl"
    cases = [json.loads(l) for l in corpus.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(cases) >= 110
    assert all(c.get("current_topic") for c in cases)


def test_fallback_default_policy_reaches_the_recorded_quality():
    """C 方案（= 生产默认，按规则层真实量纲重标定门槛）必须保持住实测水平；下限卡在实测值略低处。

    门槛注入到**预测器**与 classify 两处（只传一处会让 switch 永不触发，
    这个坑第一次跑就踩到了，见 EXPERIMENTS-P3-E.md）。
    """
    from agent.services.params import TOPIC

    from run_fallback_arms import evaluate, load_cases

    metrics = evaluate(load_cases(), mode="rules", policy=TOPIC)  # 生产默认 = C 方案
    assert metrics["accuracy"] >= 0.80, metrics                # 实测 0.8305
    assert metrics["false_new_topic_rate"] <= 0.25, metrics    # 实测 0.185
    assert metrics["continuation_recall"] >= 0.75, metrics     # 实测 0.790
    assert metrics["new_topic_recall"] >= 0.85, metrics        # 实测 0.923
    assert metrics["switch_accuracy"] >= 0.80, metrics         # 实测 0.909


def test_fallback_defaults_are_calibrated():
    """rules_* 默认值必须是实测标定值，且不得碰 onnx 的参数（含两端共用项）。"""
    from agent.services.params import TOPIC

    assert TOPIC.rules_new_topic_threshold == 0.02
    assert TOPIC.rules_incumbent_threshold == 0.02
    assert TOPIC.rules_switch_threshold == 0.02
    assert TOPIC.rules_switch_delta == 0.02
    assert TOPIC.new_topic_threshold == 0.42       # onnx 侧保持第一阶段标定值
    assert TOPIC.incumbent_threshold == 0.42
    assert TOPIC.switch_threshold == 0.55          # 两端共用项不得被兜底改动
    assert TOPIC.switch_delta == 0.15


def test_fallback_fix_is_a_real_improvement_over_the_old_params():
    """修复前后必须在**同一次评估**里可对比（旧参数显式写死，不依赖历史版本）。"""
    from agent.services.params import TOPIC

    from run_fallback_arms import BEFORE_FIX, evaluate, load_cases

    cases = load_cases()
    before = evaluate(cases, mode="rules", policy=replace(TOPIC, **BEFORE_FIX))
    after = evaluate(cases, mode="rules", policy=TOPIC)
    assert before["accuracy"] < 0.5, before                      # 实测 0.4237
    assert after["accuracy"] >= before["accuracy"] + 0.3, (before, after)
    assert after["false_new_topic_rate"] <= before["false_new_topic_rate"] - 0.4, (before, after)


def test_conservative_policy_never_invents_a_topic():
    """B 方案：保守延续 —— 假新话题必须为 0（宁可漏判，不要拆散上下文）。"""
    from agent.services.params import TOPIC

    from run_fallback_arms import evaluate, load_cases

    policy = replace(TOPIC, rules_new_topic_threshold=1.0, rules_incumbent_threshold=0.0)
    metrics = evaluate(load_cases(), mode="rules", policy=policy)
    assert metrics["false_new_topic_rate"] == 0.0
    assert metrics["continuation_recall"] == 1.0
    assert metrics["new_topic_recall"] == 0.0  # 代价写清楚：不会自动建新话题


def test_handoff_policy_keeps_the_decision_for_the_model():
    """D 方案：低置信时留在当前话题并标记待确认（交给后续机制）。"""
    from agent.services.params import TOPIC

    from run_fallback_arms import evaluate, load_cases

    metrics = evaluate(load_cases(), mode="handoff", policy=TOPIC, confident=1e-9)
    assert metrics["handoff"]["count"] > 0, "D 方案必须有交接发生，否则等于没实现"
    assert metrics["false_new_topic_rate"] <= 0.2, metrics


def test_rules_thresholds_do_not_affect_the_onnx_path():
    """rules_* 只影响兜底路径：改它们不得改变有 embedding 时的判定。

    这是「生产安全变体」的依据（见 EXPERIMENTS-P3-E.md 的 Cross-route 建议）。
    """
    from agent.services.affinity import classify
    from agent.services.params import TOPIC
    from agent.services.predict import TopicPredictor

    snapshot = json.loads((EVALS / "topic_threshold" / "scores_onnx.json").read_text(encoding="utf-8"))
    cases = [json.loads(l) for l in (EVALS / "topic_threshold" / "cases.jsonl")
             .read_text(encoding="utf-8").splitlines() if l.strip()]

    class _Fp:
        def __init__(self, t):
            self.topic_id = t["id"]
            self.title = t.get("title", "")
            self.keywords = list(t.get("keywords", []))
            self.summary_preview = t.get("summary_preview", "")

    class _Topics:
        def __init__(self, fps):
            self._fps = fps

        def list_with_fingerprints(self):
            return self._fps

    modified = replace(TOPIC, rules_new_topic_threshold=0.01, rules_incumbent_threshold=0.01)
    for case in cases:
        scores = snapshot["cases"][case["id"]]["scores"]
        fps = [_Fp(t) for t in case["topics"]]
        decisions = []
        for policy in (TOPIC, modified):
            predictor = TopicPredictor(None, None, _Topics(fps),
                                       new_topic_threshold=policy.new_topic_threshold,
                                       rules_new_topic_threshold=policy.rules_new_topic_threshold,
                                       aux_topic_threshold=policy.aux_topic_threshold,
                                       rules_aux_topic_threshold=policy.rules_aux_topic_threshold,
                                       switch_delta=policy.switch_delta,
                                       rules_switch_delta=policy.rules_switch_delta,
                                       aux_top_count=policy.aux_top_count)
            prediction = predictor._rank(dict(scores), case.get("current_topic"), backend="onnx")
            decisions.append(classify(case["message"], prediction, case.get("current_topic"), [],
                                      policy=policy).mode.value)
        assert decisions[0] == decisions[1], f"{case['id']} 的 onnx 判定被 rules_* 改变了"


def test_fallback_results_are_recorded():
    payload = json.loads((EVALS / "topic_fallback" / "results.json").read_text(encoding="utf-8"))
    arms = payload["arms"]
    assert set(arms) == {"A_before_fix", "B_conservative", "C_fixed_default", "D_handoff"}
    assert arms["A_before_fix"]["accuracy"] < arms["C_fixed_default"]["accuracy"]
    assert payload["cv"], "分层 5 折必须一起入库（否则 0.8305 会被误当成泛化估计）"
    for entry in payload["cv"]:
        assert entry["k"] == 5 and entry["pooled_accuracy"] > 0.7
