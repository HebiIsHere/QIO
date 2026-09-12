"""Injection: dynamic budget, three-surface aggregation, unified truncation.

Design (per plan):
- budget = model context window x ratio (default 25%), resolved dynamically;
- knowledge candidates are gathered from three surfaces: topic / entity / user;
- memory candidates come from the retriever (topic affinity weighted);
- all candidates are merged, ranked by score, and truncated by the budget.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from agent.knowledge.inject import InjectionSource
from agent.memory.index import estimate_tokens
from agent.prompts import (
    INJECT_HEADER,
    INJECT_MEMORY_ITEM,
    INJECT_NEW_TOPIC_DEFAULT_REASON,
    INJECT_NEW_TOPIC_SECTION,
    INJECT_RELATED_MEMORY_ITEM,
    INJECT_TOPIC_SECTION,
)
from agent.services.retrieval import RetrievalHit, Retriever
from agent.selector.tokenize import tokenize

DEFAULT_BUDGET_RATIO = 0.25


@dataclass
class BudgetConfig:
    context_window: int = 1_000_000
    budget_ratio: float = DEFAULT_BUDGET_RATIO
    # 显式硬上限（由 TokenBudgetPlanner 计算）；设置时优先于 ratio。
    hard_cap_override: int | None = None

    @property
    def hard_cap(self) -> int:
        if self.hard_cap_override is not None:
            return max(0, int(self.hard_cap_override))
        return max(1, int(self.context_window * self.budget_ratio))


@dataclass
class Candidate:
    source: str  # knowledge | memory
    surface: str  # topic | entity | user | memory
    item_id: str
    text: str
    score: float
    # 稳定身份（去重用）：同一片段/条目无论从哪个 surface 进来都算同一个；
    # 缺省时退回 item_id。
    identity: str | None = None


@dataclass
class PlannedItem:
    source: str
    surface: str
    item_id: str
    text: str
    tokens: int
    score: float = 0.0
    identity: str | None = None


@dataclass
class InjectionPlan:
    knowledge: list[PlannedItem] = field(default_factory=list)
    memory: list[PlannedItem] = field(default_factory=list)
    short_term: list[PlannedItem] = field(default_factory=list)
    total_tokens: int = 0
    hard_cap: int = 0
    truncated: bool = False
    needs_consolidation: bool = False
    budget_breakdown: dict = field(default_factory=dict)

    @property
    def all_items(self) -> list[PlannedItem]:
        return self.short_term + self.knowledge + self.memory


class InjectionBudget:
    def __init__(self, config: BudgetConfig) -> None:
        self.config = config

    @staticmethod
    def _fit(item: PlannedItem, remaining: int) -> PlannedItem | None:
        """确定性放置：放不下就按 token 上限截断文本；完全没空间则丢弃。"""
        if remaining <= 0:
            return None
        if item.tokens <= remaining:
            return item
        text = _truncate_to_tokens(item.text, remaining)
        if not text:
            return None
        return PlannedItem(
            source=item.source,
            surface=item.surface,
            item_id=item.item_id,
            text=text,
            tokens=estimate_tokens(text),
            score=item.score,
        )

    def plan(
        self,
        candidates: list[Candidate],
        *,
        min_score: float = 0.0,
        reserved: list[PlannedItem] | None = None,
    ) -> InjectionPlan:
        """Unified ranking + truncation; short-term items are reserved first.

        Invariant: plan.total_tokens <= plan.hard_cap on every path — mandatory
        (reserved) items are deterministically truncated rather than allowed to
        overflow the cap.
        """
        plan = InjectionPlan(hard_cap=self.config.hard_cap)
        remaining = self.config.hard_cap
        for item in reserved or []:
            placed = self._fit(item, remaining)
            if placed is None:
                plan.truncated = True
                continue
            if placed.tokens < item.tokens:
                plan.truncated = True
            plan.short_term.append(placed)
            plan.total_tokens += placed.tokens
            remaining -= placed.tokens
        ordered = sorted(candidates, key=lambda c: (-c.score, c.item_id))
        ranked_tokens = 0
        for cand in ordered:
            if remaining <= 0:
                plan.truncated = True
                break
            if cand.score < min_score:
                continue
            tokens = estimate_tokens(cand.text)
            if tokens > remaining:
                plan.truncated = True
                continue
            item = PlannedItem(
                source=cand.source,
                surface=cand.surface,
                item_id=cand.item_id,
                text=cand.text,
                tokens=tokens,
                score=cand.score,
            )
            if cand.source == "knowledge":
                plan.knowledge.append(item)
            else:
                plan.memory.append(item)
            plan.total_tokens += tokens
            ranked_tokens += tokens
            remaining -= tokens
        if len(ordered) > 0 and ranked_tokens >= self.config.hard_cap * 0.9:
            plan.needs_consolidation = True
        # 硬上限不变式（防任何路径溢出）
        if plan.total_tokens > plan.hard_cap:
            plan.total_tokens = plan.hard_cap
        return plan


def _truncate_to_tokens(text: str, max_tokens: int) -> str:
    """Deterministic truncation: binary-search the longest prefix within budget."""
    from agent.memory.index import estimate_tokens

    if max_tokens <= 0:
        return ""
    if estimate_tokens(text) <= max_tokens:
        return text
    lo, hi, best = 0, len(text), ""
    while lo <= hi:
        mid = (lo + hi) // 2
        cand = text[:mid]
        if estimate_tokens(cand) <= max_tokens:
            best = cand
            lo = mid + 1
        else:
            hi = mid - 1
    return best


@dataclass
class InjectionPayload:
    text: str
    plan: InjectionPlan


# surface base scores: user profile is the most stable, topic the least
SURFACE_BASE = {"user": 1.0, "entity": 0.7, "topic": 0.5, "aux_topic": 0.45}
KEYWORD_WEIGHT = 0.5


def knowledge_score(content: str, query: str, surface: str) -> float:
    base = SURFACE_BASE.get(surface, 0.5)
    query_tokens = set(tokenize(query))
    content_tokens = set(tokenize(content))
    if not query_tokens:
        return base
    overlap = len(query_tokens & content_tokens) / len(query_tokens)
    return base + KEYWORD_WEIGHT * overlap


def dedupe_candidates(
    candidates: list[Candidate], blocked: set[str]
) -> list[Candidate]:
    """按稳定身份去重：已被更高优先级 surface（Focus / 短期记忆）注入的条目跳过，
    候选之间同一身份只保留分数最高的一条。供注入组装与离线 eval 复用，
    避免两处各写一套「什么算重复」的规则。"""
    seen: dict[str, Candidate] = {}
    for cand in candidates:
        key = cand.identity or cand.item_id
        if key in blocked:
            continue
        prev = seen.get(key)
        if prev is None or cand.score > prev.score:
            seen[key] = cand
    return list(seen.values())


class InjectionAssembler:
    def __init__(
        self,
        budget: InjectionBudget,
        retriever: Retriever,
        knowledge_source: InjectionSource | None = None,
    ) -> None:
        self.budget = budget
        self.retriever = retriever
        self.knowledge_source = knowledge_source

    def build(
        self,
        query: str,
        *,
        topic_id: str | None = None,
        aux_topic_ids: list[str] | None = None,
        entity_ids: list[str] | None = None,
        user_node_id: str | None = None,
        top_k: int = 6,
        short_term: list[PlannedItem] | None = None,
        new_topic_candidate: bool = False,
        new_topic_reason: str = "",
        topic_note: str = "",
        focus_block: str = "",
        focus_item_id: str | None = None,
        entity_cards: list[str] | None = None,
    ) -> InjectionPayload:
        assert self.knowledge_source is not None
        candidates: list[Candidate] = []
        aux_topic_ids = aux_topic_ids or []

        # main topic knowledge surface
        if topic_id:
            for item in self.knowledge_source.list_active_for_node(topic_id, limit=5):
                score = knowledge_score(item["content"], query, "topic")
                candidates.append(
                    Candidate(
                        source="knowledge",
                        surface="topic",
                        item_id=item["id"],
                        text=f"[知识·{item['category']}] {item['content']}",
                        score=score,
                    )
                )

        # auxiliary topic surfaces: knowledge + recent fragment summaries
        for aux_id in aux_topic_ids:
            for item in self.knowledge_source.list_active_for_node(aux_id, limit=3):
                score = knowledge_score(item["content"], query, "aux_topic")
                candidates.append(
                    Candidate(
                        source="knowledge",
                        surface="aux_topic",
                        item_id=item["id"],
                        text=f"[相关话题知识·{item['category']}] {item['content']}",
                        score=score,
                    )
                )
            for frag in self.knowledge_source.recent_fragment_summaries(aux_id, limit=2):
                candidates.append(
                    Candidate(
                        source="memory",
                        surface="aux_topic",
                        item_id=frag["id"],
                        text=INJECT_RELATED_MEMORY_ITEM.format(title=frag["title"], summary=frag["summary"]),
                        score=0.4,
                    )
                )

        # entity / user knowledge surfaces
        for surface, node_id in [
            *[("entity", eid) for eid in (entity_ids or [])],
            ("user", user_node_id),
        ]:
            if not node_id:
                continue
            for item in self.knowledge_source.list_active_for_node(node_id, limit=5):
                score = knowledge_score(item["content"], query, surface)
                candidates.append(
                    Candidate(
                        source="knowledge",
                        surface=surface,
                        item_id=item["id"],
                        text=f"[知识·{item['category']}] {item['content']}",
                        score=score,
                    )
                )

        # memory surface: retriever with topic affinity
        hits = self.retriever.search(query, anchor_topic_id=topic_id, top_k=top_k)
        for hit in hits:
            candidates.append(
                Candidate(
                    source="memory",
                    surface="memory",
                    item_id=hit.doc_id,
                    text=INJECT_MEMORY_ITEM.format(title=hit.title or hit.topic_id, preview=hit.preview),
                    score=hit.score,
                    identity=hit.fragment_id or hit.doc_id,
                )
            )

        reserved: list[PlannedItem] = []
        # 顺序即优先级：Focus（用户明确位置）> 实体卡 > 短期记忆
        if focus_block:
            focus_id = focus_item_id or "focus"
            reserved.append(
                PlannedItem(
                    source="memory",
                    surface="focus",
                    item_id=focus_id,
                    text=focus_block,
                    tokens=estimate_tokens(focus_block),
                    identity=focus_id,
                )
            )
        # 实体卡命中（交流锚点）：高优保留
        for idx, card_text in enumerate(entity_cards or []):
            if card_text.strip():
                reserved.append(
                    PlannedItem(
                        source="memory",
                        surface="entity_card",
                        item_id=f"entity_card_{idx}",
                        text=card_text,
                        tokens=estimate_tokens(card_text),
                        identity=f"entity_card_{idx}",
                    )
                )
        reserved.extend(short_term or [])
        # 身份去重（先于预算）：同一片段已被 Focus/短期注入，就不再从检索重复注入；
        # 候选之间也按身份去重，保留分数最高的一条。
        blocked: set[str] = {(item.identity or item.item_id) for item in reserved}
        deduped = dedupe_candidates(candidates, blocked)
        plan = self.budget.plan(deduped, min_score=0.05, reserved=reserved)
        if not plan.all_items:
            return InjectionPayload(text="", plan=plan)
        sections = [INJECT_HEADER]
        if topic_note:
            sections.append(INJECT_TOPIC_SECTION.format(topic_note=topic_note))
        # Focus 由 plan.short_term 统一渲染（同一份预算截断结果），
        # 不再额外 append 一次 —— 否则每轮会注入两份几乎相同的 Focus。
        focus_item = next((i for i in plan.short_term if i.surface == "focus"), None)
        if focus_item is not None:
            sections.append(focus_item.text)
        if new_topic_candidate:
            reason = new_topic_reason or INJECT_NEW_TOPIC_DEFAULT_REASON
            sections.append(INJECT_NEW_TOPIC_SECTION.format(reason=reason))
        for item in plan.short_term:
            if item.surface == "focus":
                continue
            sections.append(item.text)
        for item in plan.knowledge:
            sections.append(item.text)
        for item in plan.memory:
            sections.append(item.text)
        return InjectionPayload(text="\n".join(sections), plan=plan)
