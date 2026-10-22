"""互动模式：批准后的真实执行、暂停 / 取消 / 恢复（子智能体 C）。

契约：docs/interactive-mode-contract.md §14（14.1 执行状态 / 14.3 run_execution /
14.4 HTTP / 14.5 允许的板面操作集合）。

设计要点（每条都对应契约条文）：

- 只执行已批准的操作集合：一次执行的操作只可能是 models.ALLOWED_INTENT_OPS。
  出现别的操作（删除 / 隐藏 / 勾选 / 改用户卡片正文）时，服务端在写入之前拒绝，
  返回「需要重新提出方案与审批」，绝不硬做、绝不擅自扩展批准范围。
- 执行前与写回前重核：材料版本（intents 既有 basis / materialWatch）、任务状态、
  审批有效性、被改动对象的版本。用户已经改过的东西不会被覆盖：版本对不上就停下并说明。
- 逐步 checkpoint：每一步写前 / 写后都落 board_executions.steps 与 checkpoints；
  每一步都是短事务，不跨模型调用持有数据库事务或全局锁。
- 幂等：每一步要新增的对象 id 在批准时就确定并写进 plan；恢复 / 重连 / 迟到重放
  都不会重复新增同一张卡片、同一个组、同一条关系。
- 迟到结果丢弃：模型返回、工具结果与完成事件都按
  (execution id, attempt, owner_instance_id) 核对；owner 或 attempt 变了就丢弃。
- 暂停 / 取消作用于真实执行：状态写在 board_executions 上，正在 await 的执行在下一个
  核对点立刻退出；不自动重试。
- 恢复不重复已完成步骤：done 步骤直接跳过；running（被中断）步骤按「对象是否已经按
  批准内容落地」判定，落地了就采纳为已完成，没落地才重做。
- 失败 / 取消的撤回复用 intents 既有机制：只撤回本任务造成且仍可安全撤回的改动，
  不覆盖用户后续编辑；影响其他工作时沿用既有 pendingDecision 待确认。
"""

from __future__ import annotations

import asyncio
import copy
import inspect
import sqlite3
from typing import Any, Iterable

from agent.interactive import models, placement

#: board_executions 里需要 JSON 序列化的列
_JSON_COLUMNS = ("plan", "steps", "checkpoints", "result")

#: 一次执行保留的 checkpoint 上限（够复盘，又不会让一行无限膨胀）
MAX_CHECKPOINTS = 200

#: 结果卡片正文的长度上限（结果卡片是给人看的，不做无限长正文）
MAX_GENERATED_CHARS = 4000

#: 允许的操作集合（冻结，唯一来源是 models）
ALLOWED_OPS = models.ALLOWED_INTENT_OPS

#: 新增类操作：恢复 / 重放靠批准时预先分配的对象 id 对账
_ADD_OPS = ("add_reply_card", "add_text_card")

#: 结果卡片正文的生成提示（唯一来源在 agent.prompts）。
#: 执行期的文本生成受允许操作集合约束：模型输出只会成为已批准操作里那张卡片的正文，
#: 永远不会被解析成新的操作。
from agent.prompts import INTERACTIVE_RESULT_CARD_SYSTEM as _GENERATE_SYSTEM  # noqa: E402


# --- 依赖取用（延迟 import：避免 intents 与 execution 的 import 环） ---------


def _intents():
    from agent.interactive import intents

    return intents


def _board():
    from agent.interactive import board

    return board


def _store():
    from agent.interactive import board_store

    return board_store


def _transaction(conn: sqlite3.Connection):
    from agent.storage.db import transaction

    return transaction(conn)


def _now() -> str:
    return models.now_iso()


def _row_value(row: sqlite3.Row, key: str, default: Any = None) -> Any:
    """读一列，列不存在时给默认值（旧库 / 只读替身也能安全读）。"""
    try:
        value = row[key]
    except (IndexError, KeyError):
        return default
    return default if value is None else value


def _fail(reason: str, detail: str, **extra: Any) -> dict:
    return {"ok": False, "reason": reason, "detail": detail, **extra}


# --- 执行记录的查询与负载 ---------------------------------------------------


def get_execution(conn: sqlite3.Connection, execution_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM board_executions WHERE id = ?", (execution_id,)
    ).fetchone()


def _board_execution_rows(
    conn: sqlite3.Connection, board_id: str, *, limit: int = 50
) -> list[sqlite3.Row]:
    count = max(1, min(int(limit or 50), 200))
    return list(
        conn.execute(
            "SELECT * FROM board_executions WHERE board_id = ?"
            " ORDER BY created_at DESC, rowid DESC LIMIT ?",
            (board_id, count),
        ).fetchall()
    )


def _intent_execution_rows(
    conn: sqlite3.Connection, intent_id: str, *, limit: int = 20
) -> list[sqlite3.Row]:
    count = max(1, min(int(limit or 20), 100))
    return list(
        conn.execute(
            "SELECT * FROM board_executions WHERE intent_id = ?"
            " ORDER BY created_at DESC, rowid DESC LIMIT ?",
            (intent_id, count),
        ).fetchall()
    )


def active_execution(conn: sqlite3.Connection, intent_id: str) -> sqlite3.Row | None:
    """这条意图活跃的执行（queued / running / paused）：幂等与暂停取消都看它。"""
    for row in _intent_execution_rows(conn, intent_id):
        if row["status"] in ("queued", "running", "paused"):
            return row
    return None


def latest_execution(conn: sqlite3.Connection, intent_id: str) -> sqlite3.Row | None:
    rows = _intent_execution_rows(conn, intent_id, limit=1)
    return rows[0] if rows else None


def _ids(value: Any) -> list[str]:
    return [str(x) for x in (value or []) if str(x)]


def execution_payload(row: sqlite3.Row) -> dict:
    """接口负载：字段与前端冻结类型 Execution 一致（内有 steps / result / owner）。"""
    result = models.loads(_row_value(row, "result", {}), {})
    if not isinstance(result, dict):
        result = {}
    steps = models.loads(_row_value(row, "steps", []), [])
    plan = models.loads(_row_value(row, "plan", {}), {})
    # F5：符号 → 正式对象 id 的稳定映射（批准时分配、重试复用同一份）。
    object_refs = {
        str(op.get("ref")): str(op.get("id"))
        for op in (plan.get("ops") if isinstance(plan, dict) else []) or []
        if isinstance(op, dict) and op.get("ref") and op.get("id")
    }
    return {
        "id": row["id"],
        "intentId": row["intent_id"],
        "boardId": row["board_id"],
        "attempt": int(row["attempt"] or 1),
        "status": row["status"],
        "steps": [step for step in steps if isinstance(step, dict)],
        "objectRefs": object_refs,
        "result": {
            "cardIds": _ids(result.get("cardIds")),
            "groupIds": _ids(result.get("groupIds")),
            "linkIds": _ids(result.get("linkIds")),
        },
        "error": row["error"],
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
        "startedAt": row["started_at"],
        "finishedAt": row["finished_at"],
        "ownerInstanceId": row["owner_instance_id"],
    }


def list_executions(conn: sqlite3.Connection, board_id: str, *, limit: int = 50) -> dict:
    """本板面的执行记录（新的在前）——真实状态由服务端给出，前端不自己拼。"""
    return {
        "executions": [
            execution_payload(row) for row in _board_execution_rows(conn, board_id, limit=limit)
        ]
    }


# --- 执行计划：批准时确定，之后不再照抄旧快照 -------------------------------


def _invalid(reason: str, detail: str) -> dict:
    return {"ok": False, "reason": reason, "detail": detail}


