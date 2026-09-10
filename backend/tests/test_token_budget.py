from __future__ import annotations

from agent.services.injection import BudgetConfig, Candidate, InjectionBudget, PlannedItem
from agent.services.token_budget import (
    CONSERVATIVE_CONTEXT_WINDOW,
    DEFAULT_COMPLETION_RESERVE,
    TokenBudgetPlanner,
)


def test_planner_subtracts_all_reserves():
    p = TokenBudgetPlanner()
    b = p.plan(
        context_window=100_000,
        system_prompt_tokens=2_000,
        adapter_overhead_tokens=500,
        tool_definitions_tokens=3_000,
        user_query_tokens=200,
        completion_reserve=4_096,
        injection_ratio=0.25,
    )
    expected_usable = 100_000 - (2_000 + 500 + 3_000 + 200 + 4_096)
    assert b.usable_for_injection == expected_usable
    assert b.injection_hard_cap == int(expected_usable * 0.25)
    assert b.completion_reserve == 4_096


def test_planner_never_negative():
    p = TokenBudgetPlanner()
    b = p.plan(context_window=1_000, system_prompt_tokens=5_000, completion_reserve=4_096)
    assert b.usable_for_injection == 0
    assert b.injection_hard_cap == 0


def test_planner_conservative_fallback_for_unknown_model():
    p = TokenBudgetPlanner()
    b = p.plan(context_window=0)  # 未知模型
    assert b.context_window == CONSERVATIVE_CONTEXT_WINDOW
    assert b.completion_reserve == DEFAULT_COMPLETION_RESERVE


def test_hard_cap_is_hard_for_reserved_items():
    # reserved item 远超 hard cap → 必须被截断，而不是完整 append
    cfg = BudgetConfig(hard_cap_override=100)
    budget = InjectionBudget(cfg)
    big = PlannedItem(source="memory", surface="topic_short", item_id="f1", text="字" * 5000, tokens=2500)
    plan = budget.plan([], reserved=[big])
    assert plan.total_tokens <= plan.hard_cap == 100
    assert plan.truncated is True


def test_ranked_item_exceeding_remaining_is_skipped():
    cfg = BudgetConfig(hard_cap_override=50)
    budget = InjectionBudget(cfg)
    cand = Candidate(source="memory", surface="memory", item_id="m1", text="字" * 500, score=0.9)
    plan = budget.plan([cand])
    assert plan.total_tokens <= 50
    assert plan.truncated is True


def test_boundary_exact_fit_and_zero_and_minus_one():
    # 精确等于 remaining
    from agent.memory.index import estimate_tokens

    text = "abcde"
    tokens = estimate_tokens(text)
    cfg = BudgetConfig(hard_cap_override=tokens)
    plan = InjectionBudget(cfg).plan(
        [], reserved=[PlannedItem(source="memory", surface="topic_short", item_id="x", text=text, tokens=tokens)]
    )
    assert plan.total_tokens == tokens  # 刚好放下

    # remaining - 1 → 截断
    cfg2 = BudgetConfig(hard_cap_override=tokens - 1)
    plan2 = InjectionBudget(cfg2).plan(
        [], reserved=[PlannedItem(source="memory", surface="topic_short", item_id="x", text=text, tokens=tokens)]
    )
    assert plan2.total_tokens <= tokens - 1

    # 0 token 上限 → 全部丢弃
    cfg3 = BudgetConfig(hard_cap_override=0)
    plan3 = InjectionBudget(cfg3).plan(
        [], reserved=[PlannedItem(source="memory", surface="topic_short", item_id="x", text=text, tokens=tokens)]
    )
    assert plan3.total_tokens == 0
