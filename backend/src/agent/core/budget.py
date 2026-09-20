"""Iteration budget: step-count guard + **optional** output-token cap.

Main loop defaults: native 128 iterations, text 64.

关于整轮输出 token 上限（产品决定，2026-09-20）：
默认**不再**用「整轮累计输出 token」当强制停止条件 —— 短回答由模型自己收尾，
长回答可以继续，复杂 Agent 工作不会因为一个固定数字被拦腰截断。
`token_budget = 0` 表示不限；用户显式配置的预算仍然严格执行。

token 统计本身不因此取消：输入 / 输出 / 总量照常累计，供 UI、Trace、
成本估算与性能分析使用（见 `ModelUsage`）。

`DEFAULT_OUTPUT_TOKENS_PER_ITER` 仍然保留，用于「继续」时追加一批预算的估值。
"""

from __future__ import annotations

from dataclasses import dataclass

from agent.adapters.base import AdapterMode

DEFAULT_OUTPUT_TOKENS_PER_ITER = 400

# 0 = 不限（默认）。历史名保留，避免破坏引用方。
DEFAULT_TOKEN_BUDGET = 0


def default_iterations(mode: AdapterMode) -> int:
    return 128 if mode == AdapterMode.NATIVE else 64


@dataclass
class IterationBudget:
    max_iterations: int
    token_budget: int = DEFAULT_TOKEN_BUDGET  # 0 = 不设输出 token 上限（默认）
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
        """「继续」时追加一批预算。不限（0）时只追加迭代，不会凭空造出上限。"""
        self.max_iterations += extra_iterations
        if self.token_budget > 0:
            self.token_budget += extra_tokens