def _num(value: Any, fallback: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback


def build_plan(row: sqlite3.Row) -> dict:
    """把这条意图已批准的操作变成可执行的计划。

    - 只接受 models.ALLOWED_INTENT_OPS；出现别的操作直接判定无效
      （批准后必须让用户重新提出方案与审批，而不是硬做）；
    - 新增类操作在这里就分配好对象 id：恢复 / 重放按同一个 id 对账，天然幂等；
    - 位置与大小只影响显示；参数形状不完整的操作判定无效。
    """
    ops_raw = models.loads(_row_value(row, "operations", []), [])
    if not isinstance(ops_raw, list):
        return _invalid(
            "invalid_operations", "这条意图的操作集合不是列表：需要重新提出方案与审批。"
        )
    ops: list[dict] = []
    for index, raw in enumerate(ops_raw):
        if not isinstance(raw, dict):
            return _invalid(
                "invalid_operations",
                "第 " + str(index + 1) + " 项不是操作对象：需要重新提出方案与审批。",
            )
        kind = str(raw.get("op") or "")
        label = "第 " + str(index + 1) + " 项"
        if kind not in ALLOWED_OPS:
            return _invalid(
                "forbidden_operation",
                label
                + "操作「"
                + (kind or "（空）")
                + "」不在允许的板面操作集合里：QIO 只能做用户在互动模式里能做的操作。"
                "需要重新提出方案与审批；本次没有写入任何改动。",
            )
        op = dict(raw)
        if kind in _ADD_OPS:
            content = str(raw.get("content") or "").strip()
            if kind == "add_text_card" and not content:
                return _invalid(
                    "invalid_operations", label + "文字卡片没有正文：需要重新提出方案与审批。"
                )
            op["id"] = models.new_id("c")
            # F5：`ref` 声明本批次内的符号（同批更早的操作可以引用它）。
            op["ref"] = str(raw.get("ref") or raw.get("cardId") or "").strip()
            op["content"] = content
        elif kind == "group_cards":
            members = [str(x) for x in (raw.get("cardIds") or []) if str(x).strip()]
            if not members:
                return _invalid(
                    "invalid_operations", label + "成组操作没有成员：需要重新提出方案与审批。"
                )
            op["id"] = models.new_id("g")
            op["ref"] = str(raw.get("ref") or raw.get("groupId") or "").strip()
            op["cardIds"] = members
        elif kind == "add_link":
            src, dst = str(raw.get("src") or "").strip(), str(raw.get("dst") or "").strip()
            if not src or not dst or src == dst:
                return _invalid(
                    "invalid_operations", label + "关系操作的端点无效：需要重新提出方案与审批。"
                )
            op["id"] = models.new_id("l")
            op["src"], op["dst"] = src, dst
        elif kind == "move_card":
            if not str(raw.get("cardId") or "").strip():
                return _invalid(
                    "invalid_operations", label + "移动操作缺少卡片：需要重新提出方案与审批。"
                )
            if raw.get("x") is None or raw.get("y") is None:
                return _invalid(
                    "invalid_operations", label + "移动操作缺少坐标：需要重新提出方案与审批。"
                )
        elif kind == "order_group":
            if not str(raw.get("groupId") or "").strip():
                return _invalid(
                    "invalid_operations", label + "排序操作缺少组：需要重新提出方案与审批。"
                )
            if not [str(x) for x in (raw.get("memberOrder") or [])]:
                return _invalid(
                    "invalid_operations", label + "排序操作缺少成员顺序：需要重新提出方案与审批。"
                )
        ops.append(op)
    return {
        "ok": True,
        "ops": ops,
        "intentId": row["id"],
        "understandingId": str(_row_value(row, "understanding_id", "") or ""),
        "intentKey": str(_row_value(row, "intent_key", "") or ""),
        "targets": {},
    }


#: 参与「方案版本」指纹的操作字段（新分配的对象 id 不算方案内容）
_BINDING_OP_KEYS = (
    "op",
    "content",
    "cardId",
    "cardIds",
    "groupId",
    "memberOrder",
    "src",
    "dst",
    "direction",
    "meaning",
    "ordered",
    "name",
    "x",
    "y",
    "w",
    "h",
    # F5 符号声明 / F7 相对摆放 / F8 排列方式：都是方案内容，批准后变化必须能被发现。
    "ref",
    "place",
    "arrange",
)


def _binding_payload(ops: Iterable[dict]) -> list[dict]:
    """参与「方案版本」指纹的操作形状（新分配的对象 id 不算方案内容）。"""
    return [
        {key: op.get(key) for key in _BINDING_OP_KEYS if key in op}
        for op in ops
        if isinstance(op, dict)
    ]


def approval_binding(conn: sqlite3.Connection, row: sqlite3.Row, plan: dict) -> dict:
    """审批绑定：批准那一刻的**方案版本 + 材料版本 + 目标对象版本**。

    批准之后这些依据任何一项变了，执行前都会对不上并退回
    「需要重新提出方案与审批」，而不是按旧依据写回。
    """
    intents = _intents()
    ops = plan.get("ops") or []
    payload = _binding_payload(ops)
    try:
        saved_seq = int(_store().load_board(conn, row["board_id"])["seq"])
    except (TypeError, ValueError):
        saved_seq = None
    progress = models.loads(_row_value(row, "progress", {}), {})
    basis = progress.get("__basisWatch") if isinstance(progress, dict) else None
    return {
        "operationsHash": _signature(payload),
        "previewHash": _signature(
            [
                {
                    "op": op.get("op"),
                    "content": op.get("content"),
                    "cardIds": op.get("cardIds"),
                    "groupId": op.get("groupId"),
                }
                for op in payload
            ]
        ),
        "materials": intents._material_watch(row),
        "basis": basis if isinstance(basis, dict) else {},
        "targets": plan.get("targets") or {},
        "boardSeq": saved_seq,
        "approvedAt": _now(),
        "operationCount": len(payload),
    }


def _signature(value: Any) -> str:
    return _intents()._signature(value)


def _target_signatures(conn: sqlite3.Connection, board_id: str, ops: list[dict]) -> dict:
    """被操作的既有对象在批准那一刻的语义版本。

    执行前与写回前都拿它对账：对象被用户改过就不再按旧快照写回
    （位置与大小不算语义变化，只移动是用户同样能做的操作）。
    """
    intents = _intents()
    state = _store().load_board(conn, board_id)["state"]
    cards = {str(c.get("id")): c for c in state.get("cards") or [] if models.is_live(c)}
    groups = {str(g.get("id")): g for g in state.get("groups") or [] if not g.get("deleted")}
    planned_new = {str(op.get("id")) for op in ops if op.get("id")}
    refs = {str(op.get("ref")) for op in ops if op.get("ref")}
    card_targets: dict[str, str] = {}
    group_targets: dict[str, str] = {}

    def _card_ref(ref: Any) -> None:
        key = str(ref or "")
        if key and key not in planned_new and key not in refs and key in cards:
            card_targets[key] = intents._card_signature(cards[key])

    for op in ops:
        kind = op["op"]
        if kind == "move_card":
            _card_ref(op.get("cardId"))
        elif kind == "add_link":
            _card_ref(op.get("src"))
            _card_ref(op.get("dst"))
        elif kind == "group_cards":
            for ref in op.get("cardIds") or []:
                _card_ref(ref)
        elif kind == "order_group":
            key = str(op.get("groupId") or "")
            if key and key not in planned_new and key not in refs and key in groups:
                group_targets[key] = intents._group_signature(groups[key])
            for ref in op.get("memberOrder") or []:
                _card_ref(ref)
    return {"cards": card_targets, "groups": group_targets}


def stale_targets(conn: sqlite3.Connection, board_id: str, plan: dict) -> list[str]:
    """与批准时不一致的既有对象（用户改过 / 已经不在板面上）。"""
    intents = _intents()
    targets = plan.get("targets") or {}
    state = _store().load_board(conn, board_id)["state"]
    cards = {str(c.get("id")): c for c in state.get("cards") or []}
    groups = {str(g.get("id")): g for g in state.get("groups") or []}
    stale: list[str] = []
    for card_id, signature in (targets.get("cards") or {}).items():
        card = cards.get(str(card_id))
        if card is None or card.get("deleted") or intents._card_signature(card) != signature:
            stale.append(str(card_id))
    for group_id, signature in (targets.get("groups") or {}).items():
        group = groups.get(str(group_id))
        if group is None or group.get("deleted") or intents._group_signature(group) != signature:
            stale.append(str(group_id))
    return stale


def _ref_keys(value: Any, decl_kind: str) -> list[str]:
    """一个声明的两种写法都要能解析：裸符号与**类型正确**的 `card:a` / `group:a`。

    不给另一种前缀起别名：`group:a` 永远不能解析到一张叫 a 的卡片（类型必须一致）。
    """
    symbol = str(value or "").strip()
    if not symbol:
        return []
    prefix = decl_kind if decl_kind in ("card", "group") else "card"
    if symbol.startswith("card:") or symbol.startswith("group:"):
        return [symbol, symbol.split(":", 1)[1]]
    return [symbol, prefix + ":" + symbol]


def preview_from_operations(
    operations: Iterable[dict],
    *,
    cards: Iterable[dict] = (),
    view: dict | None = None,
) -> dict:
    """把已批准的操作集合画成虚线预览（前端只看得到会发生什么）。

    预览里出现的是将要新增 / 将要改变的对象；移动与排序不产生新对象，因此不进预览
    （它们不是新内容），但仍会真实执行。

    F7：落点与尺寸与**真实执行**共用 placement.plan_card_placement（同一函数、同一冻结
    view），所以预览显示的坐标就是执行时会落地的坐标；`cards` / `view` 缺省时按没有
    既有卡片、没有视野处理（逐字段仍确定性）。
    """
    existing = [card for card in cards if isinstance(card, dict)]
    cards_out: list[dict] = []
    groups: list[dict] = []
    links: list[dict] = []
    ref_map: dict[str, str] = {}

    def resolve(value: Any) -> str:
        key = str(value or "").strip()
        if not key:
            return ""
        if key in ref_map:
            return ref_map[key]
        symbol = key.split(":", 1)[1] if ":" in key else key
        return ref_map.get(symbol, key)

    for index, raw in enumerate(operations or []):
        if not isinstance(raw, dict):
            continue
        kind = str(raw.get("op") or "")
        if kind in _ADD_OPS:
            symbol = str(raw.get("ref") or raw.get("cardId") or "").strip()
            preview_id = symbol or ("p_" + str(index))
            card = models.new_card(
                models.REPLY_KIND if kind == "add_reply_card" else "text",
                str(raw.get("content") or ""),
            )
            card["id"] = preview_id
            if raw.get("place") is not None and not isinstance(raw.get("place"), dict):
                card["placementNote"] = "place 参数形状无效，已按默认落点处理"
            geometry = placement.plan_card_placement(
                raw,
                kind=card["kind"],
                others=existing,
                pending=cards_out,
                view=view,
                resolve_card_id=resolve,
            )
            card.update(
                {
                    "x": geometry["x"],
                    "y": geometry["y"],
                    "w": geometry["w"],
                    "h": geometry["h"],
                    "placement": {
                        "reason": geometry["reason"],
                        "sizeAdjusted": geometry["sizeAdjusted"],
                    },
                }
            )
            cards_out.append(card)
            for key in _ref_keys(symbol, "card") or [preview_id]:
                ref_map[key] = preview_id
        elif kind == "group_cards":
            symbol = str(raw.get("ref") or raw.get("groupId") or "").strip()
            preview_id = symbol or ("pg_" + str(index))
            name = str(raw.get("name") or "").strip()
            group = models.new_group(
                name or models.default_group_name(len(groups) + 1),
                ordered=bool(raw.get("ordered", False)),
                default_name=not name,
            )
            group["id"] = preview_id
            group["members"] = [
                resolve(member) or str(member) for member in raw.get("cardIds") or []
            ]
            for key in _ref_keys(symbol, "group") or [preview_id]:
                ref_map[key] = preview_id
            groups.append(group)
        elif kind == "add_link":
            link = models.new_link(
                resolve(raw.get("src")) or "",
                resolve(raw.get("dst")) or "",
                direction=bool(raw.get("direction", False)),
                meaning=str(raw.get("meaning") or ""),
            )
            link["id"] = str(raw.get("linkId") or raw.get("ref") or link["id"])
            if isinstance(raw.get("ref"), str) and raw.get("ref").strip():
                for key in _ref_keys(raw.get("ref"), "card"):
                    ref_map[key] = link["id"]
            links.append(link)
    # P73 Stage 2 修复：预览组框必须由成员位置算出来（不是 models.new_group 的
    # 占位 0/0/320×240）。与真实落地共用 board.reflow_group，保证预览坐标 = 落地坐标。
    if groups and cards_out:
        from agent.interactive import board as board_model

        cards_by_id = {str(card["id"]): card for card in cards_out}
        for group in groups:
            board_model.reflow_group(group, cards_by_id)
    return {"cards": cards_out, "groups": groups, "links": links, "note": "", "refs": ref_map}


# --- 建立执行记录（幂等） ---------------------------------------------------


def _insert_execution(
    conn: sqlite3.Connection,
    *,
    execution_id: str,
    intent_id: str,
    board_id: str,
    attempt: int,
    status: str,
    plan: dict,
    steps: list[dict],
    owner_instance_id: str | None,
) -> sqlite3.Row:
    stamp = _now()
    conn.execute(
        "INSERT INTO board_executions ("
        " id, intent_id, board_id, attempt, status, plan, steps, checkpoints, result,"
        " error, owner_instance_id, created_at, updated_at, started_at, finished_at"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            execution_id,
            intent_id,
            board_id,
            int(attempt),
            status,
            models.dumps(plan),
            models.dumps(steps),
            models.dumps([]),
            models.dumps({}),
            None,
            owner_instance_id,
            stamp,
            stamp,
            stamp if status in ("running", "queued") else None,
            None,
        ),
    )
    row = get_execution(conn, execution_id)
    assert row is not None
    return row


