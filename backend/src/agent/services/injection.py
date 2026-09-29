"""Injection: dynamic budget, three-surface aggregation, unified truncation.

Design (per plan):
- budget = model context window x ratio (default 25%), resolved dynamically;
- knowledge candidates are gathered from three surfaces: topic / entity / user;
- memory candidates come from the retriever (topic affinity weighted);
- all candidates are merged, ranked by score, and truncated by the budget.
"""

from __future__ import annotations

import json
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

# 排除原因（`InjectionPlan.dropped` 的 reason 取值）：
#   not_recalled            —— 检索阶段就没召回（记在 Retriever.last_trace.pool 里）
#   dedupe_already_injected —— 同一身份已由更高优先级 surface（Focus / 实体卡 / 短期）注入
#   dedupe_duplicate        —— 候选之间同一身份，只保留分数最高的一条
#   below_min_score         —— 分数低于注入门槛
#   budget_insufficient     —— 预算不够（含放不下整条、剩余为 0）
#   reserved_no_space       —— 预留项（Focus / 实体卡 / 短期）自身放不下、被截断或丢弃
DROP_NOT_RECALLED = "not_recalled"
DROP_ALREADY_INJECTED = "dedupe_already_injected"
DROP_DUPLICATE = "dedupe_duplicate"
DROP_BELOW_MIN_SCORE = "below_min_score"
DROP_BUDGET = "budget_insufficient"
DROP_RESERVED = "reserved_no_space"


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
    # -- 来自唯一排序入口的可解释量（知识候选没有这些，保持 0 / 空） ----------
    relevance: float = 0.0
    relevance_term: float = 0.0
    factors: dict = field(default_factory=dict)
    rank: int = 0
    strategy: str = ""


