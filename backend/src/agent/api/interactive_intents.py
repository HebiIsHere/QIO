"""互动模式：QIO 回复、虚线预览与审批 相关路由（子智能体 C 负责实现）。

契约：docs/interactive-mode-contract.md §2（C 的表）。

审批边界由服务端判定：冲突、依赖等待、材料变化、恢复，都不交给前端自己算。
路由只做参数校验与转发，业务语义全在 agent.interactive.intents。
"""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, HTTPException, Request

from agent.interactive import intents

router = APIRouter()

#: 演示执行推进允许的结果；revert_rest 用于执行「等待用户决定」的其余撤回项
ADVANCE_OUTCOMES = tuple(intents.ADVANCE_OUTCOMES) + ("revert_rest",)


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


@router.get("/api/interactive/boards/{board_id}/intents")
async def list_intents(request: Request, board_id: str) -> dict:
    """意图列表 + 冲突分组 + 批量可用性 + 恢复信息。

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
    """创建意图。演示入口走 demo=true；演示意图必须带 demo 标记，界面上要写明。"""
    conn = _conn(request)
    payload = _body_dict(body)
    if payload.get("demo"):
        created = intents.create_demo_intents(conn, board_id=board_id)
        return {
            "created": created,
            "demo": True,
            "notice": (
                "演示意图：第一阶段没有接入真实的 QIO 理解与执行，"
                "这些内容是可控演示，不代表 QIO 的真实判断。"
            ),
        }
    raise HTTPException(status_code=400, detail="本阶段只开放演示意图入口（demo=true）")


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
    """批准。confirmDependency=true 表示「前项已完成，我确认开始」。

    开始执行时记录执行者身份：只有重启后（换了 instance_id）才会被降级为暂停。
    """
    conn = _conn(request)
    confirm = bool(_body_dict(body).get("confirmDependency"))
    return intents.approve_intent(
        conn, intent_id, confirm_dependency=confirm, instance_id=_instance_id(request)
    )


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


@router.post("/api/interactive/intents/{intent_id}/demo/advance")
async def advance(intent_id: str, request: Request, body: dict | None = None) -> dict:
    """演示执行推进（明确标注为演示）：成功 / 失败 / 暂停 / 取消。

    第一阶段没有真实的模型执行；这里用可控的演示结果走通状态机与撤回保护。

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
    """批量审批：选择部分或全部；互不相容的两项不能同时批准。"""
    conn = _conn(request)
    payload = _body_dict(body)
    approve_ids = _id_list(payload, "approve")
    reject_ids = _id_list(payload, "reject")
    return intents.batch_decide(
        conn, approve=approve_ids, reject=reject_ids, instance_id=_instance_id(request)
    )
