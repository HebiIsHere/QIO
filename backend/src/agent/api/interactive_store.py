"""互动模式：保存 / 恢复 / 提交 相关路由（子智能体 B 负责实现）。

契约：docs/interactive-mode-contract.md §2（B 的表）。路径在这一版冻结，不能改。

分工：路由只做参数校验与错误码，业务在 agent.interactive.board_store 与
agent.interactive.submission 里。三条规则在这里也成立：

- PUT /state 是**自动保存**入口，只落库：不调用 QIO、不做任何模型 / 网络调用；
- POST /submissions 是 QIO 取得表达的唯一入口，第一阶段没有接入模型调用；
- 未勾选 / 隐藏的注释不进入任何提交载荷（由服务端从已保存状态推导）。

除契约字段外，响应里额外提供（只增不改，前端按需读取）：

- GET /state 与 PUT /state 带 pending：未提交的有效改动（服务端求差，不调用 QIO）；
- GET /state 带 visibleRange：本次允许查看的范围（提交前预览的便捷入口）。
"""

from __future__ import annotations

import logging
import re
import sqlite3
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from agent.interactive import board_store, models, submission

logger = logging.getLogger(__name__)

router = APIRouter()

_BOARD_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
_MAX_NOTE = 2000


def _conn(request: Request) -> sqlite3.Connection:
    return request.app.state.ctx.conn


def _body(body: Any) -> dict:
    if body is None:
        return {}
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="请求体必须是 JSON 对象")
    return body


def _limit(value: Any, *, default: int, top: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, min(number, top))


def _board_id(raw: str) -> str:
    value = (raw or "").strip()
    if not _BOARD_ID_RE.match(value):
        raise HTTPException(status_code=400, detail="boardId 只能包含字母、数字与 _.-: 且长度 1..64")
    return value


def _state_or_400(raw: Any) -> dict:
    if not isinstance(raw, dict):
        raise HTTPException(status_code=400, detail="state 必须是 JSON 对象")
    for key in ("cards", "groups", "links"):
        if key in raw and not isinstance(raw[key], list):
            raise HTTPException(status_code=400, detail=f"state.{key} 必须是数组")
    if "selection" in raw and not isinstance(raw["selection"], list):
        raise HTTPException(status_code=400, detail="state.selection 必须是数组")
    return raw


def _pending_or_empty(conn: sqlite3.Connection, board_id: str) -> dict:
    try:
        return board_store.load_pending(conn, board_id)
    except Exception:  # noqa: BLE001 - 预览字段缺失不该让读取状态失败
        return {"baselineSeq": None, "expressions": [], "updatedAt": None}


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
    payload = _body(body)
    raw_id = payload.get("boardId")
    board_id = _board_id(str(raw_id)) if raw_id else models.new_id("board")
    title = payload.get("title")
    if title is not None and not isinstance(title, str):
        raise HTTPException(status_code=400, detail="title 必须是字符串")
    existing = conn.execute("SELECT id FROM boards WHERE id = ?", (board_id,)).fetchone()
    board = board_store.ensure_board(
        conn, board_id=board_id, title=(title or "").strip() or None
    )
    return {"board": board, "created": existing is None}


@router.get("/api/interactive/boards/{board_id}/state")
async def get_board_state(request: Request, board_id: str) -> dict:
    conn = _conn(request)
    bid = _board_id(board_id)
    loaded = board_store.load_board(conn, bid)
    baseline = submission.last_success_baseline(conn, bid)
    rows = conn.execute(
        "SELECT id, seq, status, created_at FROM board_submissions"
        " WHERE board_id = ? ORDER BY seq DESC LIMIT 20",
        (bid,),
    ).fetchall()
    return {
        "board": loaded["board"],
        "state": loaded["state"],
        "seq": loaded["seq"],
        # 契约只要求「上次成功提交信息」；这里只给摘要，快照留在服务端
        "baseline": (
            {"seq": int(baseline["seq"]), "submittedAt": baseline["submittedAt"]}
            if baseline
            else None
        ),
        "submissions": [
            {
                "id": row["id"],
                "seq": int(row["seq"]),
                "status": row["status"],
                "createdAt": row["created_at"],
            }
            for row in rows
        ],
        "drafts": board_store.get_draft(conn, bid),
        "pending": _pending_or_empty(conn, bid),
        "visibleRange": submission.visible_range(loaded["state"]),
    }


