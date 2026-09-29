# -*- coding: utf-8 -*-
"""逐条留痕：记录一次运行的输入/标准答案/候选/预测/耗时，并从记录重算指标。

这一层存在的理由很直接：上一轮报告里的数字是手写进 md 的，接收方无法核验；
以后所有汇总数字都必须从 cases.jsonl 重算出来，人工只负责解读。
"""

from __future__ import annotations

import json
from pathlib import Path

from agent.eval.experiment_log import aggregate, load_cases, log_case, start_run


def _three_cases(tmp_path: Path):
    run = start_run(tmp_path, name="recall", config={"k": 2, "arm": "rules"})
    log_case(run, job="recall", case_id="1", query="q1", gold=["g1"],
             candidates=["g1", "x"], ranked=["g1", "x"], latency_ms=1.0)
    log_case(run, job="recall", case_id="2", query="q2", gold=["g2"],
             candidates=["x", "g2"], ranked=["x", "g2"], latency_ms=2.0)
    log_case(run, job="recall", case_id="3", query="q3", gold=["g3"],
             candidates=["x", "y"], ranked=["x", "y"], latency_ms=3.0)
    return run


def test_run_meta_records_version_and_config(tmp_path: Path):
    run = start_run(tmp_path, name="recall", config={"k": 2, "arm": "rules"})
    meta = json.loads(Path(run.meta_path).read_text(encoding="utf-8"))
    assert meta["run_id"] == run.run_id
    assert meta["config"] == {"k": 2, "arm": "rules"}
    assert meta["code_version"]
    assert meta["created_at"]


def test_cases_are_append_only_jsonl(tmp_path: Path):
    run = _three_cases(tmp_path)
    rows = load_cases(run)
    assert len(rows) == 3
    assert rows[0]["case_id"] == "1" and rows[0]["ranked"] == ["g1", "x"]
    log_case(run, job="recall", case_id="4", query="q4", gold=["g4"], ranked=["g4"])
    assert len(load_cases(run)) == 4


def test_aggregate_recomputes_metrics_from_records(tmp_path: Path):
    run = _three_cases(tmp_path)
    agg = aggregate(load_cases(run), job="recall", k=2)
    assert agg["n"] == 3
    assert agg["recall@1"] == 0.3333
    assert agg["recall@2"] == 0.6667
    assert agg["mrr"] == 0.5
    assert agg["precision@2"] == 0.3333
    assert agg["no_gold_in_top2_rate"] == 0.3333
    assert agg["latency_ms"]["mean_ms"] == 2.0


def test_aggregate_filters_by_job(tmp_path: Path):
    run = _three_cases(tmp_path)
    log_case(run, job="topic", case_id="t1", query="继续", gold=["t_a"], ranked=["t_a"])
    assert aggregate(load_cases(run), job="topic", k=2)["n"] == 1
    assert aggregate(load_cases(run), job="recall", k=2)["n"] == 3
