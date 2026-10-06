"""互动模式：保存 / 恢复 / 提交 相关路由（子智能体 B 负责充实实现）。

契约：docs/interactive-mode-contract.md §2（B 的表）。

路由形状在这一版就冻结（其他模块与前端按它接入）；实现委托给
`agent.interactive.board_store` 与 `agent.interactive.submission`。
未实现的委托点统一转成 501 + 「尚未实现」说明，界面显示文字而不是白屏。
"""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, HTTPException, Request

from agent.interactive import board_store, models, submission

router = APIRouter()


def _conn(request: Request) -> sqlite3.Connection:
    return request.app.state.ctx.conn


def _not_implemented(exc: NotImplementedError) -> HTTPException:
    return HTTPException(status_code=501, detail=f"互动模式尚未实现：{exc}")


@router.get("/api/interactive/boards")
async def list_boards(request: Request) -> dict:
    conn = _conn(request)
    rows = conn.execute("SELECT id, title FROM boards ORDER BY created_at").fetchall()
    if not rows:
        board_store.ensure_board(conn)
        rows = conn.execute("SELECT id, title FROM boards ORDER BY created_at").fetchall()
    return {"boards": [{"id": row["id"], "title": row["title"]} for row in rows]}


@router.post("/api/interactive/boards")
async def create_board(request: Request, body: dict | None = None) -> dict:
    conn = _conn(request)
    board_id = str((body or {}).get("boardId") or models.new_id("board"))
    title = (body or {}).get("title")
    return {"board": board_store.ensure_board(conn, board_id=board_id, title=title)}


@router.get("/api/interactive/boards/{board_id}/state")
async def get_board_state(request: Request, board_id: str) -> dict:
    conn = _conn(request)
    loaded = board_store.load_board(conn, board_id)
    baseline = None
    try:
        baseline = submission.last_success_baseline(conn, board_id)
    except NotImplementedError:
        baseline = None
    rows = conn.execute(
        "SELECT id, seq, status, created_at FROM board_submissions"
        " WHERE board_id = ? ORDER BY seq DESC LIMIT 20",
        (board_id,),
    ).fetchall()
    return {
        "board": loaded["board"],
        "state": loaded["state"],
        "seq": loaded["seq"],
        "baseline": baseline,
        "submissions": [
            {
                "id": row["id"],
                "seq": int(row["seq"]),
                "status": row["status"],
                "createdAt": row["created_at"],
            }
            for row in rows
        ],
        "drafts": board_store.get_draft(conn, board_id),
    }


@router.put("/api/interactive/boards/{board_id}/state")
async def put_board_state(request: Request, board_id: str, body: dict) -> dict:
    """保存：每完成一次操作就写一次。**这里不调用 QIO**，也不做任何模型调用。"""
    conn = _conn(request)
    state = body.get("state") or {}
    result = board_store.save_board(conn, board_id, state, reason=str(body.get("reason") or "op"))
    return {"ok": True, "seq": result["seq"], "savedAt": result["savedAt"], "state": result["state"]}


@router.get("/api/interactive/boards/{board_id}/history")
async def get_board_history(request: Request, board_id: str, limit: int = 50) -> dict:
    conn = _conn(request)
    return {"snapshots": board_store.list_board_states(conn, board_id, limit=limit)}


@router.get("/api/interactive/boards/{board_id}/submissions")
async def list_submissions(request: Request, board_id: str, limit: int = 20) -> dict:
    conn = _conn(request)
    rows = conn.execute(
        "SELECT id, seq, status, visible, expressions, baseline, delivery, error, created_at"
        " FROM board_submissions WHERE board_id = ? ORDER BY seq DESC LIMIT ?",
        (board_id, limit),
    ).fetchall()
    return {
        "submissions": [
            {
                "id": row["id"],
                "seq": int(row["seq"]),
                "status": row["status"],
                "visible": models.loads(row["visible"], {}),
                "expressions": models.loads(row["expressions"], []),
                "baseline": models.loads(row["baseline"], {}),
                "delivery": models.loads(row["delivery"], {}),
                "error": row["error"],
                "createdAt": row["created_at"],
            }
            for row in rows
        ]
    }


@router.get("/api/interactive/boards/{board_id}/visible-range")
async def get_visible_range(request: Request, board_id: str) -> dict:
    """提交前可见范围预览：让用户看到「这次会交给 QIO 什么」。"""
    conn = _conn(request)
    loaded = board_store.load_board(conn, board_id)
    try:
        visible = submission.visible_range(loaded["state"])
    except NotImplementedError as exc:
        raise _not_implemented(exc) from exc
    return {"visibleRange": visible}


@router.post("/api/interactive/boards/{board_id}/submissions")
async def submit(request: Request, board_id: str, body: dict | None = None) -> dict:
    """提交：QIO 取得未提交表达的唯一入口。

    只传 `requestedVisible` 表示「用户这一批想给 QIO 看的卡片」；
    服务端仍以**已保存状态推导出的可见范围**为准，绝不接受未勾选卡片进入载荷。
    """
    conn = _conn(request)
    payload = body or {}
    requested = payload.get("requestedVisible")
    if requested is not None and not isinstance(requested, list):
        raise HTTPException(status_code=400, detail="requestedVisible must be a list of card ids")
    try:
        return await submission.submit_board(
            conn,
            board_id,
            requested_visible=[str(x) for x in requested] if requested else None,
            note=str(payload.get("note") or ""),
        )
    except NotImplementedError as exc:
        raise _not_implemented(exc) from exc


@router.get("/api/interactive/drafts/{board_id}")
async def get_drafts(request: Request, board_id: str) -> dict:
    return board_store.get_draft(_conn(request), board_id)


@router.put("/api/interactive/drafts/{board_id}")
async def put_drafts(request: Request, board_id: str, body: dict) -> dict:
    return board_store.save_draft(_conn(request), board_id, body.get("drafts") or {})
