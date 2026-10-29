"""MemoryLifecycle: post-turn memory/knowledge lifecycle.

Owns fragment close (summary + entities + entity cards + index), knowledge
extraction (draft → verification → attach), and budget-pressure rolling
consolidation. Extracted from AppContext so the orchestrator reads as a
pipeline instead of a pile of domain details.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import json
import logging
import sqlite3
from datetime import datetime, timezone
from typing import Any, Callable

from agent.adapters.base import BaseAdapter
from agent.services.usage_target import REASON_NO_CREDENTIAL, UsageUnavailable
from agent.storage.db import transaction

logger = logging.getLogger(__name__)

CONSOLIDATION_COOLDOWN_SECONDS = 600

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
        # 派生工作的「按 kind 用途配置」：**任务上下文隔离**（每个 asyncio 任务有自己的
        # 值），不是实例可变属性 —— 两个后台派生工作交错时不会互相覆盖，收尾也不会把
        # 别人的配置恢复成实例状态。放在实例上只是容器归属：不同 AppContext 不串值。
        self._task_adapters_ctx: contextvars.ContextVar[dict[str, Any] | None] = (
            contextvars.ContextVar(
                f"memory_lifecycle_task_adapters_{id(self)}", default=None
            )
        )

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
        from agent.memory.summary import summarize_messages_outcome
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

        run = await summarize_messages_outcome(
            adapter,
            [dict(m) for m in messages],
            input_version=int(task.content_version or 0),
        )
        outcome = run.outcome
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
        if not run.coverage.complete:
            # 覆盖不完整（任何分段 / 合并失败都走不到这里，防御性兜底）：
            # 不得把「只覆盖了一部分」的结果写成完整摘要。
            reason = "摘要未覆盖完整片段（coverage.complete=false）"
            self._enqueue_entities_task(fragment.id, task.content_version)
            self._record_failure(tracer, "summary_derivation_failed", reason)
            derived_tasks.fail(
                self.conn, task.id, reason, expected_generation=generation
            )
            return False
        summary = outcome.value

        # 降级标记写进片段数据本身（不只是 trace）：这段摘要是被本地修正过的，
        # 上层读数据时就能看见，而不必去翻 trace。
        meta = dict(fragment.meta or {})
        meta["summary_truncated"] = any(
            note.field == "summary" and note.code == NOTE_TRUNCATED_TEXT
            for note in outcome.notes
        )
        meta["summary_repairs"] = [note.render() for note in outcome.notes]
        # 覆盖情况随片段持久化：输入版本 / 覆盖消息 id / 可追溯分段 / 块数 /
        # 是否完整。失败路径不会写这个键（complete 永远不会被伪造成 true）。
        meta["summary_coverage"] = run.coverage.as_dict()

        from agent.graph.edges import EdgeService
        from agent.graph.nodes import NodeService
        from agent.services.affinity import relate_shared_entities

        nodes = NodeService(self.conn)
        edges = EdgeService(self.conn)

        # F05：领域写入（摘要 / 索引 / 实体关系 / 下游登记）与「任务完成」必须在
        # **同一个事务边界**内，事务开头用 derived_tasks.hold 核验
        # owner + claim_generation + state=running，并再次核对片段内容版本。
        # 丢了所有权的旧代任务在这里整体回滚：不覆盖摘要、不写索引、不加关联、
        # 不登记下游，也绝不会被误报为「正常完成」。
        # 模型调用已经在 with 之外完成 —— 不跨 await 持有写事务。
        try:
            with derived_tasks.hold(
                self.conn, task.id, expected_generation=generation
            ):
                row = self.conn.execute(
                    "SELECT content_version FROM fragments WHERE id = ?", (fragment.id,)
                ).fetchone()
                if row is None or int(row["content_version"]) != task.content_version:
                    raise derived_tasks.TaskSuperseded(
                        task.id, "片段内容版本已经推进，结果作废"
                    )

                entity_ids: list[str] = []
                for name in summary.entities:
                    entity, _created = nodes.mention(name, fragment.topic_id, force=False)
                    if entity is not None:
                        entity_ids.append(entity.id)
                        edges.add(fragment.topic_id, entity.id, "mention")
                relate_shared_entities(self.conn, fragment.topic_id, entity_ids)

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
                derived_tasks.complete(self.conn, task.id, expected_generation=generation)
        except derived_tasks.TaskSuperseded as exc:
            # 结果作废：一行都不写，也不报告完成。任务本身由「重排 / 取消」那一方
            # 负责（它已经把状态或代次改掉了），这里绝不回头改它的状态。
            logger.info("摘要派生结果作废：%s", self._safe_exc(exc))
            return False

        self._index_changed(entry)
        if tracer is not None:
            tracer.write("summaries", f"{fragment.id}:{summary.title or ''}")
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

        # F05：卡片写入、关系边、meta 标记与「任务完成」在同一个事务边界内，
        # 事务开头先核验 owner + claim_generation + state=running。
        # 丢了所有权的旧代任务整体回滚：不写卡、不加关系边、不写幂等标记，
        # 也不报告正常完成。模型调用已经在 with 之外（不跨 await 持写事务）。
        written: list = []
        conflicted = False
        try:
            with derived_tasks.hold(
                self.conn, task.id, expected_generation=generation
            ):
                # 归属核验通过之后、第一笔写入之前取「提交时快照」：
                # expected_revision 相对这个快照判定，模型调用期间发生的改动一律算迟到。
                commit_revisions = card_svc.current_revisions()
                for cand in candidates:
                    card = card_svc.upsert(
                        cand,
                        source="auto",
                        expected_revision=commit_revisions.get(cand.name),
                    )
                    if card is None:
                        # CAS 连续失败 / 卡不可用：这次提交被放弃了，不能谎报完成
                        conflicted = True
                        continue
                    written.append(card)
                self._mark_entities_extracted(fragment.id, int(task.content_version))
                derived_tasks.complete(self.conn, task.id, expected_generation=generation)
        except derived_tasks.TaskSuperseded as exc:
            logger.info("实体提炼结果作废：%s", self._safe_exc(exc))
            return False

        for card in written:
            # 向量是加速手段：放在事务提交之后，best effort，失败不影响卡片本身。
            self._save_card_vector(card)
            if tracer is not None:
                tracer.write("entity_cards", f"{card.id}:{card.name}")
        if conflicted:
            # 已有的提交被放弃语义：卡片保持原样，任务不带「这一版提炼过」的标记。
            return False
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
        self,
        adapter: BaseAdapter | None,
        *,
        adapters: dict[str, Any] | None = None,
        limit: int = 3,
        tracer=None,
        backfill_limit: int = 2,
    ) -> int:
        """把到期的派生任务做一轮，返回完成条数（摘要 / 知识 / 实体各自计一条）。

        知识提炼依赖摘要，所以知识任务由摘要任务在落库的同一个事务里链式登记；
        实体任务同理（但**不依赖**摘要的成功）。第一轮跑完后再补认领一次，
        「封块后摘要/知识/实体都就绪」的既有语义不变；三种派生各有自己的任务行
        （状态 / attempts / 可读 last_error / 认领代次）。

        ``adapters``（设置页改版 §8）：**按 kind** 的用途 adapter
        （``{"summary": a, "knowledge": b, "entities": c}``）。三种派生任务可以由用户
        分别指定不同模型，所以这里按 kind 取；没给的 kind 回落到 ``adapter``
        （旧调用点行为不变）。某个 kind 的用途不可用时，值是一个
        :class:`UsageUnavailable`，该条任务**如实失败并记录原因**，不偷偷改用主对话模型。

        没显式给 ``adapters`` 时，取**本任务上下文**里 ``use_adapters()` 冻结的那一份，
        并且在进入时**快照**下来：这项工作的所有批次都按同一份执行，不会被并行工作
        或同一任务里后续的 ``use_adapters()` 改写。
        """
        from agent.services import derived_tasks

        if adapters is None:
            adapters = self._task_adapters_ctx.get()

        self.backfill_entity_tasks(limit=backfill_limit)
        kinds = (
            derived_tasks.KIND_SUMMARY,
            derived_tasks.KIND_KNOWLEDGE,
            derived_tasks.KIND_ENTITIES,
        )
        done = await self._drain_batch(
            adapter, adapters=adapters, limit=limit, tracer=tracer, kinds=kinds
        )
        done += await self._drain_batch(
            adapter,
            adapters=adapters,
            limit=limit,
            tracer=tracer,
            kinds=(derived_tasks.KIND_KNOWLEDGE, derived_tasks.KIND_ENTITIES),
        )
        return done

    @contextlib.contextmanager
    def use_adapters(self, adapters: dict[str, Any] | None):
        """在一段派生工作里按 kind 分派 adapter（设置页改版 §8）。

        值存在**任务上下文**（contextvars.ContextVar），不是实例属性：
        每个后台协程（asyncio.Task 创建时复制上下文）只看得到自己那份，两个派生工作
        交错时不会互相覆盖；退出时只恢复本任务里的上一份，不把别人的配置写成实例状态。
        同一任务内嵌套使用仍是「内层覆盖、退出恢复外层」。

        语义等价于「这一段工作用这几把 adapter」；显式给
        `drain_derived_tasks(..., adapters=...)` 的调用方不需要本入口。
        """
        token = self._task_adapters_ctx.set(adapters)
        try:
            yield self
        finally:
            self._task_adapters_ctx.reset(token)

    @staticmethod
    def _task_adapter(task_kind: str, adapter: BaseAdapter | None, adapters: dict | None):
        """这条派生任务该用哪个 adapter（用途分配 → 主 adapter 兼容回落）。

        返回 adapter 实例，或 :class:`UsageUnavailable`（该用途当前不可用），
        或 None（连主 adapter 都没有）。

        兼容回落只发生在一种情况：用途解析报 **no_credential**，而调用方手里已经有
        adapter。那意味着「库里一条凭据都没有」（首次启动 / 单测注入主 adapter 的离线
        环境）—— 这时库里没有任何别的服务可选，沿用调用方给的 adapter 与基线一致。
        真有凭据但用途目标不可用时，用途解析报的是具体原因（停用 / 预算 / 显式指定
        不可用…），这里**不会**回落，那条任务会如实失败。
        """
        if adapters:
            mapped = adapters.get(task_kind)
            if isinstance(mapped, UsageUnavailable):
                if mapped.reason_code == REASON_NO_CREDENTIAL and adapter is not None:
                    return adapter
                return mapped
            if mapped is not None:
                return mapped
        return adapter

    async def _drain_batch(
        self,
        adapter: BaseAdapter | None,
        *,
        limit: int,
        tracer,
        kinds: tuple[str, ...],
        adapters: dict | None = None,
    ) -> int:
        from agent.services import derived_tasks

        # 用途配置只来自本次 drain 的显式参数（drain_derived_tasks 已在入口快照任务
        # 上下文里的那一份并显式传下来）；这里不读任何实例可变状态。
        active_adapters = adapters
        done = 0
        for task in derived_tasks.claim_due(self.conn, limit=limit, kinds=kinds):
            task_adapter = self._task_adapter(task.kind, adapter, active_adapters)
            if isinstance(task_adapter, UsageUnavailable):
                # 用途不可用：这条任务如实失败（原因可读、可重试），不借用别人的模型。
                derived_tasks.fail(
                    self.conn,
                    task.id,
                    f"{task_adapter.reason_code}：{task_adapter.message}",
                    expected_generation=task.claim_generation,
                )
                self._record_failure(
                    tracer,
                    f"{task.kind}_usage_unavailable",
                    f"{task_adapter.reason_code}：{task_adapter.message}",
                )
                continue
            if task_adapter is None:
                # 连主 adapter 都没有（没有可用凭据）：同样是可重试的失败，不假装完成。
                derived_tasks.fail(
                    self.conn,
                    task.id,
                    "no_credential：没有可用于这条派生任务的模型凭据",
                    expected_generation=task.claim_generation,
                )
                continue
            try:
                runner = {
                    derived_tasks.KIND_KNOWLEDGE: self.run_knowledge_task,
                    derived_tasks.KIND_ENTITIES: self.run_entities_task,
                }.get(task.kind, self.run_summary_task)
                if await runner(task, task_adapter, tracer=tracer):
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
        # 输入给模型的真实来源目录（证据 id 只能来自这里；不编造来源）。
        evidence_catalog = self._evidence_catalog(fragment)
        # 模型抽取放在任何写事务之外；落库与「任务完成」再进同一个事务边界。
        extraction, error = await self.extract_knowledge_candidates(
            adapter, summary, evidence_catalog=evidence_catalog, tracer=tracer
        )
        if error:
            derived_tasks.fail(self.conn, task.id, error, expected_generation=generation)
            return False
        try:
            # F05：知识条目（含 submit / verify / activate 与相关节点）与「任务完成」
            # 在同一个事务边界内；事务开头先核验 owner + claim_generation + state=running。
            # 丢了所有权的旧代任务整体回滚：不写知识、不建关联节点，也不报告正常完成。
            with derived_tasks.hold(
                self.conn, task.id, expected_generation=generation
            ):
                high_impact = self.commit_knowledge(
                    extraction,
                    topic_id=fragment.topic_id,
                    entity_ids=[],
                    fragment_id=fragment.id,
                    evidence_catalog=evidence_catalog,
                    tracer=tracer,
                )
                derived_tasks.complete(self.conn, task.id, expected_generation=generation)
        except derived_tasks.TaskSuperseded as exc:
            logger.info("知识提炼结果作废：%s", self._safe_exc(exc))
            return False
        # 事务已提交，才把这些高影响候选登记到「回答后自然确认」队列：
        # 回滚时它们不能留在内存队列里指向不存在的知识条目。
        self._pending_candidates.extend(high_impact)
        return True

    def _summary_for_fragment(self, fragment) -> Any | None:
        """把已落库的摘要还原成知识提炼需要的 FragmentSummary。

        片段行只存摘要正文；标题 / 关键词在 memory_index，实体名在 nodes。
        摘要还没写就返回 None，让知识任务保持可重试。

        **存取路径不再二次截断**（Phase 1）：生成侧的上限仍在（模型输出先由
        memory.limits 收敛），但已经存进来的摘要正文必须原样读回，不能在这里
        再按 MAX_SUMMARY 砍一刀。因此用 model_construct 跳过 pydantic 的
        「生成长度」校验 —— 那是生成契约，不是存取契约。
        """
        from agent.memory.contract import FragmentSummary
        from agent.memory.limits import MAX_ENTITIES, MAX_KEYWORDS, MAX_TITLE

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
        return FragmentSummary.model_construct(
            title=title,
            summary=text,
            entities=entities[:MAX_ENTITIES],
            keywords=keywords,
        )

    async def close_fragment(
        self,
        topic_id: str,
        adapter: BaseAdapter | None,
        tracer=None,
        *,
        adapters: dict[str, Any] | None = None,
    ) -> Any | None:
        """兼容入口：封存 + **立刻**把派生任务做完。

        新路径（编排器）只封存、不等待模型；直接调用它的地方
        （例如既有测试、离线维护）仍然拿到「封存并带摘要」的结果。
        ``adapters`` 是把三种派生分开用不同用途 adapter 的入口（见 drain_derived_tasks）。
        """
        sealed = self.seal_fragment(
            topic_id, reason="capacity", tracer=tracer, continue_same_stage=True
        )
        if sealed is None:
            return None
        await self.drain_derived_tasks(
            adapter, adapters=adapters, limit=5, tracer=tracer
        )
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
        """抽取并落库知识候选（兼容入口：抽取 + 立即落库）。

        返回 None 表示成功；返回非空字符串是**可读失败原因**（同时已进 trace/日志），
        由调用方决定是重试（知识派生任务）还是只记录（摘要任务内联调用）。
        幂等：同一个片段已经写过的同内容候选会被跳过，任务重试不会重复制造知识。

        需要「域写入与任务完成同一事务」的调用方（`run_knowledge_task`）请用
        :meth:`extract_knowledge_candidates` + :meth:`commit_knowledge`：本入口的
        落库**不在**任何所有权核验之内，只适合「已经在事务里、或没有任何任务所有权」
        的调用点。
        """
        extraction, error = await self.extract_knowledge_candidates(
            adapter, summary, tracer=tracer
        )
        if error:
            return error
        high_impact = self.commit_knowledge(
            extraction,
            topic_id=topic_id,
            entity_ids=entity_ids,
            fragment_id=fragment_id,
            tracer=tracer,
        )
        self._pending_candidates.extend(high_impact)
        return None

    async def extract_knowledge_candidates(
        self,
        adapter: BaseAdapter,
        summary: Any,
        *,
        evidence_catalog: list[dict] | None = None,
        tracer=None,
    ):
        """**只调用模型**：抽取知识候选，不碰数据库。

        Phase 1 起委托 C 的 agent.knowledge.derivation.extract_candidates（冻结签名），
        并传入真实来源目录 evidence_catalog。返回 `(提取结果 | None, 可读失败原因)`；
        任何领域写入由 :meth:`commit_knowledge` 在调用方指定的事务边界内完成 ——
        这样「模型调用不跨写事务」与「写入前后核验所有权」可以同时成立（F05）。
        """
        from agent.knowledge.derivation import extract_candidates

        extraction, error = await extract_candidates(
            adapter,
            summary=summary,
            evidence_catalog=list(evidence_catalog or []),
            tracer=tracer,
        )
        if extraction is None or error:
            reason = error or "知识抽取不可用"
            self._record_failure(tracer, "knowledge_extraction_failed", reason)
            return None, reason
        return extraction, None

    def commit_knowledge(
        self,
        extraction: Any,
        *,
        topic_id: str,
        entity_ids: list[str],
        fragment_id: str,
        evidence_catalog: list[dict] | None = None,
        tracer=None,
    ) -> list[dict]:
        """把抽取结果落库。**调用方负责事务边界与所有权核验**（F05）。

        Phase 1 起委托 C 的 agent.knowledge.derivation.commit_candidates（冻结签名），
        入参包含真实来源目录 evidence_catalog。返回本轮新建的高影响候选（由调用方
        在事务提交后再登记到待确认队列 —— 事务回滚时它们不能留在内存队列里指向
        不存在的知识条目）。
        """
        from agent.knowledge.derivation import commit_candidates

        return list(
            commit_candidates(
                self.conn,
                extraction,
                topic_id=topic_id,
                entity_ids=entity_ids,
                fragment_id=fragment_id,
                evidence_catalog=list(evidence_catalog or []),
                tracer=tracer,
            )
        )

    def _evidence_catalog(self, fragment) -> list[dict]:
        """后台知识提炼的真实来源目录（输入给模型的 evidence 清单）。

        条目形状（与 C 的 derivation 约定）：{"id": "真实 id", "kind":
        "message|fragment|summary|entity|knowledge", "label": "简短说明"}。
        只列程序拥有的真实 id：片段 / 该片段每条消息 / 摘要索引 / 既有实体 /
        该片段已有知识；不编造来源，不从模型正文里猜 id。
        """
        catalog: list[dict] = [
            {"id": str(fragment.id), "kind": "fragment", "label": "待提炼的交流片段"}
        ]
        for row in self.conn.execute(
            "SELECT id, role, content FROM messages WHERE fragment_id = ? ORDER BY rowid",
            (fragment.id,),
        ).fetchall():
            preview = str(row["content"] or "").strip().replace("\n", " ")[:80]
            catalog.append(
                {
                    "id": str(row["id"]),
                    "kind": "message",
                    "label": f"{row['role']} 消息：{preview}",
                }
            )
        index_row = self.conn.execute(
            "SELECT id, title, entity_ids FROM memory_index WHERE fragment_id = ? "
            "ORDER BY created_at DESC LIMIT 1",
            (fragment.id,),
        ).fetchone()
        entity_ids: list[str] = []
        if index_row is not None:
            catalog.append(
                {
                    "id": str(index_row["id"]),
                    "kind": "summary",
                    "label": f"摘要索引：{index_row['title'] or ''}".strip(),
                }
            )
            try:
                entity_ids = [str(i) for i in json.loads(index_row["entity_ids"] or "[]")]
            except (TypeError, ValueError):
                entity_ids = []
        if entity_ids:
            placeholders = ",".join("?" for _ in entity_ids)
            for row in self.conn.execute(
                f"SELECT id, name FROM nodes WHERE id IN ({placeholders})", entity_ids
            ).fetchall():
                catalog.append(
                    {
                        "id": str(row["id"]),
                        "kind": "entity",
                        "label": f"既有实体：{row['name']}",
                    }
                )
        for row in self.conn.execute("SELECT id, content, provenance FROM knowledge").fetchall():
            try:
                provenance = json.loads(row["provenance"] or "{}")
            except (TypeError, ValueError):
                provenance = {}
            if provenance.get("fragment_id") != fragment.id:
                continue
            preview = str(row["content"] or "").strip().replace("\n", " ")[:60]
            catalog.append(
                {
                    "id": str(row["id"]),
                    "kind": "knowledge",
                    "label": f"已有知识：{preview}",
                }
            )
        return catalog

    # -- budget-pressure consolidation -----------------------------------

    async def consolidate(
        self, topic_id: str, adapter: BaseAdapter, tracer=None
    ) -> bool:
        """Rolling summary of the open fragment under budget pressure.

        Phase 1：只处理**未覆盖的新消息**（覆盖游标见迁移 44），摘要写入与游标
        推进在同一个事务里；没有新消息时幂等返回、不调用模型；崩溃 / 取消时
        游标不前进（原文保留、可重试）。
        """
        from agent.memory.model_output import NOTE_TRUNCATED_TEXT
        from agent.memory.rolling import (
            rolling_view,
            save_state,
            summarize_rolling_outcome,
        )

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
        # 只取未覆盖的新消息：已覆盖的原文不再重复喂给模型，未覆盖的也不跳过。
        view = rolling_view(self.conn, fragment)
        messages = list(view.uncovered)
        if not messages:
            # 同一输入重跑幂等：没有新消息就不重复调用模型、不动游标。
            return False
        outcome = await summarize_rolling_outcome(adapter, view.summary, messages)
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
        # 摘要写入 + 游标推进 + 索引在同一个事务边界内：要么一起前进，要么都不写。
        with transaction(self.conn):
            advanced = save_state(
                self.conn,
                fragment.id,
                summary=summary.summary,
                summary_model=adapter.model,
                meta=meta,
                covered_message_id=str(messages[-1]["id"]),
                updated_at=now,
            )
            if not advanced:
                # 覆盖位置没有前进（迟到 / 并发）：不写入、不建索引。
                return False
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