def _next_attempt(conn: sqlite3.Connection, intent_id: str) -> int:
    row = conn.execute(
        "SELECT MAX(attempt) AS top FROM board_executions WHERE intent_id = ?", (intent_id,)
    ).fetchone()
    top = _row_value(row, "top", 0) if row is not None else 0
    try:
        return int(top or 0) + 1
    except (TypeError, ValueError):
        return 1


def ensure_execution(
    conn: sqlite3.Connection, row: sqlite3.Row, *, instance_id: str | None = None
) -> dict:
    """批准时建立一条执行记录（同一条意图已有活跃执行就复用，绝不建第二条）。

    - 计划在这里定型（操作集合 + 目标对象版本 + 预先分配的对象 id）；
    - 操作集合无效（含越权操作）时不建立执行，返回需要重新提出方案与审批；
    - 这条意图没有操作集合（历史 / 演示意图）时返回 reason="no_operations"，
      由调用方保持既有状态机（演示推进不驱动真实任务）。
    """
    intent_id = row["id"]
    board_id = row["board_id"]
    existing = active_execution(conn, intent_id)
    if existing is not None:
        return {
            "ok": True,
            "created": False,
            "execution": execution_payload(existing),
            "row": existing,
        }

    plan = build_plan(row)
    if not plan.get("ok"):
        return plan
    if not plan["ops"]:
        return {
            "ok": False,
            "reason": "no_operations",
            "detail": "这条意图没有需要执行的板面操作。",
        }

    plan["targets"] = _target_signatures(conn, board_id, plan["ops"])
    plan["plannedAt"] = _now()
    # 审批绑定：方案版本 / 材料版本 / 目标对象版本（执行前逐项重核）
    plan["approval"] = approval_binding(conn, row, plan)
    steps = [
        {
            "index": index,
            "op": op["op"],
            "status": "pending",
            "cardId": None,
            "groupId": None,
            "linkId": None,
            "detail": "",
        }
        for index, op in enumerate(plan["ops"])
    ]
    execution_id = models.new_execution_id()
    attempt = _next_attempt(conn, intent_id)
    with _transaction(conn):
        inserted = _insert_execution(
            conn,
            execution_id=execution_id,
            intent_id=intent_id,
            board_id=board_id,
            attempt=attempt,
            status="running",
            plan=plan,
            steps=steps,
            owner_instance_id=instance_id,
        )
    return {"ok": True, "created": True, "execution": execution_payload(inserted), "row": inserted}


def _set_execution(conn: sqlite3.Connection, execution_id: str, **fields: Any) -> sqlite3.Row:
    fields["updated_at"] = _now()
    assignments: list[str] = []
    values: list[Any] = []
    for key, value in fields.items():
        assignments.append(key + " = ?")
        values.append(models.dumps(value) if key in _JSON_COLUMNS else value)
    values.append(execution_id)
    conn.execute(
        "UPDATE board_executions SET " + ", ".join(assignments) + " WHERE id = ?", values
    )
    row = get_execution(conn, execution_id)
    assert row is not None
    return row


def _advance_step(
    conn: sqlite3.Connection,
    execution_id: str,
    index: int,
    *,
    status: str | None = None,
    checkpoint: dict | None = None,
    **fields: Any,
) -> None:
    """更新一步的状态，并按需追加 checkpoint（合并成一次写，短事务）。"""
    row = get_execution(conn, execution_id)
    if row is None:
        return
    steps = models.loads(row["steps"], [])
    if not isinstance(steps, list) or index >= len(steps) or not isinstance(steps[index], dict):
        return
    if status is not None:
        steps[index]["status"] = status
    for key, value in fields.items():
        steps[index][key] = value
    updates: dict[str, Any] = {"steps": steps}
    if checkpoint is not None:
        checkpoints = models.loads(row["checkpoints"], [])
        if not isinstance(checkpoints, list):
            checkpoints = []
        checkpoints.append({"index": index, "at": _now(), **checkpoint})
        updates["checkpoints"] = checkpoints[-MAX_CHECKPOINTS:]
    _set_execution(conn, execution_id, **updates)


# --- 启动（耗时工作不阻塞界面 / 服务请求） ---------------------------------


def app_owner_for(app: Any) -> Any:
    """路由层把「后台调度登记到谁的账上」解析成**唯一**口径：AppContext（ctx）。

    为什么不能直接传 FastAPI 应用对象：`AppContext.background` 才是
    `aclose()` 真正 shutdown 的那份注册表。把注册表挂在 FastAPI 应用上会另建一份，
    于是真实执行 / 互动理解的后台任务**不在关闭账上**：aclose 等不到它们、
    关闭报告里也不会有名字（契约 §2「与执行使用一致的应用归属」）。

    拿不到 ctx（老对象 / 测试替身）时退回传入对象本身：调用方仍然能自己提供一个
    持有 `background` 的宿主，行为不变。
    """
    if app is None:
        return None
    ctx = getattr(getattr(app, "state", None), "ctx", None)
    return ctx if ctx is not None else app


