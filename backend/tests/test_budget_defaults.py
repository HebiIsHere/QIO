from __future__ import annotations

from agent.adapters.base import AdapterMode
from agent.core.budget import (
    DEFAULT_OUTPUT_TOKENS_PER_ITER,
    IterationBudget,
    default_iterations,
)


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
