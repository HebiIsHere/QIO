"""维护执行入口（B）：后台维护批次 / 独立回答 / 恢复（契约 §12.4）。

模型调用规则：

* system = structured_task_system(MAINTENANCE_SYSTEM, schema=维护输出 schema,
  limits=MAINTENANCE_LIMITS) + 一条独立 JSON user 数据（json.dumps）；
* 不注入聊天回答标记，不拼接主对话上下文 / 消息历史 / 摘要；
* 输出先本地校验；失败最多一次有依据的修复（原输出 + 字段错误作为数据传入）；
* 仍失败则整轮失败（ok=False），不写任何修改，不推进处理位置。

执行：通过校验后逐条执行（每条一个事务）；merge / normalize / ask 的领域规则在
actions.py，独立回答在 questions.py，恢复在 recovery.py。
"""

from __future__ import annotations

import json
import logging
import sqlite3
from typing import Any, Callable, Mapping, Sequence

from agent.adapters.base import ChatMessage
from agent import prompts
from agent.knowledge.lifecycle import KnowledgeItem, KnowledgeService, resolve_current
from agent.maintenance import actions, models, questions, recovery, store
from agent.maintenance.models import (
    MAINTENANCE_LIMITS,
    MAX_KNOWLEDGE_CHARS,
    MaintenanceAnswerOutcome,
    MaintenanceRunOutcome,
    MaintenanceSnapshot,
    maintenance_output_schema,
    validate_maintenance_output,
)

logger = logging.getLogger(__name__)

#: 结构化任务的采样温度（与既有知识抽取一致，避免维护输出过度发散）。
_TEMPERATURE = 0.2
#: 进入维护上下文的相关旧知识上限（变化条数上限之外再设一道上下文预算）。
_MAX_CONTEXT_KNOWLEDGE = 60


def _safe_error(exc: BaseException) -> str:
    from agent.trace.redact import redact_text

    return redact_text(f"{type(exc).__name__}: {exc}")


def _trim_content(text: str) -> str:
    """数据层裁剪：先裁对象再序列化，不截断序列化后的 JSON。"""
    value = str(text or "")
    return value if len(value) <= MAX_KNOWLEDGE_CHARS else value[:MAX_KNOWLEDGE_CHARS]


async def run_structured_task(
    adapter: Any,
    *,
    prompt_key: str,
    schema: Mapping[str, Any],
    limits: Mapping[str, Any] | None,
    data: Mapping[str, Any],
    validate: Callable[[str], tuple[Any, list[str]]],
) -> tuple[Any, list[str], dict[str, Any]]:
    """一次结构化任务调用 + 失败时最多一次有依据的修复。

    返回 (解析结果 | None, 最后字段错误, meta)。异常（模型 / 网络）原样抛出，
    由调用方决定「保留原数据、不推进位置」。
    """
    user_text = json.dumps(dict(data), ensure_ascii=False, sort_keys=True)
    system_text = prompts.structured_task_system(
        prompt_key, schema=schema, limits=limits
    )
    messages = [
        ChatMessage(role="system", content=system_text),
        ChatMessage(role="user", content=user_text),
    ]
    completion = await adapter.complete(messages, tools=[], temperature=_TEMPERATURE)
    raw = (getattr(completion.message, "content", None) or "")
    value, errors = validate(raw)
    if value is not None:
        return value, [], {"attempts": 1, "repair": False}
    repair_data = json.dumps(
        {"previous_output": raw, "field_errors": errors},
        ensure_ascii=False,
        sort_keys=True,
    )
    repair_system = prompts.structured_task_system(
        prompt_key,
        schema=schema,
        limits=limits,
        repair=True,
        extra_sections=[repair_data],
    )
    repair_messages = [
        ChatMessage(role="system", content=repair_system),
        ChatMessage(role="user", content=user_text),
    ]
    repair_completion = await adapter.complete(
        repair_messages, tools=[], temperature=_TEMPERATURE
    )
    raw_repair = (getattr(repair_completion.message, "content", None) or "")
    value, errors = validate(raw_repair)
    return value, errors, {"attempts": 2, "repair": True}


def _trigger_value(snapshot: MaintenanceSnapshot) -> str:
    trigger = getattr(snapshot, "trigger", "")
    return str(getattr(trigger, "value", trigger))


