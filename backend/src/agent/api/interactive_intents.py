"""互动模式：QIO 回复、虚线预览与审批 相关路由（子智能体 C 负责实现）。

契约：docs/interactive-mode-contract.md §2 / §14.4（C 的表）。

审批边界由服务端判定：冲突、依赖等待、材料变化、恢复，都不交给前端自己算。
路由只做参数校验、转发与**启动真实执行**；业务语义全在
agent.interactive.intents（仓库层）与 agent.interactive.execution（真实执行）。
"""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, HTTPException, Request

from agent.interactive import execution as execution_module
from agent.interactive import intents

router = APIRouter()

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
        owner=request.app,
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

