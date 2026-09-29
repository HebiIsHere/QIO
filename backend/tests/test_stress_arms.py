# -*- coding: utf-8 -*-
"""三条臂的接线：走的是生产入口，不是另写一套算法。

规则臂的用例完全离线；本地臂的用例要求本机有模型文件，没有就跳过。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent.eval.embedding_backend import resolve_model_dir
from agent.eval.ranking_eval import literal_answerable_hits
from agent.eval.stress_arms import LocalEmbeddingArm, RulesArm
from agent.eval.stress_corpus import generate

HAS_MODEL = (resolve_model_dir() / "model.onnx").exists()


@pytest.fixture(scope="module")
def corpus():
    return generate(seed=11, n_memories=1400, n_topics=20, n_queries=24)


def test_rules_arm_recall_returns_one_ranking_per_case(corpus):
    arm = RulesArm(corpus)
    ranked, latencies = arm.recall(corpus.recall_cases, k=5)
    assert len(ranked) == len(corpus.recall_cases)
    assert len(latencies) == len(ranked)
    known = {m.id for m in corpus.memories}
    for ids in ranked:
        assert isinstance(ids, list) and set(ids) <= known
    literal = [
        (ids, case)
        for ids, case in zip(ranked, corpus.recall_cases)
        if case.keyword_answerable and case.category in ("fact_update", "same_topic")
    ]
    hits = sum(1 for ids, case in literal if set(ids) & set(case.expected))
    # 结构断言到此为止；「命中多少」改为新旧两臂在同一批用例上的行为对照。
    #
    # 旧双层排序（候选阶段带时效奖励 + 话题亲和奖励）实测 7/8，新纯相关性 6/8：
    # 差异在 r_00006（fact_update「最终决定」）—— 那条最新的决定能进底层召回前 36，
    # 但纯相关性下被更旧的近义记忆挤出 12 条候选池。这是显式关闭时效 / 话题奖励
    # 的代价，不是回归 bug；详见 docs/status.md 与 `python -m agent.eval.ranking_eval`。
    measured = literal_answerable_hits(corpus, k=5)
    assert measured["total"] == len(literal)
    assert hits == measured["relevance_hits"]  # 生产臂 = 纯相关性
    assert measured["legacy_hits"] >= 7, measured
    assert measured["relevance_hits"] >= 6, measured


def test_rules_arm_topic_rows_have_valid_modes(corpus):
    rows, latencies = RulesArm(corpus).topic(corpus.topic_cases)
    assert len(rows) == len(corpus.topic_cases)
    assert all(r["predicted"] in ("in_topic", "switch", "new_topic") for r in rows)
    assert all(r["backend"] == "rules" for r in rows)


def test_rules_arm_entity_matches_name_and_alias(corpus):
    rows, _ = RulesArm(corpus).entity(corpus.entity_cases)
    hits = sum(1 for r in rows if r["predicted"] == r["expected"])
    assert hits == len(rows), "查询里直接写了实体名，规则臂应当全中"


def test_rules_arm_tool_puts_expected_tool_in_candidates(corpus):
    rows, _ = RulesArm(corpus).tool(corpus.tool_cases)
    hits = sum(1 for r in rows if r["expected"] in r["ranked"][:5])
    assert hits >= len(rows) * 0.8, "查询就是工具描述原文，至少应当进候选"


def test_rules_arm_dedup_flags_exact_name_and_ignores_novel(corpus):
    rows, _ = RulesArm(corpus).dedup(corpus.dedup_cases)
    by_expect = {True: [], False: []}
    for r in rows:
        by_expect[r["expected"]].append(r["predicted"])
    assert all(by_expect[True]), "同名新话题必须被拦下"
    assert not any(by_expect[False]), "全新话题不能误拦"


@pytest.mark.skipif(not HAS_MODEL, reason="本机没有内置模型文件")
def test_local_arm_uses_onnx_path(corpus):
    """只断言接线：本地臂必须真的走向量路径，并且返回结构完整的结果。

    「向量在语义题上能拿多少分」是实验要测的量，不是测试该断言的假设 ——
    把它写成 >= 某个比例，等于用测试替实验下结论。
    """
    arm = LocalEmbeddingArm(corpus)
    ranked, _ = arm.recall(corpus.recall_cases, k=5)
    assert len(ranked) == len(corpus.recall_cases)
    rows, _ = arm.topic(corpus.topic_cases)
    assert all(r["backend"] == "onnx" for r in rows)
    known = {m.id for m in corpus.memories}
    for ids in ranked:
        assert set(ids) <= known
    entity_rows, _ = arm.entity(corpus.entity_cases)
    assert len(entity_rows) == len(corpus.entity_cases)
    dedup_rows, _ = arm.dedup(corpus.dedup_cases)
    assert len(dedup_rows) == len(corpus.dedup_cases)
