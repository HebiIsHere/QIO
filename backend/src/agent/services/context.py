"""ContextAssembler: build the per-turn injected context.

Owns the read-only context construction that used to live on AppContext:
focus block, short-term transcript, topic note, entity soft-signal, and the
injection payload. No mutation of turn/process state happens here.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from agent.graph.anchors import AnchorService
from agent.prompts import (
    INJECT_FOCUS_SECTION,
    INJECT_SHORT_TERM_SECTION,
    TOPIC_NOTE_CURRENT,
    TOPIC_NOTE_RELATED,
    TOPIC_NOTE_SWITCH,
)
from agent.services.injection import (
    BudgetConfig,
    InjectionAssembler,
    InjectionBudget,
    InjectionPayload,
)
from agent.services.token_budget import TokenBudgetPlanner

DEFAULT_BUDGET_RATIO = 0.25


class ContextAssembler:
    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        fragments,
        topics,
        retriever,
        context_registry,
        budget_ratio: float = DEFAULT_BUDGET_RATIO,
        budget_planner: TokenBudgetPlanner | None = None,
    ) -> None:
        self.conn = conn
        self.fragments = fragments
        self.topics = topics
        self.retriever = retriever
        self.context_registry = context_registry
        self.budget_ratio = budget_ratio
        self.budget_planner = budget_planner or TokenBudgetPlanner()

    # -- focus / anchor ---------------------------------------------------

    def focus_block(self, topic_id: str, fragment_id: str | None) -> str:
        """Build the focus block for the anchored fragment (empty when invalid)."""
        if not fragment_id:
            return ""
        frag = self.fragments.get(fragment_id)
        if frag is None or frag.topic_id != topic_id:
            return ""
        title = None
        row = self.conn.execute(
            "SELECT title FROM memory_index WHERE fragment_id = ?", (fragment_id,)
        ).fetchone()
        if row is not None and row["title"]:
            title = row["title"]
        if not title:
            if frag.closed_at is None:
                title = "当前片段"
            else:
                title = (frag.summary or "")[:20] or "历史片段"
        parts = [INJECT_FOCUS_SECTION.format(title=title)]
        if frag.summary:
            parts.append(frag.summary)
        for m in self.fragments.messages(fragment_id)[:3]:
            content = m["content"] or ""
            parts.append(f"[{m['role']}] {content}")
        return "\n".join(parts)

    def anchor_fragment_info(self) -> dict | None:
        """Anchor fragment metadata for the session context (id + title)."""
        active = AnchorService(self.conn).get_active()
        if active is None or not active.fragment_id:
            return None
        row = self.conn.execute(
            "SELECT title FROM memory_index WHERE fragment_id = ?",
            (active.fragment_id,),
        ).fetchone()
        return {"id": active.fragment_id, "title": row["title"] if row else None}

    # -- short-term memory ------------------------------------------------

    def short_term_items(
        self, topic_id: str, exclude_message_id: str | None = None
    ) -> list:
        """Deterministic short-term memory: open fragment transcript + recent summaries.

        转录按 token 上限截断（只保留最近消息），避免多轮对话后整段转录无限
        膨胀、单轮 token 消耗突破循环预算导致对话被 STOPPED。
        """
        from agent.knowledge.inject import InjectionSource
        from agent.memory.index import estimate_tokens
        from agent.services.injection import PlannedItem

        items: list = []
        frag = self.fragments.get_or_create_open(topic_id)
        if frag.start_message_id is not None:
            # 只取最近 N 条消息，且整体不超过 ~2.5k token（约 1/6 迭代预算）
            max_messages = 12
            max_tokens = 2_500
            rows = self.fragments.messages(frag.id)
            # invariant：current query 不属于 historical injected transcript，
            # 当前 user message 已先写入 fragment（供 topic 迁移），这里必须排除，
            # 否则它会同时出现在短期转录与显式当前消息里 → 重复注入。
            if exclude_message_id is not None:
                rows = [m for m in rows if m["id"] != exclude_message_id]
            lines = [
                f"[{m['role']}] {m['content']}"
                for m in rows[-max_messages:]
                if m["content"]
            ]
            if lines:
                text = "\n".join(lines)
                if estimate_tokens(text) > max_tokens:
                    kept: list[str] = []
                    used = 0
                    for line in reversed(lines):
                        t = estimate_tokens(line)
                        if used + t > max_tokens and kept:
                            break
                        kept.append(line)
                        used += t
                    text = "\n".join(reversed(kept))
                items.append(
                    PlannedItem(
                        source="memory",
                        surface="topic_short",
                        item_id=frag.id,
                        text=INJECT_SHORT_TERM_SECTION.format(text=text),
                        tokens=estimate_tokens(text),
                    )
                )
        for s in InjectionSource(self.conn).recent_fragment_summaries(topic_id, limit=2):
            text = f"【短期摘要·{s['title']}】\n{s['summary']}"
            items.append(
                PlannedItem(
                    source="memory",
                    surface="topic_short",
                    item_id=s["id"],
                    text=text,
                    tokens=estimate_tokens(text),
                )
            )
        return items

    # -- topic note / entity hints ---------------------------------------

    def topic_note(self, topic_id: str, prediction: Any) -> str:
        """Human-readable topic context injected so the main model can act on it."""
        parts: list[str] = []
        node = self.topics.nodes.get_topic(topic_id)
        parts.append(
            TOPIC_NOTE_CURRENT.format(name=node.name if node else topic_id, topic_id=topic_id)
        )
        if prediction.aux_topic_ids:
            names = []
            for tid in prediction.aux_topic_ids:
                n = self.topics.nodes.get_topic(tid)
                names.append(f"「{n.name if n else tid}」（{tid}）")
            parts.append(TOPIC_NOTE_RELATED.format(names="、".join(names)))
        if prediction.suggested_switch and prediction.main_topic_id:
            n = self.topics.nodes.get_topic(prediction.main_topic_id)
            score = prediction.scores.get(prediction.main_topic_id, 0.0)
            parts.append(
                TOPIC_NOTE_SWITCH.format(
                    name=n.name if n else prediction.main_topic_id,
                    topic_id=prediction.main_topic_id,
                    score=score,
                )
            )
        return "；".join(parts)

    def entity_card_topics(self, message: str) -> list[str]:
        """消息命中的实体卡，其关联话题（mention 边 topic→entity，软信号用）。"""
        from agent.entities.cards import EntityCardService

        svc = EntityCardService(self.conn)
        topics: set[str] = set()
        for card in svc.match_cards(message):
            if not card.node_id:
                continue
            rows = self.conn.execute(
                "SELECT src FROM edges WHERE dst = ? AND type = 'mention'",
                (card.node_id,),
            ).fetchall()
            for r in rows:
                topics.add(r["src"])
        return list(topics)

    # -- injection payload ------------------------------------------------

    def build_injection(
        self,
        query: str,
        *,
        topic_id: str | None = None,
        aux_topic_ids: list[str] | None = None,
        entity_ids: list[str] | None = None,
        user_node_id: str | None = None,
        model: str | None = None,
        short_term: list | None = None,
        new_topic_candidate: bool = False,
        new_topic_reason: str = "",
        topic_note: str = "",
        focus_block: str = "",
        entity_cards: list[str] | None = None,
        system_prompt_tokens: int = 0,
        adapter_overhead_tokens: int = 0,
        tool_definitions_tokens: int = 0,
        completion_reserve: int | None = None,
    ) -> InjectionPayload:
        from agent.knowledge.inject import InjectionSource
        from agent.memory.index import estimate_tokens

        context_window = self.context_registry.get(None, model or "unknown")
        breakdown = self.budget_planner.plan(
            context_window=context_window,
            system_prompt_tokens=system_prompt_tokens,
            adapter_overhead_tokens=adapter_overhead_tokens,
            tool_definitions_tokens=tool_definitions_tokens,
            user_query_tokens=estimate_tokens(query),
            completion_reserve=completion_reserve,
            injection_ratio=self.budget_ratio,
        )
        budget = InjectionBudget(
            BudgetConfig(
                context_window=context_window,
                budget_ratio=self.budget_ratio,
                hard_cap_override=breakdown.injection_hard_cap,
            )
        )
        assembler = InjectionAssembler(
            budget,
            self.retriever,
            knowledge_source=InjectionSource(self.conn),
        )
        payload = assembler.build(
            query,
            topic_id=topic_id,
            aux_topic_ids=aux_topic_ids,
            entity_ids=entity_ids or [],
            user_node_id=user_node_id,
            top_k=6,
            short_term=short_term,
            new_topic_candidate=new_topic_candidate,
            new_topic_reason=new_topic_reason,
            topic_note=topic_note,
            focus_block=focus_block,
            entity_cards=entity_cards,
        )
        payload.plan.budget_breakdown = breakdown.as_dict()
        return payload
