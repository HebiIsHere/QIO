"""维护记录的持久化层（B）：批次 / 事项 / 操作，以及维护输入读取。

只做 SQL 与 JSON 序列化，不调用模型、不做业务判断（判断在 actions / questions /
recovery）。所有状态字符串都来自 agent.maintenance.models，不另造状态名。

历史必须完整：批次与操作只追加、不回写知识正文；恢复也新增记录。
"""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

from agent.maintenance import models

logger = logging.getLogger(__name__)

#: 维护事项的 kind（契约 §12.4；迁移 47 的 CHECK 与这里一致）。
ITEM_MERGE = "merge"
ITEM_NORMALIZE = "normalize"
ITEM_ASK = "ask"
ITEM_RESOLUTION = "resolution"
ITEM_RESTORE = "restore"
ITEM_KINDS: tuple[str, ...] = (
    ITEM_MERGE,
    ITEM_NORMALIZE,
    ITEM_ASK,
    ITEM_RESOLUTION,
    ITEM_RESTORE,
)

#: A 的变化日志表（迁移 46；B 只读，不推进处理位置）。
CHANGE_LOG_TABLE = "knowledge_change_log"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id(prefix: str) -> str:
    return f"{prefix}{uuid.uuid4().hex[:12]}"


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _load_json(text: Any, default: Any) -> Any:
    if text is None or text == "":
        return default
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return default
    if isinstance(default, list) and not isinstance(data, list):
        return default
    if isinstance(default, dict) and not isinstance(data, dict):
        return default
    return data


def knowledge_snapshot(item: Any) -> dict[str, Any]:
    """一条知识版本的可回放快照（恢复 / 前后对比共用；不含任何密钥）。"""
    provenance = dict(getattr(item, "provenance", None) or {})
    state = getattr(item, "state", "")
    return {
        "id": str(getattr(item, "id", "")),
        "version": int(getattr(item, "version", 1) or 1),
        "chain_id": str(getattr(item, "chain_id", "") or ""),
        "content": str(getattr(item, "content", "") or ""),
        "category": str(getattr(item, "category", "") or ""),
        "node_ids": [str(node) for node in (getattr(item, "node_ids", None) or [])],
        "entity_ids": [str(entity) for entity in (getattr(item, "entity_ids", None) or [])],
        "provenance": provenance,
        "confidence": getattr(item, "confidence", None),
        "export": bool(getattr(item, "export", False)),
        "state": getattr(state, "value", state),
        "source": provenance.get("source"),
        "epistemic_status": provenance.get("epistemic_status"),
        "evidence_ids": list(provenance.get("evidence_ids") or []),
    }


# ---------------------------------------------------------------------------
# 批次
# ---------------------------------------------------------------------------


def create_batch(
    conn: sqlite3.Connection,
    *,
    trigger_reason: str,
    snapshot: Mapping[str, Any] | None,
    model: str | None,
    status: str = models.BatchStatus.RUNNING.value,
) -> str:
    batch_id = new_id("mb_")
    conn.execute(
        "INSERT INTO maintenance_batches"
        " (id, trigger_reason, snapshot, status, model, created_at, finished_at, stats, error)"
        " VALUES (?, ?, ?, ?, ?, ?, NULL, '{}', NULL)",
        (batch_id, str(trigger_reason), _json(dict(snapshot or {})), str(status), model, _now()),
    )
    return batch_id


def finish_batch(
    conn: sqlite3.Connection,
    batch_id: str,
    *,
    status: str,
    stats: Mapping[str, Any] | None = None,
    error: str | None = None,
) -> None:
    conn.execute(
        "UPDATE maintenance_batches SET status = ?, finished_at = ?, stats = ?, error = ?"
        " WHERE id = ?",
        (
            str(status),
            _now(),
            _json(dict(stats or {})),
            error,
            batch_id,
        ),
    )


def set_batch_model(conn: sqlite3.Connection, batch_id: str, model: str | None) -> None:
    conn.execute(
        "UPDATE maintenance_batches SET model = ? WHERE id = ?", (model, batch_id)
    )