@router.put("/api/interactive/boards/{board_id}/state")
async def put_board_state(request: Request, board_id: str, body: dict) -> dict:
    """保存：每完成一次操作就写一次。**这里不调用 QIO**，也不做任何模型调用。"""
    conn = _conn(request)
    bid = _board_id(board_id)
    payload = _body(body)
    state = _state_or_400(payload.get("state") or {})
    reason = payload.get("reason") or "op"
    if not isinstance(reason, str):
        raise HTTPException(status_code=400, detail="reason 必须是字符串")
    # --- N1 版本门（契约 §1.4）：普通整板写入必须先证明它基于当前已保存版本 ---
    # 按真实版本事实保护：旧版 / 未知版本的整板状态一律不落库（也不推进快照、不暂停任务），
    # 错误前端或跨页面旧请求不能静默把新版盖回旧版。
    # 这道门在 M4 确认门之前：带 confirm 的兼容路径同样先校验版本，不得成为绕过版本保护的入口。
    claimed_seq = board_store.parse_state_seq(state)
    current_seq = board_store.saved_seq(conn, bid)
    version_error = board_store.state_version_error(
        claimed=claimed_seq, current=current_seq or 0
    )
    if version_error is not None:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "stale_state",
                "reason": version_error,
                "currentSeq": current_seq or 0,
            },
        )
    # --- M4 确认门（08）：确认协议与真实落库对象一致，不只依赖前端禁用按钮 ---
    confirm = payload.get("confirm")
    check_id = ""
    if confirm is not None:
        if not isinstance(confirm, dict):
            raise HTTPException(status_code=400, detail="confirm 必须是对象 {checkId}")
        check_id = str(confirm.get("checkId") or "")
        claimed_version = confirm.get("stateVersion")
        if claimed_version is not None and not isinstance(claimed_version, (int, float)):
            raise HTTPException(status_code=400, detail="confirm.stateVersion 必须是版本号")

    from agent.interactive import intents as intents_module  # 延迟 import

    candidate_signature = intents_module.state_semantic_signature(state)
    # 服务端自己按**真实影响**算一次当前受影响任务：既用于无确认时的门，
    # 也用于带确认时重新核对范围（R6）。
    affected_now = intents_module.preview_material_impact(
        conn, board_id=bid, state=state
    )["affected"]
    running_ids = [
        item["intentId"] for item in affected_now if item.get("status") == "running"
    ]

    if check_id:
        # R6/R5：除了「记录存在 + 板面版本 + 候选签名」，保存前还要按真实影响重核范围——
        # 等待期间新出现的运行中受影响任务，旧 checkId 不得放行（不落库、不暂停、不推进快照）。
        failure = intents_module.confirmation_scope_error(
            conn,
            board_id=bid,
            check_id=check_id,
            candidate_signature=candidate_signature,
            state=state,
        )
        if failure is None:
            claimed = confirm.get("stateVersion")
            if claimed is not None:
                # 确认协议里的 stateVersion（如前端提供）必须与影响预判绑定的版本一致
                entry_version = intents_module.confirm_binding_version(check_id)
                if entry_version is not None and int(claimed) != int(entry_version):
                    failure = {
                        "reason": "确认时给出的版本与当时影响预判绑定的版本不一致：请重新预判并确认。",
                        "scopeChanged": False,
                    }
        if failure is not None:
            scope_changed = bool(failure.get("scopeChanged"))
            raise HTTPException(
                status_code=409,
                detail={
                    # 保留既有形状：error 仍是 stale_check；范围变化时 scopeChanged=true，
                    # affectedTasks 始终是**当前的**全量受影响任务，前端据此重新说明。
                    "error": "stale_check",
                    "reason": failure["reason"],
                    "scopeChanged": scope_changed,
                    "affectedTasks": failure.get("affectedTasks") or affected_now,
                },
            )
    elif running_ids:
        # 服务端门：这次保存会改变执行中任务依赖的材料，但没有对应的影响确认记录
        raise HTTPException(
            status_code=409,
            detail={
                "error": "impact_confirmation_required",
                "reason": (
                    "这次保存会修改执行中任务依赖的材料：需要先做影响预判并确认后才会生效；"
                    "取消则这次改动不生效、任务继续。"
                ),
                "affectedTasks": affected_now,
            },
        )

    try:
        result = board_store.save_board(conn, bid, state, reason=reason)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # 保存生效之后，服务端自己再判定一次「这次改动有没有碰到执行中任务依赖的材料」：
    # 命中就把那些任务置为 paused（保留进度），并在响应里报出来。
    # 判定不依赖前端（前端只负责在保存前给用户看影响说明）；
    # 这里失败也绝不能让保存失败 —— 保存本身是用户的操作，必须落库。
    material_impact: dict = {"paused": [], "affected": []}
    try:
        from agent.interactive import intents as intents_module

        on_saved = getattr(intents_module, "on_board_saved", None)
        if callable(on_saved):
            material_impact = on_saved(conn, board_id=bid, state=result["state"], reason=reason)
    except Exception as exc:  # noqa: BLE001 - 保护性收尾，不能影响保存结果
        material_impact = {"paused": [], "affected": [], "error": str(exc)}

    return {
        "ok": True,
        "seq": result["seq"],
        "savedAt": result["savedAt"],
        "state": result["state"],
        "pending": result.get("pending") or {"baselineSeq": None, "expressions": []},
        "materialImpact": material_impact,
        "confirmedCheckId": check_id or None,
    }


