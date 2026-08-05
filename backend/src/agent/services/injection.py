"""Injection budget and assembly.

Agreed design:
- knowledge domain: 2-4K token budget (relaxed), filled first;
- memory domain: relevance-threshold driven, uses the remaining budget;
- hard cap: context_window x 10% (dynamic);
- frontend memory-strength slider maps to the knowledge budget point.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from agent.knowledge.inject import InjectionSource
from agent.memory.index import estimate_tokens
from agent.services.retrieval import RetrievalHit, Retriever


@dataclass
class BudgetConfig:
    context_window: int = 1_000_000
    hard_cap_ratio: float = 0.1
    knowledge_min_tokens: int = 2000
    knowledge_max_tokens: int = 4000
    memory_strength: float = 0.5  # frontend slider 0..1

    @property
    def hard_cap(self) -> int:
        return int(self.context_window * self.hard_cap_ratio)

    def knowledge_budget(self) -> int:
        """Linear interpolation between min and max by memory strength."""
        return int(
            self.knowledge_min_tokens
            + (self.knowledge_max_tokens - self.knowledge_min_tokens)
            * max(0.0, min(1.0, self.memory_strength))
        )


@dataclass
class PlannedItem:
    source: str  # knowledge | memory
    item_id: str
    text: str
    tokens: int
    score: float = 0.0


@dataclass
class InjectionPlan:
    knowledge: list[PlannedItem] = field(default_factory=list)
    memory: list[PlannedItem] = field(default_factory=list)
    total_tokens: int = 0
    hard_cap: int = 0
    truncated: bool = False

    @property
    def all_items(self) -> list[PlannedItem]:
        return self.knowledge + self.memory


class InjectionBudget:
    def __init__(self, config: BudgetConfig) -> None:
        self.config = config

    def plan(
        self,
        knowledge_items: list[dict],
        memory_hits: list[RetrievalHit],
        *,
        min_relevance: float = 0.05,
    ) -> InjectionPlan:
        plan = InjectionPlan(hard_cap=self.config.hard_cap)
        remaining = self.config.hard_cap

        # knowledge first (relaxed, fixed band)
        knowledge_band = self.config.knowledge_budget()
        for item in knowledge_items:
            if remaining <= 0:
                plan.truncated = True
                break
            text = f"[知识·{item['category']}] {item['content']}"
            tokens = estimate_tokens(text)
            if tokens > remaining:
                plan.truncated = True
                break
            plan.knowledge.append(PlannedItem("knowledge", item["id"], text, tokens))
            plan.total_tokens += tokens
            remaining -= tokens
        if knowledge_band < self.config.knowledge_max_tokens and plan.total_tokens >= knowledge_band:
            # band is a soft guide for the knowledge domain only; memory takes the rest
            pass

        # memory next: relevance-threshold driven
        for hit in memory_hits:
            if remaining <= 0:
                plan.truncated = True
                break
            if hit.score < min_relevance:
                break
            text = f"[记忆·{hit.title or hit.topic_id}] {hit.preview}"
            tokens = estimate_tokens(text)
            if tokens > remaining:
                plan.truncated = True
                continue
            plan.memory.append(
                PlannedItem("memory", hit.doc_id, text, tokens, score=hit.score)
            )
            plan.total_tokens += tokens
            remaining -= tokens
        return plan


@dataclass
class InjectionPayload:
    text: str
    plan: InjectionPlan


class InjectionAssembler:
    """Builds the injection text fed to the main loop before each turn."""

    def __init__(
        self,
        budget: InjectionBudget,
        retriever: Retriever,
        knowledge_source: InjectionSource | None = None,
        knowledge_items: list[dict] | None = None,
    ) -> None:
        self.budget = budget
        self.retriever = retriever
        self.knowledge_source = knowledge_source
        self._knowledge_items = knowledge_items

    def build(
        self,
        query: str,
        *,
        anchor_topic_id: str | None = None,
        top_k: int = 6,
        min_relevance: float = 0.05,
    ) -> InjectionPayload:
        knowledge_items = self._knowledge_items
        if knowledge_items is None:
            assert self.knowledge_source is not None
            knowledge_items = self.knowledge_source.list_active()
        hits = self.retriever.search(
            query, anchor_topic_id=anchor_topic_id, top_k=top_k
        )
        plan = self.budget.plan(
            knowledge_items, hits, min_relevance=min_relevance
        )
        if not plan.all_items:
            return InjectionPayload(text="", plan=plan)
        sections = ["【长期记忆注入】"]
        for item in plan.knowledge:
            sections.append(item.text)
        for item in plan.memory:
            sections.append(item.text)
        return InjectionPayload(text="\n".join(sections), plan=plan)