def update_batch_stats(
    conn: sqlite3.Connection, batch_id: str, patch: Mapping[str, Any]
) -> None:
    row = conn.execute(
        "SELECT stats FROM maintenance_batches WHERE id = ?", (batch_id,)
    ).fetchone()
    if row is None:
        return
    stats = _load_json(row["stats"], {})
    stats.update(dict(patch))
    conn.execute(
        "UPDATE maintenance_batches SET stats = ? WHERE id = ?", (_json(stats), batch_id)
    )


def _batch_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "trigger_reason": str(row["trigger_reason"]),
        "snapshot": _load_json(row["snapshot"], {}),
        "status": str(row["status"]),
        "model": row["model"],
        "created_at": str(row["created_at"]),
        "finished_at": row["finished_at"],
        "stats": _load_json(row["stats"], {}),
        "error": row["error"],
    }


def get_batch(conn: sqlite3.Connection, batch_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM maintenance_batches WHERE id = ?", (batch_id,)
    ).fetchone()
    return _batch_row(row) if row is not None else None


def list_batches(
    conn: sqlite3.Connection, *, limit: int = 20, offset: int = 0
) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM maintenance_batches ORDER BY created_at DESC, id LIMIT ? OFFSET ?",
        (int(limit), int(offset)),
    ).fetchall()
    return [_batch_row(row) for row in rows]


# ---------------------------------------------------------------------------
# 事项（候选 / 独立问题 / 恢复）
# ---------------------------------------------------------------------------

_ITEM_FIELDS = (
    "batch_id",
    "kind",
    "status",
    "knowledge_ids",
    "before",
    "after",
    "question",
    "answer",
    "evidence_ids",
    "reason",
    "error",
)
_ITEM_JSON_FIELDS = frozenset({"knowledge_ids", "before", "after", "evidence_ids"})


def create_item(
    conn: sqlite3.Connection,
    *,
    kind: str,
    status: str,
    batch_id: str | None = None,
    knowledge_ids: Any = None,
    before: Any = None,
    after: Any = None,
    question: str | None = None,
    answer: str | None = None,
    evidence_ids: Any = None,
    reason: str | None = None,
    error: str | None = None,
) -> str:
    if kind not in ITEM_KINDS:
        raise ValueError(f"unknown maintenance item kind: {kind!r}")
    item_id = new_id("mi_")
    now = _now()
    conn.execute(
        "INSERT INTO maintenance_items"
        " (id, batch_id, kind, status, knowledge_ids, before, after, question, answer,"
        "  evidence_ids, reason, error, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            item_id,
            batch_id,
            kind,
            status,
            _json(list(knowledge_ids or [])),
            _json(list(before or [])),
            _json(after if after is not None else []),
            question,
            answer,
            _json(list(evidence_ids or [])),
            reason,
            error,
            now,
            now,
        ),
    )
    return item_id


def update_item(conn: sqlite3.Connection, item_id: str, **fields: Any) -> None:
    allowed = {key: value for key, value in fields.items() if key in _ITEM_FIELDS}
    if not allowed:
        return
    sets: list[str] = []
    params: list[Any] = []
    for key, value in allowed.items():
        sets.append(f"{key} = ?")
        if key in _ITEM_JSON_FIELDS:
            params.append(_json(value))
        else:
            params.append(value)
    sets.append("updated_at = ?")
    params.append(_now())
    params.append(item_id)
    conn.execute(
        f"UPDATE maintenance_items SET {', '.join(sets)} WHERE id = ?", params
    )


def _item_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "batch_id": row["batch_id"],
        "kind": str(row["kind"]),
        "status": str(row["status"]),
        "knowledge_ids": _load_json(row["knowledge_ids"], []),
        "before": _load_json(row["before"], []),
        "after": _load_json(row["after"], []),
        "question": row["question"],
        "answer": row["answer"],
        "evidence_ids": _load_json(row["evidence_ids"], []),
        "reason": row["reason"],
        "error": row["error"],
        "created_at": str(row["created_at"]),
        "updated_at": str(row["updated_at"]),
    }