async def start_execution(
    conn: sqlite3.Connection,
    execution_id: str,
    *,
    adapter_provider: Any = None,
    instance_id: str | None = None,
    owner: Any = None,
) -> dict:
    """把一条已批准的执行交给后台（登记句柄），立即返回当前真实状态。

    - 有 owner（app）时用既有 services/background 注册表：同名仍在跑就复用
      （重复批准 / 重连不会重复启动），关闭流程会如实收尾；
    - 没有注册表时用 asyncio.create_task；
    - 拿不到事件循环或注册表已关闭：如实把执行标成 queued 并返回原因，不假装已经在跑。
    """
    row = get_execution(conn, execution_id)
    if row is None:
        return _fail("not_found", "找不到这条执行记录。")
    if row["status"] in models.EXECUTION_TERMINAL_STATUSES:
        return {
            "ok": True,
            "scheduled": False,
            "execution": execution_payload(row),
            "detail": "这次执行已经结束，不会重复执行。",
        }
    if row["status"] == "paused":
        return {
            "ok": True,
            "scheduled": False,
            "execution": execution_payload(row),
            "detail": "这次执行处于暂停：需要用户确认继续，不自动开始。",
        }

    def _factory():
        return run_execution(
            conn, execution_id, adapter_provider=adapter_provider, instance_id=instance_id
        )

    name = "interactive-execution:" + str(execution_id)
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        fresh = _set_execution(
            conn,
            execution_id,
            status="queued",
            error="当前没有运行中的事件循环：执行已排队，但还没有被启动。",
        )
        return {
            "ok": False,
            "scheduled": False,
            "reason": "no_loop",
            "detail": "当前没有运行中的事件循环：执行已排队，但还没有被启动。",
            "execution": execution_payload(fresh),
        }

    if owner is not None:
        from agent.services.background import BackgroundClosed, registry_for

        try:
            registry = registry_for(owner, loop=loop)
            handle = registry.register(name, _factory)
        except BackgroundClosed as exc:
            fresh = _set_execution(
                conn, execution_id, status="queued", error="服务正在关闭：" + str(exc)
            )
            return {
                "ok": False,
                "scheduled": False,
                "reason": "closed",
                "detail": "服务正在关闭：执行已排队，没有在关闭流程里偷偷跑完。",
                "execution": execution_payload(fresh),
            }
        except RuntimeError as exc:
            fresh = _set_execution(
                conn, execution_id, status="queued", error="后台登记不可用：" + str(exc)
            )
            return {
                "ok": False,
                "scheduled": False,
                "reason": "no_background",
                "detail": "后台登记不可用：" + str(exc),
                "execution": execution_payload(fresh),
            }
        fresh = get_execution(conn, execution_id)
        assert fresh is not None
        return {
            "ok": True,
            "scheduled": True,
            "handle": handle.name,
            "execution": execution_payload(fresh),
        }

    loop.create_task(_factory(), name=name)
    fresh = get_execution(conn, execution_id)
    assert fresh is not None
    return {"ok": True, "scheduled": True, "execution": execution_payload(fresh)}


# --- 归属核对：迟到结果一律丢弃 ---------------------------------------------


def _owns(
    conn: sqlite3.Connection,
    execution_id: str,
    *,
    attempt: int,
    owner: str | None,
    instance_id: str | None,
) -> dict:
    """核对 (execution id, attempt, owner_instance_id)。

    任何一项对不上（被新的一次尝试取代、换了执行者、已经暂停 / 取消 / 结束），
    这一刻的模型结果、工具结果与完成事件都不得再写回。
    """
    row = get_execution(conn, execution_id)
    if row is None:
        return _fail("not_found", "执行记录已经不存在：这一次的结果不再写回。")
    if int(row["attempt"] or 1) != int(attempt):
        return _fail("superseded", "这次执行已经被新的一次尝试取代：迟到的结果不再写回。")
    current_owner = str(row["owner_instance_id"] or "") or None
    if instance_id is not None and current_owner is not None and current_owner != instance_id:
        return _fail(
            "owner_changed",
            "这次执行已经换了执行者（可能是重启或重新确认）：迟到的结果不再写回。",
        )
    if row["status"] != "running":
        return _fail(
            str(row["status"]),
            "这次执行当前是「" + str(row["status"]) + "」：不再继续写回。",
        )
    return {"ok": True, "row": row}


def _discard(conn: sqlite3.Connection, execution_id: str, guard: dict) -> dict:
    row = get_execution(conn, execution_id)
    return {
        "ok": False,
        "reason": guard.get("reason"),
        "detail": guard.get("detail"),
        "discarded": True,
        "execution": execution_payload(row) if row is not None else None,
    }


# --- 文本生成（同一条模型链路，输出只受允许操作集合约束） --------------------


def _prompt_for(intent_row: sqlite3.Row, op: dict, index: int) -> str:
    goal = str(_row_value(intent_row, "goal", "") or "")
    expected = str(_row_value(intent_row, "expected", "") or "")
    title = str(_row_value(intent_row, "title", "") or "")
    return "\n".join(
        [
            "任务标题：" + (title or "（未命名）"),
            "任务目标：" + (goal or "（未说明）"),
            "预期结果：" + (expected or "（未说明）"),
            "需要你写的内容：这张结果卡片的正文（第 " + str(index + 1) + " 步，操作类型 " + str(op["op"]) + "）。",
            "只输出正文本身。",
        ]
    )


async def _resolve_provider(adapter_provider: Any) -> Any:
    if adapter_provider is None:
        return None
    value = adapter_provider()
    if inspect.isawaitable(value):
        return await value
    return value


async def _generate_text(
    adapter_provider: Any, *, intent_row: sqlite3.Row, op: dict, index: int
) -> tuple[str | None, str]:
    """生成结果卡片正文。没有可用模型链路时给出可读失败原因（不假装成功）。"""
    if adapter_provider is None:
        return None, (
            "没有可用的模型链路（这次执行没有拿到 adapter_provider）："
            "无法生成结果卡片正文；这一张卡片没有写入。"
        )
    try:
        adapter = await _resolve_provider(adapter_provider)
    except Exception as exc:  # noqa: BLE001 - 模型链路故障要如实说，不吞
        return None, "模型链路不可用（" + type(exc).__name__ + "）：这一张卡片没有写入。"
    if adapter is None:
        return None, (
            "没有可用的模型链路（凭据缺失或未配置）：无法生成结果卡片正文；"
            "这一张卡片没有写入。"
        )
    from agent.adapters.base import ChatMessage

    messages = [
        ChatMessage(role="system", content=_GENERATE_SYSTEM),
        ChatMessage(role="user", content=_prompt_for(intent_row, op, index)),
    ]
    try:
        completion = await adapter.complete(messages, [])
    except Exception as exc:  # noqa: BLE001 - 模型失败不写回半截内容
        return None, "模型调用失败（" + type(exc).__name__ + "）：这一张卡片没有写入，也不会自动重试。"
    text = str(getattr(getattr(completion, "message", None), "content", "") or "").strip()
    if not text:
        return None, "模型没有返回可用的正文：这一张卡片没有写入。"
    return text[:MAX_GENERATED_CHARS], ""


# --- 单步落地：只用用户同样具备的板面操作 -----------------------------------


def _live_cards(state: dict) -> dict[str, dict]:
    return {
        str(card.get("id")): card
        for card in state.get("cards") or []
        if isinstance(card, dict) and models.is_live(card)
    }


def _live_groups(state: dict) -> dict[str, dict]:
    return {
        str(group.get("id")): group
        for group in state.get("groups") or []
        if isinstance(group, dict) and not group.get("deleted")
    }


def _live_links(state: dict) -> dict[str, dict]:
    return {
        str(link.get("id")): link
        for link in state.get("links") or []
        if isinstance(link, dict) and not link.get("deleted")
    }


class _StepContext:
    """一步执行需要的身份与符号表（新对象 id 已在批准时确定）。"""

    def __init__(
        self,
        plan: dict,
        *,
        execution_id: str,
        intent_id: str,
        understanding_id: str,
        view: dict | None = None,
    ):
        self.execution_id = execution_id
        self.intent_id = intent_id
        self.understanding_id = understanding_id
        #: F6：这次任务理解时冻结的可见区域（执行落点用同一份视图）。
        self.view = view if isinstance(view, dict) else None
        self.refs: dict[str, str] = {}
        for op in plan.get("ops") or []:
            for symbol in (op.get("ref"), op.get("cardId"), op.get("groupId")):
                key = str(symbol or "").strip()
                if key and op.get("id"):
                    self.refs[key] = str(op["id"])
                    # `card:a` / `group:a` 与裸符号都解析到同一个正式对象 id。
                    if ":" not in key:
                        self.refs["card:" + key] = str(op["id"])
                        self.refs["group:" + key] = str(op["id"])

    def resolve(self, ref: Any) -> str:
        key = str(ref or "").strip()
        if not key:
            return ""
        return self.refs.get(key, key)

    def card_meta(self, *, step_index: int) -> dict:
        """结果卡片 meta：结果卡、引用与任务记录靠它互相定位（契约 §14.3）。"""
        return {
            "intentId": self.intent_id,
            "reply": {
                "intentId": self.intent_id,
                "understandingId": self.understanding_id,
                "executionId": self.execution_id,
                "stepIndex": int(step_index),
            },
        }


def _arrange_members(state: dict, group: dict, arrange: str) -> None:
    """按 row（从左到右）/ column（从上到下一列）真实写入成员 x/y（F8）。

    间距用板面既有的分组间距；起点取成员当前的左上角，不把整组挪到别的地方。
    写完由 board.normalize_state 统一更新组框。
    """
    cards = _live_cards(state)
    members = [str(cid) for cid in group.get("members") or [] if str(cid) in cards]
    if not members:
        return
    gap = float(getattr(_board(), "DEFAULT_CARD_GAP", placement.PLACEMENT_GAP))
    start_x = min(cards[cid]["x"] for cid in members)
    start_y = min(cards[cid]["y"] for cid in members)
    x, y = start_x, start_y
    for cid in members:
        card = cards[cid]
        card["x"] = x
        card["y"] = y
        card["updatedAt"] = _now()
        if arrange == "row":
            x += float(card.get("w") or 0.0) + gap
        else:
            y += float(card.get("h") or 0.0) + gap


