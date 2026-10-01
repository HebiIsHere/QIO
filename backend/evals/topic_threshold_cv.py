# -*- coding: utf-8 -*-
"""话题判定的参数选择：分层 5 折 + 敏感度 + 逐类混淆（离线，可复现）。

数据来自 evals/topic_threshold_curve.py --record 记录的真实 ONNX 余弦快照
（backend/evals/topic_threshold/scores_onnx.json），所以本脚本不加载模型、
不联网。判定走**生产代码**：TopicPredictor._rank + affinity.classify(policy=...)，
评测里没有第二条平行实现。

它回答（对应 Lead 的三条约束）：
1. 参数是不是在同一批数据上挑出来的过拟合？→ 分层 5 折（折内选参、折外计分）；
2. 真新话题的召回会不会掉？→ 单独一列，并在选参目标里设下限；
3. 是不是尖峰？→ 最终参数 ±0.05 / ±0.10 的敏感度表 + 逐类混淆。

用法（backend 目录下）：
    uv run --frozen python evals/topic_threshold_cv.py
    uv run --frozen python evals/topic_threshold_cv.py --json evals/topic_threshold/cv_onnx.json
"""

from __future__ import annotations

import argparse
import json
from dataclasses import replace
from pathlib import Path

HERE = Path(__file__).with_name("topic_threshold")
CASES = HERE / "cases.jsonl"
SNAPSHOT = HERE / "scores_onnx.json"
MODES = ("in_topic", "switch", "new_topic")

# 选参目标：准确率最大化，但真新话题召回不得低于这个下限（Lead 约束 2）
GEN_NEW_FLOOR = 0.85


def load():
    cases = [json.loads(line) for line in CASES.read_text(encoding="utf-8").splitlines() if line.strip()]
    snapshot = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    scores = {cid: v["scores"] for cid, v in snapshot["cases"].items()}
    return cases, scores, snapshot["model"]


def predict_with(case, raw_scores, policy):
    """生产路径：TopicPredictor._rank（阈值来自 policy）+ affinity.classify(policy=...)。"""
    from agent.services.affinity import classify
    from agent.services.predict import TopicPredictor

    predictor = TopicPredictor(None, None, None)
    predictor.new_topic_threshold = policy.new_topic_threshold
    prediction = predictor._rank(dict(raw_scores), case.get("current_topic"), backend="onnx")
    decision = classify(case["message"], prediction, case.get("current_topic"), [], policy=policy)
    return decision.mode.value, prediction


def measure(cases, scores, policy):
    confusion = {a: {b: 0 for b in MODES} for a in MODES}
    per_cat = {}
    wrong = []
    for case in cases:
        exp = case["expected"]
        pred, _ = predict_with(case, scores[case["id"]], policy)
        confusion[exp][pred] += 1
        slot = per_cat.setdefault(case["category"], [0, 0])
        slot[1] += 1
        if pred == exp:
            slot[0] += 1
        else:
            wrong.append({"id": case["id"], "category": case["category"], "expected": exp, "predicted": pred})
    n = len(cases) or 1
    ok = sum(confusion[a][a] for a in MODES)
    gen_new = confusion["new_topic"]["new_topic"] / (sum(confusion["new_topic"].values()) or 1)
    pred_new = sum(confusion[a]["new_topic"] for a in MODES)
    false_new = pred_new - confusion["new_topic"]["new_topic"]
    return {
        "n": len(cases),
        "acc": ok / n,
        "genuinely_new_recall": gen_new,
        "new_topic_precision": confusion["new_topic"]["new_topic"] / pred_new if pred_new else 0.0,
        "false_new_rate": false_new / (n - sum(confusion["new_topic"].values()) or 1),
        "in_topic_recall": confusion["in_topic"]["in_topic"] / (sum(confusion["in_topic"].values()) or 1),
        "switch_recall": confusion["switch"]["switch"] / (sum(confusion["switch"].values()) or 1),
        "confusion": confusion,
        "per_category": {k: {"ok": v[0], "n": v[1]} for k, v in sorted(per_cat.items())},
        "wrong": wrong,
    }


def score_of(m):
    return m["acc"] if m["genuinely_new_recall"] >= GEN_NEW_FLOOR else -1.0


def grid():
    from dataclasses import replace as _replace

    from agent.services.params import TOPIC

    out = []
    for ntt in (0.35, 0.40, 0.42, 0.45, 0.50):
        for inc in (0.25, 0.30, 0.32, 0.35, 0.40):
            for sw in (0.50, 0.55, 0.60):
                for delta in (0.05, 0.10, 0.15):
                    for mchars in (0, 8, 12, 16):
                        out.append(_replace(
                            TOPIC,
                            new_topic_threshold=ntt,
                            incumbent_threshold=inc,
                            switch_threshold=sw,
                            switch_delta=delta,
                            min_new_topic_chars=mchars,
                        ))
    return out


