"""离线评测集本身要能在 CI 里跑（阶段 4.4）。

注意这个测试**不是**准确率证明：用例是本次实现时写的，规模也小。
它守的是「改了策略之后，原来判断对的那些别退化」。
"""

from __future__ import annotations

from agent.eval.boundary_eval import evaluate, load_cases, summarize


def test_case_set_shape():
    cases = load_cases()
    assert len(cases) >= 12, "评测集至少要覆盖规格规则表里的主要情形"
    kinds = {c["split"] for c in cases}
    assert kinds == {"tune", "holdout"}, "规则样本与留出样本必须分开"
    assert all(c["expect"] in {"split", "continue", "uncertain"} for c in cases)


def test_policy_matches_all_labeled_cases():
    results = evaluate(load_cases())
    report = summarize(results)

    assert report["tune"]["誤切"] == 0
    assert report["tune"]["漏切"] == 0
    assert report["holdout"]["誤切"] == 0
    assert report["holdout"]["漏切"] == 0
    assert report["model_calls"] == 0, "确定性策略不该调用模型"
