# -*- coding: utf-8 -*-
"""跑批器：结构、分层、以及"缺条件就报错而不是悄悄降级"。"""

from __future__ import annotations

import json

from agent.eval.stress_corpus import generate
from agent.eval.stress_run import run_all
from agent.eval.stress_arms import RulesArm


def _small():
    return generate(seed=5, n_memories=700, n_topics=10, n_queries=30)


def test_run_all_reports_every_job_for_every_arm():
    corpus = _small()
    payload = run_all(corpus, [RulesArm(corpus)], k=5)
    assert set(payload) == {"rules"}
    arm = payload["rules"]
    assert set(arm) >= {"recall", "topic", "entity", "tool", "dedup", "wall_ms"}
    assert 0.0 <= arm["recall"]["all"]["recall@1"] <= 1.0
    assert arm["recall"]["by_tier"].keys() == {"literal", "partial", "disjoint"}
    assert arm["topic"]["accuracy"] >= 0.0
    assert "confusions" in arm["dedup"]
    assert arm["recall"]["latency_ms"]["p50_ms"] >= 0.0


def test_run_all_is_json_serialisable():
    corpus = _small()
    payload = run_all(corpus, [RulesArm(corpus)], k=3)
    text = json.dumps(payload, ensure_ascii=False)
    assert "recall@1" in text
