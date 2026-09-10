"""TokenBudgetPlanner: explicit budget model for contextual injection.

Replaces the naive `context_window x fixed_ratio` hard budget with a
breakdown that reserves everything the model request must also carry:

    context_window
      - system prompt
      - adapter / protocol overhead
      - tool definitions
      - current user query
      - completion reserve   (explicit; never given to injection)
    = usable_for_injection

Only then is the injection hard cap derived (optionally a fraction of the
usable space). Unknown models get conservative defaults; nothing here may
return a negative cap.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

# 未知模型的保守兜底（宁可少注入，不可超窗）
CONSERVATIVE_CONTEXT_WINDOW = 32_000
DEFAULT_COMPLETION_RESERVE = 4_096
DEFAULT_INJECTION_RATIO = 0.25


@dataclass
class BudgetBreakdown:
    context_window: int
    system_prompt: int
    adapter_overhead: int
    tool_definitions: int
    user_query: int
    completion_reserve: int
    usable_for_injection: int
    injection_hard_cap: int

    def as_dict(self) -> dict:
        return asdict(self)


class TokenBudgetPlanner:
    def __init__(
        self,
        *,
        default_context_window: int = CONSERVATIVE_CONTEXT_WINDOW,
        default_completion_reserve: int = DEFAULT_COMPLETION_RESERVE,
        injection_ratio: float = DEFAULT_INJECTION_RATIO,
    ) -> None:
        self.default_context_window = default_context_window
        self.default_completion_reserve = default_completion_reserve
        self.injection_ratio = injection_ratio

    def plan(
        self,
        *,
        context_window: int | None = None,
        system_prompt_tokens: int = 0,
        adapter_overhead_tokens: int = 0,
        tool_definitions_tokens: int = 0,
        user_query_tokens: int = 0,
        completion_reserve: int | None = None,
        injection_ratio: float | None = None,
    ) -> BudgetBreakdown:
        window = int(context_window or 0) or self.default_context_window
        reserve = (
            self.default_completion_reserve
            if completion_reserve is None
            else int(completion_reserve)
        )
        reserve = max(0, reserve)
        ratio = self.injection_ratio if injection_ratio is None else injection_ratio
        ratio = min(max(ratio, 0.0), 1.0)

        consumed = (
            max(0, int(system_prompt_tokens))
            + max(0, int(adapter_overhead_tokens))
            + max(0, int(tool_definitions_tokens))
            + max(0, int(user_query_tokens))
            + reserve
        )
        usable = max(0, window - consumed)
        hard_cap = max(0, int(usable * ratio))
        return BudgetBreakdown(
            context_window=window,
            system_prompt=max(0, int(system_prompt_tokens)),
            adapter_overhead=max(0, int(adapter_overhead_tokens)),
            tool_definitions=max(0, int(tool_definitions_tokens)),
            user_query=max(0, int(user_query_tokens)),
            completion_reserve=reserve,
            usable_for_injection=usable,
            injection_hard_cap=hard_cap,
        )
