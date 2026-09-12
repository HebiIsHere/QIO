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
        """Build the focus block for the anchored fragment (empty when invalid).

        设计目标（四问）：这段历史主要讨论什么（摘要）、当时最后讨论到哪里（结尾消息）、
        形成了什么结论（结尾消息）、还有什么没解决（结尾消息）。所以结构是
        「摘要 + 开头少量消息 + 结尾少量消息」，而不是「摘要 + 前 3 条」——后者
        拿不到片段尾部的最终决定与遗留问题。

        确定性、零额外模型调用；只用 messages/summary，受 FOCUS.max_tokens 硬上限约束。
        """
        if not fragment_id:
            return ""
        frag = self.fragments.get(fragment_id)
        if frag is None or frag.topic_id != topic_id:
            return ""
        from agent.memory.index import estimate_tokens
        from agent.services.params import FOCUS

        title = None
        row = self.conn.execute(
            "SELECT title FROM memory_index WHERE fragment_id = ? "
            "ORDER BY created_at DESC LIMIT 1",
            (fragment_id,),
        ).fetchone()
        if row is not None and row["title"]:
            title = row["title"]
        if not title:
            if frag.closed_at is None:
                title = "当前片段"
            else:
                title = (frag.summary or "")[:20] or "历史片段"
        parts = [INJECT_FOCUS_SECTION.format(title=title)]

        def clip(text: str, limit: int) -> str:
            text = (text or "").strip()
            return text if len(text) <= limit else text[:limit] + "…"

        rows = self.fragments.messages(fragment_id)
        head_rows = rows[: FOCUS.head_messages]
        tail_rows = rows[-FOCUS.tail_messages :] if len(rows) > len(head_rows) else []
        omitted = max(0, len(rows) - len(head_rows) - len(tail_rows))
        head_lines = [
            f"[{m['role']}] {clip(m['content'], FOCUS.max_message_chars)}" for m in head_rows
        ]
        tail_lines = [
            f"[{m['role']}] {clip(m['content'], FOCUS.max_message_chars)}" for m in tail_rows
        ]
        summary = clip(frag.summary, FOCUS.max_summary_chars) if frag.summary else ""

        def assemble(summary_text: str, head: list[str], tail: list[str]) -> str:
            out = list(parts)
            if summary_text:
                out.append(f"【摘要】{summary_text}")
            out.extend(head)
            if omitted:
                out.append(f"【…中间省略 {omitted} 条…】")
            out.extend(tail)
            return "\n".join(out)

        block = assemble(summary, head_lines, tail_lines)
        # 硬上限：按「先丢摘要 → 再丢开头 → 再丢最早的结尾」的顺序收敛，
        # 最后兜底前缀截断，保证 estimate_tokens(block) <= max_tokens。
        if estimate_tokens(block) > FOCUS.max_tokens:
            block = assemble("", head_lines, tail_lines)
        while estimate_tokens(block) > FOCUS.max_tokens and head_lines:
            head_lines = head_lines[:-1]
            block = assemble("", head_lines, tail_lines)
        while estimate_tokens(block) > FOCUS.max_tokens and len(tail_lines) > 1:
            tail_lines = tail_lines[1:]
            block = assemble("", head_lines, tail_lines)
        if estimate_tokens(block) > FOCUS.max_tokens:
            from agent.services.injection import _truncate_to_tokens

            block = _truncate_to_tokens(block, FOCUS.max_tokens)
        return block

    def anchor_fragment_info(self) -> dict | None:
        """当前锚点片段元数据（id + title + 是否历史位置）。

        只有「历史位置」（不是当前开放片段）才会被前端当成「从这里继续」提示，
        避免几十轮后仍显示一个早已被消费的旧片段。
        """
        anchors = AnchorService(self.conn)
        active = anchors.get_active()
        if active is None or not active.fragment_id:
            return None
        row = self.conn.execute(
            "SELECT title FROM memory_index WHERE fragment_id = ? "
            "ORDER BY created_at DESC LIMIT 1",
            (active.fragment_id,),
        ).fetchone()
        title = row["title"] if row is not None and row["title"] else None
        if title is None:
            frag = self.fragments.get(active.fragment_id)
            if frag is not None and frag.summary:
                title = frag.summary.strip().splitlines()[0][:40]
        return {
            "id": active.fragment_id,
            "title": title,
            "historic": anchors.is_historic_position(active.topic_id or ""),
        }

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
        focus_item_id: str | None = None,
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
            focus_item_id=focus_item_id,
            entity_cards=entity_cards,
        )
        payload.plan.budget_breakdown = breakdown.as_dict()
        return payload