@dataclass
class PlannedItem:
    source: str
    surface: str
    item_id: str
    text: str
    tokens: int
    score: float = 0.0
    identity: str | None = None
    relevance: float = 0.0
    relevance_term: float = 0.0
    factors: dict = field(default_factory=dict)
    rank: int = 0
    strategy: str = ""


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
    #: 各阶段排除情况：{item_id, reason}，reason 见 `_DROP_REASONS`
    dropped: list[dict] = field(default_factory=list)
    #: 本次生效的排序配置（策略 / 权重 / 候选池 / 返回上限）
    ranking: dict = field(default_factory=dict)

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
            relevance=item.relevance,
            relevance_term=item.relevance_term,
            factors=item.factors,
            rank=item.rank,
            strategy=item.strategy,
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
                plan.dropped.append(
                    {"item_id": item.item_id, "reason": DROP_RESERVED}
                )
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
                plan.dropped.append({"item_id": cand.item_id, "reason": DROP_BUDGET})
                continue
            if cand.score < min_score:
                plan.dropped.append(
                    {"item_id": cand.item_id, "reason": DROP_BELOW_MIN_SCORE}
                )
                continue
            tokens = estimate_tokens(cand.text)
            if tokens > remaining:
                plan.truncated = True
                plan.dropped.append({"item_id": cand.item_id, "reason": DROP_BUDGET})
                continue
            item = PlannedItem(
                source=cand.source,
                surface=cand.surface,
                item_id=cand.item_id,
                text=cand.text,
                tokens=tokens,
                score=cand.score,
                identity=cand.identity,
                relevance=cand.relevance,
                relevance_term=cand.relevance_term,
                factors=cand.factors,
                rank=cand.rank,
                strategy=cand.strategy,
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


def knowledge_score(content: str, query: str, surface: str, *, ended: bool = False) -> float:
    """知识条目的注入分数。

    `ended=True`（用户确认「这件事已经结束」）：拿掉面的基础分，只留内容相关度。
    于是它不再常驻注入，只有在和本轮内容相关时才作为参考出现 —— 权重低于同类
    未结束条目（后者至少还有基础分）。
    """
    base = 0.0 if ended else SURFACE_BASE.get(surface, 0.5)
    query_tokens = set(tokenize(query))
    content_tokens = set(tokenize(content))
    if not query_tokens:
        return base
    overlap = len(query_tokens & content_tokens) / len(query_tokens)
    return base + KEYWORD_WEIGHT * overlap


def _row_is_ended(row: dict) -> bool:
    """知识行是否被标记为「已结束」（标记存在 provenance.ended_at）。"""
    raw = row.get("provenance")
    if not raw:
        return False
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return False
    return bool(isinstance(data, dict) and data.get("ended_at"))


def dedupe_candidates(
    candidates: list[Candidate], blocked: set[str]
) -> list[Candidate]:
    """按稳定身份去重：已被更高优先级 surface（Focus / 短期记忆）注入的条目跳过，
    候选之间同一身份只保留分数最高的一条。供注入组装与离线 eval 复用，
    避免两处各写一套「什么算重复」的规则。"""
    return partition_dedupe(candidates, blocked)[0]


def partition_dedupe(
    candidates: list[Candidate], blocked: set[str]
) -> tuple[list[Candidate], list[dict]]:
    """同 `dedupe_candidates`，但同时给出**被删掉的是谁、为什么**。

    返回 `(保留的候选, 排除记录)`；排除记录的 reason 见 `DROP_ALREADY_INJECTED`
    / `DROP_DUPLICATE`。注入链路用后者写 Trace，让「结果为什么少了一条」
    能和「排序落后」「预算不足」区分开。
    """
    seen: dict[str, Candidate] = {}
    dropped: list[dict] = []
    for cand in candidates:
        key = cand.identity or cand.item_id
        if key in blocked:
            dropped.append(
                {"item_id": cand.item_id, "identity": key, "reason": DROP_ALREADY_INJECTED}
            )
            continue
        prev = seen.get(key)
        if prev is None:
            seen[key] = cand
        elif cand.score > prev.score:
            dropped.append(
                {"item_id": prev.item_id, "identity": key, "reason": DROP_DUPLICATE}
            )
            seen[key] = cand
        else:
            dropped.append(
                {"item_id": cand.item_id, "identity": key, "reason": DROP_DUPLICATE}
            )
    return list(seen.values()), dropped


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
        top_k: int | None = None,
        query_entity_ids: list[str] | None = None,
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
                score = knowledge_score(
                    item["content"], query, "topic", ended=_row_is_ended(item)
                )
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
                score = knowledge_score(
                    item["content"], query, "aux_topic", ended=_row_is_ended(item)
                )
                candidates.append(
                    Candidate(
                        source="knowledge",
                        surface="aux_topic",
                        item_id=item["id"],
                        # 阶段 3：**其他话题**的知识只能作为参考出现。
                        # 某个话题里的决定、假设、方案状态是那一段讨论的结论，
                        # 不是本轮已经接受的前提；标签必须让人（和模型）一眼看出
                        # 「这条不一定适用」，而不是伪装成本话题的结论。
                        text=(
                            f"[其他话题知识·仅参考·{item['category']}] {item['content']}\n"
                            "（这条属于另一个话题，只能作为背景参考，不代表本轮已接受的结论）"
                        ),
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
                score = knowledge_score(
                    item["content"], query, surface, ended=_row_is_ended(item)
                )
                candidates.append(
                    Candidate(
                        source="knowledge",
                        surface=surface,
                        item_id=item["id"],
                        text=f"[知识·{item['category']}] {item['content']}",
                        score=score,
                    )
                )

        # memory surface：唯一排序入口（services/ranking.py）在 Retriever 内执行一次。
        # top_k=None 表示用集中配置里的注入返回上限，调用方不再各写一个数字。
        hits = self.retriever.search(
            query,
            anchor_topic_id=topic_id,
            entity_ids=query_entity_ids,
            top_k=top_k,
        )
        for hit in hits:
            candidates.append(
                Candidate(
                    source="memory",
                    surface="memory",
                    item_id=hit.doc_id,
                    text=INJECT_MEMORY_ITEM.format(title=hit.title or hit.topic_id, preview=hit.preview),
                    score=hit.score,
                    identity=hit.fragment_id or hit.doc_id,
                    relevance=hit.relevance,
                    relevance_term=hit.relevance_term,
                    factors=dict(hit.factors),
                    rank=hit.rank,
                    strategy=hit.strategy,
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
        deduped, dedupe_dropped = partition_dedupe(candidates, blocked)
        plan = self.budget.plan(deduped, min_score=0.05, reserved=reserved)
        # 排除原因按链路顺序拼接：先去重（重复删除），再预算（放不下 / 低于门槛）。
        plan.dropped = dedupe_dropped + plan.dropped
        # 兼容只实现 search() 的轻量替身（测试 / 离线组装）
        trace = getattr(self.retriever, "last_trace", None) or {}
        plan.ranking = {
            "strategy": trace.get("strategy", ""),
            "policy": trace.get("policy", {}),
            "candidate_pool": trace.get("candidate_pool", 0),
            "return_limit": trace.get("return_limit", 0),
            "entity_card_hits": [h["doc_id"] for h in trace.get("entity_card_hits", [])],
        }
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
