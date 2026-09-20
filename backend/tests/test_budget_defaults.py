from __future__ import annotations

from agent.adapters.base import AdapterMode
from agent.core.budget import (
    DEFAULT_OUTPUT_TOKENS_PER_ITER,
    DEFAULT_TOKEN_BUDGET,
    IterationBudget,
    default_iterations,
)
from agent.core.loop import AgentLoop
from agent.api.bus import EventBus
from agent.tools.registry import ToolRegistry


def test_default_iterations_native_and_text():
    assert default_iterations(AdapterMode.NATIVE) == 128
    assert default_iterations(AdapterMode.TEXT) == 64


def test_output_tokens_per_iter_value():
    assert DEFAULT_OUTPUT_TOKENS_PER_ITER == 400


def test_exhausted_by_output_tokens():
    b = IterationBudget(max_iterations=128, token_budget=800)
    b.consume_output_tokens(400)
    assert not b.exhausted
    b.consume_output_tokens(400)
    assert b.exhausted


def test_raise_limits_extends_budget():
    b = IterationBudget(max_iterations=8, token_budget=3200)
    b.raise_limits(32, 12800)
    assert b.max_iterations == 40
    assert b.token_budget == 16000


def test_zero_token_budget_means_no_token_gate():
    b = IterationBudget(max_iterations=3, token_budget=0)
    b.consume_output_tokens(10_000)
    assert not b.exhausted  # 只受迭代数约束


def test_exhausted_by_iterations():
    b = IterationBudget(max_iterations=2, token_budget=0)
    b.consume_iteration()
    assert not b.exhausted
    b.consume_iteration()
    assert b.exhausted


def test_consume_tokens_alias():
    b = IterationBudget(max_iterations=10, token_budget=100)
    b.consume_tokens(50)
    assert b.used_tokens == 50


# ---------------------------------------------------------------------------
# 默认不再用「整轮累计输出 token」当强制停止条件
# ---------------------------------------------------------------------------


def test_default_token_budget_is_unlimited():
    """默认必须是「不限」：让模型按任务完成情况收尾，而不是按累计字数被砍断。"""
    assert DEFAULT_TOKEN_BUDGET == 0


def test_default_iteration_budget_has_no_token_gate():
    b = IterationBudget(max_iterations=128)
    assert b.token_budget == 0
    b.consume_output_tokens(1_000_000)
    assert not b.exhausted


class _NoToolAdapter:
    mode = "native"
    model = "test-model"

    async def complete(self, messages, tools, **kwargs):
        from agent.adapters.base import ChatMessage, Completion

        return Completion(message=ChatMessage(role="assistant", content="ok"))


def test_agent_loop_default_has_no_turn_output_cap():
    """不传 token_budget 时，主循环不得自带整轮输出上限。"""
    loop = AgentLoop(_NoToolAdapter(), ToolRegistry(), EventBus())
    assert loop.budget.token_budget == 0


def test_agent_loop_honors_explicit_user_budget():
    """用户显式配置的预算仍然严格执行。"""
    loop = AgentLoop(_NoToolAdapter(), ToolRegistry(), EventBus(), token_budget=1234)
    assert loop.budget.token_budget == 1234


def test_raise_limits_keeps_unlimited_when_no_token_budget():
    b = IterationBudget(max_iterations=8, token_budget=0)
    b.raise_limits(32, 12800)
    assert b.max_iterations == 40
    assert b.token_budget == 0
    b.consume_output_tokens(999_999)
    assert not b.exhausted
