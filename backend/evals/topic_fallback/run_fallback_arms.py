# -*- coding: utf-8 -*-
"""embedding 不可用时的 Topic 判定 fallback 评测（第三阶段 E8）。

现状：有真实 embedding 时判定已较完整校准（118 条语料）；**embedding 不可用时
（规则层：查询词元 ∩ 话题关键词）质量缺乏同等级证据**。这里强制关闭 embedding，
用同一份 118 条语料测 fallback，并比较四个方案（A/B/C/D）。

三个方案走**生产代码路径**（TopicPredictor._predict_rules + affinity.classify，
只是注入不同的 rules_* 阈值）；D 是「低置信交给后续机制」的评测侧模拟
（生产侧要改 turn_orchestrator/injection，不在本轮 scope）。

用法（backend 目录下；先设 TEMP）：
    uv run --frozen python evals/topic_fallback/run_fallback_arms.py --json evals/topic_fallback/results.json
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from pathlib import Path

HERE = Path(__file__).parent
BACKEND = HERE.parents[1]
sys.path.insert(0, str(BACKEND / "src"))

CASES = HERE.parent / "topic_threshold" / "cases.jsonl"
MODES = ("in_topic", "switch", "new_topic")
SHORT_CATEGORIES = {"short_cn", "no_signal"}
MIXED_CATEGORIES = {"en", "mixed"}


class _Fingerprint:
    def __init__(self, topic_id, title, keywords, summary_preview=""):
        self.topic_id = topic_id
        self.title = title
        self.keywords = list(keywords)
        self.summary_preview = summary_preview


class _StubTopics:
    def __init__(self, fps):
        self._fps = fps

    def list_with_fingerprints(self):
        return self._fps


def load_cases():
    return [
        json.loads(line)
        for line in CASES.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def rules_decision(case, policy):
    """强制关闭 embedding：走生产规则层 + 生产判定入口。"""
    from agent.services.affinity import classify
    from agent.services.predict import TopicPredictor

    fps = [
        _Fingerprint(t["id"], t.get("title", ""), t.get("keywords", []), t.get("summary_preview", ""))
        for t in case["topics"]
    ]
    # 关键：阈值必须**注入到预测器**。只把 policy 传给 classify 是不够的 ——
    # _predict_rules 用 self.rules_new_topic_threshold 决定 main_topic_id，
    # 预测器用默认 0.2 时，任何 score<0.2 的候选都不会成为 owner，
    # 于是 switch 永远不触发（这个坑第一次跑就踩到了，见 EXPERIMENTS-P3-E.md）。
    predictor = TopicPredictor(
        None,
        None,
        _StubTopics(fps),
        new_topic_threshold=policy.new_topic_threshold,
        rules_new_topic_threshold=policy.rules_new_topic_threshold,
        aux_topic_threshold=policy.aux_topic_threshold,
        rules_aux_topic_threshold=policy.rules_aux_topic_threshold,
        switch_delta=policy.switch_delta,
        rules_switch_delta=policy.rules_switch_delta,
        aux_top_count=policy.aux_top_count,
    )
    # 生产入口：embedding=None 时走规则层
    prediction = predictor.predict(case["message"], current_topic_id=case.get("current_topic"))
    assert prediction.backend_used == "rules", prediction.backend_used
    return classify(case["message"], prediction, case.get("current_topic"), [], policy=policy), prediction


def decision_d(case, policy, *, confident: float):
    """D：规则层有明确信号才按它判；否则留在当前话题并标记「待确认」。"""
    decision, prediction = rules_decision(case, policy)
    top = max((prediction.scores or {}).values(), default=0.0)
    if top >= confident:
        return decision.mode.value, {"handoff": False, "top": top}
    if case.get("current_topic"):
        return "in_topic", {"handoff": True, "top": top}
    return decision.mode.value, {"handoff": False, "top": top}


def evaluate(cases, *, mode: str, policy, confident: float = 0.5):
    """mode="rules" 直接按规则判定；mode="handoff" 低置信时留在当前话题并标记待确认。"""
    confusion = {a: {b: 0 for b in MODES} for a in MODES}
    per_cat: dict = {}
    handoffs = useful_handoffs = 0
    rows = []
    for case in cases:
        expected = case["expected"]
        if mode == "handoff":
            predicted, extra = decision_d(case, policy, confident=confident)
            if extra["handoff"]:
                handoffs += 1
                if expected == "new_topic":
                    useful_handoffs += 1
        else:
            predicted, _ = rules_decision(case, policy)
            predicted = predicted.mode.value
        confusion[expected][predicted] += 1
        slot = per_cat.setdefault(case["category"], {"n": 0, "ok": 0})
        slot["n"] += 1
        if predicted == expected:
            slot["ok"] += 1
        rows.append({"id": case["id"], "category": case["category"], "expected": expected,
                     "predicted": predicted})

    def rate(num, den):
        return round(num / den, 4) if den else 0.0

    def subset(cats):
        sel = [r for r in rows if r["category"] in cats]
        if not sel:
            return {"n": 0, "accuracy": 0.0}
        return {"n": len(sel), "accuracy": rate(sum(1 for r in sel if r["expected"] == r["predicted"]), len(sel))}

    n = len(cases)
    return {
        "n": n,
        "accuracy": rate(sum(confusion[a][a] for a in MODES), n),
        "continuation_recall": rate(confusion["in_topic"]["in_topic"], sum(confusion["in_topic"].values())),
        "new_topic_recall": rate(confusion["new_topic"]["new_topic"], sum(confusion["new_topic"].values())),
        "false_new_topic_rate": rate(
            sum(confusion[a]["new_topic"] for a in MODES if a != "new_topic"),
            n - sum(confusion["new_topic"].values()),
        ),
        "switch_accuracy": rate(confusion["switch"]["switch"], sum(confusion["switch"].values())),
        "short_input": subset(SHORT_CATEGORIES),
        "mixed_language": subset(MIXED_CATEGORIES),
        "confusion": confusion,
        "per_category": {k: {"n": v["n"], "ok": v["ok"]} for k, v in sorted(per_cat.items())},
        "handoff": ({"count": handoffs, "useful": useful_handoffs,
                     "useful_rate": rate(useful_handoffs, handoffs)} if mode == "handoff" else None),
    }


def score_diagnostic(cases):
    """规则层到底有没有信号：按期望类别看 top score / current score 的分布。

    这决定了「明确词面信号」这条方案在 fallback 里是否真的存在（E8 的 C 方案前提）。
    """
    from agent.services.params import TOPIC

    buckets: dict = {}
    for case in cases:
        _, prediction = rules_decision(case, TOPIC)
        scores = dict(prediction.scores or {})
        current = case.get("current_topic")
        top = max(scores.values(), default=0.0)
        cur = scores.get(current, 0.0)
        slot = buckets.setdefault(case["expected"], {"n": 0, "top>0": 0, "top_hist": {},
                                                     "cur>0": 0, "cur_hist": {}})
        slot["n"] += 1
        if top > 0:
            slot["top>0"] += 1
        if cur > 0:
            slot["cur>0"] += 1
        for key, value in (("top_hist", top), ("cur_hist", cur)):
            bucket = round(value, 2)
            slot[key][str(bucket)] = slot[key].get(str(bucket), 0) + 1
    return buckets


def stratified_folds(cases, k: int = 5):
    """按 expected 分层切 k 折（与第一阶段 CV 同口径）。"""
    buckets: dict = {}
    for case in cases:
        buckets.setdefault(case["expected"], []).append(case)
    folds = [[] for _ in range(k)]
    for idx, (_, rows) in enumerate(sorted(buckets.items())):
        for i, case in enumerate(rows):
            folds[(i + idx) % k].append(case)
    return folds


def cross_validate(cases, *, safe_only: bool, k: int = 5):
    """分层 k 折：折内选参、折外计分（对「这套选参流程」的诚实估计）。"""
    from agent.services.params import TOPIC

    grid_new = (0.0, 0.01, 0.02, 0.03, 0.05, 0.08, 0.12, 0.2)
    grid_inc = (0.0, 0.01, 0.02, 0.03, 0.05)
    # 注意：兜底层读的是 rules_switch_*（不是 onnx 的 switch_*）——
    # 扫错参数会得到「扫描无效果」，这个坑第一次就跑出来过（见 EXPERIMENTS-P3-E.md）
    grid_sw = (0.55,) if safe_only else (0.02, 0.03, 0.05, 0.55)
    grid_dlt = (0.15,) if safe_only else (0.0, 0.02, 0.05, 0.15)
    folds = stratified_folds(cases, k)
    pooled_ok = pooled_n = 0
    fold_rows = []
    for i, test_cases in enumerate(folds):
        ids = {c["id"] for c in test_cases}
        train = [c for c in cases if c["id"] not in ids]
        best = None
        for new_th in grid_new:
            for inc in grid_inc:
                for sw in grid_sw:
                    for dlt in grid_dlt:
                        policy = replace(TOPIC, rules_new_topic_threshold=new_th,
                                         rules_incumbent_threshold=inc,
                                         rules_switch_threshold=sw, rules_switch_delta=dlt)
                        m = evaluate(train, mode="rules", policy=policy)
                        if m["new_topic_recall"] == 0:
                            continue  # 放弃自动建话题不算方案
                        key = (m["accuracy"], m["new_topic_recall"])
                        if best is None or key > best[0]:
                            best = (key, policy, m)
        policy = best[1]
        held = evaluate(test_cases, mode="rules", policy=policy)
        pooled_ok += sum(held["confusion"][a][a] for a in MODES)
        pooled_n += held["n"]
        fold_rows.append({"fold": i, "policy": {
            "rules_new_topic_threshold": policy.rules_new_topic_threshold,
            "rules_incumbent_threshold": policy.rules_incumbent_threshold,
            "rules_switch_threshold": policy.rules_switch_threshold,
            "rules_switch_delta": policy.rules_switch_delta,
        }, "accuracy": held["accuracy"], "n": held["n"]})
    return {"k": k, "safe_only": safe_only, "pooled_accuracy": round(pooled_ok / (pooled_n or 1), 4),
            "folds": fold_rows}


# 修复前的兜底参数（规则分数只到 0~0.22 却用 0.2 门槛；rules 专用 switch 门槛当时不存在，
# 兜底实际共用 onnx 的 switch_threshold/switch_delta = 0.55/0.15）
BEFORE_FIX = {
    "rules_new_topic_threshold": 0.2,
    "rules_incumbent_threshold": 0.2,
    "rules_switch_threshold": 0.55,
    "rules_switch_delta": 0.15,
}


def arm_policies():
    """A/B/C/D 四方案的参数与模式。

    A_before_fix    修复前的兜底（显式写死旧参数，好在同一份结果里与修复后再比一次）
    B_conservative  保守留在当前话题（inc=0 且不让任何话题成为 owner）
    C_fixed_default 规则 + 明确词面信号 = **当前生产默认**（rules_* 按真实量纲标定）
    D_handoff       低置信交给后续机制（有明确词面命中才判，否则留在当前话题并标记待确认）
    """
    from agent.services.params import TOPIC

    return {
        "A_before_fix": (replace(TOPIC, **BEFORE_FIX), "rules"),
        "B_conservative": (replace(TOPIC, rules_new_topic_threshold=1.0,
                                   rules_incumbent_threshold=0.0), "rules"),
        "C_fixed_default": (TOPIC, "rules"),
        # D 的「明确词面信号」门槛与 C 的规则门槛同一量纲
        "D_handoff": (TOPIC, "handoff"),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", default="")
    ap.add_argument("--sweep", action="store_true", help="跑 C 的阈值扫描")
    ap.add_argument("--diagnose", action="store_true", help="打印规则层分数分布")
    ap.add_argument("--verbose", action="store_true", help="打印扫描的每一行")
    ap.add_argument("--cv", action="store_true", help="分层 5 折（折内选参、折外计分）")
    args = ap.parse_args()

    cases = load_cases()
    payload = {"n": len(cases), "arms": {}, "sweep": []}
    if args.diagnose:
        diag = score_diagnostic(cases)
        payload["score_diagnostic"] = diag
        print("# 规则层分数分布（期望类别 → top>0 的比例 / current>0 的比例）")
        for expected, slot in sorted(diag.items()):
            print(f"  {expected:<10} n={slot['n']:<3} top>0: {slot['top>0']}/{slot['n']}  "
                  f"current>0: {slot['cur>0']}/{slot['n']}")
            print(f"      top 分布 {slot['top_hist']}")
            print(f"      current 分布 {slot['cur_hist']}")
    print(f"# embedding 不可用时的 Topic fallback 评测  cases={len(cases)}")
    for arm, (policy, mode) in arm_policies().items():
        confident = 1e-9 if arm.startswith("D") else 0.5
        metrics = evaluate(cases, mode=mode, policy=policy, confident=confident)
        payload["arms"][arm] = metrics
        print(f"\n## {arm}")
        print(f"   overall={metrics['accuracy']:.4f} 继续={metrics['continuation_recall']:.3f} "
              f"新话题召回={metrics['new_topic_recall']:.3f} 假新话题={metrics['false_new_topic_rate']:.3f} "
              f"切换={metrics['switch_accuracy']:.3f}")
        print(f"   短输入={metrics['short_input']['accuracy']:.3f}(n={metrics['short_input']['n']}) "
              f"中英混合={metrics['mixed_language']['accuracy']:.3f}(n={metrics['mixed_language']['n']})")
        if metrics["handoff"]:
            print(f"   交给后续机制 {metrics['handoff']['count']} 次，其中真新话题 {metrics['handoff']['useful']} "
                  f"({metrics['handoff']['useful_rate']:.3f})")
        print(f"   逐类：" + " ".join(f"{k}={v['ok']}/{v['n']}" for k, v in metrics["per_category"].items()))

    if args.sweep:
        from agent.services.params import TOPIC

        print("\n## C 的阈值扫描（规则层分数实际只有 0~0.22，见 --diagnose，所以门槛要按这个量纲扫）")
        print(f"{'new':>5} {'inc':>5} {'sw':>5} {'dlt':>5} {'acc':>7} {'继续':>6} {'新召回':>7} {'假新':>6} {'切换':>6}")
        for new_th in (0.0, 0.02, 0.03, 0.05, 0.08, 0.12, 0.2):
            for inc in (0.0, 0.02, 0.03, 0.05, 0.08):
                for sw in (0.02, 0.03, 0.05, 0.55):
                    for dlt in (0.0, 0.02, 0.15):
                        policy = replace(TOPIC, rules_new_topic_threshold=new_th,
                                         rules_incumbent_threshold=inc,
                                         rules_switch_threshold=sw, rules_switch_delta=dlt)
                        m = evaluate(cases, mode="rules", policy=policy)
                        payload["sweep"].append({"rules_new_topic_threshold": new_th,
                                                 "rules_incumbent_threshold": inc,
                                                 "switch_threshold": sw,
                                                 "switch_delta": dlt,
                                                 "accuracy": m["accuracy"],
                                                 "continuation_recall": m["continuation_recall"],
                                                 "new_topic_recall": m["new_topic_recall"],
                                                 "false_new_topic_rate": m["false_new_topic_rate"],
                                                 "switch_accuracy": m["switch_accuracy"]})
                        if args.verbose:
                            print(f"{new_th:>5.2f} {inc:>5.2f} {sw:>5.2f} {dlt:>5.2f} {m['accuracy']:>7.4f} "
                                  f"{m['continuation_recall']:>6.3f} {m['new_topic_recall']:>7.3f} "
                                  f"{m['false_new_topic_rate']:>6.3f} {m['switch_accuracy']:>6.3f}")
        # 选「准确率最高、但真新话题召回不能为 0（否则等于放弃自动建话题）」
        candidates = [r for r in payload["sweep"] if r["new_topic_recall"] > 0]
        best = max(candidates or payload["sweep"], key=lambda r: (r["accuracy"], r["new_topic_recall"]))
        print(f"   扫描最优（要求新话题召回>0）：acc={best['accuracy']} "
              f"(new={best['rules_new_topic_threshold']}, inc={best['rules_incumbent_threshold']}, "
              f"sw={best['switch_threshold']}, dlt={best['switch_delta']}) "
              f"继续={best['continuation_recall']} 新召回={best['new_topic_recall']} 假新={best['false_new_topic_rate']}")
        print("   top-5（按准确率）：")
        for row in sorted(payload["sweep"], key=lambda r: (-r["accuracy"], -r["new_topic_recall"]))[:5]:
            print(f"     acc={row['accuracy']:.4f} new={row['rules_new_topic_threshold']} "
                  f"inc={row['rules_incumbent_threshold']} sw={row['switch_threshold']} dlt={row['switch_delta']} "
                  f"继续={row['continuation_recall']} 新召回={row['new_topic_recall']} 假新={row['false_new_topic_rate']}")

    if args.cv:
        for safe in (True, False):
            cv = cross_validate(cases, safe_only=safe)
            payload.setdefault("cv", []).append(cv)
            tag = "只改 rules_*（生产安全）" if safe else "含 rules 专用 switch 门槛"
            print(f"\n## 分层 5 折（{tag}）：折内选参的诚实估计 acc={cv['pooled_accuracy']}")
            for row in cv["folds"]:
                print(f"   折 {row['fold']}: 折内选中 {row['policy']} → 折外 acc={row['accuracy']} (n={row['n']})")

    if args.json:
        Path(args.json).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n结果 -> {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
