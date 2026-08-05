"""Iteration budget: token and step-count dual constraint.

Agreed design: native mode allows 5 iterations, text mode 3; token budget
is dynamic (context x 10%) in later milestones, fixed here for the skeleton.
"""

from __future__ import annotations

from dataclasses import dataclass

from agent.adapters.base import AdapterMode

DEFAULT_TOKEN_BUDGET = 16_000  # skeleton default; M9 makes it dynamic


def default_iterations(mode: AdapterMode) -> int:
    return 5 if mode == AdapterMode.NATIVE else 3


@dataclass
class IterationBudget:
    max_iterations: int
    token_budget: int = DEFAULT_TOKEN_BUDGET
    used_iterations: int = 0
    used_tokens: int = 0

    @property
    def exhausted(self) -> bool:
        return (
            self.used_iterations >= self.max_iterations
            or self.used_tokens >= self.token_budget
        )

    @property
    def iterations_left(self) -> int:
        return max(0, self.max_iterations - self.used_iterations)

    def consume_iteration(self) -> None:
        self.used_iterations += 1

    def consume_tokens(self, tokens: int) -> None:
        if tokens > 0:
            self.used_tokens += tokens