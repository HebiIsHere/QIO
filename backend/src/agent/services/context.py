"""ContextAssembler: build the per-turn injected context.

Owns the read-only context construction that used to live on AppContext:
focus block, short-term transcript, topic note, entity soft-signal, and the
injection payload. No mutation of turn/process state happens here.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from agent.core.message_assembly import ContextBlock
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
        # 最近一次 build_context_blocks 的预算结论（A 的调用点与 trace 读取）：
        # 收尾压缩触发与预算展示依赖它们；默认值保证“还没装上下文”时也有定义。
        self.last_needs_consolidation = False
        self.last_budget: dict | None = None

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
        self,
        topic_id: str,
        exclude_message_id: str | None = None,
        fragment_id: str | None = None,
    ) -> list:
        """Deterministic short-term memory: open fragment transcript + recent summaries.

        转录按 token 上限截断（只保留最近消息），避免多轮对话后整段转录无限
        膨胀、单轮 token 消耗突破循环预算导致对话被 STOPPED。

        阶段 3：历史前提按**本轮片段所在的路径**组织（见 `_path_items`），
        不再无条件把「这个话题最近两段摘要」当作前提。
        """
        from agent.knowledge.inject import InjectionSource
        from agent.memory.index import estimate_tokens
        from agent.services.injection import PlannedItem

        items: list = []
        frag = self.fragments.get(fragment_id) if fragment_id else None
        if frag is None or frag.topic_id != topic_id:
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
        items.extend(self._path_items(topic_id, frag))
        return items

    def _path_items(self, topic_id: str, frag) -> list:
        """本轮片段所在的**路径**上的前提 + 明确标注的参考。

        层次（阶段 3）：
        1. 直接来源（这段讨论是从哪一段长出来的）；
        2. 更早的来源（祖先链，深度与 token 都有上限）；
        3. 同话题但**不在这条路径上**的片段 —— 只能作为「仅参考」出现，
           不能被当成已经接受的前提（A→B→C 与 A→D 的隔离就是靠这一条）。

        摘要还没生成时用原文尾部兜底（阶段 2 之后「已封存但未摘要」是正常中间态）。
        """
        from agent.knowledge.inject import InjectionSource
        from agent.memory.index import estimate_tokens
        from agent.services.injection import PlannedItem

        items: list = []
        budget_tokens = 1_500
        used = 0
        chain = self.fragments.ancestors(frag.id, max_depth=3)
        for ancestor_id, depth in chain:
            text = self._premise_text(ancestor_id, depth)
            if not text:
                continue
            tokens = estimate_tokens(text)
            if used + tokens > budget_tokens:
                break
            used += tokens
            items.append(
                PlannedItem(
                    source="memory",
                    surface="topic_short",
                    item_id=ancestor_id,
                    text=text,
                    tokens=tokens,
                )
            )

        on_path = {ancestor_id for ancestor_id, _ in chain} | {frag.id}
        references = [
            s
            for s in InjectionSource(self.conn).recent_fragment_summaries(topic_id, limit=4)
            if s["id"] not in on_path
        ][:2]
        for ref in references:
            text = (
                f"【同话题其他片段·仅参考】{ref['title']}\n{ref['summary']}\n"
                "（这条不在本轮讨论的接续路径上，只作背景，不代表本轮的结论）"
            )
            items.append(
                PlannedItem(
                    source="memory",
                    surface="topic_short",
                    item_id=ref["id"],
                    text=text,
                    tokens=estimate_tokens(text),
                )
            )
        return items

    def _premise_text(self, fragment_id: str, depth: int) -> str:
        """一条路径前提的文本：优先摘要，没有摘要就用原文尾部兜底（受字符上限约束）。"""
        from agent.services.params import FOCUS

        fragment = self.fragments.get(fragment_id)
        if fragment is None:
            return ""
        title = ""
        row = self.conn.execute(
            "SELECT title FROM memory_index WHERE fragment_id = ? ORDER BY created_at DESC LIMIT 1",
            (fragment_id,),
        ).fetchone()
        if row is not None and row["title"]:
            title = str(row["title"])
        label = "直接来源" if depth == 1 else f"更早的来源（第 {depth} 层）"
        summary = (fragment.summary or "").strip()
        if summary:
            body = summary[: FOCUS.max_summary_chars]
        else:
            rows = self.fragments.messages(fragment_id)[-4:]
            body = "\n".join(
                f"[{m['role']}] {str(m['content'])[:200]}" for m in rows if m["content"]
            )
            if not body:
                return ""
            body = (
                f"（这一段还没有摘要，以下是当时的原文结尾）\n{body}"
            )
        heading = f"【路径前提·{label}】{title or '（未命名）'}"
        return f"{heading}\n{body}"

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


    # -- 结构化上下文块（与 A 的唯一交接形状：ContextBlock） ----------------

    def build_context_blocks(
        self,
        query: str,
        *,
        topic_id: str | None = None,
        aux_topic_ids: list[str] | None = None,
        entity_ids: list[str] | None = None,
        user_node_id: str | None = None,
        model: str | None = None,
        fragment_id: str | None = None,
        focus_fragment_id: str | None = None,
        exclude_message_id: str | None = None,
        new_topic_candidate: bool = False,
        new_topic_reason: str = "",
        system_prompt_tokens: int = 0,
        adapter_overhead_tokens: int = 0,
        tool_definitions_tokens: int = 0,
        completion_reserve: int | None = None,
    ) -> list[ContextBlock]:
        """构造本轮结构化上下文块（键必须来自 prompts.CONTEXT_LABELS）。

        与 A 的交接：返回 list[ContextBlock]，data 只放结构化数据（条目 id /
        来源 / 所属对象 / 适用范围 / 状态 / 确定程度 / 文本），固定标签由
        prompts.context_heading / render_context_blocks 渲染。

        * current_topic：当前话题（程序给的身份，不来自正文）；
        * history_focus：用户选中的历史片段（摘要 + 头尾原文，受 FOCUS 参数约束）；
        * entity：命中的实体卡（完整卡片；来源与适用范围分开标注）；
        * short_term：滚动摘要 + **未覆盖原文**（已覆盖原文不重复、未覆盖不跳过）；
        * path_premises：本轮片段接续路径上的前提，以及仅参考的同话题片段；
        * related_memory：检索命中的相关记忆；
        * knowledge：仅 state=active 的知识（pending_review 不进注入面）。

        预算口径与 InjectionBudget / TokenBudgetPlanner 一致：硬上限不变，
        按**完整条目**裁剪（不截断序列化后的 JSON），优先级
        Focus > 实体卡 > 短期记忆；被裁掉多少写进 limits 块。
        """
        from agent.memory.index import estimate_tokens
        from agent.prompts import context_heading

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
        remaining = max(0, int(breakdown.injection_hard_cap))
        blocked: set[str] = set()
        dropped: dict[str, int] = {}
        blocks: list[ContextBlock] = []

        def cost(key: str, data: dict) -> int:
            rendered = json.dumps(data, ensure_ascii=False, sort_keys=True)
            return estimate_tokens(context_heading(key)) + estimate_tokens(rendered)

        def take(key: str, data: dict | None) -> bool:
            nonlocal remaining
            if not data:
                return False
            value = cost(key, data)
            if value > remaining:
                dropped[key] = dropped.get(key, 0) + 1
                return False
            remaining -= value
            blocks.append(ContextBlock(key=key, data=data))
            return True

        # 1) 当前话题（小、且是程序身份）：永远优先保留。
        topic_data = self._topic_block_data(
            topic_id,
            new_topic_candidate=new_topic_candidate,
            new_topic_reason=new_topic_reason,
        )
        take("current_topic", topic_data)

        # 2) Focus（用户明确位置）：先于实体卡与短期记忆。
        focus_data, focus_dropped = self._history_focus_block_data(
            topic_id, focus_fragment_id, remaining=remaining, cost=cost
        )
        if focus_dropped:
            dropped["history_focus"] = dropped.get("history_focus", 0) + focus_dropped
        if focus_data and cost("history_focus", focus_data) <= remaining:
            remaining -= cost("history_focus", focus_data)
            blocks.append(ContextBlock(key="history_focus", data=focus_data))
            blocked.add(str(focus_data.get("fragment_id") or ""))

        # 3) 实体卡：完整卡片，不被短期记忆挤掉（在短期记忆之前分配预算）。
        entity_data, entity_dropped = self._entity_block_data(
            query, topic_id, entity_ids or [], blocked, remaining=remaining, cost=cost
        )
        if entity_dropped:
            dropped["entity"] = dropped.get("entity", 0) + entity_dropped
        if entity_data and cost("entity", entity_data) <= remaining:
            remaining -= cost("entity", entity_data)
            blocks.append(ContextBlock(key="entity", data=entity_data))
            for item in entity_data.get("entries", []):
                blocked.add(str(item.get("id") or ""))
                blocked.add(str(item.get("node_id") or ""))

        # 4) 短期记忆：滚动摘要 + 未覆盖原文（同一份读取口径）。
        short_data, short_dropped = self._short_term_block_data(
            topic_id,
            fragment_id,
            exclude_message_id=exclude_message_id,
            remaining=remaining,
            cost=cost,
        )
        if short_dropped:
            dropped["short_term"] = dropped.get("short_term", 0) + short_dropped
        if short_data and cost("short_term", short_data) <= remaining:
            remaining -= cost("short_term", short_data)
            blocks.append(ContextBlock(key="short_term", data=short_data))
            blocked.add(str(short_data.get("fragment_id") or ""))
            for item in short_data.get("uncovered_messages", []):
                blocked.add(str(item.get("id") or ""))

        # 5) 路径前提（本轮接续路径 + 仅参考）
        path_data, path_dropped = self._path_premises_block_data(
            topic_id, fragment_id, blocked, remaining=remaining, cost=cost
        )
        if path_dropped:
            dropped["path_premises"] = dropped.get("path_premises", 0) + path_dropped
        if path_data and cost("path_premises", path_data) <= remaining:
            remaining -= cost("path_premises", path_data)
            blocks.append(ContextBlock(key="path_premises", data=path_data))
            for item in path_data.get("entries", []):
                blocked.add(str(item.get("fragment_id") or ""))

        # 6) 相关记忆（检索命中；已被前面块注入的片段不重复出现）
        related_data, related_dropped = self._related_memory_block_data(
            query, topic_id, blocked, remaining=remaining, cost=cost
        )
        if related_dropped:
            dropped["related_memory"] = dropped.get("related_memory", 0) + related_dropped
        if related_data and cost("related_memory", related_data) <= remaining:
            remaining -= cost("related_memory", related_data)
            blocks.append(ContextBlock(key="related_memory", data=related_data))
            for item in related_data.get("entries", []):
                blocked.add(str(item.get("fragment_id") or ""))

        # 7) 知识：只来自 state=active（pending_review 不进注入面）。
        knowledge_data, knowledge_dropped = self._knowledge_block_data(
            topic_id,
            aux_topic_ids or [],
            entity_ids or [],
            user_node_id,
            blocked,
            remaining=remaining,
            cost=cost,
        )
        if knowledge_dropped:
            dropped["knowledge"] = dropped.get("knowledge", 0) + knowledge_dropped
        if knowledge_data and cost("knowledge", knowledge_data) <= remaining:
            remaining -= cost("knowledge", knowledge_data)
            blocks.append(ContextBlock(key="knowledge", data=knowledge_data))
            for item in knowledge_data.get("entries", []):
                blocked.add(str(item.get("id") or ""))

        # 8) 本次处理限制（如实写出预算与裁剪事实；放不下就不写，绝不超硬上限）。
        limits_data = {
            "context_window": int(breakdown.context_window),
            "injection_hard_cap": int(breakdown.injection_hard_cap),
            "used_tokens": int(breakdown.injection_hard_cap - remaining),
            "remaining_tokens": int(remaining),
            "dropped_entries": dict(dropped),
            "truncated": bool(dropped),
        }
        if cost("limits", limits_data) <= remaining:
            remaining -= cost("limits", limits_data)
            blocks.append(ContextBlock(key="limits", data=limits_data))
        # 把本轮预算结论暴露给 A 的调用点（trace / 收尾压缩触发）：
        # 没有它 needs_consolidation 恒为 False，滚动摘要不会在预算压力下触发。
        hard_cap = int(breakdown.injection_hard_cap)
        used = max(0, hard_cap - remaining)
        self.last_budget = {
            "hard_cap": hard_cap,
            "used_tokens": used,
            "truncated": bool(dropped),
            "dropped_entries": dict(dropped),
        }
        self.last_needs_consolidation = bool(dropped) or (
            hard_cap > 0 and used >= hard_cap * 0.9
        )
        return blocks

    # -- 各块的数据构造（只读；预算由调用方统一分配） ----------------------

    def _topic_block_data(
        self,
        topic_id: str | None,
        *,
        new_topic_candidate: bool = False,
        new_topic_reason: str = "",
    ) -> dict | None:
        if not topic_id:
            return None
        node = self.topics.nodes.get_topic(topic_id)
        data: dict = {
            "topic_id": topic_id,
            "name": node.name if node is not None else None,
            "new_topic_candidate": bool(new_topic_candidate),
        }
        if new_topic_candidate:
            data["new_topic_reason"] = new_topic_reason or None
        return data

    @staticmethod
    def _clip(text: str | None, limit: int) -> tuple[str, bool]:
        """数据层裁剪单条文本（先控制完整对象大小，不截断序列化后的 JSON）。"""
        value = (text or "").strip()
        if len(value) <= limit:
            return value, False
        return value[:limit] + "…", True

    def _history_focus_block_data(
        self,
        topic_id: str | None,
        focus_fragment_id: str | None,
        *,
        remaining: int,
        cost,
    ) -> tuple[dict | None, int]:
        from agent.services.params import FOCUS

        if not focus_fragment_id:
            return None, 0
        fragment = self.fragments.get(focus_fragment_id)
        if fragment is None or (topic_id and fragment.topic_id != topic_id):
            return None, 0
        row = self.conn.execute(
            "SELECT title FROM memory_index WHERE fragment_id = ? "
            "ORDER BY created_at DESC LIMIT 1",
            (fragment.id,),
        ).fetchone()
        summary, summary_clipped = self._clip(
            fragment.summary, FOCUS.max_summary_chars
        )
        base = {
            "fragment_id": fragment.id,
            "title": (row["title"] if row is not None else None),
            "summary": summary,
            "summary_clipped": summary_clipped,
        }
        rows = self.fragments.messages(fragment.id)
        head_rows = list(rows[: FOCUS.head_messages])
        tail_rows = (
            list(rows[-FOCUS.tail_messages :])
            if len(rows) > len(head_rows)
            else []
        )
        omitted = max(0, len(rows) - len(head_rows) - len(tail_rows))

        def entry(message) -> dict:
            content, clipped = self._clip(message["content"], FOCUS.max_message_chars)
            return {
                "id": str(message["id"]),
                "role": str(message["role"]),
                "content": content,
                "clipped": clipped,
            }

        drop = 0
        tail_entries = [entry(message) for message in tail_rows]
        tail_entries.reverse()
        base_cost = cost(
            "history_focus",
            {**base, "head": [], "tail": [], "omitted_message_count": omitted},
        )
        budget = remaining - base_cost
        if budget < 0:
            return None, 1
        kept_tail: list[dict] = []
        for item in tail_entries:
            price = cost("history_focus", item)
            if price <= budget:
                kept_tail.append(item)
                budget -= price
            else:
                drop += 1
        kept_tail.reverse()
        kept_head: list[dict] = []
        for item in [entry(message) for message in head_rows]:
            price = cost("history_focus", item)
            if price <= budget:
                kept_head.append(item)
                budget -= price
            else:
                drop += 1
        data = {
            **base,
            "head": kept_head,
            "tail": kept_tail,
            "omitted_message_count": omitted,
        }
        return data, drop

    def _entity_block_data(
        self,
        query: str,
        topic_id: str | None,
        entity_ids: list[str],
        blocked: set[str],
        *,
        remaining: int,
        cost,
    ) -> tuple[dict | None, int]:
        from agent.entities.cards import EntityCardService
        from agent.services.params import FOCUS

        service = EntityCardService(self.conn)
        entries: list[dict] = []
        matched_entities: set[str] = set()
        for card in service.match_cards(query):
            if card.id in blocked:
                continue
            topics = service.topics_of(card)
            if topics and topic_id and topic_id in topics:
                scope = {"type": "topic", "topic_ids": [topic_id]}
            elif topics:
                scope = {"type": "other_topic", "topic_ids": list(topics)}
            else:
                scope = {"type": "unknown", "topic_ids": []}
            summary, clipped = self._clip(card.summary, FOCUS.max_summary_chars)
            entries.append(
                {
                    "kind": "entity_card",
                    "id": card.id,
                    "node_id": card.node_id,
                    "name": card.name,
                    "type": card.kind,
                    "summary": summary,
                    "summary_clipped": clipped,
                    "aliases": list(card.aliases),
                    "attributes": list(card.attributes),
                    "relations": [
                        {"type": item["type"], "target": item["target"]}
                        for item in service.to_dict(card).get("relations", [])
                    ],
                    "state": card.state,
                    "scope": scope,
                    "source": "entity_card",
                }
            )
            matched_entities.add(card.name)
        # 话题关联的实体节点（没有命中卡片时作为结构化引用出现，最多 8 条）。
        for entity_id in entity_ids[:8]:
            if entity_id in blocked:
                continue
            row = self.conn.execute(
                "SELECT id, name, type FROM nodes WHERE id = ?", (entity_id,)
            ).fetchone()
            if row is None or row["name"] in matched_entities:
                continue
            entries.append(
                {
                    "kind": "entity_reference",
                    "id": str(row["id"]),
                    "node_id": str(row["id"]),
                    "name": str(row["name"]),
                    "scope": {"type": "topic", "topic_ids": [topic_id]},
                    "source": "topic_entity",
                }
            )
        return self._fit_forward("entity", {"entries": entries}, remaining, cost)

    def _short_term_block_data(
        self,
        topic_id: str | None,
        fragment_id: str | None,
        *,
        exclude_message_id: str | None,
        remaining: int,
        cost,
    ) -> tuple[dict | None, int]:
        from agent.memory.rolling import rolling_view

        if not topic_id:
            return None, 0
        fragment = self.fragments.get(fragment_id) if fragment_id else None
        if fragment is None or (topic_id and fragment.topic_id != topic_id):
            # 读路径不建片段：只装配已经存在的开放片段。
            fragment = self.fragments.open_fragment(topic_id)
        if fragment is None:
            return None, 0
        view = rolling_view(
            self.conn, fragment, exclude_message_id=exclude_message_id
        )
        rolling = None
        if view.summary:
            rolling = {
                "summary": view.summary,
                "covered_message_id": view.covered_message_id,
                "covered_count": int(view.covered_count),
                "input_version": view.input_version,
                "updated_at": view.updated_at,
            }
        if rolling is None and not view.uncovered:
            return None, 0
        base = {"fragment_id": fragment.id, "rolling_summary": rolling}
        base_cost = cost("short_term", {**base, "uncovered_messages": []})
        if base_cost > remaining and rolling is not None:
            # 滚动摘要放不下就只放未覆盖原文（滚动摘要本身仍是完整对象，不截断）。
            base = {"fragment_id": fragment.id, "rolling_summary": None}
            base_cost = cost("short_term", {**base, "uncovered_messages": []})
        if base_cost > remaining:
            return None, len(view.uncovered)
        budget = remaining - base_cost
        kept: list[dict] = []
        dropped = 0
        for message in reversed(view.uncovered):
            entry = {
                "kind": "message",
                "id": str(message.get("id") or ""),
                "role": str(message.get("role") or ""),
                "content": str(message.get("content") or ""),
            }
            price = cost("short_term", entry)
            if price <= budget:
                kept.append(entry)
                budget -= price
            else:
                dropped += 1
        kept.reverse()
        data = {**base, "uncovered_messages": kept}
        return data, dropped

    def _path_premises_block_data(
        self,
        topic_id: str | None,
        fragment_id: str | None,
        blocked: set[str],
        *,
        remaining: int,
        cost,
    ) -> tuple[dict | None, int]:
        from agent.knowledge.inject import InjectionSource
        from agent.services.params import FOCUS

        if not topic_id:
            return None, 0
        fragment = self.fragments.get(fragment_id) if fragment_id else None
        if fragment is None or (topic_id and fragment.topic_id != topic_id):
            fragment = self.fragments.open_fragment(topic_id)
        if fragment is None:
            return None, 0
        entries: list[dict] = []
        on_path: set[str] = {fragment.id}
        for ancestor_id, depth in self.fragments.ancestors(fragment.id, max_depth=3):
            if ancestor_id in blocked:
                continue
            ancestor = self.fragments.get(ancestor_id)
            if ancestor is None:
                continue
            title_row = self.conn.execute(
                "SELECT title FROM memory_index WHERE fragment_id = ? "
                "ORDER BY created_at DESC LIMIT 1",
                (ancestor_id,),
            ).fetchone()
            summary, clipped = self._clip(
                ancestor.summary, FOCUS.max_summary_chars
            )
            entry = {
                "fragment_id": ancestor_id,
                "relation": "ancestor",
                "depth": int(depth),
                "on_path": True,
                "title": title_row["title"] if title_row is not None else None,
                "summary": summary or None,
                "summary_clipped": clipped,
            }
            if not summary:
                tail = self.fragments.messages(ancestor_id)[-4:]
                entry["original_tail"] = [
                    {
                        "id": str(message["id"]),
                        "role": str(message["role"]),
                        "content": self._clip(message["content"], 200)[0],
                    }
                    for message in tail
                ]
            entries.append(entry)
            on_path.add(ancestor_id)
        references = [
            item
            for item in InjectionSource(self.conn).recent_fragment_summaries(
                topic_id, limit=4
            )
            if item["id"] not in on_path and item["id"] not in blocked
        ][:2]
        for item in references:
            summary, clipped = self._clip(item.get("summary"), FOCUS.max_summary_chars)
            entries.append(
                {
                    "fragment_id": item["id"],
                    "relation": "reference",
                    "depth": None,
                    "on_path": False,
                    "title": item.get("title"),
                    "summary": summary or None,
                    "summary_clipped": clipped,
                }
            )
        if not entries:
            return None, 0
        return self._fit_forward("path_premises", {"entries": entries}, remaining, cost)

    def _related_memory_block_data(
        self,
        query: str,
        topic_id: str | None,
        blocked: set[str],
        *,
        remaining: int,
        cost,
    ) -> tuple[dict | None, int]:
        entries: list[dict] = []
        seen: set[str] = set()
        for hit in self.retriever.search(query, anchor_topic_id=topic_id, top_k=6):
            identity = hit.fragment_id or hit.doc_id
            if identity in blocked or identity in seen:
                continue
            seen.add(identity)
            entries.append(
                {
                    "kind": "memory_hit",
                    "doc_id": hit.doc_id,
                    "fragment_id": hit.fragment_id,
                    "title": hit.title,
                    "preview": hit.preview,
                    "score": round(float(hit.score), 4),
                    "source": "retrieval",
                }
            )
        if not entries:
            return None, 0
        return self._fit_forward("related_memory", {"entries": entries}, remaining, cost)

    def _knowledge_block_data(
        self,
        topic_id: str | None,
        aux_topic_ids: list[str],
        entity_ids: list[str],
        user_node_id: str | None,
        blocked: set[str],
        *,
        remaining: int,
        cost,
    ) -> tuple[dict | None, int]:
        from agent.knowledge.inject import InjectionSource

        source = InjectionSource(self.conn)
        surfaces: list[tuple[str, str, int]] = []
        if topic_id:
            surfaces.append(("topic", topic_id, 5))
        for aux_id in aux_topic_ids:
            surfaces.append(("aux_topic", aux_id, 3))
        for entity_id in entity_ids[:8]:
            surfaces.append(("entity", entity_id, 5))
        if not user_node_id:
            from agent.knowledge.scope import existing_user_global_node_id

            user_node_id = existing_user_global_node_id(self.conn)
        if user_node_id:
            surfaces.append(("user", user_node_id, 5))
        entries: list[dict] = []
        seen: set[str] = set()
        for surface, node_id, limit in surfaces:
            for item in source.list_active_for_node(node_id, limit=limit):
                item_id = str(item.get("id") or "")
                if not item_id or item_id in seen or item_id in blocked:
                    continue
                if str(item.get("state") or "") != "active":
                    # 只有 state=active 才进注入面；pending_review 等一律跳过。
                    continue
                seen.add(item_id)
                provenance: dict = {}
                try:
                    provenance = json.loads(item.get("provenance") or "{}")
                except (TypeError, ValueError):
                    provenance = {}
                entries.append(
                    {
                        "kind": "knowledge",
                        "id": item_id,
                        "content": item.get("content"),
                        "category": item.get("category"),
                        "scope": {"type": surface, "node_id": node_id},
                        "state": item.get("state"),
                        "epistemic_status": (
                            item.get("epistemic_status")
                            or provenance.get("epistemic_status")
                        ),
                        "source": provenance.get("source") or provenance.get("origin"),
                        "ended": bool(provenance.get("ended_at")),
                    }
                )
        if not entries:
            return None, 0
        return self._fit_forward("knowledge", {"entries": entries}, remaining, cost)

    @staticmethod
    def _fit_forward(
        key: str, data: dict, remaining: int, cost
    ) -> tuple[dict | None, int]:
        """按给定顺序保留完整条目，直到预算用完；返回 (块数据, 被裁条目数)。

        remaining 里已包含 base 的开销判断：先试整块，放不下再逐条裁剪。
        """
        if not data.get("entries"):
            return None, 0
        whole = cost(key, data)
        if whole <= remaining:
            return data, 0
        entries = list(data.get("entries") or [])
        if not entries:
            return None, 0
        base = {name: value for name, value in data.items() if name != "entries"}
        base["entries"] = []
        if cost(key, base) > remaining:
            return None, len(entries)
        budget = remaining - cost(key, base)
        kept: list[dict] = []
        dropped = 0
        for item in entries:
            price = cost(key, item)
            if price <= budget:
                kept.append(item)
                budget -= price
            else:
                dropped += 1
        if not kept:
            return None, dropped
        return {**base, "entries": kept}, dropped

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