@router.get("/api/interactive/boards/{board_id}/history")
async def get_board_history(request: Request, board_id: str, limit: int = 50) -> dict:
    conn = _conn(request)
    bid = _board_id(board_id)
    return {
        "snapshots": board_store.list_board_states(
            conn, bid, limit=_limit(limit, default=50, top=200)
        )
    }


@router.get("/api/interactive/boards/{board_id}/submissions")
async def list_submissions(request: Request, board_id: str, limit: int = 20) -> dict:
    conn = _conn(request)
    bid = _board_id(board_id)
    rows = conn.execute(
        "SELECT id, seq, status, visible, expressions, baseline, delivery, error, created_at"
        " FROM board_submissions WHERE board_id = ? ORDER BY seq DESC LIMIT ?",
        (bid, _limit(limit, default=20, top=200)),
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
    bid = _board_id(board_id)
    loaded = board_store.load_board(conn, bid)
    visible = submission.visible_range(loaded["state"])
    return {
        "visibleRange": visible,
        "checkedClearedHint": "提交成功后这些勾选会自动取消（不是删除或撤回）",
    }


@router.post("/api/interactive/boards/{board_id}/submissions")
async def submit(request: Request, board_id: str, body: dict | None = None) -> dict:
    """提交：QIO 取得未提交表达的唯一入口。

    只传 requestedVisible 表示「用户这一批想给 QIO 看的卡片」；
    服务端仍以**已保存状态推导出的可见范围**为准，绝不接受未勾选卡片进入载荷。
    """
    conn = _conn(request)
    bid = _board_id(board_id)
    payload = _body(body)
    requested = payload.get("requestedVisible")
    if requested is not None and not isinstance(requested, list):
        raise HTTPException(status_code=400, detail="requestedVisible 必须是卡片 id 数组")
    note = payload.get("note") or ""
    if not isinstance(note, str):
        raise HTTPException(status_code=400, detail="note 必须是字符串")
    if len(note) > _MAX_NOTE:
        raise HTTPException(status_code=400, detail=f"note 过长（最多 {_MAX_NOTE} 字）")
    base_version = payload.get("baseStateVersion")
    if base_version is not None and not isinstance(base_version, (int, float, str)):
        raise HTTPException(status_code=400, detail="baseStateVersion 必须是板面版本号")
    try:
        return await submission.submit_board(
            conn,
            bid,
            requested_visible=[str(item) for item in requested] if requested else None,
            note=note,
            base_state_version=base_version,
            confirmed_check_id=payload.get("confirmedCheckId"),
            # 与理解路由共用同一个 adapter 注入点：提交 → 受理 → 后台理解也走同一条链路
            adapter_provider=_adapter_provider(request),
        )
    except submission.StaleState as exc:
        # M4 路径 3：候选版本 / 确认范围与服务端不一致 → 不落库
        raise HTTPException(
            status_code=409, detail={"error": "stale_state", "reason": str(exc)}
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/api/interactive/drafts/{board_id}")
async def get_drafts(request: Request, board_id: str) -> dict:
    bid = _board_id(board_id)
    return board_store.get_draft(_conn(request), bid)


@router.put("/api/interactive/drafts/{board_id}")
async def put_drafts(request: Request, board_id: str, body: dict) -> dict:
    """保存草稿（未提交的文字输入）。草稿不是提交内容，**不调用 QIO**。"""
    bid = _board_id(board_id)
    drafts = _body(body).get("drafts")
    if drafts is not None and not isinstance(drafts, dict):
        raise HTTPException(status_code=400, detail="drafts 必须是 JSON 对象")
    try:
        return board_store.save_draft(_conn(request), bid, drafts)
    except board_store.DraftTooLong as exc:
        # 09：超限明确拒绝（不截短、不返回成功），错误形状按契约定稿
        raise HTTPException(
            status_code=422,
            detail={"error": "draft_too_long", "limit": exc.limit, "keys": exc.keys},
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# --- 理解过程：列表 / 重试 / 取消（契约 §14.4，A 的新增路由） ----------------
#
# 受理（建立理解请求）在提交路径里完成；这里只做三件事：
#   1. GET  本板面的理解过程列表（?submissionId= 过滤）；
#   2. POST 从**保存的快照**重试失败的理解（不重读当前板面）；
#   3. POST 取消尚未落地的理解（还在 accepted / understanding 的）。
#
# 理解模块（B 的 agent.interactive.understanding）用**延迟 import**：B 还没合并时
# 返回明确的 503，而不是让路由崩掉 —— A 的分支必须能独立跑。
# 模型适配器从 request.app.state.ctx 取（与 services/app.py 的 build_adapter 同一条链路）。

_UNDERSTANDING_LIMIT = 50


def _understanding_module():
    """延迟 import B 的理解模块；没有它时给出明确错误（A 分支可独立跑）。"""
    try:
        from agent.interactive import understanding  # 延迟 import：B 的模块
    except Exception as exc:  # noqa: BLE001 - 模块缺失 / 加载失败都是「未接入」
        raise HTTPException(
            status_code=503,
            detail={
                "error": "understanding_unavailable",
                "reason": (
                    "理解模块尚未接入（agent.interactive.understanding）："
                    f"{type(exc).__name__}: {exc}"
                ),
            },
        ) from exc
    return understanding


def _adapter_provider(request: Request):
    """async () -> BaseAdapter | None：复用 QIO 既有凭据 / 用量 / 预算链路。

    优先级（与 C 的 interactive_intents.py 保持一致，端到端验收只认这一条注入点）：
    1. `app.state.interactive_adapter_provider` —— 测试 / 联调显式注入的 provider；
    2. `app.state.ctx.build_adapter` —— 生产路径（services/app.py 的同一条链路）。
    两者都没有（或构建失败）时如实返回 None —— 由理解模块给出可读的失败原因，
    路由层绝不抛异常、也绝不自己拿密钥。
    """
    override = getattr(request.app.state, "interactive_adapter_provider", None)
    if callable(override):
        return override
    ctx = getattr(request.app.state, "ctx", None)
    builder = getattr(ctx, "build_adapter", None)

    if builder is None or not callable(builder):
        async def _no_adapter():
            return None

        return _no_adapter

    async def provider():
        try:
            return await builder()
        except Exception:  # noqa: BLE001 - 没有可用凭据 / adapter 时如实降级
            logger.warning("构建互动理解的 adapter 失败（按没有可用模型处理）", exc_info=True)
            return None

    return provider


def _understanding_id(raw: str) -> str:
    value = (raw or "").strip()
    if not _BOARD_ID_RE.match(value):
        raise HTTPException(
            status_code=400,
            detail="understandingId 只能包含字母、数字与 _.-: 且长度 1..64",
        )
    return value


def _understanding_row(
    conn: sqlite3.Connection, understanding_id: str, *, board_id: str | None = None
):
    if board_id is None:
        row = conn.execute(
            "SELECT * FROM board_understanding WHERE id = ?", (understanding_id,)
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT * FROM board_understanding WHERE id = ? AND board_id = ?",
            (understanding_id, board_id),
        ).fetchone()
    if row is None:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "understanding_not_found",
                "reason": f"没有这条理解记录：{understanding_id}",
            },
        )
    return row


def _as_dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list:
    return value if isinstance(value, list) else []


def _understanding_payload(row) -> dict:
    """把 board_understanding 的一行转成前端 Understanding 形状（只增字段，不改含义）。"""
    data = dict(row)
    progress = models.loads(data.get("progress"), {})
    progress = progress if isinstance(progress, (dict, list)) else {}
    output = _as_dict(models.loads(data.get("output"), {}))
    validation = _as_dict(models.loads(data.get("validation"), {}))
    materials = _as_list(models.loads(data.get("materials"), []))
    memory = _as_list(models.loads(data.get("memory"), []))
    if isinstance(progress, list):
        steps = progress
    else:
        steps = _as_list(progress.get("steps"))
    intent_ids = output.get("intentIds") or output.get("intent_ids") or []
    return {
        "id": data.get("id"),
        "boardId": data.get("board_id"),
        "submissionId": data.get("submission_id"),
        "requestId": data.get("request_id"),
        "status": data.get("status"),
        "stage": data.get("stage") or "",
        "kind": output.get("kind"),
        "steps": steps,
        "materials": materials,
        "summary": output.get("summary") or "",
        "clarification": output.get("clarification"),
        "validation": {
            "ok": bool(validation.get("ok", False)),
            "reasons": [str(item) for item in _as_list(validation.get("reasons"))],
        },
        "error": data.get("error"),
        "attempt": int(data.get("attempt") or 1),
        "adapter": data.get("adapter") or "",
        "intentIds": [str(item) for item in _as_list(intent_ids)],
        "memory": memory,
        "createdAt": data.get("created_at"),
        "updatedAt": data.get("updated_at"),
        "startedAt": data.get("started_at"),
        "finishedAt": data.get("finished_at"),
        # 只增字段：前端据此显示「能不能从保存的快照重试」，不泄露快照内容
        "hasSnapshot": bool(models.loads(data.get("snapshot"), {})),
        "progress": progress,
    }


def _understanding_rows(
    conn: sqlite3.Connection, board_id: str, submission_id: str | None
) -> list:
    if submission_id:
        return conn.execute(
            "SELECT * FROM board_understanding WHERE board_id = ? AND submission_id = ?"
            " ORDER BY created_at DESC, rowid DESC LIMIT ?",
            (board_id, submission_id, _UNDERSTANDING_LIMIT),
        ).fetchall()
    return conn.execute(
        "SELECT * FROM board_understanding WHERE board_id = ?"
        " ORDER BY created_at DESC, rowid DESC LIMIT ?",
        (board_id, _UNDERSTANDING_LIMIT),
    ).fetchall()


@router.get("/api/interactive/boards/{board_id}/understanding")
async def get_board_understanding(
    request: Request, board_id: str, submissionId: str | None = None
) -> dict:
    """本板面的理解过程列表；?submissionId= 只看某一次提交的理解。"""
    module = _understanding_module()
    conn = _conn(request)
    bid = _board_id(board_id)
    wanted = (submissionId or "").strip() or None

    lister = getattr(module, "list_understandings", None)
    if callable(lister):
        payload = lister(conn, board_id=bid, submission_id=wanted)
        if isinstance(payload, dict) and "understandings" in payload:
            return payload

    items = [_understanding_payload(row) for row in _understanding_rows(conn, bid, wanted)]
    return {"understanding": items[0] if items else None, "understandings": items}


@router.post("/api/interactive/understanding/{understanding_id}/retry")
async def retry_understanding(request: Request, understanding_id: str) -> dict:
    """从**保存的快照**重试失败的理解（不重读当前板面、不重建同一批任务）。"""
    module = _understanding_module()
    conn = _conn(request)
    uid = _understanding_id(understanding_id)
    runner = getattr(module, "run_understanding", None)
    if not callable(runner):
        raise HTTPException(
            status_code=503,
            detail={
                "error": "understanding_unavailable",
                "reason": "理解模块没有 run_understanding：无法重试",
            },
        )

    row = _understanding_row(conn, uid)
    status = str(row["status"])
    if status in models.UNDERSTANDING_ACTIVE_STATUSES:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "understanding_active",
                "reason": f"这条理解还在进行中（{status}），不能重试",
            },
        )
    if status != "failed":
        raise HTTPException(
            status_code=409,
            detail={
                "error": "not_retryable",
                "reason": (
                    f"只有失败的理解可以重试（当前 {status}）：重试只从保存的快照恢复，"
                    "不重读当前板面"
                ),
            },
        )
    snapshot = models.loads(row["snapshot"], {})
    if not isinstance(snapshot, dict) or not snapshot:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "no_snapshot",
                "reason": "这条理解没有保存的快照，不能重试（重试不重读当前板面）",
            },
        )

    conn.execute(
        "UPDATE board_understanding SET status = 'accepted', error = NULL,"
        " attempt = attempt + 1, stage = ?, updated_at = ?, started_at = NULL,"
        " finished_at = NULL WHERE id = ?",
        ("已受理：从保存的快照重试", models.now_iso(), uid),
    )
    dispatch = submission.schedule_understanding(
        conn, understanding_id=uid, adapter_provider=_adapter_provider(request)
    )
    return {
        "ok": True,
        "understanding": _understanding_payload(_understanding_row(conn, uid)),
        "background": dispatch,
    }


