"""互动模式：QIO 回复、虚线预览与审批 相关路由（子智能体 C 负责实现）。

契约：docs/interactive-mode-contract.md §2 / §14.4（C 的表）。

审批边界由服务端判定：冲突、依赖等待、材料变化、恢复，都不交给前端自己算。
路由只做参数校验、转发与**启动真实执行**；业务语义全在
agent.interactive.intents（仓库层）与 agent.interactive.execution（真实执行）。
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from agent.interactive import execution as execution_module
from agent.interactive import intents

router = APIRouter()
logger = logging.getLogger(__name__)

#: 演示执行推进允许的结果；revert_rest 用于执行「等待用户决定」的其余撤回项
ADVANCE_OUTCOMES = tuple(intents.ADVANCE_OUTCOMES) + ("revert_rest",)

#: 理解还没落定时的可读说明（前端据此显示「正在理解 / 需要补充 / 失败」）。
_UNDERSTANDING_DETAIL = {
    "accepted": "这次提交已经可靠受理，QIO 还没有开始理解：意图要等理解完成后才会出现。",
    "understanding": "QIO 正在理解这次提交：意图要等理解完成后才会出现。",
    "needs_clarification": "这次理解需要你补充说明：还没有可执行的意图。",
    "no_action": "这次提交当前无需开展任务：没有生成可执行的意图。",
    "failed": "这次理解失败了：可以从保存的快照重试，不会重建同一批任务。",
}


def _conn(request: Request) -> sqlite3.Connection:
    return request.app.state.ctx.conn


def _body_dict(body: dict | None) -> dict:
    return body if isinstance(body, dict) else {}


def _instance_id(request: Request) -> str | None:
    """当前进程实例的标识（create_app 写入 app.state.instance_id）。

    拿不到时不猜：恢复流程只会在「确认是别的进程遗留」时才暂停任务。
    """
    return getattr(request.app.state, "instance_id", None)


def _id_list(payload: dict, key: str) -> list[str]:
    value = payload.get(key) or []
    if not isinstance(value, list):
        raise HTTPException(status_code=400, detail=f"{key} 必须是意图 id 列表")
    return [str(x) for x in value]


def _adapter_provider(request: Request):
    """真实执行需要的模型链路（与理解走同一条凭据 / 用量 / 预算链路）。

    - 测试 / 联调可以放 app.state.interactive_adapter_provider 顶替；
    - 正常路径用 ctx.build_adapter（没有可用凭据时返回 None，执行会给出可读失败原因）。
    """
    override = getattr(request.app.state, "interactive_adapter_provider", None)
    if override is not None:
        return override
    ctx = getattr(request.app.state, "ctx", None)
    return getattr(ctx, "build_adapter", None)


async def _start_execution(request: Request, payload: dict) -> dict:
    """批准 / 继续之后**实际启动真实执行**；返回补齐了 execution 的结果。

    后台登记失败（没有事件循环 / 服务正在关闭）时如实说明，并把执行留在 queued。
    """
    execution = payload.get("execution") if isinstance(payload, dict) else None
    if not payload.get("ok") or not isinstance(execution, dict):
        return payload
    if execution.get("status") not in ("running", "queued"):
        return payload
    started = await execution_module.start_execution(
        _conn(request),
        str(execution["id"]),
        adapter_provider=_adapter_provider(request),
        instance_id=_instance_id(request),
        # 契约 §2：后台归属统一走 AppContext（aclose 真正 shutdown 的那份注册表），
        # 不是 FastAPI 应用对象 —— 后者会另建一份注册表，任务就不在关闭账上了。
        owner=execution_module.app_owner_for(request.app),
    )
    if isinstance(started.get("execution"), dict):
        payload["execution"] = started["execution"]
        if isinstance(payload.get("intent"), dict):
            payload["intent"]["execution"] = started["execution"]
    if not started.get("scheduled"):
        payload["executionStart"] = {
            "scheduled": False,
            "reason": started.get("reason"),
            "detail": started.get("detail"),
        }
    return payload


@router.get("/api/interactive/boards/{board_id}/intents")
async def list_intents(request: Request, board_id: str) -> dict:
    """意图列表 + 冲突分组 + 批量可用性 + 执行记录 + 恢复信息。

    恢复信息在这里给出：只有**别的进程实例**遗留的 running 才降级为 paused，
    本进程自己正在执行的任务不受影响（否则刷一次列表就会把任务暂停掉）。
    """
    conn = _conn(request)
    # 先恢复、再取列表：否则同一次响应里会给出「已经暂停、却仍显示执行中」的过期状态
    recovery = intents.recover_running_intents(
        conn, board_id, instance_id=_instance_id(request)
    )
    payload = intents.list_intents(conn, board_id)
    payload["recovery"] = recovery
    return payload


@router.post("/api/interactive/boards/{board_id}/intents")
async def create_intents(request: Request, board_id: str, body: dict | None = None) -> dict:
    """创建意图。

    - {submissionId}（真实入口）：取**该次理解**已经产出的意图。
      不重新调用模型、不新建重复任务；理解还没落定时如实说明进度；
    - {demo: true}：演示入口（界面上必须写明是演示）。
    """
    conn = _conn(request)
    payload = _body_dict(body)
    if payload.get("demo"):
        created = intents.create_demo_intents(conn, board_id=board_id)
        return {
            "created": created,
            "demo": True,
            "notice": "演示意图：演示推进只走可控状态机，不代表 QIO 的真实理解与执行。",
        }

    submission_id = str(payload.get("submissionId") or "").strip()
    if not submission_id:
        raise HTTPException(
            status_code=400,
            detail="真实入口需要 submissionId（这次提交的 id）；演示入口请传 demo=true。",
        )

    understanding = _understanding_for_submission(conn, board_id, submission_id)
    if understanding is None:
        return {
            "created": [],
            "demo": False,
            "ok": False,
            "reason": "not_ready",
            "detail": "这次提交还没有对应的理解请求：受理成功之后再取意图。",
        }
    status = str(understanding["status"] or "")
    if status in ("accepted", "understanding"):
        return {
            "created": [],
            "demo": False,
            "ok": False,
            "reason": "understanding",
            "understandingId": understanding["id"],
            "detail": _UNDERSTANDING_DETAIL.get(status, "QIO 正在理解这次提交。"),
        }
    if status != "understood":
        return {
            "created": [],
            "demo": False,
            "ok": False,
            "reason": "no_action" if status == "no_action" else "failed",
            "understandingId": understanding["id"],
            "detail": _UNDERSTANDING_DETAIL.get(status, "这次理解没有产出可执行的意图。"),
        }

    created = [
        item
        for item in intents._payloads(conn, board_id)
        if str(item.get("understandingId") or "") == str(understanding["id"])
    ]
    return {
        "created": created,
        "demo": False,
        "ok": True,
        "understandingId": understanding["id"],
        "reason": "" if created else "empty",
        "detail": "" if created else "这次理解没有产出可执行的意图。",
    }


def _understanding_for_submission(
    conn: sqlite3.Connection, board_id: str, submission_id: str
) -> sqlite3.Row | None:
    """这条提交最近一次理解请求（表由本轮迁移 37 建立；缺表时按「还没有」处理）。"""
    try:
        return conn.execute(
            "SELECT id, status, submission_id, error FROM board_understanding"
            " WHERE board_id = ? AND submission_id = ?"
            " ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (board_id, submission_id),
        ).fetchone()
    except sqlite3.Error:
        return None


@router.get("/api/interactive/boards/{board_id}/executions")
async def list_executions(request: Request, board_id: str, limit: int = 50) -> dict:
    """本板面的真实执行记录（新的在前）：状态、步骤与结果都由服务端给出。"""
    return intents.list_executions(_conn(request), board_id, limit=limit)


@router.post("/api/interactive/boards/{board_id}/material-impact")
async def material_impact(request: Request, board_id: str, body: dict) -> dict:
    """保存前的只读预判：用**待保存**的 state 判断这次改动会不会影响执行中的任务。

    纯只读：不改任何状态、不落库。真正生效的判定在保存时的 intents.on_board_saved。
    """
    conn = _conn(request)
    payload = _body_dict(body)
    state = payload.get("state")
    if not isinstance(state, dict):
        raise HTTPException(status_code=400, detail="state 必须是板面状态对象")
    return intents.preview_material_impact(conn, board_id=board_id, state=state)


@router.post("/api/interactive/boards/{board_id}/impact-check")
async def board_impact_check(request: Request, board_id: str, body: dict) -> dict:
    """M4 影响预判（收尾轮 Lead 补齐路由）：绑定当前已保存版本与候选内容，返回可确认的 checkId。

    纯只读：不改任何状态、不落库、不暂停任何任务。预判失败返回 {"ok": false, "reason"}（HTTP 200），
    前端据此显示真实原因并保留改动；确认与保存的校验分别在 PUT /state 与提交接口里做。
    """
    conn = _conn(request)
    payload = _body_dict(body)
    state_version = payload.get("stateVersion")
    change_set = payload.get("changeSet")
    state = change_set.get("state") if isinstance(change_set, dict) else None
    if not isinstance(state, dict):
        raise HTTPException(status_code=400, detail="changeSet.state 必须是板面状态对象")
    return intents.impact_check(conn, board_id=board_id, state_version=state_version, state=state)


@router.post("/api/interactive/intents/{intent_id}/approve")
async def approve(intent_id: str, request: Request, body: dict | None = None) -> dict:
    """批准。confirmDependency=true 表示「前项已完成，我确认开始」/「按当前材料继续」。

    批准后**必须实际启动真实执行**：服务端在批准时建立执行记录（幂等：已有活跃执行
    就复用），并把它交给后台执行；响应里立刻带上 execution（id / status / steps），
    前端据此显示真实状态，而不是等下一次轮询。
    """
    conn = _conn(request)
    confirm = bool(_body_dict(body).get("confirmDependency"))
    result = intents.approve_intent(
        conn, intent_id, confirm_dependency=confirm, instance_id=_instance_id(request)
    )
    return await _start_execution(request, result)


@router.post("/api/interactive/intents/{intent_id}/reject")
async def reject(intent_id: str, request: Request) -> dict:
    """拒绝：预览消失，板面原内容保持不变。"""
    conn = _conn(request)
    return intents.reject_intent(conn, intent_id)


@router.post("/api/interactive/intents/{intent_id}/preview")
async def update_preview(intent_id: str, request: Request, body: dict) -> dict:
    """调整预览：只改位置可以直接批准；改语义会标记 needs_update。"""
    conn = _conn(request)
    payload = _body_dict(body)
    preview = payload.get("preview")
    if preview is not None and not isinstance(preview, dict):
        raise HTTPException(status_code=400, detail="preview 必须是对象")
    return intents.update_preview(conn, intent_id, preview or {})


@router.post("/api/interactive/intents/{intent_id}/pause")
async def pause_intent(intent_id: str, request: Request) -> dict:
    """暂停**真实执行**：保留进度与已完成步骤，不自动继续、不自动重试。"""
    return intents.pause_intent(_conn(request), intent_id, instance_id=_instance_id(request))


@router.post("/api/interactive/intents/{intent_id}/resume")
async def resume_intent(intent_id: str, request: Request) -> dict:
    """继续：校验当前材料与已完成步骤，不重复已完成操作，然后重新启动真实执行。"""
    result = intents.resume_intent(
        _conn(request), intent_id, instance_id=_instance_id(request)
    )
    return await _start_execution(request, result)


@router.post("/api/interactive/intents/{intent_id}/cancel")
async def cancel_intent(intent_id: str, request: Request) -> dict:
    """取消真实执行，并撤回该任务造成且仍可安全撤回的改动（保留用户后续编辑）。"""
    return intents.cancel_intent(_conn(request), intent_id, instance_id=_instance_id(request))


@router.post("/api/interactive/intents/{intent_id}/demo/advance")
async def advance(intent_id: str, request: Request, body: dict | None = None) -> dict:
    """演示执行推进（明确标注为演示）：成功 / 失败 / 暂停 / 取消。

    演示推进**不得驱动真实任务**：这项任务有活跃的真实执行时，服务端拒绝推进，
    要求改用暂停 / 继续 / 取消。

    N4（本轮契约 §2）：可选字段 decisionIds: string[] —— 只处理这次**明确展示给用户**、
    且属于该意图 pendingDecision 的项。未展示项不会被顺带处理；不传时保持既有语义，
    但执行前同样重核每个待撤回对象的当前内容与影响。
    """
    conn = _conn(request)
    payload = _body_dict(body)
    outcome = str(payload.get("outcome") or "done")
    if outcome not in ADVANCE_OUTCOMES:
        raise HTTPException(
            status_code=400,
            detail=f"不认识的结果：{outcome}（可选：{'、'.join(ADVANCE_OUTCOMES)}）",
        )
    decision_ids = payload.get("decisionIds")
    if decision_ids is not None and not isinstance(decision_ids, list):
        raise HTTPException(status_code=400, detail="decisionIds 必须是意图项 id 的字符串数组")
    return intents.advance_intent(
        conn,
        intent_id,
        outcome=outcome,
        decision_ids=(None if decision_ids is None else [str(x) for x in decision_ids]),
    )


@router.post("/api/interactive/intents/batch")
async def batch(request: Request, body: dict) -> dict:
    """批量审批：选择部分或全部；互不相容的两项不能同时批准。

    批量批准同样**实际启动真实执行**（逐项建立执行记录），并把每一项的 execution
    放进结果里：前端据此立刻显示真实状态。
    """
    conn = _conn(request)
    payload = _body_dict(body)
    approve_ids = _id_list(payload, "approve")
    reject_ids = _id_list(payload, "reject")
    result = intents.batch_decide(
        conn, approve=approve_ids, reject=reject_ids, instance_id=_instance_id(request)
    )
    for item in result.get("results") or []:
        if not isinstance(item, dict) or not item.get("ok"):
            continue
        await _start_execution(request, item)
    return result

# ---------------------------------------------------------------------------
# 第三阶段（契约 §13.5，C）：意图视图 / 批量确认 / 等待 / 账本 / 结果 / 恢复
# ---------------------------------------------------------------------------
#
# 数据来源以 B 的 agent.interactive.intent_service 为准（第三阶段冻结接口）：
#   list_intents_view / get_intent / confirm_intents / ledger_summary /
#   list_results / preview_recovery / recover_intent
# 服务未接入（B 尚未合入）时：
#   * 意图视图回退到旧 intents.list_intents 的只读兼容映射（source=legacy，如实标注）；
#   * 账本 / 结果 / 恢复如实 available=false 或 503，绝不伪造账本与结果；
#   * 写入型动作（确认 / 恢复）一律 503「本次未执行」。
#
# 批量确认门槛统一为两条（契约 §13.5）：BATCH_CONFIRM_MIN 与前端
# frontend/src/interactive/intentWorkbench.ts 的同名常量必须一致；
# B 的 confirm_intents 只校验显式 intent_ids（>=1），门槛由本 API 与前端强制。

#: 「同批批量确认」的最少条数（契约 §13.5：前后端统一为两条；单条走单条确认）。
BATCH_CONFIRM_MIN = 2

#: 仍然可以「确认」的意图状态（已受理 / 执行中的状态不需要也不能再确认）。
_CONFIRMABLE_STATUSES = frozenset({"proposed", "awaiting_confirmation"})

#: 可进入恢复预览 / 恢复的状态（契约 §13.4：部分完成、失败、暂停不永久锁死）。
_RECOVERABLE_STATUSES = frozenset({"paused", "failed", "partially_completed"})

_LEGACY_STATUS_MAP = {
    "pending": "proposed",
    "needs_update": "proposed",
    "waiting_dependency": "approved",
    "waiting_confirm": "awaiting_confirmation",
    "running": "running",
    "paused": "paused",
    "done": "completed",
    "failed": "failed",
    "cancelled": "cancelled",
    "rejected": "rejected",
}


def _intent_service(request: Request):
    """意图服务：测试 / 联调注入 app.state.p3_intent_service，正常路径取 B 的模块。"""
    override = getattr(request.app.state, "p3_intent_service", None)
    if override is not None:
        return override
    try:
        from agent.interactive import intent_service
    except ImportError:
        return None
    return intent_service


def _require_intent_service(request: Request):
    service = _intent_service(request)
    if service is None:
        raise HTTPException(
            status_code=503,
            detail=(
                "意图服务尚未接入（interactive/intent_service.py 不可用）："
                "本次请求未执行、不会伪造意图或账本；等待后台组件接入后再试。"
            ),
        )
    return service


def _service_call(service, name: str, *args, **kwargs):
    """调用意图服务；失败如实变成可读错误，不吞错、不谎报成功。"""
    fn = getattr(service, name, None)
    if fn is None:
        raise HTTPException(
            status_code=503,
            detail=(
                f"意图服务缺少 {name}（B 的 intent_service 未提供该能力）："
                "本次请求未执行。"
            ),
        )
    try:
        return fn(*args, **kwargs)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 - 服务失败要变成可读的接口错误
        from agent.trace.redact import redact_text

        reason = redact_text(str(exc)) or "未知错误"
        raise HTTPException(
            status_code=500,
            detail=f"意图服务调用失败（{name}）：{reason}；本次结果未确认。",
        ) from exc


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    try:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type IN ('table', 'view') AND name = ?",
            (name,),
        ).fetchone()
    except sqlite3.Error:
        return False
    return row is not None


def _json_value(raw, default):
    if raw is None or raw == "":
        return default
    if isinstance(raw, type(default)):
        return raw
    try:
        import json

        value = json.loads(raw)
    except (TypeError, ValueError):
        return default
    return value if isinstance(value, type(default)) else default


def _legacy_basis(raw) -> list[dict]:
    out: list[dict] = []
    for entry in raw or []:
        if not isinstance(entry, dict):
            continue
        card_id = entry.get("cardId") or entry.get("card_id")
        if not card_id:
            continue
        try:
            version = int(entry.get("version") or 1)
        except (TypeError, ValueError):
            version = 1
        out.append(
            {
                "card_id": str(card_id),
                "version": version,
                "role": str(entry.get("role") or "context"),
            }
        )
    return out


def _legacy_intent_view(item: dict) -> dict:
    """旧意图（intents.list_intents）→ 第三阶段意图视图（只读兼容，不冒充新状态）。"""
    raw_status = str(item.get("status") or "")
    status = _LEGACY_STATUS_MAP.get(raw_status, "proposed")
    progress = item.get("progress") if isinstance(item.get("progress"), dict) else {}
    try:
        done = int(progress.get("done") or 0)
        total = int(progress.get("total") or 0)
    except (TypeError, ValueError):
        done, total = 0, 0
    demo = bool(item.get("demo"))
    return {
        "intent_id": str(item.get("id") or ""),
        "board_id": str(item.get("boardId") or ""),
        "client_ref": str(item.get("intentKey") or ""),
        "submission_id": item.get("submissionId"),
        "version": int(item.get("version") or 1),
        "status": status,
        "raw_legacy_status": raw_status,
        "title": str(item.get("title") or ""),
        "goal": str(item.get("goal") or ""),
        "expected": str(item.get("expected") or ""),
        "basis": _legacy_basis(item.get("basis")),
        "scope": item.get("scope") if isinstance(item.get("scope"), dict) else {},
        "depends_on": [str(x) for x in (item.get("dependsOn") or [])],
        "conflicts_with": [str(x) for x in (item.get("conflictsWith") or [])],
        "conflicts": [],
        "authorization": {},
        "authorization_valid": status != "proposed",
        "outcome": str(item.get("reason") or ""),
        "summary": str(item.get("summary") or ""),
        "attempt": int(item.get("attempt") or 1),
        "operations_summary": {
            "applied": done,
            "running": 1 if status == "running" else 0,
            "pending": max(0, total - done),
            "failed": 1 if status == "failed" else 0,
            "conflict": 0,
        },
        "created_at": str(item.get("createdAt") or ""),
        "updated_at": str(item.get("updatedAt") or ""),
        "finished_at": item.get("finishedAt"),
        "demo": demo,
    }


def _legacy_views(payload: dict) -> list[dict]:
    items = payload.get("intents") if isinstance(payload, dict) else None
    return [
        _legacy_intent_view(item)
        for item in (items or [])
        if isinstance(item, dict) and item.get("id")
    ]


def _batch_confirm_payload(views: list[dict]) -> dict:
    eligible = [
        str(view.get("intent_id"))
        for view in views
        if isinstance(view, dict)
        and view.get("intent_id")
        and str(view.get("status") or "") in _CONFIRMABLE_STATUSES
    ]
    return {
        "min": BATCH_CONFIRM_MIN,
        "eligible_ids": eligible,
        "available": len(eligible) >= BATCH_CONFIRM_MIN,
    }


@router.get("/api/interactive/boards/{board_id}/intent-views")
async def intent_views(request: Request, board_id: str) -> dict:
    """意图视图（第三阶段）：目标 / 预期 / 依据 / 范围 / 依赖 / 冲突 / 版本 / 授权 / 真实状态。

    B 的 intent_service 未接入时：回退到旧 intents.list_intents 的只读映射，
    响应里 source=legacy、operations.available=false，界面据此如实标注。
    """
    conn = _conn(request)
    service = _intent_service(request)
    if service is None:
        payload = intents.list_intents(conn, board_id)
        views = _legacy_views(payload)
        return {
            "available": True,
            "source": "legacy",
            "board_id": board_id,
            "intents": views,
            "batch_confirm": _batch_confirm_payload(views),
            "operations": {
                "available": False,
                "reason": "旧实现没有权威操作账本：账本 / 结果 / 恢复入口需要第三阶段意图服务。",
            },
        }
    result = _service_call(service, "list_intents_view", conn, board_id)
    if isinstance(result, dict):
        payload = dict(result)
        views = [view for view in (payload.get("intents") or []) if isinstance(view, dict)]
    else:
        payload = {"board_id": board_id}
        views = [view for view in (result or []) if isinstance(view, dict)]
    payload["available"] = True
    payload["source"] = "service"
    payload["intents"] = views
    payload["batch_confirm"] = _batch_confirm_payload(views)
    return payload


@router.get("/api/interactive/boards/{board_id}/intent-views/{intent_id}")
async def intent_view_detail(request: Request, board_id: str, intent_id: str) -> dict:
    """单条意图详情（含版本 / 授权快照 / 账本摘要 / 结果引用）。"""
    service = _require_intent_service(request)
    view = _service_call(service, "get_intent", _conn(request), board_id, intent_id)
    if view is None:
        raise HTTPException(status_code=404, detail="意图不存在（或不属于该板面）")
    return {"available": True, "intent": view}


def _enqueue_continuation(
    request: Request, board_id: str, items: list[dict]
) -> dict | None:
    """P73 Stage 2：无操作计划的已确认意图由服务端自动起一个续作轮执行。

    对应 architecture §18.1 的「续作 Turn（自动，不需再次提交）」：不要求用户
    再发一条消息，也不依赖前端停留在互动板面。
    """
    ctx = getattr(request.app.state, "ctx", None)
    turns = getattr(ctx, "turns", None) if ctx is not None else None
    if ctx is None or turns is None or not items:
        return None
    intent_ids: list[str] = []
    titles: list[str] = []
    submission_ids: set[str] = set()
    for item in items:
        intent = item.get("intent") if isinstance(item.get("intent"), dict) else {}
        intent_id = str(item.get("intent_id") or intent.get("intent_id") or "")
        if not intent_id:
            continue
        intent_ids.append(intent_id)
        titles.append(str(intent.get("title") or intent_id))
        sid = str(item.get("submission_id") or intent.get("submission_id") or "")
        if sid:
            submission_ids.add(sid)
    if not intent_ids:
        return None
    submission_id = next(iter(submission_ids)) if len(submission_ids) == 1 else ""
    topic_id = ""
    if submission_id:
        try:
            from agent.interactive import submission as _submission

            binding = _submission.submission_binding(ctx.conn, submission_id)
            if binding is not None:
                topic_id = str((binding.get("resumeState") or {}).get("topicId") or "")
        except Exception:  # noqa: BLE001 - 找不到原话题时退回当前话题
            logger.warning("continuation topic lookup failed", exc_info=True)
    if not topic_id:
        try:
            topic_id = str(ctx.current_topic() or "")
        except Exception:  # noqa: BLE001
            topic_id = ""
    message = (
        "【互动板面自动续作】用户已确认以下意图："
        + "；".join(
            f"{title}（{intent_id}）" for title, intent_id in zip(titles, intent_ids)
        )
        + "。请在已确认的授权范围内直接执行，并用 board_finish_intent 声明结果；"
        "不要再次提出同一件事。"
    )
    try:
        turn = turns.reserve(message, topic_id or None)
        turn.mode = "interactive"
        turn.board_id = board_id
        turn.board_submission_id = submission_id
        turn.board_topic_id = topic_id
        turn.board_resume = False
        turn.board_intent_ids = tuple(intent_ids)
        turn.board_resolved_question_ids = ()
        turn.board_attempt = 1
        turns.activate(turn)
        return {"enqueued": True, "turn_id": turn.turn_id}
    except Exception:  # noqa: BLE001 - 自动续作失败不推翻已经写好的确认
        logger.warning("auto continuation enqueue failed", exc_info=True)
        return {"enqueued": False, "reason": "enqueue_failed"}


async def _apply_confirmed(
    request: Request, service: Any, response: dict, board_id: str
) -> dict:
    """P73：确认之后对带操作计划的意图**在同一请求里**落地。

    服务没有 apply_approved_intent（旧 / 测试注入的服务）时原样返回：
    不带计划的意图保持旧行为，不会因此失败。
    P73 Stage 2：没有操作计划的已确认意图 → 自动起续作轮执行（不再要求用户再发一条）。
    """
    apply_fn = getattr(service, "apply_approved_intent", None)
    if apply_fn is None:
        return response
    results = response.get("results")
    if not isinstance(results, list):
        return response
    applied: dict[str, Any] = {}
    last_execution: Any = None
    last_intent: Any = None
    continuation_items: list[dict] = []
    # 只给本次真正确认（approved）的意图落地；already_completed 的重复确认不再落地。
    confirmed = {str(x) for x in (response.get("confirmed") or [])}
    for item in results:
        if not isinstance(item, dict) or not item.get("ok"):
            continue
        intent_id = str(item.get("intent_id") or "")
        if not intent_id or intent_id not in confirmed:
            continue
        try:
            outcome = apply_fn(
                _conn(request),
                board_id=board_id,
                intent_id=intent_id,
                instance_id=_instance_id(request),
            )
        except Exception as exc:  # noqa: BLE001 - 落地失败如实写进逐项结果，不吞错
            item["apply_error"] = f"落地调用失败：{exc}"
            continue
        if not isinstance(outcome, dict):
            continue
        if outcome.get("ok"):
            applied_payload = outcome.get("applied") or {}
            item["applied"] = applied_payload
            item["execution"] = outcome.get("execution")
            if isinstance(outcome.get("intent"), dict):
                item["intent"] = outcome["intent"]
            applied[intent_id] = applied_payload
            if outcome.get("execution") is not None:
                last_execution = outcome.get("execution")
            if isinstance(outcome.get("intent"), dict):
                last_intent = outcome.get("intent")
        elif outcome.get("reason") == "no_operations":
            # 没有可原子落地的计划：交给服务端自动续作轮执行（Stage 2）。
            continuation_items.append(
                {
                    "intent_id": intent_id,
                    "intent": outcome.get("intent") or item.get("intent"),
                    "submission_id": outcome.get("submission_id", ""),
                }
            )
            continue
        else:
            item["apply_error"] = (
                outcome.get("detail")
                or outcome.get("error")
                or outcome.get("reason")
                or "落地没有完成"
            )
    if applied:
        response["applied"] = applied
    if last_execution is not None:
        response["execution"] = last_execution
    if last_intent is not None:
        response["intent"] = last_intent
    if continuation_items:
        continuation = _enqueue_continuation(request, board_id, continuation_items)
        if continuation is not None:
            response["continuation"] = continuation
            turn_id = continuation.get("turn_id")
            if turn_id:
                for item in continuation_items:
                    item["continuation_turn_id"] = turn_id
    return response


@router.post("/api/interactive/intents/{intent_id}/confirm")
async def confirm_single_intent(
    intent_id: str, request: Request, body: dict | None = None
) -> dict:
    """单条确认：仍走 B 的 confirm_intents（显式一个 id），门槛在 API/前端。"""
    payload = _body_dict(body)
    board_id = str(payload.get("boardId") or "").strip()
    if not board_id:
        raise HTTPException(status_code=400, detail="需要 boardId（意图属于哪个板面）")
    service = _require_intent_service(request)
    result = _service_call(
        service,
        "confirm_intents",
        _conn(request),
        board_id=board_id,
        intent_ids=[str(intent_id)],
    )
    response = dict(result) if isinstance(result, dict) else {"results": result}
    # P73：带操作计划的意图在确认后立即原子落地（不需要再发一条消息）。
    response = await _apply_confirmed(request, service, response, board_id)
    response["min"] = BATCH_CONFIRM_MIN
    response["batch"] = False
    return response


@router.post("/api/interactive/intents/batch-confirm")
async def confirm_intents_batch(request: Request, body: dict) -> dict:
    """同批批量确认：至少两条（BATCH_CONFIRM_MIN）；单条请走单项确认。

    门槛只在 API / 前端强制（契约 §13.5），B 的 confirm_intents 只接收显式 ids。
    「已受理」不等于「已完成」：状态一律以服务返回的意图视图为准，不在响应里推断。
    """
    payload = _body_dict(body)
    board_id = str(payload.get("boardId") or "").strip()
    if not board_id:
        raise HTTPException(status_code=400, detail="需要 boardId（意图属于哪个板面）")
    intent_ids = _id_list(payload, "intentIds")
    if len(intent_ids) < BATCH_CONFIRM_MIN:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "batch_confirm_min",
                "min": BATCH_CONFIRM_MIN,
                "count": len(intent_ids),
                "detail": (
                    f"同批批量确认至少选择 {BATCH_CONFIRM_MIN} 条意图；"
                    "单条请使用单项确认。"
                ),
            },
        )
    service = _require_intent_service(request)
    result = _service_call(
        service,
        "confirm_intents",
        _conn(request),
        board_id=board_id,
        intent_ids=intent_ids,
    )
    response = dict(result) if isinstance(result, dict) else {"results": result}
    response = await _apply_confirmed(request, service, response, board_id)
    response["min"] = BATCH_CONFIRM_MIN
    response["batch"] = True
    return response


@router.patch("/api/interactive/intents/{intent_id}/plan")
async def update_intent_plan(
    intent_id: str, request: Request, body: dict | None = None
) -> dict:
    """P73：编辑未确认意图的操作计划（板面预演层拖动 / 删除）。

    只允许 proposed / awaiting_confirmation；保存时重新校验计划并重算预演。
    已确认的意图返回 409：先放弃或重新提案，绝不悄悄改动已批准的计划。
    """
    payload = _body_dict(body)
    board_id = str(payload.get("boardId") or "").strip()
    if not board_id:
        raise HTTPException(status_code=400, detail="需要 boardId（意图属于哪个板面）")
    operations = payload.get("operations")
    if not isinstance(operations, list):
        raise HTTPException(status_code=400, detail="operations 必须是操作对象列表")
    service = _require_intent_service(request)
    if getattr(service, "update_plan", None) is None:
        raise HTTPException(
            status_code=503,
            detail="意图服务尚未接入 update_plan：本次请求未执行，未改动任何计划。",
        )
    result = _service_call(
        service,
        "update_plan",
        _conn(request),
        board_id=board_id,
        intent_id=str(intent_id),
        operations=operations,
    )
    if not isinstance(result, dict):
        raise HTTPException(status_code=500, detail="意图服务没有返回结果：本次请求未执行。")
    if result.get("ok"):
        return result
    code = str(result.get("code") or "")
    if code == "not_editable":
        raise HTTPException(status_code=409, detail=result)
    if code == "not_found":
        raise HTTPException(status_code=404, detail=result)
    raise HTTPException(status_code=400, detail=result)


# -- 等待 / 账本 / 结果 / 恢复（只读为主）-------------------------------------


def _normalise_waiting_payload(waiting, board_id: str) -> dict:
    """把 A 的 waiting_state（items 列表、camelCase）规范成 C 的只读形状（snake_case）。"""
    if not isinstance(waiting, dict):
        return {"available": True, "waiting": waiting}
    items = waiting.get("items")
    if not isinstance(items, list):
        return {"available": True, "waiting": waiting}
    normalised: list[dict] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        normalised.append(
            {
                "submission_id": item.get("submissionId") or item.get("submission_id"),
                "question": str(item.get("question") or ""),
                "missing_fields": [
                    str(value)
                    for value in (item.get("missingFields") or item.get("missing_fields") or [])
                ],
                "question_ids": [
                    str(value)
                    for value in (item.get("questionIds") or item.get("question_ids") or [])
                ],
                "status": str(item.get("status") or "waiting"),
                "created_at": item.get("createdAt") or item.get("created_at"),
            }
        )
    return {
        "available": True,
        "board_id": waiting.get("boardId") or board_id,
        "count": len(normalised),
        "waiting": normalised,
    }


def _waiting_for(request: Request, board_id: str) -> dict:
    """等待事项：优先注入的 provider（测试 / 联调），否则读 A 的 submission.get_waiting。"""
    conn = _conn(request)
    provider = getattr(request.app.state, "p3_waiting_provider", None)
    if provider is not None:
        data = provider(board_id)
        if isinstance(data, dict):
            return {"available": True, **data}
        return {"available": True, "waiting": data}
    try:
        from agent.interactive import submission as submission_module
    except ImportError:
        submission_module = None
    getter = getattr(submission_module, "get_waiting", None) if submission_module else None
    if getter is None:
        return {
            "available": False,
            "waiting": None,
            "reason": (
                "等待持久化尚未接入（submission.get_waiting 不可用）："
                "没有可展示的等待事项，不伪造。"
            ),
        }
    try:
        try:
            waiting = getter(conn, board_id)
        except TypeError:
            waiting = getter(conn, board_id=board_id)
    except Exception as exc:  # noqa: BLE001
        from agent.trace.redact import redact_text

        reason = redact_text(str(exc)) or "未知错误"
        raise HTTPException(
            status_code=500, detail=f"读取等待事项失败：{reason}"
        ) from exc
    return _normalise_waiting_payload(waiting, board_id)


@router.get("/api/interactive/boards/{board_id}/waiting")
async def waiting_summary(request: Request, board_id: str) -> dict:
    """当前板面的等待事项（提交—等待—续作由 A 持久化；这里只读）。"""
    return _waiting_for(request, board_id)


@router.get("/api/interactive/boards/{board_id}/ledger")
async def ledger_summary(request: Request, board_id: str) -> dict:
    """操作账本摘要：权威记录来自 B 的迁移 50 board_operations（只有 applied 算完成）。"""
    conn = _conn(request)
    service = _intent_service(request)
    if service is not None:
        result = _service_call(service, "ledger_summary", conn, board_id)
        if isinstance(result, dict):
            return {"available": True, "source": "service", **result}
        return {"available": True, "source": "service", "operations": result}
    if not _table_exists(conn, "board_operations"):
        return {
            "available": False,
            "reason": "权威操作账本不可用（迁移 50 未应用）：不伪造账本。",
            "counts": {"applied": 0, "running": 0, "pending": 0, "failed": 0, "conflict": 0},
            "operations": [],
        }
    counts = {"applied": 0, "running": 0, "pending": 0, "failed": 0, "conflict": 0}
    try:
        rows = conn.execute(
            "SELECT id, intent_id, operation_key, action, status, result,"
            " created_at, updated_at FROM board_operations WHERE board_id = ?"
            " ORDER BY created_at DESC, id DESC LIMIT 100",
            (board_id,),
        ).fetchall()
    except sqlite3.Error as exc:
        raise HTTPException(
            status_code=500, detail=f"读取操作账本失败：{exc}；没有伪造账本。"
        ) from exc
    operations: list[dict] = []
    for row in rows:
        data = dict(row)
        status = str(data.get("status") or "")
        counts[status] = counts.get(status, 0) + 1
        operations.append(
            {
                "operation_id": data.get("id"),
                "intent_id": data.get("intent_id"),
                "operation_key": data.get("operation_key"),
                "action": data.get("action"),
                "status": status,
                "result": _json_value(data.get("result"), {}),
                "created_at": data.get("created_at"),
                "updated_at": data.get("updated_at"),
            }
        )
    return {"available": True, "source": "table", "counts": counts, "operations": operations}


@router.get("/api/interactive/boards/{board_id}/results")
async def intent_results(request: Request, board_id: str) -> dict:
    """结果引用（结果卡正文来自权威账本派生；服务未接入时如实不可用）。"""
    service = _intent_service(request)
    if service is None:
        return {
            "available": False,
            "results": [],
            "reason": "结果引用不可用（B 的 intent_service 未接入）：不伪造结果卡。",
        }
    result = _service_call(service, "list_results", _conn(request), board_id)
    if isinstance(result, dict):
        return {"available": True, "source": "service", **result}
    return {"available": True, "source": "service", "results": list(result or [])}


@router.get("/api/interactive/intents/{intent_id}/recovery-preview")
async def recovery_preview(request: Request, intent_id: str) -> dict:
    """恢复预览（只读、不写库）：能续作什么、为什么不能、还有哪些未完成操作 / 依赖。"""
    service = _require_intent_service(request)
    preview = _service_call(service, "preview_recovery", _conn(request), intent_id)
    if preview is None:
        raise HTTPException(status_code=404, detail="意图不存在")
    return {"available": True, "preview": preview}


@router.post("/api/interactive/intents/{intent_id}/recover")
async def recover_intent(intent_id: str, request: Request, body: dict | None = None) -> dict:
    """恢复：把失败 / 部分完成 / 暂停的意图放回可继续状态（不自动执行操作）。"""
    payload = _body_dict(body)
    board_id = str(payload.get("boardId") or "").strip()
    if not board_id:
        raise HTTPException(status_code=400, detail="需要 boardId（意图属于哪个板面）")
    expected = payload.get("expectedVersion")
    if expected is None:
        raise HTTPException(status_code=400, detail="需要 expectedVersion（恢复按版本做 CAS）")
    try:
        expected_version = int(expected)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="expectedVersion 必须是整数") from None
    if expected_version < 1:
        raise HTTPException(status_code=400, detail="expectedVersion 必须 >= 1")
    service = _require_intent_service(request)
    result = _service_call(
        service,
        "recover_intent",
        _conn(request),
        board_id=board_id,
        intent_id=str(intent_id),
        expected_version=expected_version,
    )
    response = dict(result) if isinstance(result, dict) else {"result": result}
    response["ok"] = bool(response.get("ok"))
    return response


