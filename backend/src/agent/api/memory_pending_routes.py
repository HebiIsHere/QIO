# -*- coding: utf-8 -*-
"""「记忆待确认」入口、维护记录与安全恢复 API（契约 §12.5，C 主责）。

冻结路由（前缀 /api/memory）：

    GET  /api/memory/pending?status=&kind=&limit=
         -> {"items": [PendingItemView...], "total", "truncated",
             "maintenance_available", "warnings"}
    POST /api/memory/pending/{id}/answer        body {"answer": str}
         -> MaintenanceAnswerOutcome 字段 + {"item": 最新事项}
    POST /api/memory/pending/{id}/defer
         -> {"ok": true, "item": 最新事项}
    GET  /api/memory/maintenance/batches?limit=
         -> {"batches": [...], "total", "truncated", "available"}
    GET  /api/memory/maintenance/batches/{id}
         -> {"batch": {...}, "items": [...], "operations": [...], "available"}
    GET  /api/memory/maintenance/restore/preview?batch_id=|item_id=
         -> {"ok": true, "preview": runner.preview_restore(...)}
    POST /api/memory/maintenance/restore        body {batch_id? | item_id?, confirm: true}
         -> {"ok", "restored", "blocked", "status", "result"}
    GET  /api/memory/maintenance/status
         -> {"available": true, **scheduler.pending_state()}（对象缺失时 available=false）

边界（契约 §12.4 / §12.5）：

* 读取：只读 SQL + maintenance.models 的冻结读取模型，不另造状态字符串；
  输出字段一律用 PendingItemView / PendingKnowledgeRef，两类事项按 kind / handler 区分。
* 动作：一律走 B 的 ctx.maintenance_runner（回答 / 恢复预览 / 恢复）；
  runner 不可用时返回 503 并说明「本次操作未执行」，绝不假装成功。
  defer 是持久化状态动作（runner 冻结接口里没有它），直接改事项状态。
* 隔离：本模块不写主对话 messages / 摘要 / 轮次；回答只交给维护执行器。
* B 的迁移 47 在本分支可能尚未应用：读取接口如实返回 available=false 与原因，
  写接口返回 503（不做隐式建表，schema 归 Lead / B 所有）。

挂载（api/server.py，一行）：

    from agent.api.memory_pending_routes import build_router as build_memory_pending_router
    app.include_router(build_memory_pending_router(ctx))

本模块只依赖 ctx.conn、ctx.maintenance_runner、ctx.knowledge_maintenance 与
ctx.topics（范围标签），不 import api/server.py 的路由层（来源标签函数按需惰性引用，
保持单一事实来源）。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException

from agent.knowledge.lifecycle import KnowledgeService
from agent.maintenance.models import (
    PendingAction,
    PendingItemView,
    PendingKind,
    PendingKnowledgeRef,
    PendingStatus,
)
from agent.trace.redact import redact_text

#: 待确认列表默认 / 最大条数（真实数量始终由 total 如实给出）。
DEFAULT_PENDING_LIMIT = 200
MAX_PENDING_LIMIT = 500
#: 维护批次列表默认 / 最大条数。
DEFAULT_BATCH_LIMIT = 50
MAX_BATCH_LIMIT = 200

#: 维护事项表（迁移 47，B 提供）；本模块只读 / 只改状态，不建表。
MAINTENANCE_ITEMS_TABLE = "maintenance_items"
MAINTENANCE_BATCHES_TABLE = "maintenance_batches"
MAINTENANCE_OPERATIONS_TABLE = "maintenance_operations"

#: 「记忆待确认」里两类事项的处理程序名（契约 §12.5 冻结）。
HANDLER_KNOWLEDGE = "knowledge_proposal"
HANDLER_MAINTENANCE = "maintenance"

#: 维护问题在待确认入口里的可操作状态（其余状态属于维护记录，不冒充待处理）。
_QUESTION_ACTIONABLE_STATUSES = (
    PendingStatus.AWAITING_ANSWER.value,
    PendingStatus.DEFERRED.value,
)

#: 待确认列表每个条目的动作集合（与 PendingAction 一致，不另造名字）。
_PROPOSAL_ACTIONS = [
    PendingAction.APPROVE.value,
    PendingAction.APPROVE_MODIFIED.value,
    PendingAction.REJECT.value,
]
_QUESTION_ACTIONS = [PendingAction.ANSWER.value, PendingAction.DEFER.value]


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_value(raw: Any, default: Any) -> Any:
    """宽容解析 JSON 列：坏数据退回默认值，不把解析异常带到界面上。"""
    if raw is None or raw == "":
        return default
    if isinstance(raw, type(default)):
        return raw
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return default
    return value if isinstance(value, type(default)) else default


def _dict_list(raw: Any) -> list[dict[str, Any]]:
    value = _json_value(raw, [])
    return [item for item in value if isinstance(item, dict)]


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    try:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type IN ('table', 'view') AND name = ?",
            (name,),
        ).fetchone()
    except sqlite3.Error:
        return False
    return row is not None


def _snippet(text: str, limit: int = 80) -> str:
    cleaned = " ".join(str(text or "").split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 1] + "…"


def _source_label(provenance: dict[str, Any]) -> str:
    """来源人话标签：统一复用 server.py 的 _knowledge_source_label（单一事实来源）。

    这里只用于展示待确认事项涉及的来源；原始来源键仍可在 /api/knowledge 的
    provenance_source 里看到。
    """
    from agent.api.server import _knowledge_source_label

    return _knowledge_source_label(provenance)


def _pending_status(value: Any) -> PendingStatus | None:
    try:
        return PendingStatus(str(value))
    except ValueError:
        return None


def _runner(ctx: Any) -> Any:
    """取出 B 的维护执行器；不可用时如实 503（不是 500，也不假装成功）。"""
    runner = getattr(ctx, "maintenance_runner", None)
    if runner is None:
        raise HTTPException(
            status_code=503,
            detail=(
                "维护执行器不可用（ctx.maintenance_runner 未初始化），"
                "本次操作未执行、未写入任何数据；请等待后台维护组件接入后再试。"
            ),
        )
    return runner


async def _call_runner(runner: Any, method: str, *args: Any, **kwargs: Any) -> Any:
    """调用维护执行器；失败如实返回，不吞掉错误、不谎报成功。"""
    try:
        return await getattr(runner, method)(*args, **kwargs)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 - 维护执行器的失败要变成可读的接口错误
        reason = redact_text(str(exc)) or "未知错误"
        raise HTTPException(
            status_code=500,
            detail=f"维护执行器调用失败（{method}）：{reason}；本次操作结果未确认。",
        ) from exc


# ---------------------------------------------------------------------------
# 读取模型构造（models.PendingItemView / PendingKnowledgeRef 字段名冻结）
# ---------------------------------------------------------------------------


def _node_label(ctx: Any, node_id: str) -> str | None:
    try:
        node = ctx.topics.nodes.get(node_id)
    except Exception:  # noqa: BLE001 - 范围标签缺失不影响事项本身
        return None
    if node is None:
        return None
    if node.type == "user":
        return "全局（你）"
    if node.type == "entity":
        return f"实体：{node.name}"
    if node.type == "topic":
        return f"话题：{node.name}"
    return None


def _scope_labels(ctx: Any, item: Any) -> list[str]:
    """把真实节点归属翻译成界面可读的范围标签（不编造归属）。"""
    labels: list[str] = []
    topic_id = getattr(item, "topic_id", None)
    if topic_id:
        try:
            node = ctx.topics.nodes.get_topic(topic_id)
        except Exception:  # noqa: BLE001
            node = None
        labels.append(f"话题：{node.name}" if node is not None else "话题")
    for node_id in getattr(item, "node_ids", None) or []:
        label = _node_label(ctx, node_id)
        if label and label not in labels:
            labels.append(label)
    return labels or ["未指定"]


def _knowledge_ref(ctx: Any, item: Any) -> PendingKnowledgeRef:
    provenance = dict(getattr(item, "provenance", None) or {})
    state = getattr(item, "state", None)
    return PendingKnowledgeRef(
        id=str(item.id),
        version=int(getattr(item, "version", 1) or 1),
        content=str(getattr(item, "content", "") or ""),
        category=str(getattr(item, "category", "") or ""),
        scope=_scope_labels(ctx, item),
        epistemic_status=provenance.get("epistemic_status"),
        source=_source_label(provenance),
        state=state.value if hasattr(state, "value") else (str(state) if state else None),
    )


def _missing_knowledge_ref(entry: dict[str, Any]) -> PendingKnowledgeRef:
    """事项引用的知识版本已经不在库里：如实标注缺失，不伪造正文。"""
    raw_version = entry.get("version")
    version = int(raw_version) if isinstance(raw_version, int) else 0
    return PendingKnowledgeRef(
        id=str(entry.get("id") or ""),
        version=version,
        content="（该知识版本已不存在）",
        category="",
        scope=[],
        epistemic_status=None,
        source=None,
        state="missing",
    )


def _knowledge_refs_from_json(ctx: Any, raw: Any) -> list[PendingKnowledgeRef]:
    """解析维护事项的 knowledge_ids（[{id, version, chain_id}] 或字符串 id）。"""
    service = KnowledgeService(ctx.conn)
    refs: list[PendingKnowledgeRef] = []
    for entry in _json_value(raw, []):
        if isinstance(entry, str):
            entry = {"id": entry}
        if not isinstance(entry, dict):
            continue
        knowledge_id = str(entry.get("id") or "").strip()
        if not knowledge_id:
            continue
        item = service.get(knowledge_id)
        if item is None:
            refs.append(_missing_knowledge_ref(entry))
        else:
            refs.append(_knowledge_ref(ctx, item))
    return refs


def _evidence_list(raw: Any) -> list[dict[str, Any]]:
    """证据列表：事项里存的是证据 id；这里给界面稳定的 [{"id": ...}] 形状。"""
    evidence: list[dict[str, Any]] = []
    for value in _json_value(raw, []):
        if isinstance(value, dict):
            evidence.append(dict(value))
        elif isinstance(value, (str, int)):
            text = str(value).strip()
            if text:
                evidence.append({"id": text})
    return evidence


def _proposal_view(ctx: Any, item: Any) -> PendingItemView:
    """knowledge 表 state=pending_review：主对话 / 后台提炼提出的待审批知识。"""
    ref = _knowledge_ref(ctx, item)
    before: list[dict[str, Any]] = []
    supersedes_id = getattr(item, "supersedes_id", None)
    if supersedes_id:
        previous = KnowledgeService(ctx.conn).get(str(supersedes_id))
        if previous is not None:
            before = [_knowledge_ref(ctx, previous).model_dump(mode="json")]
    provenance = dict(getattr(item, "provenance", None) or {})
    evidence = [{"id": str(value)} for value in (provenance.get("evidence_ids") or []) if str(value)]
    return PendingItemView(
        id=str(item.id),
        kind=PendingKind.KNOWLEDGE_PROPOSAL,
        status=PendingStatus.AWAITING_APPROVAL,
        handler=HANDLER_KNOWLEDGE,
        title=_snippet(item.content),
        question=None,
        knowledge=[ref],
        before=before,
        after=[ref.model_dump(mode="json")],
        evidence=evidence,
        actions=list(_PROPOSAL_ACTIONS),
        created_at=str(getattr(item, "created_at", "") or ""),
        updated_at=str(getattr(item, "updated_at", "") or ""),
        batch_id=None,
        error=None,
    )


def _maintenance_view(ctx: Any, row: sqlite3.Row | dict[str, Any]) -> PendingItemView | None:
    """maintenance_items 里的独立问题（kind=ask）→ 待确认事项。"""
    data = dict(row)
    status = _pending_status(data.get("status"))
    if status is None:
        # 状态不认识就不猜测（不冒充待回答），由调用方按「不可操作条目」跳过。
        return None
    question = str(data.get("question") or "").strip()
    return PendingItemView(
        id=str(data.get("id") or ""),
        kind=PendingKind.MAINTENANCE_QUESTION,
        status=status,
        handler=HANDLER_MAINTENANCE,
        title=_snippet(question) or "后台维护提问",
        question=question or None,
        knowledge=_knowledge_refs_from_json(ctx, data.get("knowledge_ids")),
        before=_dict_list(data.get("before")),
        after=_dict_list(data.get("after")),
        evidence=_evidence_list(data.get("evidence_ids")),
        actions=list(_QUESTION_ACTIONS),
        created_at=str(data.get("created_at") or ""),
        updated_at=str(data.get("updated_at") or ""),
        batch_id=data.get("batch_id"),
        error=data.get("error"),
    )


def _maintenance_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM maintenance_items WHERE kind = 'ask' AND status IN (?, ?)"
        " ORDER BY created_at ASC, id ASC",
        _QUESTION_ACTIONABLE_STATUSES,
    ).fetchall()


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------


def build_router(ctx: Any) -> APIRouter:  # noqa: ANN001 - ctx 由 Lead 传入（AppContext）
    """「记忆待确认」/ 维护记录路由工厂：只依赖 ctx（conn / runner / scheduler）。"""
    router = APIRouter()

    # -- 记忆待确认 ---------------------------------------------------------

    @router.get("/api/memory/pending")
    async def list_pending(
        status: str | None = None,
        kind: str | None = None,
        limit: int = DEFAULT_PENDING_LIMIT,
    ) -> dict:
        """两类待确认事项：知识待审批（复用 verify / reject）与后台维护提问（回答流程）。"""
        if limit < 1 or limit > MAX_PENDING_LIMIT:
            raise HTTPException(
                status_code=400,
                detail=f"limit 必须在 1..{MAX_PENDING_LIMIT} 之间（收到 {limit}）",
            )
        if kind is not None and kind not in {item.value for item in PendingKind}:
            raise HTTPException(status_code=400, detail=f"unknown kind: {kind}")
        requested_status: PendingStatus | None = None
        if status:
            requested_status = _pending_status(status)
            if requested_status is None:
                raise HTTPException(status_code=400, detail=f"unknown status: {status}")

        items: list[PendingItemView] = []
        warnings: list[str] = []
        maintenance_available = _table_exists(ctx.conn, MAINTENANCE_ITEMS_TABLE)

        # 1) 知识待审批：knowledge 表 state=pending_review（沿用既有 verify / reject 流程）
        wants_proposal = kind in (None, PendingKind.KNOWLEDGE_PROPOSAL.value)
        proposal_status_ok = requested_status in (None, PendingStatus.AWAITING_APPROVAL)
        if wants_proposal and proposal_status_ok:
            for item in KnowledgeService(ctx.conn).list_items(state="pending_review"):
                items.append(_proposal_view(ctx, item))

        # 2) 后台维护提问：maintenance_items kind=ask 且 status in (awaiting_answer, deferred)
        wants_question = kind in (None, PendingKind.MAINTENANCE_QUESTION.value)
        question_status_ok = requested_status in (
            None,
            PendingStatus.AWAITING_ANSWER,
            PendingStatus.DEFERRED,
        )
        if wants_question and question_status_ok:
            if not maintenance_available:
                warnings.append(
                    "维护事项存储不可用（迁移 47 未应用）：当前只显示知识待审批，"
                    "后台维护问题暂无数据可读。"
                )
            else:
                for row in _maintenance_rows(ctx.conn):
                    view = _maintenance_view(ctx, row)
                    if view is None:
                        continue
                    if requested_status is not None and view.status != requested_status:
                        continue
                    items.append(view)

        items.sort(key=lambda view: (view.created_at or "", view.id))
        total = len(items)
        return {
            "items": [view.model_dump(mode="json") for view in items[:limit]],
            "total": total,
            "truncated": total > limit,
            "maintenance_available": maintenance_available,
            "warnings": warnings,
        }

    @router.post("/api/memory/pending/{item_id}/answer")
    async def answer_pending_item(item_id: str, body: dict | None = None) -> dict:
        """提交对后台维护提问的回答：只交给维护执行器，不进入主对话。"""
        payload = body or {}
        answer = str(payload.get("answer") or "").strip()
        if not answer:
            raise HTTPException(status_code=400, detail="answer required（空的回答不会提交）")
        if len(answer) > 20000:
            raise HTTPException(status_code=400, detail="answer 过长（上限 20000 字符）")
        _require_answerable_item(ctx.conn, item_id)
        runner = _runner(ctx)
        outcome = await _call_runner(runner, "answer_question", item_id, answer)
        payload_out = _answer_outcome_payload(outcome, item_id)
        payload_out["item"] = _latest_item_view(ctx, item_id)
        return payload_out

    @router.post("/api/memory/pending/{item_id}/defer")
    async def defer_pending_item(item_id: str) -> dict:
        """稍后处理：持久化为 deferred，事项继续留在待确认入口里（不丢问题）。"""
        _require_answerable_item(ctx.conn, item_id)
        now = _now_iso()
        ctx.conn.execute(
            "UPDATE maintenance_items SET status = ?, updated_at = ? WHERE id = ?",
            (PendingStatus.DEFERRED.value, now, item_id),
        )
        ctx.conn.commit()
        return {
            "ok": True,
            "status": PendingStatus.DEFERRED.value,
            "item": _latest_item_view(ctx, item_id),
        }

    # -- 维护记录 -----------------------------------------------------------

    @router.get("/api/memory/maintenance/batches")
    async def list_maintenance_batches(limit: int = DEFAULT_BATCH_LIMIT) -> dict:
        """维护批次列表（新的在前；stats 摘要原样来自批次记录，不重新计算）。"""
        if limit < 1 or limit > MAX_BATCH_LIMIT:
            raise HTTPException(
                status_code=400,
                detail=f"limit 必须在 1..{MAX_BATCH_LIMIT} 之间（收到 {limit}）",
            )
        if not _table_exists(ctx.conn, MAINTENANCE_BATCHES_TABLE):
            return {
                "batches": [],
                "total": 0,
                "truncated": False,
                "available": False,
                "warnings": [
                    "维护批次存储不可用（迁移 47 未应用）：没有可展示的维护记录，不伪造数据。"
                ],
            }
        total = int(
            ctx.conn.execute(
                f"SELECT COUNT(*) FROM {MAINTENANCE_BATCHES_TABLE}"
            ).fetchone()[0]
        )
        rows = ctx.conn.execute(
            "SELECT id, trigger_reason, status, model, created_at, finished_at, stats, error"
            f" FROM {MAINTENANCE_BATCHES_TABLE}"
            " ORDER BY created_at DESC, id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        batches = [_batch_summary(row) for row in rows]
        return {
            "batches": batches,
            "total": total,
            "truncated": total > len(batches),
            "available": True,
        }

    @router.get("/api/memory/maintenance/batches/{batch_id}")
    async def get_maintenance_batch(batch_id: str) -> dict:
        """批次详情：批次状态 / 错误 / 事项（前后内容 + 证据）/ 实际操作记录。"""
        if not _table_exists(ctx.conn, MAINTENANCE_BATCHES_TABLE):
            raise HTTPException(
                status_code=503,
                detail="维护批次存储不可用（迁移 47 未应用），本次请求未读取任何记录。",
            )
        row = ctx.conn.execute(
            f"SELECT * FROM {MAINTENANCE_BATCHES_TABLE} WHERE id = ?", (batch_id,)
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="维护批次不存在")
        items: list[dict[str, Any]] = []
        if _table_exists(ctx.conn, MAINTENANCE_ITEMS_TABLE):
            item_rows = ctx.conn.execute(
                f"SELECT * FROM {MAINTENANCE_ITEMS_TABLE} WHERE batch_id = ?"
                " ORDER BY created_at ASC, id ASC",
                (batch_id,),
            ).fetchall()
            items = [_maintenance_record(item_row) for item_row in item_rows]
        operations: list[dict[str, Any]] = []
        if _table_exists(ctx.conn, MAINTENANCE_OPERATIONS_TABLE):
            op_rows = ctx.conn.execute(
                f"SELECT * FROM {MAINTENANCE_OPERATIONS_TABLE} WHERE batch_id = ?"
                " ORDER BY created_at ASC, id ASC",
                (batch_id,),
            ).fetchall()
            operations = [_operation_record(op_row) for op_row in op_rows]
        return {
            "batch": _batch_summary(row),
            "items": items,
            "operations": operations,
            "available": True,
        }

    # -- 安全恢复 -----------------------------------------------------------

    @router.get("/api/memory/maintenance/restore/preview")
    async def preview_restore(
        batch_id: str | None = None, item_id: str | None = None
    ) -> dict:
        """恢复影响预览（不写库）：由维护执行器逐项判断可恢复性与原因。"""
        batch_id = (batch_id or "").strip() or None
        item_id = (item_id or "").strip() or None
        if bool(batch_id) == bool(item_id):
            raise HTTPException(
                status_code=400,
                detail="需要且只能提供 batch_id 或 item_id 之一；本次未执行任何恢复预览。",
            )
        runner = _runner(ctx)
        preview = await _call_runner(
            runner, "preview_restore", batch_id=batch_id, item_id=item_id
        )
        return {"ok": True, "preview": preview}

    @router.post("/api/memory/maintenance/restore")
    async def restore_maintenance(body: dict | None = None) -> dict:
        """执行恢复：单条 / 整批；必须显式 confirm，结果里如实列出无法安全恢复的部分。"""
        payload = body or {}
        if payload.get("confirm") is not True:
            raise HTTPException(
                status_code=400,
                detail="恢复是高影响操作，需要 confirm: true；本次未执行任何恢复。",
            )
        batch_id = str(payload.get("batch_id") or "").strip() or None
        item_id = str(payload.get("item_id") or "").strip() or None
        if bool(batch_id) == bool(item_id):
            raise HTTPException(
                status_code=400,
                detail="需要且只能提供 batch_id 或 item_id 之一；本次未执行任何恢复。",
            )
        runner = _runner(ctx)
        result = await _call_runner(runner, "restore", batch_id=batch_id, item_id=item_id)
        if not isinstance(result, dict):
            return {
                "ok": True,
                "restored": None,
                "blocked": None,
                "status": None,
                "result": result,
            }
        return {
            "ok": bool(result.get("ok", True)),
            "restored": result.get("restored"),
            "blocked": result.get("blocked"),
            "status": result.get("status"),
            # 真实 recovery 的结构化结果：逐操作状态 / 无法安全恢复的原因
            "blocked_reasons": result.get("blocked_reasons") or [],
            "operations": result.get("operations") or [],
            "result": result,
        }

    # -- 维护状态 -----------------------------------------------------------

    @router.get("/api/memory/maintenance/status")
    async def maintenance_status() -> dict:
        """知识维护调度状态（A 的 pending_state）；对象缺失时如实 available=false。"""
        scheduler = getattr(ctx, "knowledge_maintenance", None)
        if scheduler is None:
            return {
                "available": False,
                "reason": (
                    "知识维护调度器未接入（ctx.knowledge_maintenance 缺失）："
                    "没有可展示的维护状态，不伪造数据。"
                ),
            }
        try:
            state = scheduler.pending_state()
        except Exception as exc:  # noqa: BLE001 - 状态读取失败要可读
            reason = redact_text(str(exc)) or "未知错误"
            raise HTTPException(status_code=500, detail=f"读取知识维护状态失败：{reason}") from exc
        if isinstance(state, dict):
            return {"available": True, **state}
        return {"available": True, "state": state}

    return router


# ---------------------------------------------------------------------------
# 事项读取 / 动作前置校验
# ---------------------------------------------------------------------------


def _require_answerable_item(conn: sqlite3.Connection, item_id: str) -> dict[str, Any]:
    """回答 / 稍后处理的前置校验：必须是维护问题且当前可操作（不改任何数据）。"""
    if not _table_exists(conn, MAINTENANCE_ITEMS_TABLE):
        raise HTTPException(
            status_code=503,
            detail="维护事项存储不可用（迁移 47 未应用），本次操作未执行、未写入任何数据。",
        )
    row = conn.execute(
        f"SELECT * FROM {MAINTENANCE_ITEMS_TABLE} WHERE id = ?", (item_id,)
    ).fetchone()
    if row is None:
        pending_knowledge = conn.execute(
            "SELECT id FROM knowledge WHERE id = ? AND state = 'pending_review'", (item_id,)
        ).fetchone()
        if pending_knowledge is not None:
            raise HTTPException(
                status_code=400,
                detail=(
                    "该事项是知识待审批（handler=knowledge_proposal），"
                    "请使用批准 / 修改后批准 / 拒绝动作，不能按维护问题回答。"
                ),
            )
        raise HTTPException(status_code=404, detail="待确认事项不存在")
    data = dict(row)
    if data.get("kind") != "ask":
        raise HTTPException(
            status_code=400,
            detail=f"该事项不是后台维护提问（kind={data.get('kind')}），不能回答或稍后处理。",
        )
    current = _pending_status(data.get("status"))
    if current not in (PendingStatus.AWAITING_ANSWER, PendingStatus.DEFERRED):
        raise HTTPException(
            status_code=409,
            detail=(
                f"该事项当前状态为 {data.get('status')}，不能回答或稍后处理"
                "（本次未修改任何数据）。"
            ),
        )
    return data


def _latest_item_view(ctx: Any, item_id: str) -> dict[str, Any] | None:
    if not _table_exists(ctx.conn, MAINTENANCE_ITEMS_TABLE):
        return None
    row = ctx.conn.execute(
        f"SELECT * FROM {MAINTENANCE_ITEMS_TABLE} WHERE id = ?", (item_id,)
    ).fetchone()
    if row is None:
        return None
    view = _maintenance_view(ctx, row)
    return view.model_dump(mode="json") if view is not None else None


def _answer_outcome_payload(outcome: Any, item_id: str) -> dict[str, Any]:
    """MaintenanceAnswerOutcome（B → C 冻结交接结构）→ JSON 字段。"""
    detail = getattr(outcome, "detail", {}) or {}
    if not isinstance(detail, dict):
        detail = {"detail": detail}
    return {
        "ok": bool(getattr(outcome, "ok", False)),
        "status": str(getattr(outcome, "status", "") or ""),
        "item_id": str(getattr(outcome, "item_id", item_id) or item_id),
        "resolution": getattr(outcome, "resolution", None),
        "applied_changes": int(getattr(outcome, "applied_changes", 0) or 0),
        "error": getattr(outcome, "error", None),
        "detail": detail,
    }


# ---------------------------------------------------------------------------
# 维护记录行 → JSON（字段原样来自 store，不重新解释状态）
# ---------------------------------------------------------------------------


def _batch_summary(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    data = dict(row)
    return {
        "id": data.get("id"),
        "trigger": data.get("trigger_reason"),
        "status": data.get("status"),
        "model": data.get("model"),
        "created_at": data.get("created_at"),
        "finished_at": data.get("finished_at"),
        "stats": _json_value(data.get("stats"), {}),
        "error": data.get("error"),
    }


def _maintenance_record(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    data = dict(row)
    return {
        "id": data.get("id"),
        "batch_id": data.get("batch_id"),
        "kind": data.get("kind"),
        "status": data.get("status"),
        "question": data.get("question"),
        "answer": data.get("answer"),
        "knowledge": _json_value(data.get("knowledge_ids"), []),
        "before": _dict_list(data.get("before")),
        "after": _dict_list(data.get("after")),
        "evidence": _evidence_list(data.get("evidence_ids")),
        "reason": data.get("reason"),
        "error": data.get("error"),
        "created_at": data.get("created_at"),
        "updated_at": data.get("updated_at"),
    }


def _operation_record(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    data = dict(row)
    return {
        "id": data.get("id"),
        "batch_id": data.get("batch_id"),
        "item_id": data.get("item_id"),
        "action": data.get("action"),
        "target_ids": _json_value(data.get("target_ids"), []),
        "result_id": data.get("result_id"),
        "before": _dict_list(data.get("before")),
        "after": _dict_list(data.get("after")),
        "status": data.get("status"),
        "created_at": data.get("created_at"),
    }


__all__ = ["build_router"]
