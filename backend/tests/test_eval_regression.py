from __future__ import annotations

import json
from pathlib import Path

from agent.eval.retrieval_eval import evaluate as eval_retrieval
from agent.eval.retrieval_eval import load_cases as load_retrieval
from agent.eval.topic_eval import evaluate as eval_topic
from agent.eval.topic_eval import load_cases as load_topic
from agent.eval.anchor_eval import evaluate as eval_anchor
from agent.eval.anchor_eval import load_cases as load_anchor
from agent.eval.anchor_eval import public_metrics as anchor_public

EVALS = Path(__file__).resolve().parents[1] / "evals"
BASELINE = json.loads((EVALS / "baseline.json").read_text(encoding="utf-8"))

HIGHER_BETTER = {
    "topic_prediction": [
        "in_topic_accuracy",
        "switch_accuracy",
        "new_topic_precision",
        "new_topic_recall",
    ],
    "retrieval": ["recall@1", "recall@5", "mrr"],
}
LOWER_BETTER = {
    "topic_prediction": ["false_new_rate", "false_switch_rate"],
    "retrieval": ["wrong_memory_injection_rate", "stale_knowledge_injection_rate"],
}


def _run() -> dict:
    topic = eval_topic(load_topic(EVALS / "topic_prediction" / "cases.jsonl"))
    retrieval = eval_retrieval(load_retrieval(EVALS / "retrieval" / "cases.jsonl"))
    return {"topic_prediction": topic, "retrieval": retrieval}


def test_evals_do_not_regress_below_baseline():
    metrics = _run()
    for suite, keys in HIGHER_BETTER.items():
        for k in keys:
            assert metrics[suite][k] >= BASELINE[suite][k], f"{suite}.{k} regressed"
    for suite, keys in LOWER_BETTER.items():
        for k in keys:
            assert metrics[suite][k] <= BASELINE[suite][k], f"{suite}.{k} regressed"


def test_fact_update_prefers_current_decision():
    cases = {c["id"]: c for c in load_retrieval(EVALS / "retrieval" / "cases.jsonl")}
    from agent.eval.retrieval_eval import rank_case

    ranked = rank_case(cases["fact_update_db"])
    assert ranked[0] == "sqlite_decision"  # 当前决定排在旧状态之前


def test_topic_eval_is_deterministic():
    a = _run()["topic_prediction"]
    b = _run()["topic_prediction"]
    assert {k: v for k, v in a.items() if k != "rows"} == {
        k: v for k, v in b.items() if k != "rows"
    }


def test_anchor_continuation_does_not_regress_baseline():
    """Anchor Continuation：生产方案（Focus + 语义检索 + 身份去重）不得退化。"""
    metrics = anchor_public(eval_anchor(load_anchor(EVALS / "anchor_continuation" / "cases.jsonl")))
    base = metrics["baseline"]
    saved = BASELINE["anchor_continuation"]["baseline"]
    assert base["recall@5"] >= saved["recall@5"]
    assert base["mrr"] >= saved["mrr"]
    assert base["wrong_memory_injection_rate"] <= saved["wrong_memory_injection_rate"]
    assert base["duplicate_injection_rate"] == 0.0
    # 距离偏置的结论（实现与否）必须与 baseline 里记录的一致
    assert metrics["decision"]["verdict"] == BASELINE["anchor_continuation"]["decision"]["verdict"]