def _frozen_view(conn: sqlite3.Connection, intent_row: sqlite3.Row) -> dict | None:
    """F6/F7：这次任务理解时冻结的可见区域（执行落点与审批预览同源）；取不到就是 None。"""
    uid = str(_row_value(intent_row, "understanding_id", "") or "")
    if not uid:
        return None
    try:
        row = conn.execute(
            "SELECT snapshot FROM board_understanding WHERE id = ?", (uid,)
        ).fetchone()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    snapshot = models.loads(_row_value(row, "snapshot", {}), {})
    if not isinstance(snapshot, dict):
        return None
    from agent.interactive import submission as submission_module  # 延迟 import：避免环

    return submission_module.normalize_view(snapshot.get("view"))


def apply_step(
    state: dict, op: dict, ctx: "_StepContext", *, index: int
) -> tuple[dict | None, dict, str | None]:
    """在当前板面上落地一步（返回新状态 / 步骤结果 / 可读失败原因）。

    只使用用户在互动模式里能做的操作：不删除、不隐藏、不勾选用户内容，
    也不改任何用户卡片的正文。新增类操作按批准时分配的 id 落地，重复执行不会产生第二份。
    """
    kind = op["op"]
    cards = _live_cards(state)
    groups = _live_groups(state)

    if kind in _ADD_OPS:
        op_id = str(op["id"])
        if op_id in cards:
            return state, {"cardId": op_id, "detail": "这一步已经按批准内容落地（不重复新增）"}, None
        card_kind = models.REPLY_KIND if kind == "add_reply_card" else "text"
        card = models.new_card(card_kind, str(op.get("content") or ""))
        card["id"] = op_id
        card["meta"] = ctx.card_meta(step_index=index)
        # F7：落点与尺寸走唯一实现（默认尺寸 / 夹取 / 冲突找空位 / 相对摆放）——
        # 与审批预览同一个函数、同一份冻结 view，所以预览坐标就是落地坐标。
        geometry = placement.plan_card_placement(
            op,
            kind=card_kind,
            others=list(cards.values()) if isinstance(cards, dict) else list(cards),
            view=ctx.view,
            resolve_card_id=ctx.resolve,
        )
        card["x"] = geometry["x"]
        card["y"] = geometry["y"]
        card["w"] = geometry["w"]
        card["h"] = geometry["h"]
        card["checked"] = False
        card["hidden"] = False
        state.setdefault("cards", []).append(card)
        state["updatedAt"] = _now()
        detail = "新增结果卡片" if kind == "add_reply_card" else "新增文字卡片"
        return _board().normalize_state(state), {"cardId": op_id, "detail": detail}, None

    if kind == "move_card":
        card_id = ctx.resolve(op.get("cardId"))
        card = cards.get(card_id)
        if card is None:
            return None, {}, "要移动的卡片已经不在板面上：这一步没有写入，也不会自动重试。"
        card["x"] = _num(op.get("x"), card["x"])
        card["y"] = _num(op.get("y"), card["y"])
        card["updatedAt"] = _now()
        state["updatedAt"] = _now()
        return _board().normalize_state(state), {"cardId": card_id, "detail": "只移动了位置"}, None

    if kind == "group_cards":
        group_id = str(op["id"])
        members: list[str] = []
        missing: list[str] = []
        for ref in op.get("cardIds") or []:
            real = ctx.resolve(ref)
            if real and real in cards and real not in members:
                members.append(real)
            else:
                missing.append(str(ref))
        if missing or not members:
            return (
                None,
                {},
                "成组引用的卡片（"
                + "、".join(missing[:3] or ["（无）"])
                + "）已经不在板面上：这一步没有写入。",
            )
        if group_id in groups:
            return state, {"groupId": group_id, "detail": "这一步已经按批准内容落地（不重复建组）"}, None
        before = set(groups)
        work = _board().create_group(state, members, str(op.get("name") or ""))
        created = [g for g in work.get("groups") or [] if str(g.get("id")) not in before]
        if not created:
            return None, {}, "成组没有按批准内容落地：这一步没有写入任何改动。"
        target = created[-1]
        target["id"] = group_id
        if op.get("ordered"):
            target["ordered"] = True
        return _board().normalize_state(work), {"groupId": group_id, "detail": "建立组并调整成员"}, None

    if kind == "order_group":
        group_id = ctx.resolve(op.get("groupId"))
        group = groups.get(group_id)
        if group is None:
            return None, {}, "要排序的组已经不在板面上：这一步没有写入。"
        present = [str(m) for m in group.get("members") or []]
        wanted: list[str] = []
        for ref in op.get("memberOrder") or []:
            real = ctx.resolve(ref)
            if real in present and real not in wanted:
                wanted.append(real)
        if not wanted:
            return None, {}, "排序引用的成员都不在这个组里：这一步没有写入。"
        rest = [m for m in present if m not in wanted]
        group["members"] = wanted + rest
        group["ordered"] = bool(op.get("ordered", True))
        group["updatedAt"] = _now()
        state["updatedAt"] = _now()
        # F8：`arrange` 才真实写入成员 x/y；没有 arrange 就是**纯逻辑排序**，
        # 文案必须如实说「只改逻辑顺序」，不得报告成已经重新排列卡片。
        arrange = str(op.get("arrange") or "").strip()
        if arrange in ("row", "column"):
            _arrange_members(state, group, arrange)
            detail = (
                "调整组内成员顺序，并按"
                + ("从左到右" if arrange == "row" else "从上到下一列")
                + "真实重排了卡片位置"
            )
        else:
            detail = "只调整组内成员顺序（逻辑顺序），没有移动任何卡片"
        return (
            _board().normalize_state(state),
            {"groupId": group_id, "detail": detail},
            None,
        )

    if kind == "add_link":
        link_id = str(op["id"])
        links = _live_links(state)
        if link_id in links:
            return state, {"linkId": link_id, "detail": "这一步已经按批准内容落地（不重复建关系）"}, None
        src = ctx.resolve(op.get("src"))
        dst = ctx.resolve(op.get("dst"))
        if src not in cards or dst not in cards or src == dst:
            return None, {}, "关系端点已经不在板面上：这一步没有写入。"
        for existing in links.values():
            if {str(existing.get("src")), str(existing.get("dst"))} == {src, dst}:
                return (
                    state,
                    {
                        "linkId": str(existing.get("id")),
                        "detail": "同一对端点已经有关系，未改动已有关系",
                    },
                    None,
                )
        link = models.new_link(
            src,
            dst,
            direction=bool(op.get("direction", False)),
            meaning=str(op.get("meaning") or ""),
        )
        link["id"] = link_id
        state.setdefault("links", []).append(link)
        state["updatedAt"] = _now()
        return _board().normalize_state(state), {"linkId": link_id, "detail": "建立关系"}, None

    return None, {}, "不认识的操作「" + str(kind) + "」：这一步没有写入。"


# --- 结果、撤回与收尾 -------------------------------------------------------


def _object_signature(intents: Any, state: dict, step_fields: dict) -> str:
    """这一步刚落地对象的语义指纹（按**刚落地的板面**算；撤回保护靠它）。"""
    if step_fields.get("cardId"):
        for card in state.get("cards") or []:
            if str(card.get("id")) == str(step_fields["cardId"]):
                return intents._card_signature(card)
        return ""
    if step_fields.get("groupId"):
        for group in state.get("groups") or []:
            if str(group.get("id")) == str(step_fields["groupId"]):
                return intents._group_signature(group)
        return ""
    if step_fields.get("linkId"):
        for link in state.get("links") or []:
            if str(link.get("id")) == str(step_fields["linkId"]):
                return intents._link_signature(link)
        return ""
    return ""


def _partial_applied(conn: sqlite3.Connection, intent_row: sqlite3.Row) -> dict | None:
    """这条意图**目前已经造成**的改动（失败 / 取消时的撤回保护依据）。

    只看真实执行记录里已经完成的步骤：每一步都带着落地那一刻的语义指纹。
    """
    row = latest_execution(conn, intent_row["id"])
    if row is None:
        return None
    plan = models.loads(_row_value(row, "plan", {}), {})
    steps = models.loads(_row_value(row, "steps", []), [])
    if not isinstance(plan, dict) or not isinstance(steps, list):
        return None
    applied = _collect_applied(conn, board_id=intent_row["board_id"], plan=plan, steps=steps)
    if applied["cardIds"] or applied["groupIds"] or applied["linkIds"]:
        return applied
    return None


