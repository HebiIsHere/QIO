# -*- coding: utf-8 -*-
"""阶段三 B：意图 / 授权 / 权威操作账本 / 完成核对服务（契约 §13.4）。

边界：

* 本模块不调用模型、不生成正文；只做程序可校验的意图、授权、账本与完成核对；
* 板面对象写入由 board_tools + board_store.write_board_cas 完成，本模块保证
  「意图状态 / 授权快照 / 操作账本」与板面写入在同一事务里；
* 视图字段为 C 的 API 读取面（snake_case，字段名按 Lead/C 备案冻结）；
* operation_key 幂等：同意图内相同 key + 相同请求 hash 返回原结果（replayed），
  不同请求返回 conflict；账本由程序写，模型漏列 key 不能掩盖未完成。

状态机（tool_models.INTENT_STATUSES）：proposed / awaiting_confirmation / approved /
running / paused / completed / partially_completed / failed / cancelled / rejected。
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from typing import Any, Iterable, Mapping

from agent.interactive import board_store, models, tool_models
from agent.storage.db import transaction

logger = logging.getLogger(__name__)

# -- 状态 / 动作 ------------------------------------------------------------

STATUS_PROPOSED = "proposed"
STATUS_AWAITING = "awaiting_confirmation"
STATUS_APPROVED = "approved"
STATUS_RUNNING = "running"
STATUS_PAUSED = "paused"
STATUS_COMPLETED = "completed"
STATUS_PARTIALLY_COMPLETED = "partially_completed"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"
STATUS_REJECTED = "rejected"

CONFIRMABLE_STATUSES = (STATUS_PROPOSED, STATUS_AWAITING)
FINISHABLE_STATUSES = (
    STATUS_APPROVED,
    STATUS_RUNNING,
    STATUS_PARTIALLY_COMPLETED,
    STATUS_FAILED,
)
EXECUTABLE_STATUSES = (STATUS_APPROVED, STATUS_RUNNING)
CONFLICT_BLOCKING_STATUSES = (
    STATUS_APPROVED,
    STATUS_RUNNING,
    STATUS_PAUSED,
    STATUS_COMPLETED,
    STATUS_PARTIALLY_COMPLETED,
)
TERMINAL_STATUSES = (STATUS_COMPLETED, STATUS_CANCELLED, STATUS_REJECTED)

OP_PENDING = "pending"
OP_RUNNING = "running"
OP_APPLIED = "applied"
OP_FAILED = "failed"
OP_CONFLICT = "conflict"

#: 完成核对结果 → outcome 对应意图终态。
FINISH_OUTCOMES = {
    "completed": STATUS_COMPLETED,
    "partially_completed": STATUS_PARTIALLY_COMPLETED,
    "failed": STATUS_FAILED,
}

_JSON_COLUMNS = ("preview", "impact", "depends_on", "conflicts_with", "material_refs",
                 "progress", "applied", "revert", "operations", "basis", "scope",
                 "authorization")

AUTHORIZATION_SOURCE = "explicit_user_confirmation"


# -- 基础工具 ---------------------------------------------------------------


def _now() -> str:
    return models.now_iso()


def _dumps(value: Any) -> str:
    return models.dumps(value)


def _loads(text: Any, default: Any) -> Any:
    return models.loads(text, default)


def _row_value(row: sqlite3.Row, key: str, default: Any = None) -> Any:
    try:
        value = row[key]
    except (IndexError, KeyError):
        return default
    return default if value is None else value


def _get_row(conn: sqlite3.Connection, intent_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM board_intents WHERE id = ?", (str(intent_id),)
    ).fetchone()


def _board_exists(conn: sqlite3.Connection, board_id: str) -> bool:
    row = conn.execute("SELECT 1 FROM boards WHERE id = ?", (str(board_id),)).fetchone()
    return row is not None


def _extend(conn: sqlite3.Connection, board_id: str) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT * FROM board_intents WHERE board_id = ? ORDER BY rowid",
            (str(board_id),),
        ).fetchall()
    )


def _fail(code: str, message: str, *, status: str = "failed", data: dict | None = None) -> dict:
    payload = {"ok": False, "code": code, "status": status, "error": str(message)}
    if data:
        payload.update(data)
    return payload


def _validate_model(model: type, payload: Any) -> tuple[Any, str | None]:
    try:
        return model.model_validate(payload), None
    except Exception as exc:  # noqa: BLE001 - pydantic 错误转成可读字段错误
        return None, str(exc)[:800]


def _spec_from_any(value: Any) -> Any:
    if isinstance(value, tool_models.IntentSpec):
        return value
    return tool_models.IntentSpec.model_validate(value)


def _scope_payload(spec: Any) -> dict[str, Any]:
    return spec.scope.model_dump(mode="json")


def _spec_payload(spec: Any) -> dict[str, Any]:
    return spec.model_dump(mode="json")


def _refs_payload(refs: Iterable[Any]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for ref in refs or []:
        if isinstance(ref, tool_models.DependencyRef):
            out.append({"kind": ref.kind, "value": ref.value})
        elif isinstance(ref, dict):
            out.append({"kind": str(ref.get("kind") or ""), "value": str(ref.get("value") or "")})
    return out


# -- 意图视图 ---------------------------------------------------------------


def _operations(conn: sqlite3.Connection, intent_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM board_operations WHERE intent_id = ? ORDER BY created_at, id",
        (str(intent_id),),
    ).fetchall()
    return [_operation_row(row) for row in rows]


def _operation_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "operation_id": str(row["id"]),
        "board_id": str(row["board_id"]),
        "intent_id": str(row["intent_id"]),
        "operation_key": str(row["operation_key"]),
        "request_hash": str(row["request_hash"] or ""),
        "action": str(row["action"]),
        "status": str(row["status"]),
        "before": _loads(row["before"], {}),
        "after": _loads(row["after"], {}),
        "result": _loads(row["result"], {}),
        "created_at": str(row["created_at"]),
        "updated_at": str(row["updated_at"]),
    }


def _operation_summary(operations: list[dict[str, Any]]) -> dict[str, int]:
    summary = {OP_APPLIED: 0, OP_RUNNING: 0, OP_PENDING: 0, OP_FAILED: 0, OP_CONFLICT: 0}
    for operation in operations:
        status = operation["status"]
        if status in summary:
            summary[status] += 1
    return summary


def _authorization_valid(row: sqlite3.Row) -> bool:
    authorization = _loads(_row_value(row, "authorization", "{}"), {})
    if not isinstance(authorization, dict) or not authorization:
        return False
    if str(row["status"]) not in (STATUS_APPROVED, STATUS_RUNNING, STATUS_PAUSED):
        return False
    try:
        version = int(authorization.get("version") or 0)
    except (TypeError, ValueError):
        return False
    return version == int(_row_value(row, "version", 1) or 1)


def _intent_view(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    intent_id = str(row["id"])
    operations = _operations(conn, intent_id)
    conflicts = []
    for raw in _loads(row["conflicts_with"], []) or []:
        other = _get_row(conn, str(raw))
        conflicts.append(
            {
                "intent_id": str(raw),
                "status": str(other["status"]) if other is not None else "missing",
            }
        )
    version = int(_row_value(row, "version", 1) or 1)
    authorization = _loads(_row_value(row, "authorization", "{}"), {})
    return {
        "intent_id": intent_id,
        "board_id": str(row["board_id"]),
        "client_ref": str(_row_value(row, "client_ref", "") or ""),
        "submission_id": row["submission_id"],
        "version": version,
        "status": str(row["status"]),
        "title": str(row["title"] or ""),
        "goal": str(_row_value(row, "goal", "") or ""),
        "expected": str(_row_value(row, "expected", "") or ""),
        "basis": [
            dict(item)
            for item in _loads(_row_value(row, "basis", []), [])
            if isinstance(item, dict)
        ],
        "scope": _loads(_row_value(row, "scope", "{}"), {}),
        "depends_on": [str(item) for item in _loads(row["depends_on"], []) or []],
        "conflicts_with": [str(item) for item in _loads(row["conflicts_with"], []) or []],
        "conflicts": conflicts,
        "authorization": authorization,
        "authorization_valid": _authorization_valid(row),
        "outcome": str(_row_value(row, "outcome", "") or ""),
        "summary": str(row["summary"] or ""),
        "attempt": int(_row_value(row, "attempt", 0) or 0),
        "operations_summary": _operation_summary(operations),
        "created_at": str(row["created_at"]),
        "updated_at": str(row["updated_at"]),
        "finished_at": _row_value(row, "finished_at", None),
    }


# -- 版本 / 授权快照记录 -----------------------------------------------------


def _record_version(
    conn: sqlite3.Connection, row: sqlite3.Row, *, authorization: dict | None = None
) -> None:
    version = int(_row_value(row, "version", 1) or 1)
    payload = _intent_view(conn, row)
    if authorization is None:
        authorization = _loads(_row_value(row, "authorization", "{}"), {})
    conn.execute(
        "INSERT OR IGNORE INTO board_intent_versions"
        " (intent_id, version, payload, authorization, created_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (str(row["id"]), version, _dumps(payload), _dumps(authorization), _now()),
    )


def _update_row(conn: sqlite3.Connection, intent_id: str, **fields: Any) -> None:
    fields.setdefault("updated_at", _now())
    assignments: list[str] = []
    values: list[Any] = []
    for key, value in fields.items():
        assignments.append(f"{key} = ?")
        values.append(_dumps(value) if key in _JSON_COLUMNS else value)
    values.append(str(intent_id))
    conn.execute(
        f"UPDATE board_intents SET {', '.join(assignments)} WHERE id = ?", values
    )


# -- 范围 / 依据 / 依赖 / 冲突校验 ------------------------------------------


def _board_state(conn: sqlite3.Connection, board_id: str) -> dict:
    return board_store.current_state(conn, board_id)


def _visible_card_ids(state: dict) -> set[str]:
    return {
        str(card["id"])
        for card in models.selectable_cards(state)
        if isinstance(card, dict) and card.get("id")
    }


def _live_group_ids(state: dict) -> set[str]:
    return {
        str(group["id"])
        for group in state.get("groups") or []
        if isinstance(group, dict) and group.get("id") and not group.get("deleted")
    }


def _card_version(state: dict, card_id: str) -> int | None:
    for card in state.get("cards") or []:
        if isinstance(card, dict) and str(card.get("id")) == str(card_id):
            try:
                return int(card.get("version", 1) or 1)
            except (TypeError, ValueError):
                return 1
    return None


def _validate_scope(conn: sqlite3.Connection, board_id: str, scope: dict[str, Any]) -> str | None:
    """授权范围必须引用真实、当前可见的对象；不把查看许可当编辑许可。"""
    state = _board_state(conn, board_id)
    visible = _visible_card_ids(state)
    groups = _live_group_ids(state)
    for key in ("read_card_ids", "write_card_ids"):
        for raw in scope.get(key) or []:
            card_id = str(raw)
            if card_id not in visible:
                return f"{key} 里的卡片不可见或不存在：{card_id}"
    for raw in scope.get("group_ids") or []:
        if str(raw) not in groups:
            return f"group_ids 里的分组不存在：{raw}"
    actions = [str(action) for action in scope.get("allowed_actions") or []]
    unknown = [action for action in actions if action not in tool_models.ALLOWED_BOARD_ACTIONS]
    if unknown:
        return "allowed_actions 不在契约枚举内：" + ", ".join(unknown[:5])
    return None


def _validate_basis(conn: sqlite3.Connection, board_id: str, spec: Any) -> str | None:
    state = _board_state(conn, board_id)
    visible = _visible_card_ids(state)
    for ref in spec.basis or []:
        card_id = str(ref.card_id)
        if card_id not in visible:
            return f"依据卡片不可见或不存在：{card_id}"
        actual = _card_version(state, card_id)
        if actual is not None and int(ref.version) != int(actual):
            return f"依据卡片版本已变化：{card_id} 期望 v{ref.version}，当前 v{actual}"
    return None


def _edge_targets(conn: sqlite3.Connection, board_id: str, refs: Iterable[Any], *, kind: str) -> tuple[list[str], str | None]:
    out: list[str] = []
    for ref in refs or []:
        if ref.kind == "intent_id":
            row = _get_row(conn, ref.value)
            if row is None:
                return [], f"{kind} 引用的意图不存在：{ref.value}"
            if str(row["board_id"]) != str(board_id):
                return [], f"{kind} 引用了其他板面的意图：{ref.value}"
            target = str(row["id"])
        else:
            return [], f"{kind} 的 client_ref 只能在同一提案内解析：{ref.value}"
        if target not in out:
            out.append(target)
    return out, None


def _detect_cycle(conn: sqlite3.Connection, board_id: str, new_edges: dict[str, list[str]]) -> str | None:
    """依赖图（含本次新增边）做环检测；返回环路径或 None。"""
    graph: dict[str, list[str]] = {}
    for row in _extend(conn, board_id):
        graph[str(row["id"])] = [str(x) for x in _loads(row["depends_on"], []) or []]
    for intent_id, deps in new_edges.items():
        graph[str(intent_id)] = list(deps)
    for node in graph:
        seen: set[str] = set()
        stack = [(node, [node])]
        while stack:
            current, path = stack.pop()
            if current in seen:
                continue
            seen.add(current)
            for dep in graph.get(current, []):
                if dep == node:
                    return " -> ".join(path + [dep])
                if dep not in graph:
                    continue
                stack.append((dep, path + [dep]))
    return None


def _normalize_conflicts(
    conn: sqlite3.Connection, board_id: str, intent_id: str, other_ids: Iterable[str]
) -> None:
    """冲突正规化：对称、去重、不自冲突（同一事务内写两侧）。"""
    targets = [str(item) for item in other_ids if str(item) != str(intent_id)]
    row = _get_row(conn, intent_id)
    if row is None:
        return
    current = [str(item) for item in _loads(row["conflicts_with"], []) or []]
    merged = list(dict.fromkeys([*current, *targets]))
    if merged != current:
        _update_row(conn, intent_id, conflicts_with=merged)
    for other_id in targets:
        other = _get_row(conn, other_id)
        if other is None or str(other["board_id"]) != str(board_id):
            continue
        theirs = [str(item) for item in _loads(other["conflicts_with"], []) or []]
        if str(intent_id) in theirs:
            continue
        theirs.append(str(intent_id))
        _update_row(conn, other_id, conflicts_with=theirs)


def _resolve_questions(
    conn: sqlite3.Connection, *, board_id: str, submission_id: str, question_ids: Iterable[str]
) -> list[str]:
    """把已经回答的问题从等待里解除；A 的持久化函数不可用时如实降级（不伪造）。"""
    ids = [str(item) for item in question_ids or [] if str(item).strip()]
    if not ids:
        return []
    try:
        from agent.interactive import submission as submission_mod  # TEMP-P3-INTEGRATION

        resolver = getattr(submission_mod, "resolve_waiting", None)
        if callable(resolver):
            try:
                resolver(
                    conn,
                    board_id=board_id,
                    submission_id=submission_id,
                    question_ids=ids,
                )
            except TypeError:
                resolver(conn, submission_id=submission_id, question_ids=ids)
            return ids
    except Exception:  # noqa: BLE001 - A 的模块未就绪时只降级，不伪造已解除
        logger.debug("等待解除模块不可用：跳过 resolved_question_ids", exc_info=True)
        return []
    return []


def _waiting_items(conn: sqlite3.Connection, *, board_id: str, submission_id: str) -> list[dict[str, Any]] | None:
    """读取未解决等待；A 的模块不可用返回 None（调用方如实标 waiting_unavailable）。"""
    try:
        from agent.interactive import submission as submission_mod  # TEMP-P3-INTEGRATION

        getter = getattr(submission_mod, "get_waiting", None)
        if not callable(getter):
            return None
        try:
            # 完成核对按整个板面判「无等待」（A 的 get_waiting 语义）。
            result = getter(conn, board_id=board_id)
        except TypeError:
            result = getter(conn, board_id, submission_id=submission_id)
        if isinstance(result, dict):
            items = result.get("items") or result.get("waiting") or []
        else:
            items = result or []
        return [dict(item) for item in items if isinstance(item, dict)]
    except Exception:  # noqa: BLE001
        return None


# -- 提案 / 更新 / 确认 ------------------------------------------------------


def _insert_intent(
    conn: sqlite3.Connection,
    *,
    board_id: str,
    submission_id: str,
    spec: Any,
    status: str,
    depends_on: list[str],
    conflicts_with: list[str],
) -> str:
    intent_id = models.new_id("i")
    stamp = _now()
    scope = _scope_payload(spec)
    conn.execute(
        "INSERT INTO board_intents ("
        " id, board_id, submission_id, title, summary, status, preview, impact,"
        " depends_on, conflicts_with, conflict_key, material_refs, progress, applied,"
        " revert, reason, demo, created_at, updated_at, goal, expected, operations,"
        " basis, intent_key, understanding_id, client_ref, version, scope,"
        " authorization, outcome, attempt, finished_at"
        ") VALUES (?, ?, ?, ?, '', ?, '{}', '{}', ?, ?, '', '[]', '{}', '{}', '{}', '',"
        " 0, ?, ?, ?, ?, '[]', ?, '', '', ?, 1, ?, '{}', '', 0, NULL)",
        (
            intent_id,
            board_id,
            submission_id,
            spec.title,
            status,
            _dumps(depends_on),
            _dumps(conflicts_with),
            stamp,
            stamp,
            spec.goal,
            spec.expected,
            _dumps([ref.model_dump(mode="json") for ref in spec.basis]),
            spec.client_ref,
            _dumps(scope),
        ),
    )
    row = _get_row(conn, intent_id)
    assert row is not None
    _record_version(conn, row, authorization={})
    return intent_id


def _detect_cycle_graph(graph: Mapping[str, list[str]]) -> str | None:
    """纯依赖图环检测（节点：正式 id 或 client:xxx 占位）；返回环路径或 None。"""
    color: dict[str, int] = {}

    def visit(node: str, stack: list[str]) -> str | None:
        color[node] = 1
        stack.append(node)
        for dep in graph.get(node, []):
            if dep not in graph:
                continue
            state = color.get(dep, 0)
            if state == 1:
                index = stack.index(dep) if dep in stack else 0
                return " -> ".join(stack[index:] + [dep])
            if state == 0:
                found = visit(dep, stack)
                if found is not None:
                    return found
        stack.pop()
        color[node] = 2
        return None

    for node in list(graph.keys()):
        if color.get(node, 0) == 0:
            found = visit(node, [])
            if found is not None:
                return found
    return None


def propose_intents(
    conn: sqlite3.Connection,
    *,
    board_id: str,
    submission_id: str,
    intents: Iterable[Any],
    resolved_question_ids: Iterable[str] = (),
) -> dict[str, Any]:
    """为一次提交创建待确认意图（client_ref 只在本次提案内解析）。

    所有引用 / 范围 / 环校验都在**写库之前**完成：失败不会留下半截意图。
    """
    board_id = str(board_id or "").strip()
    submission_id = str(submission_id or "").strip()
    if not board_id or not _board_exists(conn, board_id):
        return _fail("board_not_found", "板面不存在")
    if not submission_id:
        return _fail("submission_required", "必须提供当前提交身份")
    submission = conn.execute(
        "SELECT board_id FROM board_submissions WHERE id = ?", (submission_id,)
    ).fetchone()
    if submission is None:
        return _fail("submission_not_found", "提交不存在")
    if str(submission["board_id"]) != board_id:
        return _fail("board_mismatch", "提交不属于这个板面")
    specs: list[Any] = []
    try:
        for value in intents:
            specs.append(_spec_from_any(value))
    except Exception as exc:  # noqa: BLE001
        return _fail("invalid_intent", f"意图参数不合法：{exc}")
    if not specs:
        return _fail("invalid_intent", "至少需要一个意图")
    client_refs = [spec.client_ref for spec in specs]
    if len(set(client_refs)) != len(client_refs):
        return _fail("duplicate_client_ref", "同一次提案里的 client_ref 必须唯一")
    board_rows = _extend(conn, board_id)
    existing_ids = {str(row["id"]) for row in board_rows}
    graph: dict[str, list[str]] = {
        str(row["id"]): [str(item) for item in _loads(row["depends_on"], []) or []]
        for row in board_rows
    }
    resolved_deps: dict[str, list[str]] = {}
    resolved_conflicts: dict[str, list[str]] = {}
    for spec in specs:
        scope_error = _validate_scope(conn, board_id, _scope_payload(spec))
        if scope_error is not None:
            return _fail("scope_invalid", scope_error)
        basis_error = _validate_basis(conn, board_id, spec)
        if basis_error is not None:
            return _fail("basis_invalid", basis_error)
        deps: list[str] = []
        for ref in spec.depends_on:
            if ref.kind == "client_ref":
                if ref.value not in client_refs or ref.value == spec.client_ref:
                    return _fail("dependency_invalid", f"依赖引用无效：{ref.value}")
                deps.append("client:" + ref.value)
            else:
                if ref.value not in existing_ids:
                    return _fail("dependency_invalid", f"依赖意图不存在：{ref.value}")
                deps.append(str(ref.value))
        conflicts: list[str] = []
        for ref in spec.conflicts_with:
            if ref.kind == "client_ref":
                if ref.value not in client_refs or ref.value == spec.client_ref:
                    return _fail("conflict_invalid", f"冲突引用无效：{ref.value}")
                conflicts.append("client:" + ref.value)
            else:
                if ref.value not in existing_ids:
                    return _fail("conflict_invalid", f"冲突意图不存在：{ref.value}")
                conflicts.append(str(ref.value))
        if any(dep == "client:" + spec.client_ref for dep in deps):
            return _fail("dependency_invalid", "意图不能依赖自己")
        resolved_deps[spec.client_ref] = list(dict.fromkeys(deps))
        resolved_conflicts[spec.client_ref] = list(dict.fromkeys(conflicts))
        graph["client:" + spec.client_ref] = resolved_deps[spec.client_ref]
    cycle = _detect_cycle_graph(graph)
    if cycle is not None:
        return _fail("dependency_cycle", f"依赖关系成环：{cycle}")
    try:
        with transaction(conn):
            created: dict[str, str] = {}
            for spec in specs:
                created[spec.client_ref] = _insert_intent(
                    conn,
                    board_id=board_id,
                    submission_id=submission_id,
                    spec=spec,
                    status=STATUS_AWAITING,
                    depends_on=[],
                    conflicts_with=[],
                )
            for spec in specs:
                intent_id = created[spec.client_ref]
                formal_deps = [
                    created[item.split(":", 1)[1]]
                    if item.startswith("client:")
                    else item
                    for item in resolved_deps[spec.client_ref]
                ]
                formal_conflicts = [
                    created[item.split(":", 1)[1]]
                    if item.startswith("client:")
                    else item
                    for item in resolved_conflicts[spec.client_ref]
                ]
                _update_row(
                    conn,
                    intent_id,
                    depends_on=formal_deps,
                    conflicts_with=formal_conflicts,
                )
            for spec in specs:
                intent_id = created[spec.client_ref]
                formal_conflicts = [
                    created[item.split(":", 1)[1]]
                    if item.startswith("client:")
                    else item
                    for item in resolved_conflicts[spec.client_ref]
                ]
                _normalize_conflicts(conn, board_id, intent_id, formal_conflicts)
            questions = _resolve_questions(
                conn,
                board_id=board_id,
                submission_id=submission_id,
                question_ids=resolved_question_ids,
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("propose_intents failed", exc_info=True)
        return _fail("proposal_failed", f"提案未保存：{exc}")
    views = []
    for spec in specs:
        row = _get_row(conn, created[spec.client_ref])
        if row is not None:
            views.append(_intent_view(conn, row))
    return {
        "ok": True,
        "board_id": board_id,
        "submission_id": submission_id,
        "intents": views,
        "resolved_question_ids": questions,
        "client_refs": dict(created),
    }


def _current_spec(row: sqlite3.Row) -> Any:
    """把数据库行还原成 IntentSpec（用于更新后的整体校验）。"""
    payload = {
        "client_ref": str(_row_value(row, "client_ref", "") or "i") or "i",
        "title": str(row["title"] or "意图"),
        "goal": str(_row_value(row, "goal", "") or "目标"),
        "expected": str(_row_value(row, "expected", "") or "预期"),
        "basis": [
            dict(item)
            for item in _loads(_row_value(row, "basis", []), [])
            if isinstance(item, dict)
        ],
        "scope": _loads(_row_value(row, "scope", "{}"), {}),
        "depends_on": [
            {"kind": "intent_id", "value": str(item)}
            for item in _loads(row["depends_on"], []) or []
        ],
        "conflicts_with": [
            {"kind": "intent_id", "value": str(item)}
            for item in _loads(row["conflicts_with"], []) or []
        ],
    }
    return tool_models.IntentSpec.model_validate(payload)


def update_intent(
    conn: sqlite3.Connection,
    *,
    board_id: str,
    intent_id: str,
    expected_version: int,
    changes: Mapping[str, Any],
    resolved_question_ids: Iterable[str] = (),
) -> dict[str, Any]:
    """按 expected_version 更新意图；实质变化使旧授权失效并回到待确认。"""
    board_id = str(board_id or "").strip()
    row = _get_row(conn, intent_id)
    if row is None:
        return _fail("intent_not_found", "意图不存在", status="not_found")
    if str(row["board_id"]) != board_id:
        return _fail("board_mismatch", "意图不属于这个板面")
    current_version = int(_row_value(row, "version", 1) or 1)
    if int(expected_version) != current_version:
        return _fail(
            "version_conflict",
            f"意图版本不符：期望 v{expected_version}，当前 v{current_version}",
            status="conflict",
            data={"current_version": current_version},
        )
    status = str(row["status"])
    if status in TERMINAL_STATUSES:
        return _fail("invalid_status", f"意图状态为 {status}，不能再修改")
    try:
        spec = _current_spec(row)
        spec_payload = spec.model_dump(mode="json")
        spec_payload.update({key: value for key, value in dict(changes or {}).items()})
        updated = tool_models.IntentSpec.model_validate(spec_payload)
    except Exception as exc:  # noqa: BLE001
        return _fail("invalid_changes", f"意图修改不合法：{exc}")
    scope_error = _validate_scope(conn, board_id, _scope_payload(updated))
    if scope_error is not None:
        return _fail("scope_invalid", scope_error)
    basis_error = _validate_basis(conn, board_id, updated)
    if basis_error is not None:
        return _fail("basis_invalid", basis_error)
    old_payload = _spec_payload(spec)
    new_payload = _spec_payload(updated)
    substantive_keys = ("goal", "expected", "basis", "scope", "depends_on", "conflicts_with")
    substantive = any(
        old_payload.get(key) != new_payload.get(key) for key in substantive_keys
    )
    dependency_error: str | None = None
    resolved_deps: list[str] = []
    for ref in updated.depends_on:
        if ref.kind == "client_ref":
            if ref.value != updated.client_ref:
                dependency_error = "更新时依赖只能使用已有 intent_id"
            continue
        target = _get_row(conn, ref.value)
        if target is None or str(target["board_id"]) != board_id:
            dependency_error = f"依赖意图不存在：{ref.value}"
            continue
        if str(target["id"]) == str(intent_id):
            dependency_error = "意图不能依赖自己"
            continue
        resolved_deps.append(str(target["id"]))
    if dependency_error is not None:
        return _fail("dependency_invalid", dependency_error)
    resolved_conflicts: list[str] = []
    for ref in updated.conflicts_with:
        if ref.kind == "client_ref":
            if ref.value != updated.client_ref:
                return _fail("conflict_invalid", "更新时冲突只能使用已有 intent_id")
            continue
        target = _get_row(conn, ref.value)
        if target is None or str(target["board_id"]) != board_id:
            return _fail("conflict_invalid", f"冲突意图不存在：{ref.value}")
        if str(target["id"]) != str(intent_id):
            resolved_conflicts.append(str(target["id"]))
    cycle = _detect_cycle(conn, board_id, {str(intent_id): resolved_deps})
    if cycle is not None:
        return _fail("dependency_cycle", f"依赖关系成环：{cycle}")
    try:
        with transaction(conn):
            new_version = current_version + 1
            if substantive:
                new_status = STATUS_AWAITING
                authorization: dict[str, Any] = {}
            else:
                new_status = status if status in CONFIRMABLE_STATUSES else STATUS_AWAITING
                authorization = _loads(_row_value(row, "authorization", "{}"), {})
                if isinstance(authorization, dict) and authorization:
                    authorization = dict(authorization)
                    authorization["version"] = new_version
            _update_row(
                conn,
                intent_id,
                title=updated.title,
                goal=updated.goal,
                expected=updated.expected,
                basis=[ref.model_dump(mode="json") for ref in updated.basis],
                scope=_scope_payload(updated),
                depends_on=resolved_deps,
                conflicts_with=resolved_conflicts,
                version=new_version,
                status=new_status,
                authorization=authorization,
            )
            _normalize_conflicts(conn, board_id, intent_id, resolved_conflicts)
            resolved_questions = _resolve_questions(
                conn,
                board_id=board_id,
                submission_id=str(row["submission_id"] or ""),
                question_ids=resolved_question_ids,
            )
            new_row = _get_row(conn, intent_id)
            if new_row is not None:
                _record_version(conn, new_row, authorization=authorization)
    except Exception as exc:  # noqa: BLE001
        logger.warning("update_intent failed", exc_info=True)
        return _fail("update_failed", f"意图更新未保存：{exc}")
    new_row = _get_row(conn, intent_id)
    return {
        "ok": True,
        "status": STATUS_AWAITING if substantive else str(new_row["status"]),
        "version": int(_row_value(new_row, "version", 1) or 1),
        "requires_confirmation": bool(substantive),
        "authorization_invalidated": bool(substantive),
        "resolved_question_ids": resolved_questions,
        "intent": _intent_view(conn, new_row),
    }


def confirm_intents(
    conn: sqlite3.Connection, *, board_id: str, intent_ids: Iterable[str]
) -> dict[str, Any]:
    """用户显式确认：写授权快照并进入 approved；门槛由 API / 前端负责。"""
    board_id = str(board_id or "").strip()
    ids = list(dict.fromkeys(str(item) for item in intent_ids or [] if str(item).strip()))
    if not ids:
        return _fail("intent_ids_required", "至少需要一个意图 id")
    if not _board_exists(conn, board_id):
        return _fail("board_not_found", "板面不存在")
    results: list[dict[str, Any]] = []
    approved: list[str] = []
    try:
        with transaction(conn):
            for intent_id in ids:
                row = _get_row(conn, intent_id)
                if row is None:
                    results.append({"intent_id": intent_id, "ok": False, "status": "missing", "reason": "意图不存在"})
                    continue
                if str(row["board_id"]) != board_id:
                    results.append({"intent_id": intent_id, "ok": False, "status": "board_mismatch", "reason": "意图不属于这个板面"})
                    continue
                status = str(row["status"])
                if status in (STATUS_APPROVED, STATUS_RUNNING):
                    results.append({"intent_id": intent_id, "ok": True, "status": status, "reason": "已确认"})
                    approved.append(intent_id)
                    continue
                if status not in CONFIRMABLE_STATUSES:
                    results.append(
                        {
                            "intent_id": intent_id,
                            "ok": False,
                            "status": status,
                            "reason": f"当前状态 {status} 不能确认",
                        }
                    )
                    continue
                conflicts = [
                    str(item)
                    for item in _loads(row["conflicts_with"], []) or []
                ]
                blocking = []
                for other_id in conflicts:
                    other = _get_row(conn, other_id)
                    if other is not None and str(other["status"]) in CONFLICT_BLOCKING_STATUSES:
                        blocking.append(other_id)
                if blocking:
                    results.append(
                        {
                            "intent_id": intent_id,
                            "ok": False,
                            "status": "conflict",
                            "reason": "与已确认的互不相容意图冲突：" + ", ".join(blocking[:3]),
                        }
                    )
                    continue
                authorization = {
                    "version": int(_row_value(row, "version", 1) or 1),
                    "scope": _loads(_row_value(row, "scope", "{}"), {}),
                    "depends_on": [str(item) for item in _loads(row["depends_on"], []) or []],
                    "conflicts_with": conflicts,
                    "confirmed_at": _now(),
                    "confirmed_by": "user",
                    "source": AUTHORIZATION_SOURCE,
                }
                _update_row(
                    conn,
                    intent_id,
                    status=STATUS_APPROVED,
                    authorization=authorization,
                )
                new_row = _get_row(conn, intent_id)
                if new_row is not None:
                    _record_version(conn, new_row, authorization=authorization)
                results.append(
                    {
                        "intent_id": intent_id,
                        "ok": True,
                        "status": STATUS_APPROVED,
                        "version": int(_row_value(new_row, "version", 1) or 1),
                        "requires_confirmation": False,
                    }
                )
                approved.append(intent_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("confirm_intents failed", exc_info=True)
        return _fail("confirm_failed", f"确认未保存：{exc}")
    return {
        "ok": all(item.get("ok") for item in results) if results else False,
        "board_id": board_id,
        "results": results,
        "confirmed": approved,
        "gate": "program_authorization_snapshot",
    }


# -- 读取视图 ---------------------------------------------------------------


def list_intents_view(conn: sqlite3.Connection, board_id: str) -> dict[str, Any]:
    board_id = str(board_id or "").strip()
    if not board_id or not _board_exists(conn, board_id):
        return {"board_id": board_id, "seq": None, "intents": [], "available": False}
    rows = _extend(conn, board_id)
    seq = board_store.saved_seq(conn, board_id)
    operations_summary: dict[str, int] = {
        OP_APPLIED: 0,
        OP_RUNNING: 0,
        OP_PENDING: 0,
        OP_FAILED: 0,
        OP_CONFLICT: 0,
    }
    for row in rows:
        summary = _operation_summary(_operations(conn, str(row["id"])))
        for key, value in summary.items():
            operations_summary[key] += value
    return {
        "board_id": board_id,
        "seq": seq,
        "intents": [_intent_view(conn, row) for row in rows],
        "operations": operations_summary,
        "available": True,
    }


def get_intent(conn: sqlite3.Connection, board_id: str, intent_id: str) -> dict[str, Any] | None:
    row = _get_row(conn, intent_id)
    if row is None or str(row["board_id"]) != str(board_id):
        return None
    return _intent_view(conn, row)


# -- 操作账本 / 授权执行 -----------------------------------------------------


def _operation_for_key(conn: sqlite3.Connection, intent_id: str, operation_key: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM board_operations WHERE intent_id = ? AND operation_key = ?",
        (str(intent_id), str(operation_key)),
    ).fetchone()


def _scope_actions(scope: Mapping[str, Any]) -> set[str]:
    return {str(action) for action in scope.get("allowed_actions") or []}


def request_hash(payload: Any) -> str:
    """请求指纹：同一 operation_key 重试必须给出完全相同的请求。"""
    return hashlib.sha256(
        _dumps(payload if payload is not None else {}).encode("utf-8")
    ).hexdigest()


def authorize_operation(
    conn: sqlite3.Connection,
    *,
    board_id: str,
    intent_id: str,
    action: str,
    operation_key: str,
    request_payload: Any,
) -> dict[str, Any]:
    """工具写入前的授权 + 幂等检查（不写账本；账本由 begin_operation 写）。"""
    row = _get_row(conn, intent_id)
    if row is None:
        return _fail("intent_not_found", "意图不存在", status="not_found")
    if str(row["board_id"]) != str(board_id):
        return _fail("board_mismatch", "意图不属于这个板面")
    status = str(row["status"])
    if status == STATUS_AWAITING or status == STATUS_PROPOSED:
        return _fail(
            "awaiting_approval",
            "意图尚未获得用户确认，不能执行板面写入",
            status="awaiting_approval",
        )
    if status == STATUS_PAUSED:
        return _fail("intent_paused", "意图已暂停，不能执行新的写入", status="paused")
    if status in TERMINAL_STATUSES:
        return _fail("intent_finished", f"意图状态为 {status}，不能继续执行")
    if not _authorization_valid(row):
        return _fail(
            "authorization_stale",
            "旧授权已失效（意图范围已变化），需要用户重新确认",
            status="awaiting_approval",
        )
    authorization = _loads(_row_value(row, "authorization", "{}"), {})
    scope = authorization.get("scope") if isinstance(authorization, dict) else {}
    scope = scope if isinstance(scope, dict) else {}
    if str(action) not in _scope_actions(scope):
        return _fail(
            "scope_violation",
            f"动作 {action} 不在已确认的授权范围内",
            status="rejected",
        )
    existing = _operation_for_key(conn, intent_id, operation_key)
    fingerprint = request_hash(request_payload)
    if existing is not None:
        if str(existing["request_hash"] or "") != fingerprint:
            return _fail(
                "operation_key_conflict",
                "同一 operation_key 对应了不同请求：拒绝执行，未写板面",
                status="conflict",
                data={"operation_key": operation_key},
            )
        operation = _operation_row(existing)
        if operation["status"] == OP_APPLIED:
            return {
                "ok": True,
                "replayed": True,
                "operation": operation,
                "intent": _intent_view(conn, row),
                "authorization": authorization,
            }
        if operation["status"] in (OP_RUNNING, OP_PENDING):
            return _fail(
                "operation_in_progress",
                "同一操作仍在执行中，未重复写入",
                status="running",
                data={"operation_key": operation_key},
            )
    return {
        "ok": True,
        "replayed": False,
        "operation": None,
        "intent": _intent_view(conn, row),
        "authorization": authorization,
        "scope": scope,
        "request_hash": fingerprint,
    }


def begin_operation(
    conn: sqlite3.Connection,
    *,
    board_id: str,
    intent_id: str,
    action: str,
    operation_key: str,
    request_payload: Any,
    before: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """授权 + 幂等检查后写一条 running 账本（与后续板面写入同一事务）。"""
    authorized = authorize_operation(
        conn,
        board_id=board_id,
        intent_id=intent_id,
        action=action,
        operation_key=operation_key,
        request_payload=request_payload,
    )
    if not authorized.get("ok") or authorized.get("replayed"):
        return authorized
    row = _get_row(conn, intent_id)
    assert row is not None
    operation_id = models.new_id("bo")
    stamp = _now()
    conn.execute(
        "INSERT INTO board_operations"
        " (id, board_id, intent_id, operation_key, request_hash, action, status, before,"
        "  after, result, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, '{}', '{}', ?, ?)",
        (
            operation_id,
            str(board_id),
            str(intent_id),
            str(operation_key),
            str(authorized.get("request_hash") or ""),
            str(action),
            OP_RUNNING,
            _dumps(dict(before or {})),
            stamp,
            stamp,
        ),
    )
    if str(row["status"]) != STATUS_RUNNING:
        _update_row(conn, intent_id, status=STATUS_RUNNING, attempt=int(_row_value(row, "attempt", 0) or 0) + 1)
    authorized["operation_id"] = operation_id
    authorized["replayed"] = False
    return authorized


def complete_operation(
    conn: sqlite3.Connection,
    operation_id: str,
    *,
    after: Mapping[str, Any] | None = None,
    result: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM board_operations WHERE id = ?", (str(operation_id),)
    ).fetchone()
    if row is None:
        return _fail("operation_not_found", "操作记录不存在")
    conn.execute(
        "UPDATE board_operations SET status = ?, after = ?, result = ?, updated_at = ?"
        " WHERE id = ?",
        (
            OP_APPLIED,
            _dumps(dict(after or {})),
            _dumps(dict(result or {})),
            _now(),
            str(operation_id),
        ),
    )
    return {"ok": True, "operation": _operation_row(
        conn.execute("SELECT * FROM board_operations WHERE id = ?", (str(operation_id),)).fetchone()
    )}


def fail_operation(
    conn: sqlite3.Connection,
    operation_id: str,
    *,
    status: str = OP_FAILED,
    error: str = "",
    result: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM board_operations WHERE id = ?", (str(operation_id),)
    ).fetchone()
    if row is None:
        return _fail("operation_not_found", "操作记录不存在")
    payload = dict(result or {})
    if error:
        payload.setdefault("error", str(error)[:2000])
    conn.execute(
        "UPDATE board_operations SET status = ?, result = ?, updated_at = ? WHERE id = ?",
        (str(status), _dumps(payload), _now(), str(operation_id)),
    )
    return {"ok": True}


def new_objects_used(conn: sqlite3.Connection, intent_id: str) -> dict[str, int]:
    """创建数量按整个意图累计（跨续作 / 重试不重置，幂等重放不重复计数）。"""
    used = {"cards": 0, "groups": 0, "links": 0}
    for operation in _operations(conn, str(intent_id)):
        if operation["status"] != OP_APPLIED:
            continue
        action = operation["action"]
        result = operation["result"] if isinstance(operation["result"], dict) else {}
        if action == "create_card":
            used["cards"] += 1
        elif action == "group_cards" and result.get("created_group_id"):
            used["groups"] += 1
        elif action == "add_link" and result.get("created"):
            used["links"] += 1
    return used


# -- 完成核对 ---------------------------------------------------------------


def _dependency_states(conn: sqlite3.Connection, row: sqlite3.Row) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for raw in _loads(row["depends_on"], []) or []:
        other = _get_row(conn, str(raw))
        out.append(
            {
                "intent_id": str(raw),
                "status": str(other["status"]) if other is not None else "missing",
            }
        )
    return out


def finish_intent(
    conn: sqlite3.Connection,
    *,
    board_id: str,
    intent_id: str,
    expected_version: int,
    outcome: str,
    summary: str,
    operation_keys: Iterable[str],
) -> dict[str, Any]:
    """事务内核对授权 / 账本 / 依赖 / 等待 / 冲突后声明本次尝试结果。"""
    board_id = str(board_id or "").strip()
    row = _get_row(conn, intent_id)
    if row is None:
        return _fail("intent_not_found", "意图不存在", status="not_found")
    if str(row["board_id"]) != board_id:
        return _fail("board_mismatch", "意图不属于这个板面")
    version = int(_row_value(row, "version", 1) or 1)
    if int(expected_version) != version:
        return _fail(
            "version_conflict",
            f"意图版本不符：期望 v{expected_version}，当前 v{version}（可能有新尝试或用户修改）",
            status="conflict",
            data={"current_version": version},
        )
    status = str(row["status"])
    if status in (STATUS_CANCELLED, STATUS_PAUSED, STATUS_COMPLETED):
        return _fail(
            "late_completion",
            f"意图状态已是 {status}：迟到完成不能覆盖取消 / 暂停 / 已完成",
            status="conflict",
        )
    if status in (STATUS_PROPOSED, STATUS_AWAITING):
        return _fail("awaiting_approval", "意图尚未确认，不能声明完成", status="awaiting_approval")
    if status not in FINISHABLE_STATUSES:
        return _fail("invalid_status", f"意图状态 {status} 不能声明完成")
    if outcome not in FINISH_OUTCOMES:
        return _fail("invalid_outcome", f"outcome 不合法：{outcome}")
    if not _authorization_valid(row):
        return _fail(
            "authorization_stale",
            "当前授权无效（意图版本已变化），需要在确认后重新执行",
            status="awaiting_approval",
        )
    provided_keys = [str(item) for item in operation_keys or [] if str(item).strip()]
    if len(provided_keys) != len(set(provided_keys)):
        return _fail("invalid_operation_keys", "operation_keys 不得重复")
    operations = _operations(conn, intent_id)
    unfinished = [
        operation
        for operation in operations
        if operation["status"] in (OP_PENDING, OP_RUNNING)
    ]
    applied = {
        operation["operation_key"]: operation
        for operation in operations
        if operation["status"] == OP_APPLIED
    }
    missing_keys = [key for key in applied if key not in provided_keys]
    unknown_keys = [key for key in provided_keys if key not in applied]
    if unknown_keys:
        return _fail(
            "unknown_operation_keys",
            "operation_keys 引用了账本里不存在的操作：" + ", ".join(unknown_keys[:5]),
            status="conflict",
        )
    waits = _waiting_items(conn, board_id=board_id, submission_id=str(row["submission_id"] or ""))
    waiting_unavailable = waits is None
    unresolved_waiting = [
        item for item in (waits or []) if str(item.get("status") or "") not in ("resolved", "answered")
    ]
    dependencies = _dependency_states(conn, row)
    unfinished_dependencies = [
        item for item in dependencies if item["status"] != STATUS_COMPLETED
    ]
    conflict_states = []
    for raw in _loads(row["conflicts_with"], []) or []:
        other = _get_row(conn, str(raw))
        conflict_states.append(
            {"intent_id": str(raw), "status": str(other["status"]) if other is not None else "missing"}
        )
    unresolved_conflicts = [
        item for item in conflict_states if item["status"] in CONFLICT_BLOCKING_STATUSES
    ]
    target_status = FINISH_OUTCOMES[outcome]
    if outcome == "completed":
        blockers: list[dict[str, Any]] = []
        if unfinished:
            blockers.append({"kind": "running_operations", "ids": [op["operation_id"] for op in unfinished]})
        if missing_keys:
            blockers.append({"kind": "unlisted_applied_operations", "keys": missing_keys})
        if unresolved_waiting:
            blockers.append({"kind": "waiting", "ids": [str(item.get("id") or item.get("question_id") or "") for item in unresolved_waiting]})
        if unfinished_dependencies:
            blockers.append({"kind": "dependencies", "ids": [item["intent_id"] for item in unfinished_dependencies]})
        if unresolved_conflicts:
            blockers.append({"kind": "conflicts", "ids": [item["intent_id"] for item in unresolved_conflicts]})
        if blockers:
            return _fail(
                "completion_blocked",
                "存在未结束项，不能声明 completed",
                status="conflict",
                data={
                    "blockers": blockers,
                    "waiting_unavailable": waiting_unavailable,
                    "operations": _operation_summary(operations),
                },
            )
    results = []
    for key in provided_keys:
        operation = applied.get(key)
        if operation is None:
            continue
        results.append(
            {
                "result_id": operation["operation_id"],
                "operation_key": key,
                "action": operation["action"],
                "result": operation["result"],
                "after": operation["after"],
                "created_at": operation["updated_at"],
            }
        )
    try:
        with transaction(conn):
            _update_row(
                conn,
                intent_id,
                status=target_status,
                outcome=outcome,
                summary=str(summary or "")[: tool_models.MAX_SUMMARY_CHARS],
                finished_at=_now(),
            )
            new_row = _get_row(conn, intent_id)
            if new_row is not None:
                _record_version(conn, new_row, authorization=_loads(_row_value(new_row, "authorization", "{}"), {}))
    except Exception as exc:  # noqa: BLE001
        logger.warning("finish_intent failed", exc_info=True)
        return _fail("finish_failed", f"完成结果未保存：{exc}")
    new_row = _get_row(conn, intent_id)
    return {
        "ok": True,
        "status": target_status,
        "outcome": outcome,
        "intent": _intent_view(conn, new_row),
        "results": results,
        "waiting_unavailable": waiting_unavailable,
        "operations": _operation_summary(operations),
    }


# -- 暂停 / 取消 / 恢复 ------------------------------------------------------


def pause_intent(conn: sqlite3.Connection, *, board_id: str, intent_id: str, expected_version: int) -> dict[str, Any]:
    return _change_status(
        conn, board_id=board_id, intent_id=intent_id, expected_version=expected_version,
        status=STATUS_PAUSED, extra={},
    )


def cancel_intent(conn: sqlite3.Connection, *, board_id: str, intent_id: str, expected_version: int) -> dict[str, Any]:
    return _change_status(
        conn, board_id=board_id, intent_id=intent_id, expected_version=expected_version,
        status=STATUS_CANCELLED, extra={"finished_at": _now()},
    )


def reject_intent(conn: sqlite3.Connection, *, board_id: str, intent_id: str, expected_version: int) -> dict[str, Any]:
    return _change_status(
        conn, board_id=board_id, intent_id=intent_id, expected_version=expected_version,
        status=STATUS_REJECTED, extra={"finished_at": _now()},
    )


def _change_status(
    conn: sqlite3.Connection,
    *,
    board_id: str,
    intent_id: str,
    expected_version: int,
    status: str,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    row = _get_row(conn, intent_id)
    if row is None or str(row["board_id"]) != str(board_id):
        return _fail("intent_not_found", "意图不存在", status="not_found")
    version = int(_row_value(row, "version", 1) or 1)
    if int(expected_version) != version:
        return _fail(
            "version_conflict",
            f"意图版本不符：期望 v{expected_version}，当前 v{version}",
            status="conflict",
            data={"current_version": version},
        )
    with transaction(conn):
        _update_row(
            conn,
            intent_id,
            status=status,
            version=version + 1,
            **(dict(extra or {})),
        )
        new_row = _get_row(conn, intent_id)
        if new_row is not None:
            _record_version(conn, new_row, authorization=_loads(_row_value(new_row, "authorization", "{}"), {}))
    return {"ok": True, "status": status, "version": version + 1, "intent": _intent_view(conn, _get_row(conn, intent_id))}


def recover_intent(
    conn: sqlite3.Connection, *, board_id: str, intent_id: str, expected_version: int
) -> dict[str, Any]:
    """把 paused / failed / partially_completed 且旧授权仍有效的意图放回 approved。"""
    row = _get_row(conn, intent_id)
    if row is None or str(row["board_id"]) != str(board_id):
        return _fail("intent_not_found", "意图不存在", status="not_found")
    version = int(_row_value(row, "version", 1) or 1)
    if int(expected_version) != version:
        return _fail(
            "version_conflict",
            f"意图版本不符：期望 v{expected_version}，当前 v{version}",
            status="conflict",
            data={"current_version": version},
        )
    status = str(row["status"])
    if status == STATUS_APPROVED:
        return {"ok": True, "status": STATUS_APPROVED, "version": version, "already": True, "intent": _intent_view(conn, row)}
    if status not in (STATUS_PAUSED, STATUS_FAILED, STATUS_PARTIALLY_COMPLETED):
        return _fail("invalid_status", f"当前状态 {status} 不能恢复")
    authorization = _loads(_row_value(row, "authorization", "{}"), {})
    if not _authorization_valid(row):
        return _fail(
            "authorization_stale",
            "旧授权已失效，需要用户重新确认后才能恢复",
            status="awaiting_approval",
        )
    with transaction(conn):
        _update_row(
            conn,
            intent_id,
            status=STATUS_APPROVED,
            attempt=int(_row_value(row, "attempt", 0) or 0) + 1,
            version=version + 1,
        )
        new_row = _get_row(conn, intent_id)
        if new_row is not None:
            _record_version(conn, new_row, authorization=authorization)
    return {"ok": True, "status": STATUS_APPROVED, "version": version + 1, "intent": _intent_view(conn, new_row)}


def preview_recovery(conn: sqlite3.Connection, intent_id: str) -> dict[str, Any]:
    """恢复预览（只读）：未结束操作 / 依赖 / 等待 / 授权有效性 / 可否恢复。"""
    row = _get_row(conn, intent_id)
    if row is None:
        return {"ok": False, "code": "intent_not_found", "intent_id": str(intent_id)}
    operations = _operations(conn, str(intent_id))
    unfinished_operations = [
        operation for operation in operations if operation["status"] in (OP_PENDING, OP_RUNNING)
    ]
    failed_operations = [operation for operation in operations if operation["status"] == OP_CONFLICT]
    dependencies = _dependency_states(conn, row)
    unfinished_dependencies = [item for item in dependencies if item["status"] != STATUS_COMPLETED]
    waits = _waiting_items(conn, board_id=str(row["board_id"]), submission_id=str(row["submission_id"] or ""))
    waiting_unavailable = waits is None
    unresolved_waiting = [
        item for item in (waits or []) if str(item.get("status") or "") not in ("resolved", "answered")
    ]
    status = str(row["status"])
    authorization_ok = _authorization_valid(row)
    can_resume = status in (STATUS_PAUSED, STATUS_FAILED, STATUS_PARTIALLY_COMPLETED) and authorization_ok
    reasons: list[str] = []
    if status == STATUS_AWAITING:
        reasons.append("意图尚未确认")
    if status in TERMINAL_STATUSES:
        reasons.append(f"意图已结束（{status}）")
    if not authorization_ok and status not in (STATUS_AWAITING,):
        reasons.append("旧授权已失效，需要用户重新确认")
    return {
        "ok": True,
        "intent_id": str(intent_id),
        "board_id": str(row["board_id"]),
        "status": status,
        "version": int(_row_value(row, "version", 1) or 1),
        "authorization_valid": authorization_ok,
        "can_resume": can_resume,
        "unfinished_operations": unfinished_operations,
        "failed_operations": failed_operations,
        "unfinished_dependencies": unfinished_dependencies,
        "unresolved_waiting": unresolved_waiting,
        "waiting_unavailable": waiting_unavailable,
        "reasons": reasons,
        "operations": _operation_summary(operations),
        "results": [
            operation["result"]
            for operation in operations
            if operation["status"] == OP_APPLIED
        ],
    }


# -- 只读账本 / 结果（C 的 API 读取） ---------------------------------------


def ledger_operations(
    conn: sqlite3.Connection, *, board_id: str, intent_id: str | None = None
) -> dict[str, Any]:
    board_id = str(board_id or "").strip()
    if not board_id or not _board_exists(conn, board_id):
        return {"available": False, "board_id": board_id, "operations": []}
    if intent_id is not None:
        rows = conn.execute(
            "SELECT * FROM board_operations WHERE board_id = ? AND intent_id = ?"
            " ORDER BY created_at, id",
            (board_id, str(intent_id)),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM board_operations WHERE board_id = ? ORDER BY created_at, id",
            (board_id,),
        ).fetchall()
    operations = [_operation_row(row) for row in rows]
    return {
        "available": True,
        "board_id": board_id,
        "intent_id": intent_id,
        "counts": _operation_summary(operations),
        "operations": operations,
    }


def ledger_summary(conn: sqlite3.Connection, board_id: str) -> dict[str, Any]:
    view = ledger_operations(conn, board_id=board_id)
    return {
        "available": view["available"],
        "board_id": board_id,
        "counts": view.get("counts", {}),
        "operations": view.get("operations", []),
    }


def list_results(conn: sqlite3.Connection, board_id: str) -> dict[str, Any]:
    """结果卡引用：只从权威账本里 applied 的创建操作派生，不读模型文本。"""
    ledger = ledger_operations(conn, board_id=board_id)
    results: list[dict[str, Any]] = []
    if ledger.get("available"):
        for operation in ledger["operations"]:
            if operation["status"] != OP_APPLIED:
                continue
            payload = operation["result"] if isinstance(operation["result"], dict) else {}
            card = payload.get("card") if isinstance(payload.get("card"), dict) else {}
            card_id = payload.get("card_id") or card.get("id")
            if operation["action"] != "create_card" or not card_id:
                continue
            content = str(card.get("content") or payload.get("content") or "")
            title = str(card.get("title") or "")
            results.append(
                {
                    "result_id": operation["operation_id"],
                    "operation_key": operation["operation_key"],
                    "card_id": str(card_id),
                    "kind": str(card.get("kind") or "reply"),
                    "title": title,
                    "content": content,
                    "intent_id": operation["intent_id"],
                    "source": "board_tool",
                    "created_at": operation["updated_at"],
                }
            )
    return {"available": ledger.get("available", False), "board_id": board_id, "results": results}


def list_waiting_view(conn: sqlite3.Connection, board_id: str) -> dict[str, Any]:
    """C 的 API 读取等待摘要（A 的持久化不可用时如实 available=false，不伪造）。"""
    board_id = str(board_id or "").strip()
    items = _waiting_items(conn, board_id=board_id, submission_id="")
    if items is None:
        return {"available": False, "board_id": board_id, "count": 0, "items": []}
    return {
        "available": True,
        "board_id": board_id,
        "count": len(items),
        "items": items,
    }
