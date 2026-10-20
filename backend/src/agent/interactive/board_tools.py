# -*- coding: utf-8 -*-
"""阶段三 B：互动模式 12 个板面工具（契约 §13.4）。

规则：

* 板面身份、提交身份、等待与授权只从 runtime.require_board_turn() 的可信绑定取；
  模型传入的 board_id 一律不采信；普通模式没有绑定 → failed/interactive_unavailable；
* 参数模型来自冻结的 interactive/tool_models.py（arg_model），registry 生成 schema；
* 写工具先在权威操作账本登记（operation_key 幂等 / 请求 hash / 授权检查），再在
  同一事务里做板面 CAS 写入（board_store.write_board_cas）；冲突只暂停受影响步骤；
* 模型漏列 operation_key 不能掩盖未完成：账本由程序写，finish 由程序核对；
* 契约里「给模型看的固定文案」只在 prompts.py；本模块只引用 TOOL_DESCRIPTIONS。
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable, Mapping

from agent.interactive import board, board_store, intent_service, models, runtime, tool_models
from agent.prompts import TOOL_DESCRIPTIONS
from agent.storage.db import transaction
from agent.tools.base import TOOL_RESULT_PROTOCOL_VERSION, Tool, ToolResult, ToolStatus
from agent.tools.contracts import validate_model

logger = logging.getLogger(__name__)

PROTOCOL_VERSION = TOOL_RESULT_PROTOCOL_VERSION


def _safe_error(exc: BaseException) -> str:
    from agent.trace.redact import redact_text

    return redact_text(f"{type(exc).__name__}: {exc}")


def _result(
    ok: bool,
    status: str,
    code: str,
    *,
    data: Mapping[str, Any] | None = None,
    error: str | None = None,
    recoverable: bool | None = None,
) -> ToolResult:
    payload = dict(data or {})
    # content 只放由程序数据生成的一句话（不是固定文案来源）；完整事实在 data 里。
    summary = code
    if payload.get("operation_key"):
        summary += " operation_key=" + str(payload["operation_key"])
    return ToolResult(
        ok=ok,
        content=summary,
        error=error,
        category=None if ok else code,
        recoverable=recoverable,
        protocol_version=PROTOCOL_VERSION,
        status=status,
        code=code,
        data=payload,
    )


def _ok(action: str, data: Mapping[str, Any], *, status: str = ToolStatus.COMPLETED.value, code: str = "ok") -> ToolResult:
    body = {"action": action, **dict(data)}
    return _result(True, status, code, data=body)


def _fail(
    action: str,
    code: str,
    message: str,
    *,
    status: str = ToolStatus.FAILED.value,
    data: Mapping[str, Any] | None = None,
    recoverable: bool | None = None,
) -> ToolResult:
    body = {"action": action, **dict(data or {})}
    return _result(False, status, code, data=body, error=message, recoverable=recoverable)


_SERVICE_STATUS = {
    "awaiting_approval": ToolStatus.AWAITING_APPROVAL.value,
    "awaiting_input": ToolStatus.AWAITING_INPUT.value,
    "conflict": ToolStatus.CONFLICT.value,
    "running": ToolStatus.RUNNING.value,
    "paused": ToolStatus.FAILED.value,
    "not_found": ToolStatus.FAILED.value,
}


def _from_service(action: str, payload: Mapping[str, Any], *, ok_status: str = ToolStatus.COMPLETED.value) -> ToolResult:
    if payload.get("ok"):
        data = dict(payload)
        data.pop("ok", None)
        return _ok(action, data, status=ok_status, code=str(payload.get("code") or "ok"))
    status = _SERVICE_STATUS.get(str(payload.get("status") or ""), ToolStatus.FAILED.value)
    return _fail(
        action,
        str(payload.get("code") or "failed"),
        str(payload.get("error") or "板面服务拒绝了本次操作"),
        status=status,
        data={key: value for key, value in payload.items() if key not in ("ok", "code", "status", "error")},
    )


def _redacted_failure(action: str, exc: BaseException) -> ToolResult:
    logger.warning("board tool %s failed", action, exc_info=True)
    return _fail(
        action,
        "internal_error",
        _safe_error(exc),
        status=ToolStatus.FAILED.value,
        recoverable=True,
    )


# -- 公共读取 / 绑定 ---------------------------------------------------------


def _conn(tool: "_BoardTool") -> Any:
    return tool.app.conn


def _latest_state(tool: "_BoardTool", turn: "Any") -> dict[str, Any]:
    return board_store.current_state(_conn(tool), turn.board_id)


def _visible_ids(state: Mapping[str, Any]) -> set[str]:
    return {
        str(card["id"])
        for card in models.selectable_cards(state)
        if isinstance(card, dict) and card.get("id")
    }


def _card_by_id(state: Mapping[str, Any], card_id: str) -> dict | None:
    for card in state.get("cards") or []:
        if isinstance(card, dict) and str(card.get("id")) == str(card_id):
            return card
    return None


def _group_by_id(state: Mapping[str, Any], group_id: str) -> dict | None:
    for group in state.get("groups") or []:
        if isinstance(group, dict) and str(group.get("id")) == str(group_id) and not group.get("deleted"):
            return group
    return None


def _link_by_id(state: Mapping[str, Any], link_id: str) -> dict | None:
    for link in state.get("links") or []:
        if isinstance(link, dict) and str(link.get("id")) == str(link_id) and not link.get("deleted"):
            return link
    return None


def _card_snapshot(card: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": str(card.get("id") or ""),
        "kind": str(card.get("kind") or ""),
        "version": int(card.get("version", 1) or 1),
        "content": str(card.get("content") or ""),
        "title": str((card.get("meta") or {}).get("title") or "") if isinstance(card.get("meta"), dict) else "",
        "x": float(card.get("x") or 0.0),
        "y": float(card.get("y") or 0.0),
        "w": float(card.get("w") or 0.0),
        "h": float(card.get("h") or 0.0),
        "checked": bool(card.get("checked", False)),
        "hidden": bool(card.get("hidden", False)),
    }


def _group_snapshot(group: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": str(group.get("id") or ""),
        "version": int(group.get("version", 1) or 1),
        "name": str(group.get("name") or ""),
        "ordered": bool(group.get("ordered", False)),
        "members": [str(item) for item in group.get("members") or []],
    }


def _link_snapshot(link: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": str(link.get("id") or ""),
        "version": int(link.get("version", 1) or 1),
        "src": str(link.get("src") or ""),
        "dst": str(link.get("dst") or ""),
        "direction": bool(link.get("direction", False)),
        "meaning": str(link.get("meaning") or ""),
    }


def _intent_or_error(
    tool: "_BoardTool", turn: "Any", intent_id: str
) -> tuple[dict | None, ToolResult | None]:
    view = intent_service.get_intent(_conn(tool), turn.board_id, intent_id)
    if view is None:
        return None, _fail("board", "intent_not_found", "意图不存在或不属于当前板面", status=ToolStatus.FAILED.value)
    return view, None



def _authorization_gate(action: str, view: Mapping[str, Any]) -> ToolResult | None:
    """写工具的统一前置门：状态 / 有效授权先生效，再做动作级范围检查。"""
    status = str(view.get("status") or "")
    if status in (intent_service.STATUS_AWAITING, intent_service.STATUS_PROPOSED):
        return _fail(
            action,
            "awaiting_approval",
            "意图尚未获得用户确认，不能执行板面写入",
            status=ToolStatus.AWAITING_APPROVAL.value,
        )
    if status == intent_service.STATUS_PAUSED:
        return _fail(action, "intent_paused", "意图已暂停，不能执行新的写入")
    if status in intent_service.TERMINAL_STATUSES:
        return _fail(action, "intent_finished", f"意图状态为 {status}，不能继续执行")
    if not view.get("authorization_valid"):
        return _fail(
            action,
            "authorization_stale",
            "旧授权已失效（意图范围已变化），需要用户重新确认",
            status=ToolStatus.AWAITING_APPROVAL.value,
        )
    return None


def _scope_of(view: Mapping[str, Any]) -> dict[str, Any]:
    authorization = view.get("authorization") if isinstance(view.get("authorization"), dict) else {}
    scope = authorization.get("scope") if isinstance(authorization.get("scope"), dict) else {}
    return scope


def _resolve_position(
    tool: "_BoardTool", turn: "Any", state: Mapping[str, Any], position: Any
) -> tuple[dict[str, float] | None, str | None]:
    """把 Position 解析成板面坐标；relative 只引用正式且获准读取的卡片。"""
    if position is None:
        return None, None
    if position.kind == "absolute":
        return {"x": float(position.x), "y": float(position.y)}, None
    target_id = str(position.relative_to or "").strip()
    if target_id not in _visible_ids(state):
        return None, f"relative 位置只能引用获准读取的卡片：{target_id}"
    target = _card_by_id(state, target_id)
    if target is None:
        return None, f"relative 位置引用的卡片不存在：{target_id}"
    x, y, w, h = board.card_rect(target)
    gap = float(position.gap if position.gap is not None else 24.0)
    side = position.side or "right"
    if side == "right":
        return {"x": x + w + gap, "y": y}, None
    if side == "left":
        return {"x": x - gap, "y": y}, None
    if side == "below":
        return {"x": x, "y": y + h + gap}, None
    return {"x": x, "y": y - gap}, None


def _required_card_versions(args: Any) -> tuple[dict[str, int], list[str]]:
    raw = dict(getattr(args, "expected_versions", None) or {})
    normalized: dict[str, int] = {}
    for card_id, version in raw.items():
        normalized[str(card_id)] = int(version)
    missing = [str(card_id) for card_id in getattr(args, "card_ids", None) or [] if str(card_id) not in normalized]
    return normalized, missing


def _versions_map(state: Mapping[str, Any], card_ids: list[str], group_ids: list[str] = (), link_ids: list[str] = ()) -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = {"cards": {}, "groups": {}, "links": {}}
    for card_id in card_ids:
        card = _card_by_id(state, card_id)
        if card is not None:
            out["cards"][str(card_id)] = int(card.get("version", 1) or 1)
    for group_id in group_ids:
        group = _group_by_id(state, group_id)
        if group is not None:
            out["groups"][str(group_id)] = int(group.get("version", 1) or 1)
    for link_id in link_ids:
        link = _link_by_id(state, link_id)
        if link is not None:
            out["links"][str(link_id)] = int(link.get("version", 1) or 1)
    return out


# -- 写操作事务封装 ----------------------------------------------------------


def _run_write(
    tool: "_BoardTool",
    turn: "Any",
    args: Any,
    *,
    action: str,
    before: Mapping[str, Any] | None,
    expected_versions: Mapping[str, Any],
    mutate: Callable[[dict], tuple[dict, dict]],
) -> ToolResult:
    """授权 + 幂等账本 + CAS 板面写入 + 操作落账；冲突只暂停本次受影响步骤。"""
    conn = _conn(tool)
    try:
        with transaction(conn):
            entered = intent_service.begin_operation(
                conn,
                board_id=turn.board_id,
                intent_id=str(args.intent_id),
                action=action,
                operation_key=str(args.operation_key),
                request_payload=args.model_dump(mode="json"),
                before=before or {},
            )
            if not entered.get("ok"):
                return _from_service(action, entered)
            if entered.get("replayed"):
                operation = entered.get("operation") or {}
                return _ok(
                    action,
                    {
                        "board_id": turn.board_id,
                        "intent_id": str(args.intent_id),
                        "intent_version": int((entered.get("intent") or {}).get("version") or 1),
                        "operation_key": str(args.operation_key),
                        "replayed": True,
                        "result": operation.get("result") or {},
                        "after": operation.get("after") or {},
                    },
                    code="replayed",
                )
            saved = board_store.write_board_cas(
                conn,
                turn.board_id,
                expected_versions=dict(expected_versions or {}),
                mutate=mutate,
                reason=f"board_tool:{action}",
            )
            if not saved.get("ok"):
                intent_service.fail_operation(
                    conn,
                    str(entered["operation_id"]),
                    status=intent_service.OP_CONFLICT,
                    error="version_conflict",
                    result={"conflicts": saved.get("conflicts") or []},
                )
                return _fail(
                    action,
                    "version_conflict",
                    "版本冲突：只暂停受影响步骤，其它独立操作可继续",
                    status=ToolStatus.CONFLICT.value,
                    data={
                        "board_id": turn.board_id,
                        "intent_id": str(args.intent_id),
                        "operation_key": str(args.operation_key),
                        "conflicts": saved.get("conflicts") or [],
                        "seq": saved.get("seq"),
                        "intent_still_approved": True,
                    },
                    recoverable=True,
                )
            payload = saved.get("result") or {}
            intent_service.complete_operation(
                conn,
                str(entered["operation_id"]),
                after=payload.get("after") or {},
                result=payload.get("result") or {},
            )
            intent_view = intent_service.get_intent(conn, turn.board_id, str(args.intent_id)) or {}
        data = {
            "board_id": turn.board_id,
            "intent_id": str(args.intent_id),
            "intent_version": int(intent_view.get("version") or 1),
            "intent_status": intent_view.get("status"),
            "operation_key": str(args.operation_key),
            "operation_id": entered.get("operation_id"),
            "replayed": False,
            "seq": saved.get("seq"),
            "result": payload.get("result") or {},
            "after": payload.get("after") or {},
            "versions": payload.get("versions") or {},
        }
        return _ok(action, data, code=str(payload.get("code") or "applied"))
    except Exception as exc:  # noqa: BLE001 - 单次工具失败不破坏主循环
        return _redacted_failure(action, exc)


# -- 工具基类 ---------------------------------------------------------------


class _BoardTool(Tool):
    """只从 runtime 可信绑定取板面；参数模型 / 描述来自冻结来源。"""

    action: str = ""
    is_concurrency_safe = False

    def __init__(self, app: Any) -> None:
        self.app = app
        self.description = TOOL_DESCRIPTIONS[self.name]
        self.arg_model = tool_models.TOOL_ARG_MODELS[self.name]
        self.parameters = board_store and None  # 占位，run 前由注册表用 arg_model 生成
        from agent.tools.contracts import json_schema_for

        self.parameters = json_schema_for(self.arg_model)
        self.is_concurrency_safe = self.name in tool_models.READ_ONLY_TOOLS

    async def run(self, **kwargs: Any) -> ToolResult:
        action = self.name
        args, errors = validate_model(self.arg_model, kwargs)
        if args is None:
            return _fail(
                action,
                "invalid_arguments",
                "参数不合法，请修正后重试：" + "；".join(errors[:5]),
                data={"field_errors": errors[:10]},
            )
        try:
            turn = runtime.require_board_turn()
        except runtime.BoardToolUnavailable as exc:
            return _fail(action, exc.code, str(exc), status=ToolStatus.FAILED.value)
        try:
            return await self._run(turn, args)
        except Exception as exc:  # noqa: BLE001
            return _redacted_failure(action, exc)

    async def _run(self, turn: Any, args: Any) -> ToolResult:  # pragma: no cover - 抽象
        raise NotImplementedError


# ---------------------------------------------------------------------------
# 只读工具
# ---------------------------------------------------------------------------


class BoardInspectTool(_BoardTool):
    name = "board_inspect"

    async def _run(self, turn: Any, args: Any) -> ToolResult:
        conn = _conn(self)
        state = _latest_state(self, turn)
        visible = _visible_ids(state)
        cards = []
        for card in state.get("cards") or []:
            if not isinstance(card, dict) or str(card.get("id")) not in visible:
                continue
            cards.append(
                {
                    "id": str(card["id"]),
                    "kind": str(card.get("kind") or ""),
                    "version": int(card.get("version", 1) or 1),
                    "checked": bool(card.get("checked", False)),
                    "hidden": bool(card.get("hidden", False)),
                    "folded": bool(card.get("folded", False)),
                    "bookmarked": bool(card.get("bookmarked", False)),
                    "title": str((card.get("meta") or {}).get("title") or ""),
                    "content_preview": str(card.get("content") or "")[:120],
                }
            )
        groups = []
        for group in state.get("groups") or []:
            if not isinstance(group, dict) or group.get("deleted"):
                continue
            members = [str(item) for item in group.get("members") or [] if str(item) in visible]
            if not members:
                continue
            groups.append(
                {
                    "id": str(group["id"]),
                    "version": int(group.get("version", 1) or 1),
                    "name": str(group.get("name") or ""),
                    "ordered": bool(group.get("ordered", False)),
                    "members": members,
                }
            )
        links = []
        for link in state.get("links") or []:
            if not isinstance(link, dict) or link.get("deleted"):
                continue
            if str(link.get("src")) in visible and str(link.get("dst")) in visible:
                links.append(_link_snapshot(link))
        intent_view = intent_service.list_intents_view(conn, turn.board_id)
        waiting = intent_service.list_waiting_view(conn, turn.board_id)
        return _ok(
            self.name,
            {
                "board_id": turn.board_id,
                "submission_id": turn.submission_id,
                "seq": intent_view.get("seq"),
                "board_identity_source": "runtime_board_turn_context",
                "cards": cards,
                "groups": groups,
                "links": links,
                "intents": intent_view.get("intents") or [],
                "operations": intent_view.get("operations") or {},
                "waiting": waiting,
                "counts": {
                    "cards": len(cards),
                    "groups": len(groups),
                    "links": len(links),
                    "not_visible_cards": len(
                        [
                            card
                            for card in state.get("cards") or []
                            if isinstance(card, dict) and not card.get("deleted") and str(card.get("id")) not in visible
                        ]
                    ),
                },
            },
        )


class BoardReadCardsTool(_BoardTool):
    name = "board_read_cards"

    async def _run(self, turn: Any, args: Any) -> ToolResult:
        conn = _conn(self)
        source = str(args.source)
        if source == "submission":
            if not turn.submission_id:
                return _fail(self.name, "submission_unavailable", "当前轮没有提交身份，不能读取提交快照")
            row = conn.execute(
                "SELECT board_id, visible, after_state FROM board_submissions WHERE id = ?",
                (turn.submission_id,),
            ).fetchone()
            if row is None or str(row["board_id"]) != turn.board_id:
                return _fail(self.name, "submission_not_found", "提交快照不存在或不属于当前板面")
            snapshot = models.loads(row["visible"], None)
            if not isinstance(snapshot, dict):
                snapshot = models.loads(row["after_state"], {})
            raw_cards = snapshot.get("cards") or []
            allowed = {str(card["id"]) for card in raw_cards if isinstance(card, dict) and card.get("id")}
            cards = [dict(card) for card in raw_cards if isinstance(card, dict) and card.get("id")]
        else:
            state = _latest_state(self, turn)
            allowed = _visible_ids(state)
            cards = [
                dict(card)
                for card in state.get("cards") or []
                if isinstance(card, dict) and str(card.get("id")) in allowed and not card.get("deleted")
            ]
        requested = [str(item) for item in args.card_ids or []]
        if requested:
            not_readable = [card_id for card_id in requested if card_id not in allowed]
            if not_readable:
                return _fail(
                    self.name,
                    "not_readable",
                    "这些卡片不在本轮获准读取的范围内："
                    + ", ".join(not_readable[:5])
                    + "（未勾选 / 已隐藏 / 不存在，不能通过编号绕过）",
                    status=ToolStatus.FAILED.value,
                    data={"not_readable": not_readable},
                )
            by_id = {str(card["id"]): card for card in cards}
            ordered = [by_id[card_id] for card_id in requested if card_id in by_id]
        else:
            ordered = cards
        offset = int(args.offset)
        limit = int(args.limit)
        page = ordered[offset : offset + limit]
        out = []
        for card in page:
            meta = card.get("meta") if isinstance(card.get("meta"), dict) else {}
            out.append(
                {
                    "id": str(card.get("id")),
                    "kind": str(card.get("kind") or ""),
                    "version": int(card.get("version", 1) or 1),
                    "content": str(card.get("content") or ""),
                    "title": str(meta.get("title") or ""),
                    "checked": bool(card.get("checked", False)),
                    "hidden": bool(card.get("hidden", False)),
                }
            )
        return _ok(
            self.name,
            {
                "board_id": turn.board_id,
                "source": source,
                "offset": offset,
                "limit": limit,
                "total": len(ordered),
                "cards": out,
            },
        )


# ---------------------------------------------------------------------------
# 意图工具
# ---------------------------------------------------------------------------


class BoardProposeIntentsTool(_BoardTool):
    name = "board_propose_intents"

    async def _run(self, turn: Any, args: Any) -> ToolResult:
        if not turn.submission_id or str(args.submission_id) != turn.submission_id:
            return _fail(
                self.name,
                "submission_binding_mismatch",
                "只能为当前运行上下文绑定的那一次提交提出意图",
            )
        conn = _conn(self)
        payload = intent_service.propose_intents(
            conn,
            board_id=turn.board_id,
            submission_id=str(args.submission_id),
            intents=list(args.intents),
            resolved_question_ids=list(args.resolved_question_ids or []),
        )
        if not payload.get("ok"):
            return _from_service(self.name, payload)
        payload.pop("ok", None)
        payload["requires_confirmation"] = True
        payload["next"] = "awaiting_confirmation"
        return _ok(self.name, payload, status=ToolStatus.AWAITING_APPROVAL.value, code="intents_proposed")


class BoardUpdateIntentTool(_BoardTool):
    name = "board_update_intent"

    async def _run(self, turn: Any, args: Any) -> ToolResult:
        conn = _conn(self)
        payload = intent_service.update_intent(
            conn,
            board_id=turn.board_id,
            intent_id=str(args.intent_id),
            expected_version=int(args.expected_version),
            changes=dict(args.changes or {}),
            resolved_question_ids=list(args.resolved_question_ids or []),
        )
        if not payload.get("ok"):
            return _from_service(self.name, payload)
        status = ToolStatus.AWAITING_APPROVAL.value if payload.get("requires_confirmation") else ToolStatus.COMPLETED.value
        payload.pop("ok", None)
        return _ok(self.name, payload, status=status, code="awaiting_confirmation" if payload.get("requires_confirmation") else "intent_updated")


class BoardWaitForInputTool(_BoardTool):
    name = "board_wait_for_input"

    async def _run(self, turn: Any, args: Any) -> ToolResult:
        if not turn.submission_id or str(args.submission_id) != turn.submission_id:
            return _fail(
                self.name,
                "submission_binding_mismatch",
                "只能为当前运行上下文绑定的那一次提交记录等待",
            )
        conn = _conn(self)
        question = str(args.question)
        persisted = False
        waiting_id = None
        unavailable = False
        # 等待持久化（迁移 49，A 的 submission.get_waiting）；读取失败如实上抛。
        try:
            from agent.interactive import submission as submission_mod

            recorder = getattr(submission_mod, "record_waiting", None)
            if callable(recorder):
                recorded = recorder(
                    conn,
                    board_id=turn.board_id,
                    submission_id=turn.submission_id,
                    question=question,
                    missing_fields=list(args.missing_fields or []),
                    turn_id=turn.turn_id,
                )
                if isinstance(recorded, dict):
                    persisted = bool(recorded.get("id"))
                    waiting_id = recorded.get("id")
            else:
                unavailable = True
        except Exception:  # noqa: BLE001 - 等待未持久化不能阻塞对话呈现
            logger.warning("record_waiting 不可用，问题仍作为对话呈现一次", exc_info=True)
            unavailable = True
        runtime.record_delivered_question(question, missing_fields=list(args.missing_fields or []))
        return _ok(
            self.name,
            {
                "board_id": turn.board_id,
                "submission_id": turn.submission_id,
                "question": question,
                "missing_fields": list(args.missing_fields or []),
                "question_delivered": True,
                "persisted": persisted,
                "waiting_id": waiting_id,
                "waiting_unavailable": unavailable,
            },
            status=ToolStatus.AWAITING_INPUT.value,
            code="question_delivered",
        )


# ---------------------------------------------------------------------------
# 板面写入工具
# ---------------------------------------------------------------------------


class BoardCreateCardTool(_BoardTool):
    name = "board_create_card"

    async def _run(self, turn: Any, args: Any) -> ToolResult:
        conn = _conn(self)
        view, error = _intent_or_error(self, turn, str(args.intent_id))
        if error is not None:
            return error
        gate = _authorization_gate(self.name, view)
        if gate is not None:
            return gate
        scope = _scope_of(view)
        if "create_card" not in set(scope.get("allowed_actions") or []):
            return _fail(self.name, "scope_violation", "本次授权范围没有 create_card，不能在板面创建卡片", status=ToolStatus.FAILED.value)
        used = intent_service.new_objects_used(conn, str(args.intent_id))
        if int(used["cards"]) >= int(scope.get("max_new_cards") or 0):
            return _fail(
                self.name,
                "scope_limit",
                "本次意图允许新建的卡片数量已用完（按整个意图累计，重试不重置）",
                status=ToolStatus.FAILED.value,
                data={"used": used["cards"], "limit": int(scope.get("max_new_cards") or 0)},
            )
        state = _latest_state(self, turn)
        position, position_error = _resolve_position(self, turn, state, args.position)
        if position_error is not None:
            return _fail(self.name, "invalid_position", position_error, data={"position_error": position_error})
        fields: dict[str, Any] = {}
        if args.title:
            fields["meta"] = {"title": str(args.title)}
        if args.width is not None:
            fields["w"] = float(args.width)
        if args.height is not None:
            fields["h"] = float(args.height)
        if position is not None:
            fields["x"] = position["x"]
            fields["y"] = position["y"]
        content = str(args.content)

        def mutate(current: dict) -> tuple[dict, dict]:
            before_ids = {str(card.get("id")) for card in current.get("cards") or [] if isinstance(card, dict)}
            new_state = board.add_card(current, models.REPLY_KIND, content, **fields)
            created = next(
                (
                    card
                    for card in new_state.get("cards") or []
                    if isinstance(card, dict) and str(card.get("id")) not in before_ids
                ),
                None,
            )
            if created is None:
                raise ValueError("创建卡片失败：板面状态没有产生新卡片")
            snapshot = _card_snapshot(created)
            return new_state, {
                "after": snapshot,
                "result": {
                    "card_id": snapshot["id"],
                    "card": snapshot,
                    "action": "create_card",
                },
                "versions": {"cards": {snapshot["id"]: snapshot["version"]}, "groups": {}, "links": {}},
            }

        return _run_write(
            self,
            turn,
            args,
            action="create_card",
            before={},
            expected_versions={},
            mutate=mutate,
        )


class BoardUpdateCardTool(_BoardTool):
    name = "board_update_card"

    async def _run(self, turn: Any, args: Any) -> ToolResult:
        view, error = _intent_or_error(self, turn, str(args.intent_id))
        if error is not None:
            return error
        gate = _authorization_gate(self.name, view)
        if gate is not None:
            return gate
        scope = _scope_of(view)
        card_id = str(args.card_id)
        if card_id not in set(scope.get("write_card_ids") or []):
            return _fail(
                self.name,
                "scope_violation",
                "这张卡片不在已确认的写入范围内：查看许可不等于编辑许可",
                status=ToolStatus.FAILED.value,
                data={"card_id": card_id},
            )
        state = _latest_state(self, turn)
        current = _card_by_id(state, card_id)
        if current is None:
            return _fail(self.name, "card_not_found", "卡片不存在或已删除", status=ToolStatus.FAILED.value)
        patch = dict(args.patch or {})
        if "title" in patch:
            meta = dict(current.get("meta") or {}) if isinstance(current.get("meta"), dict) else {}
            meta["title"] = str(patch.pop("title"))
            patch["meta"] = meta
        before = _card_snapshot(current)

        def mutate(state_in: dict) -> tuple[dict, dict]:
            new_state = board.update_card(state_in, card_id, patch)
            updated = _card_by_id(new_state, card_id)
            if updated is None:
                raise ValueError("更新卡片失败：目标卡片不存在")
            snapshot = _card_snapshot(updated)
            return new_state, {
                "after": snapshot,
                "result": {
                    "card_id": card_id,
                    "card": snapshot,
                    "changed": int(snapshot["version"]) != int(before["version"]),
                },
                "versions": {"cards": {card_id: snapshot["version"]}, "groups": {}, "links": {}},
            }

        return _run_write(
            self,
            turn,
            args,
            action="update_card",
            before=before,
            expected_versions={card_id: int(args.expected_version)},
            mutate=mutate,
        )


class BoardMoveCardTool(_BoardTool):
    name = "board_move_card"

    async def _run(self, turn: Any, args: Any) -> ToolResult:
        view, error = _intent_or_error(self, turn, str(args.intent_id))
        if error is not None:
            return error
        gate = _authorization_gate(self.name, view)
        if gate is not None:
            return gate
        scope = _scope_of(view)
        card_id = str(args.card_id)
        if card_id not in set(scope.get("write_card_ids") or []):
            return _fail(self.name, "scope_violation", "这张卡片不在已确认的写入范围内", status=ToolStatus.FAILED.value)
        state = _latest_state(self, turn)
        current = _card_by_id(state, card_id)
        if current is None:
            return _fail(self.name, "card_not_found", "卡片不存在或已删除")
        position, position_error = _resolve_position(self, turn, state, args.position)
        if position_error is not None:
            return _fail(self.name, "invalid_position", position_error)
        before = _card_snapshot(current)

        def mutate(state_in: dict) -> tuple[dict, dict]:
            new_state = board.update_card(state_in, card_id, {"x": position["x"], "y": position["y"]})
            updated = _card_by_id(new_state, card_id)
            if updated is None:
                raise ValueError("移动卡片失败：目标卡片不存在")
            snapshot = _card_snapshot(updated)
            return new_state, {
                "after": snapshot,
                "result": {"card_id": card_id, "card": snapshot, "position": position},
                "versions": {"cards": {card_id: snapshot["version"]}, "groups": {}, "links": {}},
            }

        return _run_write(
            self,
            turn,
            args,
            action="move_card",
            before=before,
            expected_versions={card_id: int(args.expected_version)},
            mutate=mutate,
        )


class BoardGroupCardsTool(_BoardTool):
    name = "board_group_cards"

    async def _run(self, turn: Any, args: Any) -> ToolResult:
        view, error = _intent_or_error(self, turn, str(args.intent_id))
        if error is not None:
            return error
        gate = _authorization_gate(self.name, view)
        if gate is not None:
            return gate
        scope = _scope_of(view)
        card_ids = [str(item) for item in args.card_ids or []]
        state = _latest_state(self, turn)
        visible = _visible_ids(state)
        not_visible = [card_id for card_id in card_ids if card_id not in visible]
        if not_visible:
            return _fail(
                self.name,
                "scope_violation",
                "组操作涉及了不在本轮获准读取范围内的卡片：" + ", ".join(not_visible[:5]),
                status=ToolStatus.FAILED.value,
                data={"not_readable": not_visible},
            )
        allowed_groups = set(scope.get("group_ids") or [])
        target_group_id = str(args.group_id) if args.group_id else None
        if target_group_id is not None and target_group_id not in allowed_groups:
            return _fail(self.name, "scope_violation", "目标分组不在已确认的授权范围内", status=ToolStatus.FAILED.value)
        for card_id in card_ids:
            current_group = models.group_of(state, card_id)
            if current_group is not None and str(current_group["id"]) not in allowed_groups:
                return _fail(
                    self.name,
                    "scope_violation",
                    "受影响的原分组不在已确认的授权范围内：" + str(current_group["id"]),
                    status=ToolStatus.FAILED.value,
                )
        if target_group_id is None:
            used = intent_service.new_objects_used(_conn(self), str(args.intent_id))
            if int(used["groups"]) >= int(scope.get("max_new_groups") or 0):
                return _fail(
                    self.name,
                    "scope_limit",
                    "本次意图允许新建的分组数量已用完（按整个意图累计）",
                    status=ToolStatus.FAILED.value,
                    data={"used": used["groups"], "limit": int(scope.get("max_new_groups") or 0)},
                )
        expected, missing = _required_card_versions(args)
        if missing:
            return _fail(
                self.name,
                "expected_versions_required",
                "组操作必须提供全部受影响卡片的 expected_versions",
                data={"missing": missing},
            )
        before_groups = {
            str(group.get("id")): _group_snapshot(group)
            for group in state.get("groups") or []
            if isinstance(group, dict)
        }

        def mutate(state_in: dict) -> tuple[dict, dict]:
            if target_group_id is None:
                new_state = board.create_group(state_in, card_ids, str(args.name or ""))
                group = models.group_of(new_state, card_ids[0])
                if group is None:
                    raise ValueError("建组失败：没有产生新分组")
                created = str(group["id"]) not in before_groups
                group_id = str(group["id"])
            else:
                new_state = state_in
                for card_id in card_ids:
                    new_state = board.join_group(new_state, card_id, target_group_id)
                group = _group_by_id(new_state, target_group_id)
                if group is None:
                    raise ValueError("加入分组失败：目标分组不存在")
                created = False
                group_id = target_group_id
            snapshot = _group_snapshot(group)
            card_snapshots = [
                _card_snapshot(card)
                for card in new_state.get("cards") or []
                if isinstance(card, dict) and str(card.get("id")) in card_ids
            ]
            versions = _versions_map(new_state, card_ids, [group_id])
            return new_state, {
                "after": {"group": snapshot, "cards": card_snapshots},
                "result": {
                    "group_id": group_id,
                    "created_group_id": group_id if created else None,
                    "cards": card_ids,
                    "action": "group_cards",
                },
                "versions": versions,
            }

        return _run_write(
            self,
            turn,
            args,
            action="group_cards",
            before={"groups": list(before_groups.values())},
            expected_versions=expected,
            mutate=mutate,
        )


class BoardOrderGroupTool(_BoardTool):
    name = "board_order_group"

    async def _run(self, turn: Any, args: Any) -> ToolResult:
        view, error = _intent_or_error(self, turn, str(args.intent_id))
        if error is not None:
            return error
        gate = _authorization_gate(self.name, view)
        if gate is not None:
            return gate
        scope = _scope_of(view)
        group_id = str(args.group_id)
        if group_id not in set(scope.get("group_ids") or []):
            return _fail(self.name, "scope_violation", "分组不在已确认的授权范围内", status=ToolStatus.FAILED.value)
        state = _latest_state(self, turn)
        group = _group_by_id(state, group_id)
        if group is None:
            return _fail(self.name, "group_not_found", "分组不存在或已删除")
        live_members = [str(item) for item in group.get("members") or []]
        member_order = [str(item) for item in args.member_order or []]
        if sorted(set(member_order)) != sorted(set(live_members)) or len(member_order) != len(set(member_order)):
            return _fail(
                self.name,
                "invalid_member_order",
                "member_order 必须完整且只包含该分组的当前成员",
                data={"members": live_members},
            )
        expected, missing = _required_card_versions(args)
        if missing:
            return _fail(
                self.name,
                "expected_versions_required",
                "顺序调整必须提供全部受影响卡片的 expected_versions",
                data={"missing": missing},
            )
        before = {"group": _group_snapshot(group), "cards": [_card_snapshot(_card_by_id(state, cid)) for cid in live_members if _card_by_id(state, cid) is not None]}

        def mutate(state_in: dict) -> tuple[dict, dict]:
            new_state = state_in
            for index, card_id in enumerate(member_order):
                new_state = board.move_within_group(new_state, group_id, card_id, index)
            new_state = board.set_group_ordered(new_state, group_id, True)
            if args.arrange in ("row", "column"):
                current_group = _group_by_id(new_state, group_id)
                if current_group is not None:
                    gx, gy = float(current_group.get("x") or 0.0), float(current_group.get("y") or 0.0)
                    step_w = board.DEFAULT_CARD_W + board.DEFAULT_CARD_GAP
                    step_h = board.DEFAULT_CARD_H + board.DEFAULT_CARD_GAP
                    for index, card_id in enumerate(member_order):
                        card = _card_by_id(new_state, card_id)
                        if card is None:
                            continue
                        if args.arrange == "row":
                            pos = {"x": gx + board.GROUP_PAD_X + index * step_w, "y": gy + board.GROUP_PAD_TOP}
                        else:
                            pos = {"x": gx + board.GROUP_PAD_X, "y": gy + board.GROUP_PAD_TOP + index * step_h}
                        new_state = board.update_card(new_state, card_id, pos)
            final_group = _group_by_id(new_state, group_id)
            if final_group is None:
                raise ValueError("顺序调整失败：分组不存在")
            card_snapshots = [
                _card_snapshot(card)
                for card in new_state.get("cards") or []
                if isinstance(card, dict) and str(card.get("id")) in member_order
            ]
            return new_state, {
                "after": {"group": _group_snapshot(final_group), "cards": card_snapshots},
                "result": {
                    "group_id": group_id,
                    "member_order": member_order,
                    "arrange": str(args.arrange),
                    "action": "order_group",
                },
                "versions": _versions_map(new_state, member_order, [group_id]),
            }

        return _run_write(
            self,
            turn,
            args,
            action="order_group",
            before=before,
            expected_versions=expected,
            mutate=mutate,
        )


class BoardAddLinkTool(_BoardTool):
    name = "board_add_link"

    async def _run(self, turn: Any, args: Any) -> ToolResult:
        view, error = _intent_or_error(self, turn, str(args.intent_id))
        if error is not None:
            return error
        gate = _authorization_gate(self.name, view)
        if gate is not None:
            return gate
        scope = _scope_of(view)
        state = _latest_state(self, turn)
        visible = _visible_ids(state)
        src, dst = str(args.src), str(args.dst)
        missing = [card_id for card_id in (src, dst) if card_id not in visible]
        if missing:
            return _fail(
                self.name,
                "scope_violation",
                "关系端点不在本轮获准读取范围内：" + ", ".join(missing),
                status=ToolStatus.FAILED.value,
                data={"not_readable": missing},
            )
        pair_exists = any(
            isinstance(link, dict)
            and not link.get("deleted")
            and {str(link.get("src")), str(link.get("dst"))} == {src, dst}
            for link in state.get("links") or []
        )
        if not pair_exists:
            used = intent_service.new_objects_used(_conn(self), str(args.intent_id))
            if int(used["links"]) >= int(scope.get("max_new_links") or 0):
                return _fail(
                    self.name,
                    "scope_limit",
                    "本次意图允许新建的关系数量已用完（按整个意图累计）",
                    status=ToolStatus.FAILED.value,
                    data={"used": used["links"], "limit": int(scope.get("max_new_links") or 0)},
                )
        expected = dict(args.expected_versions or {}) if hasattr(args, "expected_versions") else {}
        before_ids = {
            str(link.get("id"))
            for link in state.get("links") or []
            if isinstance(link, dict)
        }

        def mutate(state_in: dict) -> tuple[dict, dict]:
            new_state = board.add_link(
                state_in,
                src,
                dst,
                direction=bool(args.direction),
                meaning=str(args.meaning or ""),
            )
            links = [
                link
                for link in new_state.get("links") or []
                if isinstance(link, dict) and {str(link.get("src")), str(link.get("dst"))} == {src, dst}
            ]
            if not links:
                raise ValueError("建立关系失败：板面状态没有产生链接")
            link = links[0]
            created = str(link.get("id")) not in before_ids
            snapshot = _link_snapshot(link)
            return new_state, {
                "after": snapshot,
                "result": {
                    "link_id": snapshot["id"],
                    "created": created,
                    "src": src,
                    "dst": dst,
                    "action": "add_link",
                },
                "versions": _versions_map(new_state, [src, dst], (), [snapshot["id"]]),
            }

        return _run_write(
            self,
            turn,
            args,
            action="add_link",
            before={"cards": [_card_snapshot(_card_by_id(state, src)), _card_snapshot(_card_by_id(state, dst))]},
            expected_versions={str(key): int(value) for key, value in expected.items()},
            mutate=mutate,
        )


class BoardFinishIntentTool(_BoardTool):
    name = "board_finish_intent"

    async def _run(self, turn: Any, args: Any) -> ToolResult:
        conn = _conn(self)
        payload = intent_service.finish_intent(
            conn,
            board_id=turn.board_id,
            intent_id=str(args.intent_id),
            expected_version=int(args.expected_version),
            outcome=str(args.outcome),
            summary=str(args.summary),
            operation_keys=list(args.operation_keys or []),
        )
        action = self.name
        if not payload.get("ok"):
            return _from_service(action, payload)
        outcome = str(args.outcome)
        status = {
            "completed": ToolStatus.COMPLETED.value,
            "partially_completed": ToolStatus.PARTIALLY_COMPLETED.value,
            "failed": ToolStatus.FAILED.value,
        }.get(outcome, ToolStatus.FAILED.value)
        data = dict(payload)
        data.pop("ok", None)
        data["outcome"] = outcome
        return _ok(action, data, status=status, code="intent_" + outcome)


TOOL_CLASSES: tuple[type[_BoardTool], ...] = (
    BoardInspectTool,
    BoardReadCardsTool,
    BoardProposeIntentsTool,
    BoardUpdateIntentTool,
    BoardWaitForInputTool,
    BoardCreateCardTool,
    BoardUpdateCardTool,
    BoardMoveCardTool,
    BoardGroupCardsTool,
    BoardOrderGroupTool,
    BoardAddLinkTool,
    BoardFinishIntentTool,
)


def build_board_tools(app: Any) -> list[Tool]:
    """板面工具注册工厂（Lead 在 services/app.py 注册；不 import services.app）。"""
    return [tool_class(app) for tool_class in TOOL_CLASSES]