def _collect_applied(
    conn: sqlite3.Connection, *, board_id: str, plan: dict, steps: list[dict]
) -> dict:
    """按当前板面重算这次执行造成的结果（保存语义指纹，供撤回保护使用）。"""
    intents = _intents()
    state = _store().load_board(conn, board_id)["state"]
    cards = {str(c.get("id")): c for c in state.get("cards") or []}
    groups = {str(g.get("id")): g for g in state.get("groups") or []}
    links = {str(l.get("id")): l for l in state.get("links") or []}
    card_ids: list[str] = []
    group_ids: list[str] = []
    link_ids: list[str] = []
    for step in steps:
        if step.get("status") != "done":
            continue
        if step.get("cardId") and str(step["cardId"]) not in card_ids:
            card_ids.append(str(step["cardId"]))
        if step.get("groupId") and str(step["groupId"]) not in group_ids:
            group_ids.append(str(step["groupId"]))
        if step.get("linkId") and str(step["linkId"]) not in link_ids:
            link_ids.append(str(step["linkId"]))
    # 语义指纹以**落地那一刻**的记录为准（step.signature）：用户之后改过它时，
    # 撤回保护才能识别出「这不是我们留下的那一份」，从而保留用户的修改。
    signatures: dict[str, str] = {}
    for step in steps:
        if not isinstance(step, dict) or step.get("status") != "done":
            continue
        recorded = str(step.get("signature") or "")
        if not recorded:
            continue
        for key in (step.get("cardId"), step.get("groupId"), step.get("linkId")):
            if key:
                signatures[str(key)] = recorded
    for card_id in card_ids:
        if card_id in cards and card_id not in signatures:
            signatures[card_id] = intents._card_signature(cards[card_id])
    for group_id in group_ids:
        if group_id in groups and group_id not in signatures:
            signatures[group_id] = intents._group_signature(groups[group_id])
    for link_id in link_ids:
        if link_id in links and link_id not in signatures:
            signatures[link_id] = intents._link_signature(links[link_id])
    ref_map = {
        str(op.get("ref")): str(op.get("id"))
        for op in plan.get("ops") or []
        if op.get("ref") and op.get("id")
    }
    return {
        "cardIds": [cid for cid in card_ids if cid in cards and not cards[cid].get("deleted")],
        "groupIds": [gid for gid in group_ids if gid in groups and not groups[gid].get("deleted")],
        "linkIds": [lid for lid in link_ids if lid in links and not links[lid].get("deleted")],
        "signatures": signatures,
        "previewIdMap": ref_map,
        "appliedAt": _now(),
    }


def _finish_intent(conn: sqlite3.Connection, intent_row: sqlite3.Row, *, applied: dict) -> None:
    intents = _intents()
    total = max(1, len(models.loads(_row_value(intent_row, "operations", []), []) or []))
    intents._update(
        conn,
        intent_row["id"],
        status="done",
        applied=applied,
        reason="已完成：批准的内容已经成为正式板面内容。",
        progress=intents._stored_progress(
            intent_row, text="已完成", done=total, total=total, drop_owner=True
        ),
    )
    intents._refresh_dependency_states(conn, intent_row["board_id"])


def _revert_locked(conn: sqlite3.Connection, intent_row: sqlite3.Row, outcome: str) -> dict:
    """失败 / 取消的撤回（复用 intents 既有机制：保留用户后续修改 + 待确认项）。"""
    intents = _intents()
    fresh = intents._get_row(conn, intent_row["id"])
    assert fresh is not None
    return intents._revert_applied(conn, fresh, outcome)


def _settle_failed(
    conn: sqlite3.Connection,
    *,
    intent_row: sqlite3.Row,
    execution_id: str,
    detail: str,
    reason: str = "failed",
) -> dict:
    intents = _intents()
    # 先如实记下这次任务**已经造成**的改动，撤回保护才有依据
    # （否则失败撤回会因为 applied 为空而什么都不撤）。
    partial = _partial_applied(conn, intent_row)
    if partial is not None:
        intents._update(conn, intent_row["id"], applied=partial)
    report = _revert_locked(conn, intent_row, "failed")
    with _transaction(conn):
        row = _set_execution(
            conn,
            execution_id,
            status="failed",
            error=detail,
            owner_instance_id=None,
            finished_at=_now(),
        )
        fresh = intents._get_row(conn, intent_row["id"])
        assert fresh is not None
        intents._update(
            conn,
            fresh["id"],
            status="failed",
            revert=report,
            reason="执行失败：" + detail + report["reasonText"] + "不会自动重试。",
            progress=intents._stored_progress(fresh, text="失败：已撤回本任务造成的改动"),
        )
        intents._refresh_dependency_states(conn, fresh["board_id"])
    return {
        "ok": False,
        "reason": reason,
        "detail": detail,
        "execution": execution_payload(row),
        "revert": report,
    }


def _pause_for(
    conn: sqlite3.Connection,
    *,
    intent_row: sqlite3.Row,
    execution_id: str,
    detail: str,
    reason: str,
) -> dict:
    intents = _intents()
    with _transaction(conn):
        row = _set_execution(
            conn, execution_id, status="paused", error=detail, owner_instance_id=None
        )
        fresh = intents._get_row(conn, intent_row["id"])
        assert fresh is not None
        intents._update(
            conn,
            fresh["id"],
            status="paused",
            reason=detail,
            progress=intents._stored_progress(fresh, text="已暂停：等待你确认继续"),
        )
    return {
        "ok": False,
        "reason": reason,
        "detail": detail,
        "execution": execution_payload(row),
        "paused": True,
    }


def _needs_new_plan(
    conn: sqlite3.Connection, *, intent_row: sqlite3.Row, execution_id: str, detail: str
) -> dict:
    """批准的操作集合已经不合法（越权 / 形状变了）：退回待更新，不硬做。"""
    intents = _intents()
    with _transaction(conn):
        row = _set_execution(
            conn,
            execution_id,
            status="failed",
            error=detail,
            owner_instance_id=None,
            finished_at=_now(),
        )
        intents._update(
            conn,
            intent_row["id"],
            status="needs_update",
            reason=detail,
            progress=intents._stored_progress(intent_row, text="需要重新提出方案与审批"),
        )
    return {
        "ok": False,
        "reason": "needs_new_plan",
        "detail": detail,
        "requiresUpdate": True,
        "execution": execution_payload(row),
    }


def _discard_intent_missing(conn: sqlite3.Connection, execution_id: str) -> dict:
    row = _set_execution(
        conn,
        execution_id,
        status="failed",
        error="任务记录已经不存在：这次执行没有继续。",
        owner_instance_id=None,
        finished_at=_now(),
    )
    return {
        "ok": False,
        "reason": "intent_missing",
        "detail": "任务记录已经不存在：这次执行没有继续。",
        "execution": execution_payload(row),
    }


def _stale_step_targets(
    intents: Any, state: dict, plan: dict, op: dict, ctx: "_StepContext"
) -> list[str]:
    """写回前只核对这一步要碰的既有对象（对象版本保护）。"""
    targets = plan.get("targets") or {}
    card_targets = targets.get("cards") or {}
    group_targets = targets.get("groups") or {}
    cards = {str(c.get("id")): c for c in state.get("cards") or []}
    groups = {str(g.get("id")): g for g in state.get("groups") or []}
    kind = op["op"]
    refs: list[str] = []
    if kind == "move_card":
        refs = [ctx.resolve(op.get("cardId"))]
    elif kind == "group_cards":
        refs = [ctx.resolve(x) for x in op.get("cardIds") or []]
    elif kind == "add_link":
        refs = [ctx.resolve(op.get("src")), ctx.resolve(op.get("dst"))]
    elif kind == "order_group":
        refs = [ctx.resolve(x) for x in op.get("memberOrder") or []]
    stale: list[str] = []
    for ref in refs:
        signature = card_targets.get(str(ref))
        if signature is None:
            continue
        card = cards.get(str(ref))
        if card is None or card.get("deleted") or intents._card_signature(card) != signature:
            stale.append(str(ref))
    if kind == "order_group":
        group_ref = ctx.resolve(op.get("groupId"))
        signature = group_targets.get(str(group_ref))
        if signature is not None:
            group = groups.get(str(group_ref))
            if group is None or group.get("deleted") or intents._group_signature(group) != signature:
                stale.append(str(group_ref))
    return stale


# --- 真实执行主循环 ---------------------------------------------------------


