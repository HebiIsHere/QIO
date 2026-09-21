"""MemoryLifecycle: post-turn memory/knowledge lifecycle.

Owns fragment close (summary + entities + entity cards + index), knowledge
extraction (draft → verification → attach), and budget-pressure rolling
consolidation. Extracted from AppContext so the orchestrator reads as a
pipeline instead of a pile of domain details.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from typing import Any, Callable

from agent.adapters.base import BaseAdapter
from agent.storage.db import transaction

logger = logging.getLogger(__name__)

CONSOLIDATION_COOLDOWN_SECONDS = 600

# 高影响候选是要用户点头的：用一句人话说明「为什么值得你确认」，
# 而不是把内部类别枚举丢给界面。
HIGH_IMPACT_REASON = {
    "user_profile": "这会影响 QIO 对你的长期理解",
    "agent_self": "这会影响 QIO 对自己的认识",
    "goal": "这会影响 QIO 对你目标的判断",
}


class MemoryLifecycle:
    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        fragments,
        topics,
        index_builder,
        embedding=None,
        user_root_id: Callable[[], str],
        refresh_selector: Callable[[], None],
        # 增量入口：参数是 memory_index 行的 id。
        # 只处理变化的那一条，不再「每封一块就重建整个索引」。
        upsert_selector: Callable[[str], None] | None = None,
        remove_selector: Callable[[str], None] | None = None,
    ) -> None:
        self.conn = conn
        self.fragments = fragments
        self.topics = topics
        self.index_builder = index_builder
        self.embedding = embedding
        self.user_root_id = user_root_id
        self.refresh_selector = refresh_selector
        self.upsert_selector = upsert_selector
        self.remove_selector = remove_selector
        # 本轮新建的高影响候选：等回答完成后再由会话层提示用户
        self._pending_candidates: list[dict] = []

    def take_knowledge_candidates(self) -> list[dict]:
        """取走（并清空）待提示的高影响知识候选。"""
        pending, self._pending_candidates = self._pending_candidates, []
        return pending

    def _index_changed(self, entry: dict | None) -> None:
        """一条 memory index 记录写入之后的索引维护。

        优先走增量 upsert（只处理这一条）；调用方没有注入增量入口时，
        回退到旧的全量刷新，保证向后兼容。
        """
        if entry is None:
            self.refresh_selector()
            return
        index_id = entry.get("index_id")
        if self.upsert_selector is not None and index_id:
            self.upsert_selector(index_id)
        else:
            self.refresh_selector()

    # -- fragment close ---------------------------------------------------

    # -- 阶段 2：封存与派生分开 ------------------------------------------

    def seal_fragment(
        self, topic_id: str, *, reason: str = "capacity", tracer=None
    ) -> Any | None:
        """封存该话题的开放片段：**只做对话状态**，事务内完成、不等模型。

        摘要与索引作为派生任务登记到 `derived_tasks`，由执行器慢慢做：
        模型失败、进程退出都不会让「已经封存」这件事回退，也不会卡住对话。
        """
        from agent.services import derived_tasks

        fragment = self.fragments.open_fragment(topic_id)
        if fragment is None or fragment.start_message_id is None:
            return None
        messages = self.fragments.messages(fragment.id)
        if not messages:
            return None

        content_version = len(messages)
        with transaction(self.conn):
            # 封存与「派发派生任务」必须同一个事务：不能出现「封了但没人做摘要」
            # 或「派了任务但片段还开着」这两种半截状态。
            self.fragments.seal(
                fragment.id, reason=reason, content_version=content_version
            )
            derived_tasks.enqueue(
                self.conn, derived_tasks.KIND_SUMMARY, fragment.id, content_version
            )
        if tracer is not None:
            tracer.write("fragments_sealed", fragment.id)
        return self.fragments.get(fragment.id)

    async def run_summary_task(self, task, adapter: BaseAdapter, *, tracer=None) -> bool:
        """执行一条摘要派生任务。返回是否完成（失败会进可重试状态）。"""
        from agent.memory.summary import summarize_fragment
        from agent.services import derived_tasks

        fragment = self.fragments.get(task.fragment_id)
        if fragment is None:
            # 片段已经不存在（被清理）：任务没有意义了，判为完成，避免无限重试
            derived_tasks.complete(self.conn, task.id)
            return True

        messages = self.fragments.messages(fragment.id)
        if len(messages) != task.content_version:
            # 内容与派发时不一致：不拿这份结果去覆盖（迟到结果不得覆盖新内容）
            derived_tasks.fail(
                self.conn,
                task.id,
                f"内容已变化（任务记录 {task.content_version} 条，实际 {len(messages)} 条）",
            )
            return False

        summary, error = await summarize_fragment(adapter, [dict(m) for m in messages])
        if summary is None:
            # 摘要失败不使对话或导航失败：片段保持「已封存、无摘要」，
            # 原文仍可读（上下文里有预算受控的原文回退）。
            derived_tasks.fail(self.conn, task.id, error or "摘要模型不可用")
            return False

        # 再确认一次内容版本，然后在一个事务里写摘要 + 索引
        row = self.conn.execute(
            "SELECT content_version FROM fragments WHERE id = ?", (fragment.id,)
        ).fetchone()
        if row is None or int(row["content_version"]) != task.content_version:
            derived_tasks.fail(self.conn, task.id, "片段内容版本已经推进，结果作废")
            return False

        entity_ids: list[str] = []
        from agent.graph.edges import EdgeService
        from agent.graph.nodes import NodeService

        nodes = NodeService(self.conn)
        edges = EdgeService(self.conn)
        for name in summary.entities:
            entity, _created = nodes.mention(name, fragment.topic_id, force=False)
            if entity is not None:
                entity_ids.append(entity.id)
                edges.add(fragment.topic_id, entity.id, "mention")
        from agent.services.affinity import relate_shared_entities

        relate_shared_entities(self.conn, fragment.topic_id, entity_ids)

        with transaction(self.conn):
            self.conn.execute(
                "UPDATE fragments SET summary = ?, summary_model = ?, summary_version = 1 "
                "WHERE id = ? AND content_version = ?",
                (summary.summary, adapter.model, fragment.id, task.content_version),
            )
            entry = self.index_builder.build(
                fragment_id=fragment.id,
                topic_id=fragment.topic_id,
                title=summary.title,
                summary_text=summary.summary,
                entities=summary.entities,
                keywords=summary.keywords,
                message_texts=[m["content"] for m in messages],
            )
        self._index_changed(entry)
        if tracer is not None:
            tracer.write("summaries", f"{fragment.id}:{summary.title or ''}")

        # 实体卡与知识提炼：稳定来源 + 幂等 upsert，失败只记录（任务已算完成）
        try:
            await self._extract_entities_and_knowledge(adapter, messages, summary, fragment, entity_ids, tracer)
        except Exception:  # noqa: BLE001 - 派生数据失败不影响对话
            logger.warning("derived extraction failed", exc_info=True)

        derived_tasks.complete(self.conn, task.id)
        return True

    async def _extract_entities_and_knowledge(
        self, adapter, messages, summary, fragment, entity_ids: list[str], tracer=None
    ) -> None:
        from agent.entities.cards import EntityCardService
        from agent.entities.extract import extract_entity_cards

        try:
            candidates = await extract_entity_cards(adapter, [dict(m) for m in messages])
            card_svc = EntityCardService(self.conn)
            for cand in candidates:
                card = card_svc.upsert(cand)
                try:
                    if self.embedding is not None and hasattr(
                        self.embedding, "save_entity_card_vector"
                    ):
                        self.embedding.save_entity_card_vector(
                            card.id, f"{card.name}：{card.summary or ''}"
                        )
                except Exception:
                    pass
        except Exception:
            logger.warning("entity card extraction failed", exc_info=True)

        await self.extract_knowledge(
            adapter, summary, fragment.topic_id, entity_ids, fragment.id, tracer=tracer
        )

    async def drain_derived_tasks(
        self, adapter: BaseAdapter, *, limit: int = 3, tracer=None
    ) -> int:
        """把到期的派生任务做一轮。返回完成条数。"""
        from agent.services import derived_tasks

        done = 0
        for task in derived_tasks.claim_due(
            self.conn, limit=limit, kinds=(derived_tasks.KIND_SUMMARY,)
        ):
            try:
                if await self.run_summary_task(task, adapter, tracer=tracer):
                    done += 1
            except Exception as exc:  # noqa: BLE001 - 单条任务失败不能中断整批
                logger.warning("derived task failed", exc_info=True)
                derived_tasks.fail(self.conn, task.id, f"{type(exc).__name__}: {exc}")
        return done

    async def close_fragment(
        self, topic_id: str, adapter: BaseAdapter, tracer=None
    ) -> Any | None:
        """兼容入口：封存 + **立刻**把派生任务做完。

        新路径（编排器）只封存、不等待模型；直接调用它的地方
        （例如既有测试、离线维护）仍然拿到「封存并带摘要」的结果。
        """
        sealed = self.seal_fragment(topic_id, reason="capacity", tracer=tracer)
        if sealed is None:
            return None
        await self.drain_derived_tasks(adapter, limit=5, tracer=tracer)
        return self.fragments.get(sealed.id)

    # -- knowledge extraction --------------------------------------------

    async def extract_knowledge(
        self,
        adapter: BaseAdapter,
        summary: Any,
        topic_id: str,
        entity_ids: list[str],
        fragment_id: str,
        tracer=None,
    ) -> None:
        from agent.knowledge.lifecycle import HIGH_IMPACT_CATEGORIES, KnowledgeService
        from agent.knowledge.verify import VerificationService
        from agent.memory.summary import extract_knowledge_candidates

        extraction, error = await extract_knowledge_candidates(adapter, summary)
        if extraction is None:
            logger.info("knowledge extraction skipped: %s", error)
            return
        ks = KnowledgeService(self.conn)
        vs = VerificationService(self.conn, ks)
        for cand in extraction.candidates:
            node_ids: list[str] = []
            if cand.attach == "user":
                node_ids = [self.user_root_id()]
            elif cand.attach == "entity" and cand.entity:
                node, _ = self.topics.nodes.mention(cand.entity, topic_id, force=False)
                if node is not None:
                    node_ids = [node.id]
            else:
                node_ids = [topic_id]
            try:
                item = ks.create(
                    category=cand.category,
                    content=cand.content,
                    node_ids=node_ids,
                    provenance={"fragment_id": fragment_id},
                )
                if tracer is not None:
                    tracer.write("knowledge", item.id)
                ks.submit(item.id)
                if cand.category in HIGH_IMPACT_CATEGORIES:
                    # 留在待确认状态，并登记到「回答后在对话里自然确认」的队列
                    self._pending_candidates.append(
                        {
                            "knowledge_id": item.id,
                            "category": cand.category,
                            "content": cand.content,
                            "impact": "high",
                            "reason": HIGH_IMPACT_REASON.get(
                                cand.category, "这条知识会长期生效"
                            ),
                        }
                    )
                    continue  # stays draft; user confirmation required
                result = vs.review(ks.get(item.id), verified_by="system")
                if result.accepted:
                    ks.activate(item.id)
            except Exception as exc:  # extraction must not break the fragment close
                logger.warning("knowledge candidate failed: %s", exc)

    # -- budget-pressure consolidation -----------------------------------

    async def consolidate(self, topic_id: str, adapter: BaseAdapter) -> bool:
        """Rolling summary of the open fragment under budget pressure."""
        from agent.memory.summary import summarize_rolling

        fragment = self.fragments.get_or_create_open(topic_id)
        if fragment.start_message_id is None:
            return False
        meta = dict(fragment.meta)
        last = meta.get("consolidated_at")
        if last:
            try:
                last_ts = datetime.fromisoformat(last)
                if last_ts.tzinfo is None:
                    last_ts = last_ts.replace(tzinfo=timezone.utc)
                age = (datetime.now(timezone.utc) - last_ts).total_seconds()
                if age < CONSOLIDATION_COOLDOWN_SECONDS:
                    return False
            except ValueError:
                pass
        messages = self.fragments.messages(fragment.id)
        summary, error = await summarize_rolling(
            adapter, fragment.summary, [dict(m) for m in messages]
        )
        if summary is None:
            logger.info("consolidation skipped: %s", error)
            return False
        now = datetime.now(timezone.utc).isoformat()
        meta["consolidated"] = True
        meta["consolidated_at"] = now
        meta["consolidation_count"] = int(meta.get("consolidation_count", 0)) + 1
        with transaction(self.conn):
            self.conn.execute(
                "UPDATE fragments SET summary = ?, summary_model = ?, "
                "summary_version = summary_version + 1, meta = ? WHERE id = ?",
                (summary.summary, adapter.model, json.dumps(meta, ensure_ascii=False), fragment.id),
            )
            entry = self.index_builder.build(
                fragment_id=fragment.id,
                topic_id=topic_id,
                title=summary.title,
                summary_text=summary.summary,
                entities=summary.entities,
                keywords=summary.keywords,
                message_texts=[m["content"] for m in messages],
            )
        self._index_changed(entry)
        return True
