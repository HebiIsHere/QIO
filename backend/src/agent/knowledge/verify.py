"""Tiered verification.

- High impact (user_profile / agent_self / goal): explicit user confirmation;
- Low impact (general_fact / tool_experience): automatic verification,
  optionally through a cross-model validator.

v1 cross-validation: if a validator callable is provided it must return
True/False; without one, low-impact entries are accepted with a baseline
confidence.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Callable

from agent.knowledge.lifecycle import (
    HIGH_IMPACT_CATEGORIES,
    KnowledgeItem,
    KnowledgeService,
)

Validator = Callable[[KnowledgeItem], bool]


@dataclass(frozen=True)
class VerifyResult:
    knowledge_id: str
    accepted: bool
    reason: str


class VerificationService:
    def __init__(self, conn: sqlite3.Connection, knowledge: KnowledgeService) -> None:
        self.conn = conn
        self.knowledge = knowledge

    def needs_user_confirmation(self, item: KnowledgeItem) -> bool:
        return item.category in HIGH_IMPACT_CATEGORIES

    def review(
        self,
        item: KnowledgeItem,
        *,
        cross_validator: Validator | None = None,
        verified_by: str = "system",
    ) -> VerifyResult:
        """Run the tiered review for a pending item."""
        if self.needs_user_confirmation(item):
            if verified_by != "user":
                return VerifyResult(
                    item.id,
                    False,
                    f"high-impact category '{item.category}' requires user confirmation",
                )
            self.knowledge.verify(item.id, verified_by="user")
            return VerifyResult(item.id, True, "confirmed by user")
        # low impact
        if cross_validator is not None:
            try:
                accepted = cross_validator(item)
            except Exception as exc:
                return VerifyResult(item.id, False, f"cross-validation error: {exc}")
            if not accepted:
                return VerifyResult(item.id, False, "cross-validation rejected")
        self.knowledge.verify(item.id, verified_by=verified_by)
        return VerifyResult(item.id, True, "auto-verified (low impact)")