def _snapshot_payload(snapshot: MaintenanceSnapshot) -> dict[str, Any]:
    return {
        "batch_seq": int(getattr(snapshot, "batch_seq", 0) or 0),
        "trigger": _trigger_value(snapshot),
        "change_seq_max": int(getattr(snapshot, "change_seq_max", 0) or 0),
        "pending_change_count": int(getattr(snapshot, "pending_change_count", 0) or 0),
        "waiting_turns": int(getattr(snapshot, "waiting_turns", 0) or 0),
        "change_ids": list(getattr(snapshot, "change_ids", ()) or ()),
        "knowledge_ids": list(getattr(snapshot, "knowledge_ids", ()) or ()),
        "limits": dict(getattr(snapshot, "limits", None) or MAINTENANCE_LIMITS),
    }


def _knowledge_entry(item: KnowledgeItem) -> dict[str, Any]:
    provenance = dict(item.provenance or {})
    return {
        "id": item.id,
        "version": int(item.version),
        "chain_id": item.chain_id,
        "content": _trim_content(item.content),
        "category": item.category,
        "node_ids": list(item.node_ids),
        "entity_ids": list(item.entity_ids),
        "epistemic_status": provenance.get("epistemic_status"),
        "source": provenance.get("source"),
        "state": item.state.value,
        "evidence_ids": list(provenance.get("evidence_ids") or []),
    }


def _ordered_unique(values: Sequence[str]) -> list[str]:
    out: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in out:
            out.append(text)
    return out


