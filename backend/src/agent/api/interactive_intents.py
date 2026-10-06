"""互动模式：QIO 回复、虚线预览与审批 相关路由（子智能体 C 负责充实实现）。

契约：docs/interactive-mode-contract.md §2（C 的表）。

审批边界由服务端判定：冲突、依赖等待、材料变化、恢复，都不交给前端自己算。
"""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, HTTPException, Request

from agent.interactive import intents

router = APIRouter()


def _conn(request: Request) -> sqlite3.Connection:
    return request.app.state.ctx.conn


def _bad(exc: NotImplementedError) -> HTTPException:
    return HTTPException(status_code=501, detail=f"互动模式尚未实现：{exc}")


@router.get("/api/interactive/boards/{board_id}/intents")
async def list_intents(request: Request, board_id: str) -> dict:
    conn = _conn(request)
    try:
        payload = intents.list_intents(conn, board_id)
    except NotImplementedError as exc:
        raise _bad(exc) from exc
    try:
        payload["recovery"] = intents.recover_running_intents(conn, board_id)
    except NotImplementedError:
        payload.setdefault("recovery", {"paused": []})
    return payload


@router.post("/api/interactive/boards/{board_id}/intents")
async def create_intents(request: Request, board_id: str, body: dict | None = None) -> dict:
    """创建意图。演示入口走 `{"demo": true}`；演示意图必须带 demo 标记，界面上要写明。"""
    conn = _conn(request)
    payload = body or {}
    if payload.get("demo"):
        try:
            created = intents.create_demo_intents(conn, board_id=board_id)
        except NotImplementedError as exc:
            raise _bad(exc) from exc
        return {"created": created, "demo": True}
    raise HTTPException(status_code=400, detail="本阶段只开放演示意图入口（demo=true）")


@router.post("/api/interactive/intents/{intent_id}/approve")
async def approve(intent_id: str, request: Request, body: dict | None = None) -> dict:
    conn = _conn(request)
    confirm = bool((body or {}).get("confirmDependency"))
    try:
        return intents.approve_intent(conn, intent_id, confirm_dependency=confirm)
    except NotImplementedError as exc:
        raise _bad(exc) from exc


@router.post("/api/interactive/intents/{intent_id}/reject")
async def reject(intent_id: str, request: Request) -> dict:
    conn = _conn(request)
    try:
        return intents.reject_intent(conn, intent_id)
    except NotImplementedError as exc:
        raise _bad(exc) from exc


@router.post("/api/interactive/intents/{intent_id}/preview")
async def update_preview(intent_id: str, request: Request, body: dict) -> dict:
    conn = _conn(request)
    try:
        return intents.update_preview(conn, intent_id, body.get("preview") or {})
    except NotImplementedError as exc:
        raise _bad(exc) from exc


@router.post("/api/interactive/intents/{intent_id}/demo/advance")
async def advance(intent_id: str, request: Request, body: dict | None = None) -> dict:
    """演示执行推进（明确标注为演示）：成功 / 失败 / 暂停 / 取消。

    第一阶段没有真实的模型执行；这里用可控的演示结果走通状态机与撤回保护。
    """
    conn = _conn(request)
    outcome = str((body or {}).get("outcome") or "done")
    try:
        return intents.advance_intent(conn, intent_id, outcome=outcome)
    except NotImplementedError as exc:
        raise _bad(exc) from exc


@router.post("/api/interactive/intents/batch")
async def batch(request: Request, body: dict) -> dict:
    conn = _conn(request)
    approve_ids = [str(x) for x in (body.get("approve") or [])]
    reject_ids = [str(x) for x in (body.get("reject") or [])]
    try:
        return intents.batch_decide(conn, approve=approve_ids, reject=reject_ids)
    except NotImplementedError as exc:
        raise _bad(exc) from exc
