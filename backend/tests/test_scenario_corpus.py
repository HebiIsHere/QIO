# -*- coding: utf-8 -*-
"""人工场景集：标注必须自洽，开发/测试集必须按场景划分。"""

from __future__ import annotations

import pytest

from agent.eval.scenario_corpus import build_scenarios, load_scenarios, select_split


def _loaded():
    return build_scenarios()


def test_every_query_points_at_existing_memory():
    loaded = _loaded()
    known = {m.id for m in loaded.corpus.memories}
    assert loaded.corpus.recall_cases
    for case in loaded.corpus.recall_cases:
        assert set(case.expected) <= known
        assert set(case.stale) <= known


def test_stale_memories_are_older_than_the_newest_expected():
    loaded = _loaded()
    by_id = {m.id: m for m in loaded.corpus.memories}
    checked = 0
    for case in loaded.corpus.recall_cases:
        if not case.stale:
            continue
        newest = min(by_id[m].age_days for m in case.expected)
        for stale_id in case.stale:
            assert by_id[stale_id].age_days > newest
        checked += 1
    assert checked >= 3, "至少要有几个带旧版本标注的用例"


def test_labels_come_from_scenarios_not_rotation():
    loaded = _loaded()
    kinds = {s["kind"] for s in load_scenarios()}
    assert {c.category for c in loaded.corpus.recall_cases} == kinds
    assert len(kinds) >= 6, "场景类型要覆盖事实修订/同话题干扰/跨话题/同名实体/近期噪声等"


def test_every_query_declares_why_it_belongs():
    for scenario in load_scenarios():
        for query in scenario["queries"]:
            assert query.get("why"), f"{query['id']} 缺少 why"


def test_split_is_by_scenario_not_by_query():
    loaded = _loaded()
    dev = select_split(loaded, "dev")
    test = select_split(loaded, "test")
    assert dev.corpus.recall_cases and test.corpus.recall_cases
    assert not ({c.id for c in dev.corpus.recall_cases} & {c.id for c in test.corpus.recall_cases})
    for scenario in load_scenarios():
        ids = [q["id"] for q in scenario["queries"]]
        assert {loaded.splits[i] for i in ids} == {scenario["split"]}


def test_tier_matches_overlap_definition():
    loaded = _loaded()
    for case in loaded.corpus.recall_cases:
        assert case.keyword_answerable == (case.overlap > 0)
        if case.tier == "literal":
            assert case.overlap >= 0.4
        elif case.tier == "partial":
            assert 0 < case.overlap < 0.4
        else:
            assert case.overlap == 0
