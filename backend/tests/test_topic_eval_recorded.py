# -*- coding: utf-8 -*-
"""recorded 后端的硬约束：缺快照必须**大声失败**，不许静默回落。

静默回落（退回 fake / BM25 / 零向量）会让评测看起来有数字、实际测的是另一个
几何体 —— 这正是这次把 12 条旧集里 4 条切换到 recorded 的原因。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent.eval import topic_eval

EVALS = Path(__file__).resolve().parents[1] / "evals"


def test_missing_snapshot_file_fails_loudly(tmp_path):
    with pytest.raises(RuntimeError) as excinfo:
        topic_eval.recorded_scores_for("embedding_continue", tmp_path / "nope.json")
    assert "快照不存在" in str(excinfo.value)


def test_missing_case_in_snapshot_fails_loudly(tmp_path):
    snapshot = tmp_path / "scores.json"
    snapshot.write_text(json.dumps({"model": "test", "cases": {}}), encoding="utf-8")
    with pytest.raises(RuntimeError) as excinfo:
        topic_eval.recorded_scores_for("embedding_continue", snapshot)
    assert "没有它" in str(excinfo.value)


def test_empty_scores_fail_loudly(tmp_path):
    snapshot = tmp_path / "scores.json"
    snapshot.write_text(
        json.dumps({"model": "test", "cases": {"c1": {"scores": {}, "current": None}}}), encoding="utf-8"
    )
    with pytest.raises(RuntimeError) as excinfo:
        topic_eval.recorded_scores_for("c1", snapshot)
    assert "为空" in str(excinfo.value)


def test_shipped_snapshot_covers_every_recorded_case_and_carries_model_identity():
    cases = topic_eval.load_cases(EVALS / "topic_prediction" / "cases.jsonl")
    recorded_ids = {c["id"] for c in cases if c.get("backend") == "recorded"}
    assert recorded_ids, "12 条旧集里应当仍有 recorded 后端的用例"
    snapshot = topic_eval.load_recorded_scores(EVALS / "topic_prediction" / "scores_onnx.json")
    assert snapshot["model"].startswith("onnx:")
    assert recorded_ids <= set(snapshot["cases"])
    for case_id in recorded_ids:
        entry = snapshot["cases"][case_id]
        assert entry["scores"], case_id
        assert set(entry["scores"]) == {
            t["id"] for t in next(c for c in cases if c["id"] == case_id)["topics"]
        }, case_id


def test_predict_mode_uses_the_snapshot_scores():
    """recorded 用例的判定必须与快照里的余弦一致（防止悄悄换回 fake）。"""
    snapshot = topic_eval.load_recorded_scores(EVALS / "topic_prediction" / "scores_onnx.json")
    scores = snapshot["cases"]["embedding_continue"]["scores"]
    assert scores["t_db"] > 0.5 > snapshot["cases"]["embedding_new"]["scores"]["t_db"]
    cases = {c["id"]: c for c in topic_eval.load_cases(EVALS / "topic_prediction" / "cases.jsonl")}
    assert topic_eval.predict_mode(cases["embedding_continue"]) == "in_topic"
    assert topic_eval.predict_mode(cases["embedding_new"]) == "new_topic"