def get_item(conn: sqlite3.Connection, item_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM maintenance_items WHERE id = ?", (item_id,)
    ).fetchone()
    return _item_row(row) if row is not None else None


def list_items(
    conn: sqlite3.Connection,
    *,
    batch_id: str | None = None,
    kind: str | None = None,
    status: str | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    sql = "SELECT * FROM maintenance_items WHERE 1=1"
    params: list[Any] = []
    if batch_id is not None:
        sql += " AND batch_id = ?"
        params.append(batch_id)
    if kind is not None:
        sql += " AND kind = ?"
        params.append(kind)
    if status is not None:
        sql += " AND status = ?"
        params.append(status)
    sql += " ORDER BY created_at, id LIMIT ?"
    params.append(int(limit))
    return [_item_row(row) for row in conn.execute(sql, params).fetchall()]


def _knowledge_id_set(knowledge_ids: Any) -> set[str]:
    out: set[str] = set()
    for entry in knowledge_ids or []:
        if isinstance(entry, Mapping):
            value = entry.get("id")
        else:
            value = entry
        if value:
            out.add(str(value))
    return out


def find_duplicate_question(
    conn: sqlite3.Connection, *, knowledge_ids: Any, question: str
) -> dict[str, Any] | None:
    """同一知识集合 + 同一问题的待回答事项（存在则不再重复催问）。"""
    wanted_ids = _knowledge_id_set(knowledge_ids)
    wanted_question = (question or "").strip()
    if not wanted_question:
        return None
    rows = conn.execute(
        "SELECT * FROM maintenance_items WHERE kind = ? AND status IN (?, ?)"
        " ORDER BY created_at",
        (
            ITEM_ASK,
            models.PendingStatus.AWAITING_ANSWER.value,
            models.PendingStatus.DEFERRED.value,
        ),
    ).fetchall()
    for row in rows:
        item = _item_row(row)
        if _knowledge_id_set(item["knowledge_ids"]) != wanted_ids:
            continue
        if (item["question"] or "").strip() == wanted_question:
            return item
    return None


def list_open_questions(
    conn: sqlite3.Connection, *, limit: int = 20
) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM maintenance_items WHERE kind = ? AND status IN (?, ?)"
        " ORDER BY created_at LIMIT ?",
        (
            ITEM_ASK,
            models.PendingStatus.AWAITING_ANSWER.value,
            models.PendingStatus.DEFERRED.value,
            int(limit),
        ),
    ).fetchall()
    return [_item_row(row) for row in rows]


# ---------------------------------------------------------------------------
# 操作（真正落地的知识修改）
# ---------------------------------------------------------------------------


def create_operation(
    conn: sqlite3.Connection,
    *,
    action: str,
    batch_id: str | None = None,
    item_id: str | None = None,
    target_ids: Iterable[Any] = (),
    result_id: str | None = None,
    before: Any = None,
    after: Any = None,
    status: str = models.MaintenanceCandidateStatus.APPLIED.value,
) -> str:
    operation_id = new_id("mo_")
    conn.execute(
        "INSERT INTO maintenance_operations"
        " (id, batch_id, item_id, action, target_ids, result_id, before, after, status, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            operation_id,
            batch_id,
            item_id,
            str(action),
            _json(list(target_ids)),
            result_id,
            _json(list(before or [])),
            _json(after if after is not None else {}),
            str(status),
            _now(),
        ),
    )
    return operation_id


def _operation_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "batch_id": row["batch_id"],
        "item_id": row["item_id"],
        "action": str(row["action"]),
        "target_ids": _load_json(row["target_ids"], []),
        "result_id": row["result_id"],
        "before": _load_json(row["before"], []),
        "after": _load_json(row["after"], {}),
        "status": str(row["status"]),
        "created_at": str(row["created_at"]),
    }