async def run_execution(
    conn: sqlite3.Connection,
    execution_id: str,
    *,
    adapter_provider: Any = None,
    instance_id: str | None = None,
) -> dict:
    """真实执行一条已批准的任务：逐步落地、逐步 checkpoint，支持暂停 / 取消 / 恢复。

    - 不自动重试；每一步之前都重核任务状态、审批有效性、材料版本与对象版本；
    - 模型调用只发生在「已批准的结果卡片缺少正文」时，输出只作为那张卡片的正文；
    - 任何 await 之后都重新核对归属：owner / attempt 变了就丢弃，不写回。
    """
    intents = _intents()
    row = get_execution(conn, execution_id)
    if row is None:
        return _fail("not_found", "找不到这条执行记录。")
    attempt = int(row["attempt"] or 1)
    owner = str(row["owner_instance_id"] or "") or None

    if row["status"] in models.EXECUTION_TERMINAL_STATUSES:
        return {
            "ok": True,
            "discarded": True,
            "execution": execution_payload(row),
            "detail": "这次执行已经结束，不会重复执行。",
        }
    if row["status"] == "paused":
        return {
            "ok": True,
            "discarded": True,
            "execution": execution_payload(row),
            "detail": "这次执行处于暂停：需要用户确认继续，不自动开始。",
        }

    guard = _owns(conn, execution_id, attempt=attempt, owner=owner, instance_id=instance_id)
    if not guard["ok"]:
        return _discard(conn, execution_id, guard)

    if row["status"] == "queued":
        row = _set_execution(conn, execution_id, status="running", started_at=_now())

    intent_row = intents._get_row(conn, row["intent_id"])
    if intent_row is None:
        return _discard_intent_missing(conn, execution_id)

    # 任务状态：暂停 / 取消 / 结束都不再继续（暂停与取消作用于真实执行）
    if intent_row["status"] != "running":
        return {
            "ok": False,
            "discarded": True,
            "reason": "intent_not_running",
            "detail": (
                "任务当前是「"
                + str(intent_row["status"])
                + "」：这次执行不再继续写回（暂停 / 取消后，迟到的结果不会落地）。"
            ),
            "execution": execution_payload(get_execution(conn, execution_id)),
        }

    plan = models.loads(_row_value(row, "plan", {}), {})
    if not isinstance(plan, dict) or not plan.get("ops"):
        return _settle_failed(
            conn,
            intent_row=intent_row,
            execution_id=execution_id,
            detail="这次执行没有可执行的操作计划：没有写入任何改动。",
            reason="no_plan",
        )

    # 审批有效性：批准的操作集合必须仍然只包含允许的操作
    ops = plan["ops"]
    forbidden = [
        str(op.get("op") or "")
        for op in ops
        if not isinstance(op, dict) or op.get("op") not in ALLOWED_OPS
    ]
    if forbidden:
        return _needs_new_plan(
            conn,
            intent_row=intent_row,
            execution_id=execution_id,
            detail=(
                "批准的操作集合里出现了当前不允许的操作（"
                + "、".join(forbidden[:3])
                + "）：QIO 只能做用户在互动模式里能做的操作。"
                "需要重新提出方案与审批；本次没有写入任何改动。"
            ),
        )

    # 方案版本：批准之后操作集合被改过 → 退回「需要重新提出方案与审批」
    binding = plan.get("approval") if isinstance(plan.get("approval"), dict) else None
    if binding and binding.get("operationsHash"):
        # 与批准时记下的方案版本对账：这里比的是**意图行现在重新校验出来的操作集合**
        # （与批准时用的是同一套归一化，避免把「缺少可选字段」误判成方案变化）
        current_plan = build_plan(intent_row)
        current_hash = _signature(
            _binding_payload(current_plan.get("ops") or [] if current_plan.get("ok") else [])
        )
        if current_hash != binding.get("operationsHash"):
            return _needs_new_plan(
                conn,
                intent_row=intent_row,
                execution_id=execution_id,
                detail=(
                    "批准的操作集合在开始执行之前发生了变化（方案版本对不上）："
                    "需要重新提出方案与审批；本次没有写入任何改动。"
                ),
            )

    # 材料版本：批准之后材料变了就停下来（保留进度，等用户确认）
    watch = intents._material_watch(intent_row)
    if not watch and binding:
        # 批准时记录的材料版本（意图行的私有进度键）缺失时按执行记录里的绑定回退
        bound = binding.get("materials")
        watch = {str(k): str(v) for k, v in bound.items()} if isinstance(bound, dict) else {}
    state = _store().load_board(conn, intent_row["board_id"])["state"]
    if watch:
        current = intents._material_signatures(state, list(watch))
        changed = [cid for cid, sig in watch.items() if sig != current.get(cid)]
        if changed:
            labels = "、".join(intents._material_label(state, cid) for cid in changed[:3])
            return _pause_for(
                conn,
                intent_row=intent_row,
                execution_id=execution_id,
                reason="materials_changed",
                detail=(
                    "这项任务依赖的材料在批准之后发生了变化（"
                    + labels
                    + "）：已经停下并保留进度，需要你确认后才会继续；不会自动重试。"
                ),
            )

    # 对象版本：要改动的既有对象被用户改过就不按旧快照写回
    changed_objects = stale_targets(conn, intent_row["board_id"], plan)
    if changed_objects:
        labels = "、".join(intents._material_label(state, cid) for cid in changed_objects[:3])
        return _pause_for(
            conn,
            intent_row=intent_row,
            execution_id=execution_id,
            reason="objects_changed",
            detail=(
                "这项任务要改动的对象已经被你改过（"
                + labels
                + "）：没有按旧快照覆盖你的修改，任务已暂停；需要你确认后才会继续。"
            ),
        )

    ctx = _StepContext(
        plan,
        execution_id=execution_id,
        intent_id=intent_row["id"],
        understanding_id=str(_row_value(intent_row, "understanding_id", "") or ""),
        # F7：落点用理解时冻结的同一份 view（没有就是 None，不编造）。
        view=_frozen_view(conn, intent_row),
    )
    steps = models.loads(_row_value(row, "steps", []), [])
    if not isinstance(steps, list):
        steps = []

    for index in range(len(ops)):
        step = steps[index] if index < len(steps) and isinstance(steps[index], dict) else {}
        status = str(step.get("status") or "pending")
        if status in ("done", "skipped"):
            # 恢复不重复已完成步骤
            continue
        if status == "failed":
            return _settle_failed(
                conn,
                intent_row=intent_row,
                execution_id=execution_id,
                detail=str(step.get("detail") or "这一步之前已经失败：不会自动重试。"),
            )

        guard = _owns(conn, execution_id, attempt=attempt, owner=owner, instance_id=instance_id)
        if not guard["ok"]:
            return _discard(conn, execution_id, guard)

        op = ops[index]
        with _transaction(conn):
            _advance_step(
                conn,
                execution_id,
                index,
                status="running",
                detail="正在执行",
                checkpoint={"phase": "before", "op": op["op"]},
            )

        # 需要模型写正文时走同一条模型链路；await 之后必须重新核对归属
        if op["op"] in _ADD_OPS and not str(op.get("content") or "").strip():
            text, reason = await _generate_text(
                adapter_provider, intent_row=intent_row, op=op, index=index
            )
            guard = _owns(conn, execution_id, attempt=attempt, owner=owner, instance_id=instance_id)
            if not guard["ok"]:
                return _discard(conn, execution_id, guard)
            if text is None:
                with _transaction(conn):
                    _advance_step(
                        conn,
                        execution_id,
                        index,
                        status="failed",
                        detail=reason,
                        checkpoint={"phase": "failed", "op": op["op"], "reason": reason},
                    )
                return _settle_failed(
                    conn,
                    intent_row=intent_row,
                    execution_id=execution_id,
                    detail=reason,
                    reason="no_adapter" if adapter_provider is None else "generate_failed",
                )
            op = {**op, "content": text, "generated": True}
            ops[index] = op

        guard = _owns(conn, execution_id, attempt=attempt, owner=owner, instance_id=instance_id)
        if not guard["ok"]:
            return _discard(conn, execution_id, guard)

        # 写回前重核（一）：任务状态。任何路径把任务暂停 / 取消之后，这一步都不再写回。
        current_intent = intents._get_row(conn, intent_row["id"])
        if current_intent is None or current_intent["status"] != "running":
            status_text = "已不存在" if current_intent is None else str(current_intent["status"])
            return _discard(
                conn,
                execution_id,
                {
                    "reason": "intent_not_running",
                    "detail": "任务当前是「" + status_text + "」：这一步不再写回。",
                },
            )

        # 写回前重核（二）：按当前板面重算，不照抄旧快照
        state = _store().load_board(conn, intent_row["board_id"])["state"]
        still_stale = _stale_step_targets(intents, state, plan, op, ctx)
        if still_stale:
            labels = "、".join(intents._material_label(state, cid) for cid in still_stale[:3])
            return _pause_for(
                conn,
                intent_row=intent_row,
                execution_id=execution_id,
                reason="objects_changed",
                detail="写回前发现对象已经被改过（" + labels + "）：没有覆盖你的修改，任务已暂停。",
            )

        new_state, step_fields, error = apply_step(state, op, ctx, index=index)
        if error is not None:
            with _transaction(conn):
                _advance_step(
                    conn,
                    execution_id,
                    index,
                    status="failed",
                    detail=error,
                    checkpoint={"phase": "failed", "op": op["op"], "reason": error},
                )
            return _settle_failed(
                conn,
                intent_row=intent_row,
                execution_id=execution_id,
                detail=error,
                reason="step_failed",
            )
        assert new_state is not None
        with _transaction(conn):
            _store().save_board(
                conn,
                intent_row["board_id"],
                new_state,
                reason="execution:" + str(execution_id) + ":" + str(index),
            )
            landed = _store().load_board(conn, intent_row["board_id"])["state"]
            _advance_step(
                conn,
                execution_id,
                index,
                status="done",
                detail=str(step_fields.get("detail") or ""),
                cardId=step_fields.get("cardId"),
                groupId=step_fields.get("groupId"),
                linkId=step_fields.get("linkId"),
                signature=_object_signature(intents, landed, step_fields),
                checkpoint={"phase": "done", "op": op["op"], **step_fields},
            )

    # 收尾：按当前板面重算这次执行的结果，写回意图（结果卡、引用与任务记录互相定位）
    fresh_row = get_execution(conn, execution_id)
    assert fresh_row is not None
    final_steps = models.loads(_row_value(fresh_row, "steps", []), [])
    if not isinstance(final_steps, list):
        final_steps = []
    applied = _collect_applied(
        conn, board_id=intent_row["board_id"], plan=plan, steps=final_steps
    )
    with _transaction(conn):
        _finish_intent(conn, intent_row, applied=applied)
        row = _set_execution(
            conn,
            execution_id,
            status="done",
            result={
                "cardIds": applied["cardIds"],
                "groupIds": applied["groupIds"],
                "linkIds": applied["linkIds"],
            },
            error=None,
            owner_instance_id=None,
            finished_at=_now(),
        )
    return {
        "ok": True,
        "execution": execution_payload(row),
        "applied": applied,
        "detail": "已完成：批准的内容已经按当前板面落地。",
    }


# --- 仓库层：暂停 / 取消 / 恢复（状态与 execution 同步） ---------------------


