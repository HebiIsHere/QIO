# -*- coding: utf-8 -*-
"""冻结机制：改动场景文件后必须被检出，逼着人显式重新冻结。"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from agent.eval.scenario_corpus import DEFAULT_PATH, freeze, frozen_status


def _copy(tmp_path: Path) -> Path:
    target = tmp_path / "scenarios.jsonl"
    shutil.copy2(DEFAULT_PATH, target)
    return target


def test_freeze_then_status_is_ok(tmp_path: Path):
    source = _copy(tmp_path)
    frozen = tmp_path / "FROZEN.json"
    payload = freeze(source, frozen)
    assert payload["counts"]["cases"] > 0
    assert payload["test_cases"]
    status = frozen_status(source, frozen)
    assert status["status"] == "ok"
    assert status["counts"] == payload["counts"]


def test_editing_scenarios_after_freeze_is_detected(tmp_path: Path):
    source = _copy(tmp_path)
    frozen = tmp_path / "FROZEN.json"
    freeze(source, frozen)
    with source.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "scenario_id": "s99",
                    "kind": "fact_revision",
                    "split": "test",
                    "topic": "新增话题",
                    "note": "冻结后追加，应当被检出",
                    "memories": [{"id": "s99_m1", "text": "新记忆", "age_days": 1, "kind": "decision"}],
                    "queries": [
                        {
                            "id": "s99_q1",
                            "query": "新问题？",
                            "expected": ["s99_m1"],
                            "stale": [],
                            "why": "测试用",
                        }
                    ],
                },
                ensure_ascii=False,
            )
            + "\n"
        )
    status = frozen_status(source, frozen)
    assert status["status"] == "changed"
    assert "已被修改" in status["detail"]


def test_missing_freeze_reports_missing(tmp_path: Path):
    source = _copy(tmp_path)
    status = frozen_status(source, tmp_path / "nope.json")
    assert status["status"] == "missing"
