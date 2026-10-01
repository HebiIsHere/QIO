# -*- coding: utf-8 -*-
"""分段边界：确定性规则 vs 规则+语义（真实 ONNX 余弦快照）。

评测对象是**生产策略** agent.memory.boundary.FragmentBoundaryPolicy（纯确定性）。
第二条臂只在规则返回 uncertain 时加一个「语义漂移」判定：
当前输入与上一段上下文的余弦低于阈值 → 认为阶段已经变了；否则保守延续。

语义分数来自 backend/evals/boundary_semantic/scores_onnx.json（真实模型记录的快照），
所以本脚本离线可复现，CI 上不需要 ONNX 模型。

用法（backend 目录下）：
    uv run --frozen python evals/boundary_semantic/record_scores.py     # 先记录快照
    uv run --frozen python evals/boundary_semantic/run_boundary_arms.py
    uv run --frozen python evals/boundary_semantic/run_boundary_arms.py --json out.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BACKEND / "src"))

HERE = Path(__file__).parent
CASES = HERE / "cases.jsonl"
SCORES = HERE / "scores_onnx.json"

SPLIT = "split"


def load_cases():
    return [json.loads(l) for l in CASES.read_text(encoding="utf-8").splitlines() if l.strip()]


def load_scores():
    if not SCORES.exists():
        raise SystemExit(f"缺少语义快照 {SCORES}；先跑 record_scores.py（需要 ONNX 模型）")
    return json.loads(SCORES.read_text(encoding="utf-8"))


def rules_decision(case):
    from agent.memory.boundary import FragmentBoundaryPolicy

    policy = FragmentBoundaryPolicy(max_turns=10, max_tokens=8000)
    return policy.decide(user_input=case["text"], fragment_turns=1, fragment_tokens=200)


def semantic_decision(case, sim, threshold):
    """规则 + 语义：只在规则 uncertain 时才用语义信号。"""
    decision = rules_decision(case)
    if decision.action != "uncertain":
        return decision.action, decision.reason
    return (SPLIT if sim < threshold else "continue"), "semantic_drift"


def evaluate(cases, scores, semantic_threshold=None):
    per_cat: dict = {}
    confusion = {"split": {"split": 0, "continue": 0}, "continue": {"split": 0, "continue": 0}}
    rows = []
    for case in cases:
        sim = scores["cases"][case["id"]]["sim"]
        if semantic_threshold is None:
            decision = rules_decision(case)
            action = decision.action if decision.action in (SPLIT, "continue") else "continue"
            reason = decision.reason
        else:
            action, reason = semantic_decision(case, sim, semantic_threshold)
        expected = case["expect"]
        ok = (action == SPLIT) == (expected == SPLIT)
        confusion[expected][action] += 1
        slot = per_cat.setdefault(case["category"], {"n": 0, "ok": 0, "wrong": []})
        slot["n"] += 1
        if ok:
            slot["ok"] += 1
        else:
            slot["wrong"].append({"id": case["id"], "expected": expected, "actual": action})
        rows.append({"id": case["id"], "category": case["category"], "expected": expected,
                     "actual": action, "reason": reason, "sim": round(sim, 4), "ok": ok})
    n = len(cases)
    wrong_split = [r for r in rows if r["expected"] != SPLIT and r["actual"] == SPLIT]
    missed_split = [r for r in rows if r["expected"] == SPLIT and r["actual"] != SPLIT]
    return {
        "n": n,
        "accuracy": round(sum(1 for r in rows if r["ok"]) / n, 4),
        "split_recall": round(confusion["split"]["split"] / (sum(confusion["split"].values()) or 1), 4),
        "false_split": len(wrong_split),
        "missed_split": len(missed_split),
        "wrong_split_ids": [r["id"] for r in wrong_split],
        "missed_split_ids": [r["id"] for r in missed_split],
        "per_category": {k: {"n": v["n"], "ok": v["ok"], "wrong": v["wrong"]} for k, v in sorted(per_cat.items())},
        "rows": rows,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", default="")
    args = ap.parse_args()

    cases = load_cases()
    scores = load_scores()
    print(f"# 分段边界评测  cases={len(cases)}  模型={scores['model']}")
    print()
    rules = evaluate(cases, scores, semantic_threshold=None)
    print(f"## 臂 1：确定性规则（生产现状，shadow 模式）")
    print(f"   accuracy={rules['accuracy']} split_recall={rules['split_recall']} "
          f"误切={rules['false_split']} 漏切={rules['missed_split']}")
    for cat, st in rules["per_category"].items():
        print(f"   {cat:<22} {st['ok']}/{st['n']}")
    print(f"   漏切/误切：{rules['missed_split_ids']} / {rules['wrong_split_ids']}")
    print()

    print("## 臂 2：规则 + 语义（仅在 uncertain 时用余弦漂移判定）")
    print(f"{'th':>5} {'acc':>6} {'split_R':>8} {'误切':>5} {'漏切':>5}")
    curve = []
    for threshold in [0.30, 0.35, 0.40, 0.45, 0.50, 0.55, 0.60]:
        m = evaluate(cases, scores, semantic_threshold=threshold)
        curve.append({"threshold": threshold, **{k: v for k, v in m.items() if k != "rows"}})
        print(f"{threshold:>5.2f} {m['accuracy']:>6.3f} {m['split_recall']:>8.3f} "
              f"{m['false_split']:>5} {m['missed_split']:>5}")
    best = max(curve, key=lambda row: (row["accuracy"], row["split_recall"]))
    print(f"   最好的一档：th={best['threshold']} acc={best['accuracy']} "
          f"（规则臂 acc={rules['accuracy']}，差值 {best['accuracy'] - rules['accuracy']:+.4f}）")
    print(f"   语义信号能覆盖的 uncertain 用例：见 scores_onnx.json 的 sim 分布")
    print()

    if args.json:
        Path(args.json).write_text(
            json.dumps({"model": scores["model"], "rules": {k: v for k, v in rules.items() if k != "rows"},
                        "semantic_curve": curve, "n": len(cases)},
                       ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"结果 -> {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
