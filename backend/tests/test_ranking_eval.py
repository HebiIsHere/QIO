# -*- coding: utf-8 -*-
"""新旧排序对照：口径 A（固定候选成员）与口径 B（整链路）都从逐条记录重算。"""

from __future__ import annotations

from agent.eval.ranking_eval import compare, literal_answerable_hits
from agent.eval.stress_corpus import Memory, RecallCase, StressCorpus, Topic


def _synthetic_corpus() -> StressCorpus:
    topics = [Topic(id="t1", title="日志系统", keywords=(), query_title="日志系统")]
    memories = [
        Memory(
            id=f"noise_{i}",
            text="日志 迁移 方案 记录 系统 切换 结果 做法",
            topic_id="t1",
            kind="chatter",
            age_days=1.0,
        )
        for i in range(15)
    ]
    # 正确答案：词面更弱、而且很旧（拿不到任何时效奖励）
    memories.append(
        Memory(id="gold", text="日志迁移", topic_id="t1", kind="decision", age_days=300.0)
    )
    case = RecallCase(
        id="q1",
        category="fact_update",
        query="日志 迁移 方案 记录 系统 切换 结果 做法",
        expected=("gold",),
        stale=(),
        keyword_answerable=True,
        overlap=0.5,
        tier="literal",
    )
    return StressCorpus(seed=0, topics=topics, memories=memories, recall_cases=[case])


def test_compare_reports_both_comparison_modes():
    result = compare(_synthetic_corpus(), recall_top=16, pool_size=12, limit=5)

    summary = result["summary"]
    assert set(summary["by_arm"]) == {"legacy", "relevance", "relevance_fixed_pool"}
    assert summary["n"] == 1
    record = result["records"][0]
    assert record["config"]["ranking"]["strategy"] == "relevance"
    assert record["config"]["recall_top"] == 16
    # 口径 A：固定同一批候选成员 → 两臂候选池完全相同，差异只可能来自排序
    assert (
        record["relevance_fixed_pool"]["candidate_pool"]
        == record["legacy"]["candidate_pool"]
    )


def test_intermediate_filter_loss_is_reported_with_case_id():
    result = compare(_synthetic_corpus(), recall_top=16, pool_size=12, limit=5)

    lost = result["summary"]["lost_by_intermediate_filter"]
    # 正确答案在底层召回上限（16）之内，却被旧中间筛选（奖励截 12）切掉了
    assert lost["count"] == 1
    assert lost["cases"][0]["case_id"] == "q1"
    assert lost["cases"][0]["cut"] == ["gold"]


def test_compare_is_deterministic_at_a_fixed_time():
    first = compare(_synthetic_corpus(), recall_top=16, pool_size=12, limit=5)["summary"]
    second = compare(_synthetic_corpus(), recall_top=16, pool_size=12, limit=5)["summary"]

    assert first == second


def test_literal_answerable_hits_returns_recomputed_counts():
    measured = literal_answerable_hits(_synthetic_corpus(), k=5)

    assert measured["total"] == 1
    assert measured["legacy_hits"] == 0  # 旧中间筛选把 gold 切掉 → 两臂都搜不到
    assert measured["relevance_hits"] == 0
    assert measured["changed"] == []