def get_operation(conn: sqlite3.Connection, operation_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM maintenance_operations WHERE id = ?", (operation_id,)
    ).fetchone()
    return _operation_row(row) if row is not None else None


def list_operations(
    conn: sqlite3.Connection,
    *,
    batch_id: str | None = None,
    item_id: str | None = None,
    status: str | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    sql = "SELECT * FROM maintenance_operations WHERE 1=1"
    params: list[Any] = []
    if batch_id is not None:
        sql += " AND batch_id = ?"
        params.append(batch_id)
    if item_id is not None:
        sql += " AND item_id = ?"
        params.append(item_id)
    if status is not None:
        sql += " AND status = ?"
        params.append(status)
    sql += " ORDER BY created_at, id LIMIT ?"
    params.append(int(limit))
    return [_operation_row(row) for row in conn.execute(sql, params).fetchall()]


def list_restorable_operations(
    conn: sqlite3.Connection, *, batch_id: str
) -> list[dict[str, Any]]:
    """一个批次里真正改过知识、且行为 kind 是 merge/normalize 的操作。"""
    rows = conn.execute(
        "SELECT o.* FROM maintenance_operations o"
        " JOIN maintenance_items i ON i.id = o.item_id"
        " WHERE o.batch_id = ? AND o.status = ? AND i.kind IN (?, ?)"
        " ORDER BY o.created_at, o.id",
        (
            batch_id,
            models.MaintenanceCandidateStatus.APPLIED.value,
            ITEM_MERGE,
            ITEM_NORMALIZE,
        ),
    ).fetchall()
    return [_operation_row(row) for row in rows]


def list_operations_for_item(
    conn: sqlite3.Connection, item_id: str
) -> list[dict[str, Any]]:
    return [
        operation
        for operation in list_operations(conn, item_id=item_id)
        if operation["status"] == models.MaintenanceCandidateStatus.APPLIED.value
    ]


# ---------------------------------------------------------------------------
# A 的变化日志（只读）
# ---------------------------------------------------------------------------


def load_pending_changes(
    conn: sqlite3.Connection,
    *,
    change_ids: Iterable[int] = (),
    change_seq_max: int | None = None,
    limit: int = models.MAX_BATCH_CHANGES,
) -> list[dict[str, Any]]:
    """读取本轮维护允许处理的变化（A 的迁移 46 表；B 只读、不推进位置）。

    表还不存在（A 的迁移尚未合入 / 旧库）时返回空列表并告警——维护输入退化为
    只含快照给的知识，不伪造变化记录。
    """
    ids = [int(value) for value in change_ids or ()]
    try:
        if ids:
            placeholders = ", ".join("?" for _ in ids)
            rows = conn.execute(
                f"SELECT seq, knowledge_id, chain_id, version, source, occurred_at,"
                f" created_turn_seq FROM {CHANGE_LOG_TABLE}"
                f" WHERE seq IN ({placeholders}) ORDER BY seq LIMIT ?",
                [*ids, int(limit)],
            ).fetchall()
        else:
            if change_seq_max is None:
                return []
            rows = conn.execute(
                f"SELECT seq, knowledge_id, chain_id, version, source, occurred_at,"
                f" created_turn_seq FROM {CHANGE_LOG_TABLE}"
                f" WHERE consumed_batch_seq IS NULL AND seq <= ?"
                f" ORDER BY seq LIMIT ?",
                (int(change_seq_max), int(limit)),
            ).fetchall()
    except sqlite3.OperationalError:
        logger.warning(
            "变化日志表 %s 不可用：本轮维护输入缺少待处理变化明细", CHANGE_LOG_TABLE
        )
        return []
    return [
        {
            "seq": int(row["seq"]),
            "knowledge_id": str(row["knowledge_id"]),
            "chain_id": str(row["chain_id"] or ""),
            "version": int(row["version"] or 1),
            "source": str(row["source"] or ""),
            "occurred_at": row["occurred_at"],
            "created_turn_seq": int(row["created_turn_seq"] or 0),
        }
        for row in rows
    ]