def pause_intent(
    conn: sqlite3.Connection, intent_id: str, *, instance_id: str | None = None
) -> dict:
    """暂停：作用于真实执行（execution 与意图同步暂停，保留进度与已完成步骤）。"""
    intents = _intents()
    row = intents._get_row(conn, intent_id)
    if row is None:
        return _fail("not_found", "找不到这项意图。")
    execution = active_execution(conn, intent_id)
    payload = execution_payload(execution) if execution is not None else None
    if row["status"] == "paused":
        return {
            "ok": True,
            "intent": intents._intent_payload(row, {}, execution=payload),
            "execution": payload,
            "reason": "already_paused",
            "detail": "这项任务已经处于暂停。",
        }
    if row["status"] != "running":
        return {
            "ok": False,
            "reason": "not_running",
            "detail": "只有执行中的任务可以暂停；这项任务当前是「" + str(row["status"]) + "」。",
            "intent": intents._intent_payload(row, {}, execution=payload),
            "execution": payload,
        }
    text = "已暂停并保留进度：重新打开不会自动继续，需要你确认。"
    with _transaction(conn):
        if execution is not None:
            execution = _set_execution(
                conn, execution["id"], status="paused", owner_instance_id=None
            )
            payload = execution_payload(execution)
        intents._update(
            conn,
            row["id"],
            status="paused",
            reason=text,
            progress=intents._stored_progress(row, text="已暂停：等待你确认继续", drop_owner=True),
        )
    fresh = intents._get_row(conn, intent_id)
    assert fresh is not None
    return {
        "ok": True,
        "reason": "paused",
        "detail": text,
        "intent": intents._intent_payload(fresh, {}, execution=payload),
        "execution": payload,
    }


def cancel_intent(
    conn: sqlite3.Connection, intent_id: str, *, instance_id: str | None = None
) -> dict:
    """取消：作用于真实执行，并撤回本任务造成且仍可安全撤回的改动。"""
    intents = _intents()
    row = intents._get_row(conn, intent_id)
    if row is None:
        return _fail("not_found", "找不到这项意图。")
    execution = active_execution(conn, intent_id)
    if row["status"] not in (
        "running",
        "paused",
        "waiting_dependency",
        "waiting_confirm",
        "pending",
    ):
        payload = execution_payload(execution) if execution is not None else None
        return {
            "ok": False,
            "reason": "not_cancellable",
            "detail": "这项任务已经结束，不能再取消。",
            "intent": intents._intent_payload(row, {}, execution=payload),
            "execution": payload,
        }
    partial = _partial_applied(conn, row)
    if partial is not None:
        intents._update(conn, row["id"], applied=partial)
    report = _revert_locked(conn, row, "cancelled")
    text = "已取消。" + report["reasonText"] + "不会自动重试。"
    with _transaction(conn):
        if execution is not None:
            execution = _set_execution(
                conn,
                execution["id"],
                status="cancelled",
                error="用户取消",
                owner_instance_id=None,
                finished_at=_now(),
            )
        intents._update(
            conn,
            row["id"],
            status="cancelled",
            revert=report,
            reason=text,
            progress=intents._stored_progress(row, text="已取消", drop_owner=True),
        )
        intents._refresh_dependency_states(conn, row["board_id"])
    payload = execution_payload(execution) if execution is not None else None
    fresh = intents._get_row(conn, intent_id)
    assert fresh is not None
    return {
        "ok": True,
        "reason": "cancelled",
        "detail": report["reasonText"],
        "revert": report,
        "intent": intents._intent_payload(fresh, {}, execution=payload),
        "execution": payload,
    }


def resume_intent(
    conn: sqlite3.Connection, intent_id: str, *, instance_id: str | None = None
) -> dict:
    """继续：校验当前材料与已完成步骤，不重复已完成操作、不自动重试。

    - 暂停期间材料变过就按当前材料刷新依据并说明；
    - 复用同一条执行记录（保留已完成步骤），但把 attempt 前进一步：旧一次尝试的
      迟到结果会因为 attempt 对不上被丢弃；
    - 被中断（running）的步骤交回 pending；落地时按批准时分配的 id 对账，
      已经落地的对象会被采纳而不是重复新增。
    """
    intents = _intents()
    row = intents._get_row(conn, intent_id)
    if row is None:
        return _fail("not_found", "找不到这项意图。")
    if row["status"] != "paused":
        execution = active_execution(conn, intent_id)
        payload = execution_payload(execution) if execution is not None else None
        return {
            "ok": False,
            "reason": "not_paused",
            "detail": "只有暂停中的任务可以继续；这项任务当前是「" + str(row["status"]) + "」。",
            "intent": intents._intent_payload(row, {}, execution=payload),
            "execution": payload,
        }

    resumed = intents._resume_paused_intent(conn, row, instance_id=instance_id, rows_by_id={})
    execution = active_execution(conn, intent_id)
    payload = execution_payload(execution) if execution is not None else None
    if execution is not None:
        steps = models.loads(_row_value(execution, "steps", []), [])
        if isinstance(steps, list):
            for step in steps:
                if isinstance(step, dict) and step.get("status") == "running":
                    step["status"] = "pending"
                    step["detail"] = "上次被中断：这次按已落地的对象对账后继续"
        attempt = int(execution["attempt"] or 1) + 1
        execution = _set_execution(
            conn,
            execution["id"],
            attempt=attempt,
            status="running",
            steps=steps if isinstance(steps, list) else [],
            error=None,
            owner_instance_id=instance_id,
            finished_at=None,
        )
        payload = execution_payload(execution)
    fresh = intents._get_row(conn, intent_id)
    assert fresh is not None
    return {
        **resumed,
        "intent": intents._intent_payload(fresh, {}, execution=payload),
        "execution": payload,
    }


# --- P73：计划预演与同步原子应用 -------------------------------------------


def preview_for_intent(
    conn: sqlite3.Connection, row: sqlite3.Row, plan: dict | None = None
) -> dict:
    """按意图的操作计划派生板面虚线预演（与真实落地共用 placement）。

    只读：不改板面、不写库。计划无效时如实返回原因，由调用方决定是否落库。
    """
    plan = plan if isinstance(plan, dict) else build_plan(row)
    if not plan.get("ok"):
        return {
            "ok": False,
            "reason": str(plan.get("reason") or "invalid_plan"),
            "detail": str(plan.get("detail") or ""),
        }
    try:
        loaded = _store().load_board(conn, str(row["board_id"]))
        cards = loaded.get("state", {}).get("cards") or []
    except Exception:  # noqa: BLE001 - 预演失败不阻塞提案 / 编辑
        cards = []
    preview = preview_from_operations(
        plan.get("ops") or [], cards=cards, view=_frozen_view(conn, row)
    )
    preview.pop("refs", None)
    return {"ok": True, "preview": preview}


def apply_ops_to_state(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    plan: dict,
    *,
    execution_id: str,
    state: dict | None = None,
) -> dict:
    """在内存里按计划落地全部操作：不写库、不调用模型。

    调用方传入事务里读到的 state 时，全部步骤与 CAS 共用同一份状态；
    新对象 id 已在 build_plan 时分配，重复执行不会产生第二份。
    """
    base_state = (
        state
        if isinstance(state, dict)
        else _store().load_board(conn, str(row["board_id"]))["state"]
    )
    ctx = _StepContext(
        plan,
        execution_id=execution_id,
        intent_id=str(row["id"]),
        understanding_id=str(_row_value(row, "understanding_id", "") or ""),
        view=None,
    )
    work = copy.deepcopy(base_state)
    steps: list[dict] = []
    applied: dict[str, list[str]] = {"cardIds": [], "groupIds": [], "linkIds": []}
    mapping: dict[str, str] = {}
    for index, op in enumerate(plan.get("ops") or []):
        new_state, fields, error = apply_step(work, op, ctx, index=index)
        if error is not None:
            return {
                "ok": False,
                "reason": "step_failed",
                "detail": error,
                "index": index,
            }
        assert new_state is not None
        work = new_state
        fields = fields or {}
        steps.append(
            {
                "index": index,
                "op": str(op.get("op") or ""),
                "status": "done",
                "detail": str(fields.get("detail") or ""),
                "cardId": fields.get("cardId"),
                "groupId": fields.get("groupId"),
                "linkId": fields.get("linkId"),
            }
        )
        for key, bucket in (
            ("cardId", "cardIds"),
            ("groupId", "groupIds"),
            ("linkId", "linkIds"),
        ):
            value = fields.get(key)
            if value:
                applied[bucket].append(str(value))
        symbol = str(op.get("ref") or "").strip()
        real = fields.get("cardId") or fields.get("groupId") or fields.get("linkId")
        if symbol and real:
            mapping[symbol] = str(real)
    return {
        "ok": True,
        "state": work,
        "steps": steps,
        "applied": applied,
        "previewIdMap": mapping,
    }


__all__ = [
    "ALLOWED_OPS",
    "active_execution",
    "apply_ops_to_state",
    "build_plan",
    "cancel_intent",
    "ensure_execution",
    "execution_payload",
    "get_execution",
    "latest_execution",
    "list_executions",
    "pause_intent",
    "preview_for_intent",
    "preview_from_operations",
    "resume_intent",
    "run_execution",
    "stale_targets",
    "start_execution",
]