class MaintenanceRunner:
    """B 的维护执行器（A 的调度器调用 run_batch；C 的 API 调用回答 / 恢复）。"""

    def __init__(self, ctx: Any) -> None:
        self.ctx = ctx

    @property
    def conn(self) -> sqlite3.Connection:
        return self.ctx.conn

    async def _build_adapter(self) -> Any:
        # 设置页改版 §8：知识维护批次走「知识提炼」用途自己的模型。
        # 没有该用途可用目标时返回 None，调用方按「没有可用模型适配器」如实失败
        # （不改用主对话模型偷偷跑完 —— 那会绕过用户的用途分配）。
        # build_background_adapter 只在「库里一条凭据都没有」时保持基线（见其文档）。
        builder = getattr(self.ctx, "build_background_adapter", None)
        if callable(builder):
            return await builder("knowledge-distill")
        builder = getattr(self.ctx, "build_adapter_for_usage", None)
        if callable(builder):
            return await builder("knowledge-distill")
        legacy = getattr(self.ctx, "build_adapter", None)
        if legacy is None:
            return None
        return await legacy()

    # -- 输入组装 ---------------------------------------------------------

    def _build_input(
        self,
        snapshot: MaintenanceSnapshot,
        limits: Mapping[str, int],
    ) -> tuple[dict[str, Any], dict[str, dict[str, Any]], set[str], dict[str, Any]]:
        """组装维护输入（只含数据；不读主对话消息 / 摘要 / 工具历史）。"""
        conn = self.conn
        max_changes = int(limits.get("max_batch_changes") or models.MAX_BATCH_CHANGES)
        changes = store.load_pending_changes(
            conn,
            change_ids=getattr(snapshot, "change_ids", ()) or (),
            change_seq_max=int(getattr(snapshot, "change_seq_max", 0) or 0),
            limit=max_changes,
        )
        svc = KnowledgeService(conn)
        changed_ids = _ordered_unique(
            [change["knowledge_id"] for change in changes]
            + list(getattr(snapshot, "knowledge_ids", ()) or ())
        )
        changed_items: list[KnowledgeItem] = []
        seen: set[str] = set()
        for knowledge_id in changed_ids:
            current = resolve_current(conn, knowledge_id)
            if current is None or current.id in seen:
                continue
            seen.add(current.id)
            changed_items.append(current)
        related: list[KnowledgeItem] = []
        if changed_items:
            scope_nodes = {
                str(node) for item in changed_items for node in item.node_ids
            }
            categories = {item.category for item in changed_items}
            rows = conn.execute(
                "SELECT id FROM knowledge WHERE state = 'active'"
                " ORDER BY updated_at DESC LIMIT 500"
            ).fetchall()
            for row in rows:
                if len(changed_items) + len(related) >= _MAX_CONTEXT_KNOWLEDGE:
                    break
                item = svc.get(str(row["id"]))
                if item is None or item.id in seen:
                    continue
                if item.category not in categories:
                    continue
                if not (set(str(node) for node in item.node_ids) & scope_nodes):
                    continue
                related.append(item)
                seen.add(item.id)
        knowledge_items = changed_items + related
        entries = [_knowledge_entry(item) for item in knowledge_items]
        allowed_knowledge = {
            entry["id"]: {
                "version": int(entry["version"]),
                "chain_id": str(entry["chain_id"] or ""),
            }
            for entry in entries
        }
        evidence: list[dict[str, Any]] = []
        for change in changes:
            evidence.append(
                {
                    "id": f"chg:{change['seq']}",
                    "kind": "change",
                    "label": (
                        f"待处理变化 seq={change['seq']} knowledge={change['knowledge_id']}"
                    ),
                }
            )
        for entry in entries:
            evidence.append(
                {
                    "id": entry["id"],
                    "kind": "knowledge",
                    "label": f"知识 v{entry['version']}：{entry['content'][:60]}",
                }
            )
        allowed_evidence = {item["id"] for item in evidence}
        open_questions = []
        for item in store.list_open_questions(conn, limit=10):
            open_questions.append(
                {
                    "id": item["id"],
                    "question": item["question"],
                    "knowledge_ids": [
                        {
                            "id": entry.get("id"),
                            "version": entry.get("version"),
                            "chain_id": entry.get("chain_id"),
                        }
                        for entry in item.get("knowledge_ids") or []
                        if isinstance(entry, dict)
                    ],
                    "status": item["status"],
                }
            )
        data = {
            "trigger": _trigger_value(snapshot),
            "batch_seq": int(getattr(snapshot, "batch_seq", 0) or 0),
            "change_seq_max": int(getattr(snapshot, "change_seq_max", 0) or 0),
            "pending_changes": [
                {
                    "id": f"chg:{change['seq']}",
                    "knowledge_id": change["knowledge_id"],
                    "chain_id": change["chain_id"],
                    "version": change["version"],
                    "source": change["source"],
                    "created_turn_seq": change["created_turn_seq"],
                }
                for change in changes
            ],
            "knowledge": entries,
            "open_questions": open_questions,
            "evidence": evidence,
            "limits": dict(limits),
        }
        meta = {
            "pending_changes": len(changes),
            "knowledge": len(entries),
            "related_knowledge": len(related),
            "open_questions": len(open_questions),
        }
        return data, allowed_knowledge, allowed_evidence, meta

    # -- 批次执行 ---------------------------------------------------------

    async def run_batch(self, snapshot: MaintenanceSnapshot) -> MaintenanceRunOutcome:
        conn = self.conn
        limits = dict(getattr(snapshot, "limits", None) or MAINTENANCE_LIMITS)
        batch_id = store.create_batch(
            conn,
            trigger_reason=_trigger_value(snapshot),
            snapshot=_snapshot_payload(snapshot),
            model=None,
        )
        try:
            adapter = await self._build_adapter()
            if adapter is None:
                raise RuntimeError("没有可用模型适配器")
            store.set_batch_model(conn, batch_id, getattr(adapter, "model", None))
            data, allowed_knowledge, allowed_evidence, input_meta = self._build_input(
                snapshot, limits
            )
            output, errors, meta = await run_structured_task(
                adapter,
                prompt_key="MAINTENANCE_SYSTEM",
                schema=maintenance_output_schema(),
                limits=limits,
                data=data,
                validate=validate_maintenance_output,
            )
            if output is None:
                reason = "维护输出校验失败（已尝试一次修复）：" + "；".join(errors[:5])
                store.finish_batch(
                    conn,
                    batch_id,
                    status=models.BatchStatus.FAILED.value,
                    stats={
                        "attempts": meta.get("attempts", 0),
                        "field_errors": errors[:10],
                        "input": input_meta,
                        "applied": 0,
                        "asked": 0,
                        "rejected": 0,
                    },
                    error=reason[:2000],
                )
                return MaintenanceRunOutcome(
                    ok=False,
                    status=models.BatchStatus.FAILED.value,
                    batch_id=batch_id,
                    error=reason[:2000],
                    detail={
                        "attempts": meta.get("attempts", 0),
                        "field_errors": errors[:10],
                    },
                )
            executed: list[actions.CandidateOutcome] = []
            for candidate in output.candidates:
                outcome = actions.execute_candidate(
                    conn,
                    candidate,
                    batch_id=batch_id,
                    allowed_knowledge=allowed_knowledge,
                    allowed_evidence=allowed_evidence,
                )
                executed.append(outcome)
            applied = [item for item in executed if item.applied]
            asked = [
                item
                for item in executed
                if item.asked and not item.detail.get("duplicate_question")
            ]
            duplicates = [
                item for item in executed if item.detail.get("duplicate_question")
            ]
            rejected = [item for item in executed if item.rejected]
            failed = [item for item in executed if item.failed]
            stats = {
                "candidates": len(executed),
                "applied": len(applied),
                "asked": len(asked),
                "rejected": len(rejected),
                "failed": len(failed),
                "duplicate_questions": len(duplicates),
                "attempts": meta.get("attempts", 0),
                "repair": bool(meta.get("repair")),
                "input": input_meta,
                "candidates_detail": [
                    {
                        "status": item.status,
                        "item_id": item.item_id,
                        "result_id": item.result_id,
                        "reason": item.reason[:300],
                    }
                    for item in executed
                ],
            }
            detail = {
                "candidates": len(executed),
                "duplicate_questions": len(duplicates),
                "attempts": meta.get("attempts", 0),
                "repair": bool(meta.get("repair")),
                "input": input_meta,
                "candidate_results": [
                    {
                        "status": item.status,
                        "item_id": item.item_id,
                        "result_id": item.result_id,
                    }
                    for item in executed
                ],
            }
            if failed:
                # 存储基础设施失败：整轮失败、不推进处理位置；已落地项如实计入 detail。
                reason = "维护执行失败（已写入的条目见 stats）：" + "；".join(
                    (item.error or item.reason or "unknown")[:200] for item in failed[:3]
                )
                store.finish_batch(
                    conn,
                    batch_id,
                    status=models.BatchStatus.FAILED.value,
                    stats=stats,
                    error=reason[:2000],
                )
                return MaintenanceRunOutcome(
                    ok=False,
                    status=models.BatchStatus.FAILED.value,
                    batch_id=batch_id,
                    applied=len(applied),
                    asked=len(asked),
                    rejected=len(rejected),
                    error=reason[:2000],
                    detail=detail,
                )
            status = (
                models.BatchStatus.COMPLETED.value
                if not rejected
                else models.BatchStatus.PARTIAL.value
            )
            store.finish_batch(conn, batch_id, status=status, stats=stats)
            return MaintenanceRunOutcome(
                ok=True,
                status=status,
                batch_id=batch_id,
                processed_change_seq=int(getattr(snapshot, "change_seq_max", 0) or 0),
                applied=len(applied),
                asked=len(asked),
                rejected=len(rejected),
                detail=detail,
            )
        except Exception as exc:  # noqa: BLE001 - 失败保留原数据、不推进处理位置
            logger.warning("维护批次失败", exc_info=True)
            reason = _safe_error(exc)
            try:
                store.finish_batch(
                    conn,
                    batch_id,
                    status=models.BatchStatus.FAILED.value,
                    stats={"input": _snapshot_payload(snapshot)},
                    error=reason[:2000],
                )
            except Exception:  # noqa: BLE001
                logger.warning("维护失败状态记录失败", exc_info=True)
            return MaintenanceRunOutcome(
                ok=False,
                status=models.BatchStatus.FAILED.value,
                batch_id=batch_id,
                error=reason[:2000],
            )

    # -- 独立回答 / 恢复 --------------------------------------------------

    async def answer_question(
        self, item_id: str, answer: str
    ) -> MaintenanceAnswerOutcome:
        return await questions.answer_question(self.ctx, item_id, answer)

    async def preview_restore(
        self, *, batch_id: str | None = None, item_id: str | None = None
    ) -> dict[str, Any]:
        return recovery.preview_restore(self.conn, batch_id=batch_id, item_id=item_id)

    async def restore(
        self, *, batch_id: str | None = None, item_id: str | None = None
    ) -> dict[str, Any]:
        return recovery.restore(self.conn, batch_id=batch_id, item_id=item_id)
