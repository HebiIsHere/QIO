"""DecayPolicy: recency weighting that depends on the kind of information.

Not everything decays at the same rate. An authoritative, still-active fact
must not lose to a fresh-but-irrelevant chit-chat just because it is older.

Kinds (conceptual; extend as reliable metadata appears):
    ephemeral     — ordinary conversational memory          (fast decay)
    task_state    — progress/state of an ongoing task       (medium)
    project_decision — a decision made for a project        (slow)
    preference    — a user preference                       (slow)
    authoritative — verified/active knowledge               (no decay)

This module only encodes the policy. Callers supply the `kind`; when the
underlying store has no reliable category (current memory_index), callers pass
`ephemeral`, which reproduces the previous single half-life behaviour exactly.
`authoritative` never decays — that is how Knowledge avoids losing to episodic
memory in freshness competition.
"""

from __future__ import annotations

import math

EPHEMERAL = "ephemeral"
TASK_STATE = "task_state"
PROJECT_DECISION = "project_decision"
PREFERENCE = "preference"
AUTHORITATIVE = "authoritative"

# 单位：天。None = 不衰减（权威信息）。
DEFAULT_HALF_LIVES: dict[str, float | None] = {
    EPHEMERAL: 30.0,
    TASK_STATE: 90.0,
    PROJECT_DECISION: 365.0,
    PREFERENCE: 365.0,
    AUTHORITATIVE: None,
}

# knowledge.category → decay kind（用于未来 category-aware 接入）
KNOWLEDGE_CATEGORY_TO_KIND: dict[str, str] = {
    "general_fact": AUTHORITATIVE,
    "tool_experience": PROJECT_DECISION,
    "user_profile": PREFERENCE,
    "goal": PROJECT_DECISION,
    "agent_self": AUTHORITATIVE,
}


class DecayPolicy:
    def __init__(self, half_lives: dict[str, float | None] | None = None) -> None:
        self.half_lives = {**DEFAULT_HALF_LIVES, **(half_lives or {})}

    def half_life(self, kind: str) -> float | None:
        return self.half_lives.get(kind, self.half_lives[EPHEMERAL])

    def weight(self, age_days: float, kind: str = EPHEMERAL) -> float:
        """Freshness weight in (0, 1]. `authoritative` → always 1.0."""
        hl = self.half_life(kind)
        if hl is None:
            return 1.0
        if not math.isfinite(age_days):
            return 0.0
        if age_days <= 0:
            return 1.0
        return math.exp(-age_days / hl)

    def kind_for_knowledge_category(self, category: str | None) -> str:
        return KNOWLEDGE_CATEGORY_TO_KIND.get(category or "", AUTHORITATIVE)