@router.post("/api/interactive/boards/{board_id}/understanding/{understanding_id}/cancel")
async def cancel_understanding(request: Request, board_id: str, understanding_id: str) -> dict:
    """取消尚未落地的理解（还在 accepted / understanding 的）。

    冻结的状态集合里没有 cancelled：取消把记录落成 failed，并把「用户取消」写进
    error（可读、可从保存的快照重试），绝不假装理解成功或产出意图。
    """
    _understanding_module()
    conn = _conn(request)
    bid = _board_id(board_id)
    uid = _understanding_id(understanding_id)
    row = _understanding_row(conn, uid, board_id=bid)
    status = str(row["status"])
    if status not in models.UNDERSTANDING_ACTIVE_STATUSES:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "not_cancellable",
                "reason": f"这条理解已经结束（{status}），不能取消",
            },
        )

    stopped = submission.cancel_background_understanding(uid)
    reason = (
        "这次理解已被取消（用户取消）：没有产出意图，可以从保存的快照重试"
        + ("；已停止在跑的后台任务" if stopped else "；后台任务当时不在运行")
    )
    stamp = models.now_iso()
    conn.execute(
        "UPDATE board_understanding SET status = 'failed', error = ?, updated_at = ?,"
        " finished_at = ? WHERE id = ? AND status IN ('accepted', 'understanding')",
        (reason, stamp, stamp, uid),
    )
    return {
        "ok": True,
        "cancelled": True,
        "stoppedBackground": stopped,
        "understanding": _understanding_payload(_understanding_row(conn, uid, board_id=bid)),
    }
