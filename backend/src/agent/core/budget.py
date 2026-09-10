"""Iteration budget: step-count and output-token dual constraint.

Main loop defaults: native 128 iterations, text 64. The token gate counts
model OUTPUT tokens (completion_tokens), so its budget is estimated as
`max_iterations x DEFAULT_OUTPUT_TOKENS_PER_ITER`.
"""

from __future__ import annotations

from dataclasses import dataclass

from agent.adapters.base import AdapterMode

DEFAULT_OUTPUT_TOKENS_PER_ITER = 400

# 兼容旧引用：默认总输出 token 预算 = native 迭代数 x 每轮输出估值。
DEFAULT_TOKEN_BUDGET = 128 * DEFAULT_OUTPUT_TOKENS_PER_ITER


def default_iterations(mode: AdapterMode) -> int:
    return 128 if mode == AdapterMode.NATIVE else 64


@dataclass
class IterationBudget:
    max_iterations: int
    token_budget: int = DEFAULT_TOKEN_BUDGET  # 0 = 不设输出 token 上限
    used_iterations: int = 0
    used_tokens: int = 0  # 累计输出 token（completion_tokens）

    @property
    def exhausted(self) -> bool:
        if self.used_iterations >= self.max_iterations:
            return True
        return self.token_budget > 0 and self.used_tokens >= self.token_budget

    @property
    def iterations_left(self) -> int:
        return max(0, self.max_iterations - self.used_iterations)

    def consume_iteration(self) -> None:
        self.used_iterations += 1

    def consume_output_tokens(self, n: int) -> None:
        if n > 0:
            self.used_tokens += n

    # 向后兼容别名
    def consume_tokens(self, tokens: int) -> None:
        self.consume_output_tokens(tokens)

    def raise_limits(self, extra_iterations: int, extra_tokens: int) -> None:
        """「继续」时追加一批预算。"""
        self.max_iterations += extra_iterations
        if self.token_budget > 0:
            self.token_budget += extra_tokens