def legacy_policy():
    """旧实现（单阈值）在新代码里的等价参数：没有现任兜底、没有短输入保护、
    切换不要求 margin。用来做「改造前 vs 改造后」的同口径对照。"""
    from agent.services.params import TOPIC

    return replace(
        TOPIC,
        new_topic_threshold=0.7,
        incumbent_threshold=1.0,
        switch_threshold=0.55,
        switch_delta=0.0,
        min_new_topic_chars=0,
    )


def fmt_policy(pol):
    return (f"ntt={pol.new_topic_threshold} inc={pol.incumbent_threshold} "
            f"sw={pol.switch_threshold} delta={pol.switch_delta} minchars={pol.min_new_topic_chars}")


def stratified_folds(cases, k=5):
    """按 expected 分层切 k 折（同类别的用例轮转分配，保证每折类别比例接近）。"""
    buckets = {}
    for case in cases:
        buckets.setdefault(case["expected"], []).append(case)
    folds = [[] for _ in range(k)]
    for idx, (_, rows) in enumerate(sorted(buckets.items())):
        for i, case in enumerate(rows):
            folds[(i + idx) % k].append(case)
    return folds


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", default="", help="把结果写到指定路径")
    ap.add_argument("--k", type=int, default=5)
    args = ap.parse_args()

    from agent.services.params import TOPIC

    cases, scores, model = load()
    print(f"# 数据：{model}")
    print(f"# 用例 {len(cases)} 条；五种参数的组合网格 + 分层 {args.k} 折 + 敏感度")
    print()

    # -- 0) 改造前 vs 改造后（全量，同口径） --------------------------------
    legacy = measure(cases, scores, legacy_policy())
    current = measure(cases, scores, TOPIC)
    print("## 0) 同口径对照（全量数据）")
    print(f"  旧实现(单阈值0.7) : acc={legacy['acc']:.3f} in_topic={legacy['in_topic_recall']:.3f} "
          f"new_R={legacy['genuinely_new_recall']:.3f} false_new={legacy['false_new_rate']:.3f}")
    print(f"  当前 params.TOPIC : acc={current['acc']:.3f} in_topic={current['in_topic_recall']:.3f} "
          f"new_R={current['genuinely_new_recall']:.3f} false_new={current['false_new_rate']:.3f}")
    print("  （旧实现逐类：" + json.dumps({k: "%d/%d" % (v["ok"], v["n"]) for k, v in legacy["per_category"].items()}, ensure_ascii=False) + "）")
    print()

    # -- 1) 全网格：Pareto 前沿 --------------------------------------------
    combos = grid()
    scored = []
    for pol in combos:
        m = measure(cases, scores, pol)
        scored.append((m["acc"], pol, m))
    feasible = [t for t in scored if t[2]["genuinely_new_recall"] >= GEN_NEW_FLOOR]
    print(f"## 1a) 按准确率排序的 top-15（{len(combos)} 组，不设召回下限）")
    for _, pol, m in sorted(scored, key=lambda t: (-t[0], -t[2]["genuinely_new_recall"]))[:15]:
        print(f"  acc={m['acc']:.3f} new_R={m['genuinely_new_recall']:.3f} "
              f"in_topic={m['in_topic_recall']:.3f} false_new={m['false_new_rate']:.3f} | {fmt_policy(pol)}")
    print()
    print("## 1b) Pareto：给定「真新话题召回」下限时能拿到的最好准确率")
    for floor in (0.0, 0.5, 0.6, 0.7, 0.75, 0.8, 0.85, 0.9, 1.0):
        pool = [t for t in scored if t[2]["genuinely_new_recall"] >= floor]
        if not pool:
            print(f"  new_R >= {floor:.2f} : 无可行组合")
            continue
        acc, pol, m = max(pool, key=lambda t: t[0])
        print(f"  new_R >= {floor:.2f} : acc={acc:.3f} "
              f"(实际 new_R={m['genuinely_new_recall']:.3f} in_topic={m['in_topic_recall']:.3f} "
              f"false_new={m['false_new_rate']:.3f}) | {fmt_policy(pol)}")
    print(f"  达到 new_R >= {GEN_NEW_FLOOR} 的组合：{len(feasible)}/{len(combos)}")
    print()

    # -- 1c) 具名候选点 -----------------------------------------------------
    print("## 1c) 具名候选（人工可读的对照）")
    candidates = {
        "legacy-0.7": legacy_policy(),
        "cand-ntt40-inc32": replace(TOPIC, new_topic_threshold=0.40, incumbent_threshold=0.32,
                                    switch_threshold=0.55, switch_delta=0.10, min_new_topic_chars=12),
        "cand-ntt40-inc35": replace(TOPIC, new_topic_threshold=0.40, incumbent_threshold=0.35,
                                    switch_threshold=0.55, switch_delta=0.10, min_new_topic_chars=12),
        "cand-ntt40-inc40": replace(TOPIC, new_topic_threshold=0.40, incumbent_threshold=0.40,
                                    switch_threshold=0.55, switch_delta=0.10, min_new_topic_chars=12),
        "cand-ntt42-inc42": replace(TOPIC, new_topic_threshold=0.42, incumbent_threshold=0.42,
                                    switch_threshold=0.55, switch_delta=0.10, min_new_topic_chars=12),
        "cand-ntt45-inc45": replace(TOPIC, new_topic_threshold=0.45, incumbent_threshold=0.45,
                                    switch_threshold=0.55, switch_delta=0.10, min_new_topic_chars=12),
        "cand-ntt50-inc50": replace(TOPIC, new_topic_threshold=0.50, incumbent_threshold=0.50,
                                    switch_threshold=0.55, switch_delta=0.10, min_new_topic_chars=12),
        "legacy+minchars": replace(legacy_policy(), min_new_topic_chars=12),
    }
    candidate_rows = []
    for name, pol in candidates.items():
        m = measure(cases, scores, pol)
        candidate_rows.append({"name": name, "policy": fmt_policy(pol), "acc": round(m["acc"], 4),
                               "genuinely_new_recall": round(m["genuinely_new_recall"], 4),
                               "in_topic_recall": round(m["in_topic_recall"], 4),
                               "switch_recall": round(m["switch_recall"], 4),
                               "false_new_rate": round(m["false_new_rate"], 4)})
        print(f"  {name:<18} acc={m['acc']:.3f} new_R={m['genuinely_new_recall']:.3f} "
              f"in_topic={m['in_topic_recall']:.3f} sw_R={m['switch_recall']:.3f} "
              f"false_new={m['false_new_rate']:.3f}")
    print()

    # -- 2) 分层 k 折（折内选参、折外计分） --------------------------------
    folds = stratified_folds(cases, args.k)
    pool_ok = pool_n = 0
    pool_gen_ok = pool_gen_n = 0
    fold_reports = []
    for i, test_cases in enumerate(folds):
        test_ids = {c["id"] for c in test_cases}
        train = [c for c in cases if c["id"] not in test_ids]
        best = None
        for pol in combos:
            m = measure(train, scores, pol)
            key = (score_of(m), m["genuinely_new_recall"], m["acc"])
            if best is None or key > best[0]:
                best = (key, pol, m)
        pol = best[1]
        held = measure(test_cases, scores, pol)
        pool_ok += sum(held["confusion"][a][a] for a in MODES)
        pool_n += held["n"]
        pool_gen_ok += held["confusion"]["new_topic"]["new_topic"]
        pool_gen_n += sum(held["confusion"]["new_topic"].values())
        fold_reports.append({"fold": i, "policy": fmt_policy(pol), "train_acc": round(best[2]["acc"], 4),
                             "test_acc": round(held["acc"], 4),
                             "test_gen_new": round(held["genuinely_new_recall"], 4),
                             "test_n": held["n"]})
        print(f"## 2) 折 {i}: 折内选中 {fmt_policy(pol)} → 折外 acc={held['acc']:.3f} "
              f"new_R={held['genuinely_new_recall']:.3f} (n={held['n']})")
    cv_acc = pool_ok / (pool_n or 1)
    cv_gen = pool_gen_ok / (pool_gen_n or 1)
    print(f"  分层 {args.k} 折汇总（折内选参的诚实估计）：acc={cv_acc:.3f}  genuinely_new recall={cv_gen:.3f}")
    fixed = measure(cases, scores, TOPIC)
    print(f"  当前固定参数的同一份数据成绩    ：acc={fixed['acc']:.3f}  genuinely_new recall={fixed['genuinely_new_recall']:.3f}")

    # 固定参数（params.TOPIC）在每一折上的折外成绩：参数是在全量上选的，
    # 所以这不是无偏估计，只用来确认「没有哪一折特别塌」。
    fixed_pool_ok = fixed_pool_n = 0
    fixed_pool_gen_ok = fixed_pool_gen_n = 0
    fixed_fold_rows = []
    for i, test_cases in enumerate(folds):
        held = measure(test_cases, scores, TOPIC)
        fixed_pool_ok += sum(held["confusion"][a][a] for a in MODES)
        fixed_pool_n += held["n"]
        fixed_pool_gen_ok += held["confusion"]["new_topic"]["new_topic"]
        fixed_pool_gen_n += sum(held["confusion"]["new_topic"].values())
        fixed_fold_rows.append({"fold": i, "acc": round(held["acc"], 4),
                                "genuinely_new_recall": round(held["genuinely_new_recall"], 4), "n": held["n"]})
        print(f"     固定参数 折 {i}: acc={held['acc']:.3f} new_R={held['genuinely_new_recall']:.3f} (n={held['n']})")
    print(f"  固定参数分折池化：acc={fixed_pool_ok / (fixed_pool_n or 1):.3f}  "
          f"genuinely_new recall={fixed_pool_gen_ok / (fixed_pool_gen_n or 1):.3f}")
    print()

    # -- 3) 敏感度：最终参数附近 ±0.05 / ±0.10 ----------------------------
    print("## 3) 敏感度（当前 params.TOPIC 附近；每行只动一个参数）")
    print(f"  base: {fmt_policy(TOPIC)} → acc={fixed['acc']:.3f} new_R={fixed['genuinely_new_recall']:.3f}")
    sensitivity = []
    for name in ("new_topic_threshold", "incumbent_threshold", "switch_threshold", "switch_delta"):
        base = getattr(TOPIC, name)
        for step in (-0.10, -0.05, 0.05, 0.10):
            value = round(float(base) + step, 4)
            if value <= 0:
                continue
            pol = replace(TOPIC, **{name: value})
            m = measure(cases, scores, pol)
            sensitivity.append({"param": name, "value": value, "acc": round(m["acc"], 4),
                                "new_R": round(m["genuinely_new_recall"], 4),
                                "false_new": round(m["false_new_rate"], 4)})
            print(f"  {name}={value:<5} acc={m['acc']:.3f} new_R={m['genuinely_new_recall']:.3f} "
                  f"false_new={m['false_new_rate']:.3f}")
    for step in (-4, 4):
        value = int(TOPIC.min_new_topic_chars) + step
        if value < 0:
            continue
        pol = replace(TOPIC, min_new_topic_chars=value)
        m = measure(cases, scores, pol)
        sensitivity.append({"param": "min_new_topic_chars", "value": value, "acc": round(m["acc"], 4),
                            "new_R": round(m["genuinely_new_recall"], 4), "false_new": round(m["false_new_rate"], 4)})
        print(f"  min_new_topic_chars={value:<3} acc={m['acc']:.3f} new_R={m['genuinely_new_recall']:.3f} "
              f"false_new={m['false_new_rate']:.3f}")
    print()

    # -- 4) 最终配置的逐类混淆 ---------------------------------------------
    print("## 4) 当前配置的逐类结果与混淆")
    for cat, st in fixed["per_category"].items():
        print(f"  {cat:<24} {st['ok']}/{st['n']}")
    print(f"  混淆(exp->pred): {json.dumps(fixed['confusion'], ensure_ascii=False)}")
    print("  误判：")
    for w in fixed["wrong"]:
        print(f"    {w['id']:<10} {w['category']:<24} 期望={w['expected']:<9} 实际={w['predicted']}")

    payload = {
        "model": model,
        "n": len(cases),
        "gen_new_floor": GEN_NEW_FLOOR,
        "legacy": {k: v for k, v in legacy.items() if k != "wrong"},
        "current": {k: v for k, v in fixed.items()},
        "grid_top15": [{"policy": fmt_policy(pol), **{k: round(v, 4) for k, v in m.items()
                                                      if k in ("acc", "genuinely_new_recall", "in_topic_recall", "false_new_rate")}}
                        for _, pol, m in sorted(scored, key=lambda t: (-t[0], -t[2]["genuinely_new_recall"]))[:15]],
        "candidates": candidate_rows,
        "feasible_count": len(feasible),
        "cv": {"k": args.k, "pooled_acc": round(cv_acc, 4), "pooled_genuinely_new_recall": round(cv_gen, 4),
               "folds": fold_reports,
               "fixed_policy_pooled_acc": round(fixed_pool_ok / (fixed_pool_n or 1), 4),
               "fixed_policy_pooled_genuinely_new_recall": round(fixed_pool_gen_ok / (fixed_pool_gen_n or 1), 4),
               "fixed_policy_folds": fixed_fold_rows},
        "sensitivity": sensitivity,
    }
    if args.json:
        Path(args.json).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n结果 -> {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
