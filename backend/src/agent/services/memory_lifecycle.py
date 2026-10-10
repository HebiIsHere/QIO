"""MemoryLifecycle: post-turn memory/knowledge lifecycle.

Owns fragment close (summary + entities + entity cards + index), knowledge
extraction (draft → verification → attach), and budget-pressure rolling
consolidation. Extracted from AppContext so the orchestrator reads as a
pipeline instead of a pile of domain details.
"""

from __future__ import annotations

import asyncio
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
        self,
        topic_id: str,
        *,
        reason: str = "capacity",
        tracer=None,
        continue_same_stage: bool = False,
    ) -> Any | None:
        """封存该话题的开放片段：**只做对话状态**，事务内完成、不等模型。

        摘要与索引作为派生任务登记到 `derived_tasks`，由执行器慢慢做：
        模型失败、进程退出都不会让「已经封存」这件事回退，也不会卡住对话。

        `continue_same_stage=True`（容量分段）时，额外登记「下一段接这一段、
        同阶段延续」的待用信息 —— 新片段仍然在真有消息要写时才创建，
        但它的来源与同阶段标记不会丢（阶段 4）。
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
            if continue_same_stage:
                self.fragments.mark_continuation(
                    topic_id, fragment.id, same_stage=True, reason=reason
                )
        if tracer is not None:
            tracer.write("fragments_sealed", fragment.id)
        return self.fragments.get(fragment.id)

    async def run_summary_task(self, task, adapter: BaseAdapter, *, tracer=None) -> bool:
        """执行一条摘要派生任务。返回是否完成（失败会进可重试状态）。

        M05：摘要与实体提炼是**两条独立任务**（各自的内容版本、尝试次数、可读原因）。
        摘要失败不让实体那条跟着归零 —— 实体只依赖原文，原文本就在，所以摘要这条
        不可恢复时，实体任务照样登记并独立推进。
        """
        from agent.memory.model_output import NOTE_TRUNCATED_TEXT
        from agent.memory.summary import summarize_fragment_outcome
        from agent.services import derived_tasks

        generation = task.claim_generation
        fragment = self.fragments.get(task.fragment_id)
        if fragment is None:
            # 片段已经不存在（被清理）：任务没有意义了，判为完成，避免无限重试
            derived_tasks.complete(self.conn, task.id, expected_generation=generation)
            return True

        messages = self.fragments.messages(fragment.id)
        if len(messages) != task.content_version:
            # 内容与派发时不一致：不拿这份结果去覆盖（迟到结果不得覆盖新内容）
            derived_tasks.fail(
                self.conn,
                task.id,
                f"内容已变化（任务记录 {task.content_version} 条，实际 {len(messages)} 条）",
                expected_generation=generation,
            )
            return False

        outcome = await summarize_fragment_outcome(adapter, [dict(m) for m in messages])
        self._record_repairs(tracer, "摘要", outcome.notes)
        if outcome.value is None:
            # 摘要失败不使对话或导航失败：片段保持「已封存、无摘要」，原文仍可读。
            #
            # 失败隔离（M05）：实体提炼只依赖对话原文、不依赖摘要，所以这里把它作为
            # **独立任务**登记，由实体任务自己认领/重试/留痕 —— 摘要这条失败不再
            # 顺带决定实体的成败。
            reason = outcome.error or "摘要模型不可用"
            self._enqueue_entities_task(fragment.id, task.content_version)
            self._record_failure(tracer, "summary_derivation_failed", reason)
            derived_tasks.fail(self.conn, task.id, reason, expected_generation=generation)
            return False
        summary = outcome.value

        # 再确认一次内容版本，然后在一个事务里写摘要 + 索引
        row = self.conn.execute(
            "SELECT content_version FROM fragments WHERE id = ?", (fragment.id,)
        ).fetchone()
        if row is None or int(row["content_version"]) != task.content_version:
            derived_tasks.fail(
                self.conn,
                task.id,
                "片段内容版本已经推进，结果作废",
                expected_generation=generation,
            )
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

        # 降级标记写进片段数据本身（不只是 trace）：这段摘要是被本地修正过的，
        # 上层读数据时就能看见，而不必去翻 trace。
        meta = dict(fragment.meta or {})
        meta["summary_truncated"] = any(
            note.field == "summary" and note.code == NOTE_TRUNCATED_TEXT
            for note in outcome.notes
        )
        meta["summary_repairs"] = [note.render() for note in outcome.notes]

        with transaction(self.conn):
            self.conn.execute(
                "UPDATE fragments SET summary = ?, summary_model = ?, summary_version = 1, "
                "meta = ? WHERE id = ? AND content_version = ?",
                (
                    summary.summary,
                    adapter.model,
                    json.dumps(meta, ensure_ascii=False),
                    fragment.id,
                    task.content_version,
                ),
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
            # 知识提炼依赖摘要，所以摘要落库的同一个事务里登记它：
            # 不会出现「摘要写好了但知识任务没登记」的半截状态。
            derived_tasks.enqueue(
                self.conn, derived_tasks.KIND_KNOWLEDGE, fragment.id, task.content_version
            )
            # 实体提炼只依赖原文，登记在同一个事务里只是为了「摘要成功 ⇒ 实体任务一定
            # 在队列里」；它的成功/失败与这条摘要任务完全独立（M05）。
            derived_tasks.enqueue(
                self.conn, derived_tasks.KIND_ENTITIES, fragment.id, task.content_version
            )
        self._index_changed(entry)
        if tracer is not None:
            tracer.write("summaries", f"{fragment.id}:{summary.title or ''}")

        derived_tasks.complete(self.conn, task.id, expected_generation=generation)
        return True

    # -- 实体提炼（独立派生任务）------------------------------------------

    def _enqueue_entities_task(self, fragment_id: str, content_version: int) -> str:
        """幂等登记实体提炼任务（摘要失败路径 / 首次派发用）。"""
        from agent.services import derived_tasks

        task_id, _created = derived_tasks.enqueue(
            self.conn, derived_tasks.KIND_ENTITIES, fragment_id, content_version
        )
        return task_id

    def _mark_entities_extracted(self, fragment_id: str, content_version: int) -> None:
        """把「这一版原文已经提炼过实体」写进片段数据（幂等身份的一部分）。

        写卡片与写这个标记分两步：崩溃在两者之间时，重试会因为标记缺失而重跑一次
        提取，但卡片按字段合并（幂等），不会重复制造卡片。
        """
        row = self.conn.execute(
            "SELECT meta FROM fragments WHERE id = ?", (fragment_id,)
        ).fetchone()
        if row is None:
            return
        try:
            meta = json.loads(row["meta"] or "{}")
        except (TypeError, ValueError):
            meta = {}
        if not isinstance(meta, dict):
            meta = {}
        meta["entities_extracted_version"] = int(content_version)
        self.conn.execute(
            "UPDATE fragments SET meta = ? WHERE id = ?",
            (json.dumps(meta, ensure_ascii=False), fragment_id),
        )

    def _save_card_vector(self, card) -> None:
        """实体卡向量（best effort）：失败不影响卡片本身已经写好。"""
        if self.embedding is None or not hasattr(self.embedding, "save_entity_card_vector"):
            return
        try:
            self.embedding.save_entity_card_vector(
                card.id, f"{card.name}：{card.summary or ''}"
            )
        except Exception:  # noqa: BLE001 - 向量是加速手段，不是数据本体
            pass

    async def run_entities_task(self, task, adapter: BaseAdapter, *, tracer=None) -> bool:
        """执行一条实体提炼派生任务：**只依赖对话原文**，不依赖摘要（M05）。

        * 独立的状态行：state / attempts / last_error / 认领代次都在这条任务上，
          「摘要完成」不再等于「实体完成」；
        * 幂等：内容版本 + fragments.meta 的 entities_extracted_version 双重身份，
          重复 drain 不会重复调用模型；卡片写入走字段合并（同名卡不会重复创建）；
        * 迟到结果：发起模型调用前取一次卡修订版本快照，提交时按 `expected_revision`
          再次核对（不只是发起前核对），被用户改过的值一律保留。
        """
        from agent.entities.cards import EntityCardService
        from agent.entities.extract import (
            extract_entity_cards_outcome,
            prepare_candidates_for_commit,
        )
        from agent.services import derived_tasks

        generation = task.claim_generation
        fragment = self.fragments.get(task.fragment_id)
        if fragment is None:
            derived_tasks.complete(self.conn, task.id, expected_generation=generation)
            return True
        meta = dict(fragment.meta or {})
        if int(meta.get("entities_extracted_version") or 0) == int(task.content_version):
            # 这一版原文已经提炼过：任务被判完成（不重复调用模型、不重复写卡片）
            derived_tasks.complete(self.conn, task.id, expected_generation=generation)
            return True

        messages = self.fragments.messages(fragment.id)
        if len(messages) != task.content_version:
            derived_tasks.fail(
                self.conn,
                task.id,
                f"内容已变化（任务记录 {task.content_version} 条，实际 {len(messages)} 条）",
                expected_generation=generation,
            )
            return False

        card_svc = EntityCardService(self.conn)
        # 提交时核对修订版本：快照取自**模型调用之前**。
        snapshot = card_svc.revision_snapshot()
        outcome = await extract_entity_cards_outcome(adapter, [dict(m) for m in messages])
        self._record_repairs(tracer, "实体卡", outcome.notes)
        if outcome.value is None:
            reason = outcome.error or "实体卡提炼不可用"
            self._record_failure(tracer, "entity_card_extraction_failed", reason)
            derived_tasks.fail(self.conn, task.id, reason, expected_generation=generation)
            return False

        candidates, notes = prepare_candidates_for_commit(outcome.value)
        if notes:
            self._record_repairs(tracer, "实体卡", notes)
        for cand in candidates:
            card = card_svc.upsert(
                cand, source="auto", expected_revision=snapshot.get(cand.name)
            )
            if card is None:
                continue
            self._save_card_vector(card)
            if tracer is not None:
                tracer.write("entity_cards", f"{card.id}:{card.name}")
        self._mark_entities_extracted(fragment.id, int(task.content_version))
        derived_tasks.complete(self.conn, task.id, expected_generation=generation)
        return True

    def backfill_entity_tasks(self, *, limit: int = 2) -> int:
        """有边界补派（M05）：摘要已完成的**历史片段**如果缺实体任务，补登记。

        新代码在摘要落库时就会登记实体任务，所以这里命中的是升级前完成的片段。
        有边界 = 每次最多补 `limit` 条；幂等 = enqueue 按 (kind, 片段, 内容版本) 去重，
        补过一次就不会再补。补派走同一套自动合并规则：不覆盖人工修订、不制造重复卡片。
        """
        from agent.services import derived_tasks

        created = 0
        for fragment_id, version in derived_tasks.fragments_missing_entity_tasks(
            self.conn, limit=max(0, int(limit))
        ):
            _, is_new = derived_tasks.enqueue(
                self.conn, derived_tasks.KIND_ENTITIES, fragment_id, version
            )
            created += int(is_new)
        return created

    async def drain_derived_tasks(
        self, adapter: BaseAdapter, *, limit: int = 3, tracer=None, backfill_limit: int = 2
    ) -> int:
        """把到期的派生任务做一轮，返回完成条数（摘要 / 知识 / 实体各自计一条）。

        知识提炼依赖摘要，所以知识任务由摘要任务在落库的同一个事务里链式登记；
        实体任务同理（但**不依赖**摘要的成功）。第一轮跑完后再补认领一次，
        「封块后摘要/知识/实体都就绪」的既有语义不变；三种派生各有自己的任务行
        （状态 / attempts / 可读 last_error / 认领代次）。
        """
        from agent.services import derived_tasks

        self.backfill_entity_tasks(limit=backfill_limit)
        kinds = (
            derived_tasks.KIND_SUMMARY,
            derived_tasks.KIND_KNOWLEDGE,
            derived_tasks.KIND_ENTITIES,
        )
        done = await self._drain_batch(adapter, limit=limit, tracer=tracer, kinds=kinds)
        done += await self._drain_batch(
            adapter,
            limit=limit,
            tracer=tracer,
            kinds=(derived_tasks.KIND_KNOWLEDGE, derived_tasks.KIND_ENTITIES),
        )
        return done

    async def _drain_batch(
        self, adapter: BaseAdapter, *, limit: int, tracer, kinds: tuple[str, ...]
    ) -> int:
        from agent.services import derived_tasks

        done = 0
        for task in derived_tasks.claim_due(self.conn, limit=limit, kinds=kinds):
            try:
                runner = {
                    derived_tasks.KIND_KNOWLEDGE: self.run_knowledge_task,
                    derived_tasks.KIND_ENTITIES: self.run_entities_task,
                }.get(task.kind, self.run_summary_task)
                if await runner(task, adapter, tracer=tracer):
                    done += 1
            except asyncio.CancelledError:
                # 关闭 / 取消（M06）：不是失败 —— 过代次校验后放回队列，
                # 不留永久 running；被杀掉的这条任务的迟到结果会因代次不匹配被丢弃。
                derived_tasks.release(
                    self.conn,
                    task.id,
                    expected_generation=task.claim_generation,
                    reason="取消/关闭",
                )
                raise
            except Exception as exc:  # noqa: BLE001 - 单条任务失败不能中断整批
                logger.warning("derived task failed: %s", self._safe_exc(exc))
                derived_tasks.fail(
                    self.conn,
                    task.id,
                    f"{type(exc).__name__}: {exc}",
                    expected_generation=task.claim_generation,
                )
        return done

    async def run_knowledge_task(self, task, adapter: BaseAdapter, *, tracer=None) -> bool:
        """执行一条知识派生任务。返回是否完成（失败会进可重试状态）。"""
        from agent.services import derived_tasks

        generation = task.claim_generation
        fragment = self.fragments.get(task.fragment_id)
        if fragment is None:
            # 片段已经不存在（被清理）：任务没有意义了，判为完成，避免无限重试
            derived_tasks.complete(self.conn, task.id, expected_generation=generation)
            return True
        summary = self._summary_for_fragment(fragment)
        if summary is None:
            # 摘要还没落库：不是结构错误，等摘要任务完成后重试
            derived_tasks.fail(
                self.conn,
                task.id,
                "摘要尚未生成（知识提炼依赖摘要），稍后重试",
                expected_generation=generation,
            )
            return False
        error = await self.extract_knowledge(
            adapter, summary, fragment.topic_id, [], fragment.id, tracer=tracer
        )
        if error:
            derived_tasks.fail(self.conn, task.id, error, expected_generation=generation)
            return False
        derived_tasks.complete(self.conn, task.id, expected_generation=generation)
        return True

    def _summary_for_fragment(self, fragment) -> Any | None:
        """把已落库的摘要还原成知识提炼需要的 FragmentSummary。

        片段行只存摘要正文；标题 / 关键词在 memory_index，实体名在 nodes。
        摘要还没写就返回 None，让知识任务保持可重试。
        """
        from agent.memory.summary import (
            MAX_ENTITIES,
            MAX_KEYWORDS,
            MAX_SUMMARY,
            MAX_TITLE,
            FragmentSummary,
        )

        text = (fragment.summary or "").strip()
        if not text:
            return None
        row = self.conn.execute(
            "SELECT title, keywords, entity_ids FROM memory_index "
            "WHERE fragment_id = ? ORDER BY created_at DESC LIMIT 1",
            (fragment.id,),
        ).fetchone()
        title = (row["title"] if row is not None else None) or text[:MAX_TITLE]
        keywords: list[str] = []
        entities: list[str] = []
        if row is not None:
            try:
                keywords = [str(k) for k in json.loads(row["keywords"] or "[]")][
                    :MAX_KEYWORDS
                ]
            except (TypeError, ValueError):
                keywords = []
            try:
                ids = [str(i) for i in json.loads(row["entity_ids"] or "[]")]
            except (TypeError, ValueError):
                ids = []
            if ids:
                placeholders = ",".join("?" for _ in ids)
                entities = [
                    str(item["name"])
                    for item in self.conn.execute(
                        f"SELECT name FROM nodes WHERE id IN ({placeholders})", ids
                    ).fetchall()
                ]
        return FragmentSummary(
            title=title,
            summary=text[:MAX_SUMMARY],
            entities=entities[:MAX_ENTITIES],
            keywords=keywords,
        )

    async def close_fragment(
        self, topic_id: str, adapter: BaseAdapter, tracer=None
    ) -> Any | None:
        """兼容入口：封存 + **立刻**把派生任务做完。

        新路径（编排器）只封存、不等待模型；直接调用它的地方
        （例如既有测试、离线维护）仍然拿到「封存并带摘要」的结果。
        """
        sealed = self.seal_fragment(
            topic_id, reason="capacity", tracer=tracer, continue_same_stage=True
        )
        if sealed is None:
            return None
        await self.drain_derived_tasks(adapter, limit=5, tracer=tracer)
        return self.fragments.get(sealed.id)

    # -- 派生诊断 ----------------------------------------------------------

    @staticmethod
    def _record_repairs(tracer, scope: str, notes) -> None:
        """本地修正过什么必须留痕：可修正不等于可以不看见。

        只记录「修正类型 + 计数」，不落模型原文；trace 侧还会再过一次脱敏。
        """
        from agent.memory.model_output import render_notes

        if tracer is None or not notes:
            return
        detail = render_notes(notes)
        logger.info("%s派生修正：%s", scope, detail)
        tracer.write("derivation_repairs", f"{scope}:{detail}")

    @staticmethod
    def _safe_exc(exc: BaseException) -> str:
        """异常文本进日志前先过统一脱敏（同 _record_failure 的口径）。"""
        from agent.trace.redact import redact_text

        return redact_text(f"{type(exc).__name__}: {exc}")

    @staticmethod
    def _record_failure(tracer, code: str, message: str) -> None:
        """派生失败的统一诊断通道：日志 + trace warning。

        两个出口都过统一脱敏（agent.trace.redact）：失败原因可能夹带模型输入，
        而 AGENTS.md 的硬性约束是「日志 / Trace / 错误信息不得出现密钥原文」。
        """
        from agent.trace.redact import redact_text

        safe = redact_text(message)
        logger.warning("%s: %s", code, safe)
        if tracer is not None:
            tracer.warning(code, safe)

    # -- knowledge extraction --------------------------------------------

    async def extract_knowledge(
        self,
        adapter: BaseAdapter,
        summary: Any,
        topic_id: str,
        entity_ids: list[str],
        fragment_id: str,
        tracer=None,
    ) -> str | None:
        """抽取并落库知识候选。

        返回 None 表示成功；返回非空字符串是**可读失败原因**（同时已进 trace/日志），
        由调用方决定是重试（知识派生任务）还是只记录（摘要任务内联调用）。
        幂等：同一个片段已经写过的同内容候选会被跳过，任务重试不会重复制造知识。
        """
        from agent.knowledge.lifecycle import HIGH_IMPACT_CATEGORIES, KnowledgeService
        from agent.knowledge.verify import VerificationService
        from agent.memory.summary import extract_knowledge_candidates_outcome

        outcome = await extract_knowledge_candidates_outcome(adapter, summary)
        self._record_repairs(tracer, "知识抽取", outcome.notes)
        if outcome.value is None:
            # 知识抽取失败不影响已完成的摘要与索引，但**必须留痕**：
            # 失败原因要能在 trace 与日志里读出来，而不是悄悄没有知识条目。
            reason = outcome.error or "知识抽取不可用"
            self._record_failure(tracer, "knowledge_extraction_failed", reason)
            return reason
        ks = KnowledgeService(self.conn)
        vs = VerificationService(self.conn, ks)
        existing = self._knowledge_contents_for_fragment(fragment_id)
        for cand in outcome.value.candidates:
            if cand.content in existing:
                # 幂等：重试时不重复写同一条知识
                continue
            existing.add(cand.content)
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
                logger.warning("knowledge candidate failed: %s", self._safe_exc(exc))
        return None

    def _knowledge_contents_for_fragment(self, fragment_id: str) -> set[str]:
        """这个片段已经写过的知识内容（供任务重试时幂等跳过）。"""
        contents: set[str] = set()
        for row in self.conn.execute(
            "SELECT content, provenance FROM knowledge"
        ).fetchall():
            try:
                provenance = json.loads(row["provenance"] or "{}")
            except (TypeError, ValueError):
                provenance = {}
            if provenance.get("fragment_id") == fragment_id:
                contents.add(str(row["content"]))
        return contents

    # -- budget-pressure consolidation -----------------------------------

    async def consolidate(
        self, topic_id: str, adapter: BaseAdapter, tracer=None
    ) -> bool:
        """Rolling summary of the open fragment under budget pressure."""
        from agent.memory.model_output import NOTE_TRUNCATED_TEXT
        from agent.memory.summary import summarize_rolling_outcome

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
        outcome = await summarize_rolling_outcome(
            adapter, fragment.summary, [dict(m) for m in messages]
        )
        self._record_repairs(tracer, "滚动摘要", outcome.notes)
        if outcome.value is None:
            self._record_failure(
                tracer, "rolling_summary_failed", outcome.error or "滚动摘要不可用"
            )
            return False
        summary = outcome.value
        now = datetime.now(timezone.utc).isoformat()
        meta["consolidated"] = True
        meta["consolidated_at"] = now
        meta["consolidation_count"] = int(meta.get("consolidation_count", 0)) + 1
        # 与封块摘要同一口径：被本地截断/修正过就写进片段数据本身
        meta["summary_truncated"] = any(
            note.field == "summary" and note.code == NOTE_TRUNCATED_TEXT
            for note in outcome.notes
        )
        meta["summary_repairs"] = [note.render() for note in outcome.notes]
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
