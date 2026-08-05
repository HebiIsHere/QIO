"""Knowledge domain: lifecycle state machine, verification, injection."""

from agent.knowledge.inject import InjectionSource
from agent.knowledge.lifecycle import (
    CATEGORIES,
    HIGH_IMPACT_CATEGORIES,
    LOW_IMPACT_CATEGORIES,
    KnowledgeItem,
    KnowledgeService,
    KnowledgeState,
    impact_of,
)
from agent.knowledge.verify import VerificationService

__all__ = [
    "KnowledgeItem",
    "KnowledgeService",
    "KnowledgeState",
    "VerificationService",
    "InjectionSource",
    "CATEGORIES",
    "HIGH_IMPACT_CATEGORIES",
    "LOW_IMPACT_CATEGORIES",
    "impact_of",
]