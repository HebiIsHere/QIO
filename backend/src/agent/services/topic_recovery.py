"""话题回答恢复记录（P1-03 / R14-B F03+F04+F13）。

一次逻辑话题操作在「拿掉了话题、却没完成回答」时留一条**持久化**的可恢复记录；恢复入口
（`POST /api/recovery/records/{record_id}/retry-answer`）只重做**未完成的步骤**：

* `failed_stage == "context_prepare"` → 只重做上下文装配 + 回答生成，
  **不重新执行** create_topic / switch_topic，不新增用户消息、不新建重复轮次；
* `failed_stage == "answer"` → 只重做回答；
* `failed_stage == "ownership"` → 显式恢复（重新绑定 + 如实说明）。

R14-B 的幂等边界（F03/F04/F13）
-------------------------------
1. **持久化**：记录落 SQLite 表 `topic_recovery_records`（迁移 79 追加）；TURN_END、
   断连、刷新、进程重启都不会删掉未完成事项。旧 app 内存 dict 只作兼容读取
   （scope 没有数据库连接时，例如 loop 的纯单元测试桥替身）。
2. **原子领取 + 租约**：`retry_topic_answer` 先用一条条件 UPDATE 领取
   （`status='pending' AND 租约缺失/已过期`），领取成功才生成；**模型调用期间不持有
   SQLite 写事务**。并发/重复请求：已有答案 → 返回同一 `answer_id`；正在执行 →
   结构化「处理中」（202 + `code=recovery_in_progress`），不重复生成。
   不同恢复事项互不全局串行（每条记录自己的租约）。
3. **唯一逻辑答案**：`answer_key = sha256(turn_id + ":" + user_message_id)`
   （没有 user_message_id 时退化为 turn_id+operation_id）。`messages.answer_key`
   上的部分唯一索引保证多个恢复记录/并发请求最终只产生**一份**正式答案。
4. **事务收口**：答案行写入与记录置 `answered`（含 answer_id）在**同一事务**提交。
   生成成功但保存失败：正文暂存进记录的 `answer_text`，后续重试只补存、不重复生成。
   崩溃发生在外部模型调用与暂存之间时，**可能再次调用模型**——不声称外部调用 exactly-once。
5. **迟到答案（F13）**：答案写回失败时刻原用户消息所在的**固定片段**，不重开已封存
   片段（closed_at 不变）、不写当前开放片段、不搬动原问题、不新建重复轮次；仍然是
   普通 `messages` 行（+`late_answer_of_message_id` 标记），所以历史加载 / 检索 /
   导出 / 删除清理天然统一，长期只保留一份正文。
6. 恢复前复核导航版本（`recovery_nav_version`）：用户后来的手动选择不会被覆盖；
   恢复本身不推进导航版本。日志/错误文本一律过 `agent/trace/redact.py`。

阶段/失败语义的唯一来源仍是 `services/topic_operation.py`（不在这里另造一套）。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from agent.services.topic_operation import RECOVERY_KIND, TopicStages
from agent.storage.db import transaction

logger = logging.getLogger(__name__)

__all__ = [
    "RECOVERY_KIND",
    "TOPIC_RECOVERY_ATTR",
    "TOPIC_RECOVERY_TABLE",
    "LEASE_SECONDS",
    "STATE_ANSWER_PENDING",
    "STATE_ANSWER_IN_PROGRESS",
    "ACTION_RETRY_ANSWER",
    "TopicContextPrepareError",
    "RecoveryPersistError",
    "new_record_id",
    "answer_key_for",
    "record_topic_failure",
    "record_topic_answer_failure",
    "get_record",
    "list_records",
    "update_record",
    "find_pending",
    "mark_answered",
    "claim_record",
    "release_claim",
    "list_pending_inbox_records",
    "inbox_record",
    "retry_topic_answer",
    "write_recovered_answer",
]


class TopicContextPrepareError(RuntimeError):
    """目标话题上下文装配失败（结构化事实，供恢复记录与说明使用）。

    turn_orchestrator 的重建闭包在失败时抛它；loop 捕获后按字段写恢复记录，
    **不**按错误文本猜阶段。`stage` 唯一取值口径见 services/topic_operation.py。
    """

    def __init__(
        self,
        detail: str = "",
        *,
        stage: str = "context_prepare",
        operation_id: str | None = None,
        topic_id: str | None = None,
    ) -> None:
        self.detail = str(detail or "")
        self.stage = str(stage or "context_prepare")
        self.operation_id = operation_id
        self.topic_id = topic_id
        super().__init__(self.detail or self.stage)

    def as_dict(self) -> dict:
        return {
            "stage": self.stage,
            "operation_id": self.operation_id,
            "topic_id": self.topic_id,
            "detail": self.detail,
        }


class RecoveryPersistError(RuntimeError):
    """答案已经生成，但保存到唯一权威位置失败（可恢复：记录保留并暂存正文）。"""


#: 持久化表名（迁移 79）。
TOPIC_RECOVERY_TABLE = "topic_recovery_records"
#: app 上挂**旧内存记录**的属性名（迁移期兼容读取；有 conn 时一律以 SQLite 为准）。
TOPIC_RECOVERY_ATTR = "topic_recovery_records"

#: 单次领取的租约时长（秒）。模型调用期间不持写事务，靠租约防重复生成；
#: 进程崩溃留下的租约到期后可被重新领取。故意给足（生成可能较慢）。
LEASE_SECONDS = 600.0

#: 清单里这类恢复事项的状态分类（前端按它决定能不能点「重试回答」）。
STATE_ANSWER_PENDING = "answer_pending"
STATE_ANSWER_IN_PROGRESS = "answer_in_progress"
ACTION_RETRY_ANSWER = "retry_answer"

_ALLOWED_STATUSES = ("pending", "answered")

_RECORD_COLUMNS = (
    "record_id, kind, status, turn_id, topic_id, from_topic_id, fragment_id, "
    "user_message_id, operation_id, param_version, base_nav_version, "
    "recovery_nav_version, failed_stage, message, stages_json, answer_key, "
    "answer_id, answer_text, last_error, claim_owner, claim_until, attempt_count, "
    "created_at, updated_at"
)


# ── 小工具 ─────────────────────────────────────────────────────────────


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _error_detail(exc: BaseException) -> str:
    """异常 → 脱敏短说明（新增输出路径必须过 redact）。"""
    from agent.trace.redact import sanitize_error_text

    return sanitize_error_text(str(exc)) or type(exc).__name__


def _parse_ts(value: Any) -> datetime | None:
    """解析时间戳（ISO / SQLite datetime('now') 两种形状）；解析不了返回 None。"""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def new_record_id() -> str:
    return f"trec_{uuid.uuid4().hex[:12]}"


def answer_key_for(
    turn_id: str,
    *,
    user_message_id: str | None = None,
    operation_id: str | None = None,
) -> str:
    """唯一逻辑答案身份：同一原问题/轮次只能有一份正式答案。

    * 有 user_message_id：sha256(turn_id + ":" + user_message_id)；
    * 没有（旧记录兼容）：退化为 sha256(turn_id + ":" + operation_id)。
    """
    turn = str(turn_id or "")
    user_message = str(user_message_id or "")
    if user_message:
        basis = f"{turn}:{user_message}"
    else:
        basis = f"{turn}:{str(operation_id or '')}"
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


def _new_message_id() -> str:
    return f"msg_{uuid.uuid4().hex[:12]}"


def _conn_of(scope: Any) -> sqlite3.Connection | None:
    """取 scope 上的库连接（AppContext 直接有 conn；桥替身可能挂在 .app 上）。"""
    for candidate in (scope, getattr(scope, "app", None)):
        if candidate is None:
            continue
        conn = getattr(candidate, "conn", None)
        if isinstance(conn, sqlite3.Connection):
            return conn
    return None


def _table_available(conn: sqlite3.Connection) -> bool:
    try:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (TOPIC_RECOVERY_TABLE,),
        ).fetchone()
    except sqlite3.Error:  # noqa: BLE001 - 读不动按「不可用」处理（有内存兼容读取）
        return False
    return row is not None


# ── 旧内存记录（迁移期兼容读取；有 conn 时不会是权威来源）─────────────


def _memory_store(scope: Any) -> dict[str, dict]:
    store = getattr(scope, TOPIC_RECOVERY_ATTR, None)
    if isinstance(store, dict):
        return store
    store = {}
    try:
        setattr(scope, TOPIC_RECOVERY_ATTR, store)
    except Exception:  # noqa: BLE001 - 只读 app 替身：记录退化为本次调用可见
        logger.warning("failed to attach topic recovery store", exc_info=True)
    return store


def _memory_get(scope: Any, record_id: str) -> dict | None:
    store = getattr(scope, TOPIC_RECOVERY_ATTR, None)
    if not isinstance(store, dict):
        return None
    record = store.get(str(record_id or ""))
    return dict(record) if isinstance(record, dict) else None


def _memory_list(scope: Any) -> list[dict]:
    store = getattr(scope, TOPIC_RECOVERY_ATTR, None)
    if not isinstance(store, dict):
        return []
    return [dict(item) for item in store.values() if isinstance(item, dict)]


def _memory_record_failure(scope: Any, rid: str, payload: dict) -> dict:
    store = _memory_store(scope)
    record = {"record_id": rid, **payload}
    previous = store.get(rid)
    if isinstance(previous, dict):
        record["status"] = previous.get("status", "pending")
        record["answer"] = previous.get("answer")
    store[rid] = record
    return dict(record)


# ── 行 → 记录 dict ─────────────────────────────────────────────────────


def _row_to_record(row: sqlite3.Row) -> dict:
    stages: Any = {}
    try:
        stages = json.loads(row["stages_json"] or "{}")
    except (TypeError, ValueError):
        stages = {}
    answer_id = row["answer_id"]
    record = {
        "record_id": row["record_id"],
        "kind": row["kind"],
        "status": row["status"],
        "turn_id": row["turn_id"] or "",
        "topic_id": row["topic_id"] or "",
        "from_topic_id": row["from_topic_id"],
        "fragment_id": row["fragment_id"],
        "user_message_id": row["user_message_id"],
        "operation_id": row["operation_id"],
        "param_version": row["param_version"],
        "base_nav_version": row["base_nav_version"],
        "recovery_nav_version": row["recovery_nav_version"],
        "failed_stage": row["failed_stage"] or "",
        "message": row["message"] or "",
        "stages": stages if isinstance(stages, dict) else {},
        "answer_key": row["answer_key"],
        "answer_id": answer_id,
        # 兼容旧键：历史调用方按 message_id 读 answer_id。
        "message_id": answer_id,
        "answer_text": row["answer_text"],
        "last_error": row["last_error"],
        "claim_owner": row["claim_owner"],
        "claim_until": row["claim_until"],
        "attempt_count": int(row["attempt_count"] or 0),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        # 兼容旧键：旧实现把最近一次尝试时间叫 last_attempt_at。
        "last_attempt_at": row["updated_at"],
    }
    return record


def _select_row(conn: sqlite3.Connection, record_id: str) -> sqlite3.Row | None:
    return conn.execute(
        f"SELECT {_RECORD_COLUMNS} FROM {TOPIC_RECOVERY_TABLE} WHERE record_id = ?",
        (str(record_id or ""),),
    ).fetchone()


def _get_from_db(conn: sqlite3.Connection, record_id: str) -> dict | None:
    if not _table_available(conn):
        return None
    try:
        row = _select_row(conn, record_id)
    except sqlite3.Error:  # noqa: BLE001 - 读失败按「没有」处理，调用方还有内存兼容
        logger.warning("failed to read topic recovery record", exc_info=True)
        return None
    return _row_to_record(row) if row is not None else None


# ── 写入一条恢复记录（loop 失败时调用）───────────────────────────────

def _lookup_fragment_for_message(conn: sqlite3.Connection, message_id: str | None) -> str | None:
    if not message_id:
        return None
    try:
        row = conn.execute(
            "SELECT fragment_id FROM messages WHERE id = ?", (str(message_id),)
        ).fetchone()
    except sqlite3.Error:  # noqa: BLE001 - 消息表读不到就不猜片段
        return None
    return str(row["fragment_id"]) if row is not None and row["fragment_id"] else None


def record_topic_failure(
    scope: Any,
    *,
    turn_id: str,
    topic_id: str | None,
    failed_stage: str,
    message: str,
    from_topic_id: str | None = None,
    fragment_id: str | None = None,
    user_message_id: str | None = None,
    operation_id: str | None = None,
    param_version: int | None = None,
    base_nav_version: int | None = None,
    # 失败时刻的真实位置版本（恢复基线）：话题工具成功导航会先推进版本，
    # 申请起点快照（base_nav_version）在恢复时必然落后，不能拿它做复核。
    recovery_nav_version: int | None = None,
    stages: TopicStages | None = None,
    record_id: str | None = None,
) -> dict:
    """写入一条可恢复记录（同 record_id 幂等覆盖；已完成记录不会被迟到失败覆盖）。

    * 有数据库连接 → 持久化到 SQLite（TURN_END/重启/断连后仍可见）；
    * 没有数据库连接（纯单元测试的桥替身）→ 退回旧内存 dict（仅兼容读取）。
    """
    conn = _conn_of(scope)
    rid = str(record_id or "") or new_record_id()
    stage_payload = stages.as_dict() if stages is not None else {}
    now = _now_iso()
    key = answer_key_for(
        turn_id,
        user_message_id=user_message_id,
        operation_id=operation_id,
    )
    if conn is None or not _table_available(conn):
        return _memory_record_failure(
            scope,
            rid,
            {
                "kind": RECOVERY_KIND,
                "status": "pending",
                "turn_id": str(turn_id or ""),
                "topic_id": str(topic_id or ""),
                "from_topic_id": from_topic_id,
                "fragment_id": fragment_id,
                "user_message_id": user_message_id,
                "operation_id": operation_id,
                "param_version": param_version,
                "base_nav_version": base_nav_version,
                "recovery_nav_version": recovery_nav_version,
                "failed_stage": str(failed_stage or ""),
                "message": str(message or ""),
                "stages": stage_payload,
                "answer_key": key,
                "answer": None,
                "answer_id": None,
                "answer_text": None,
            },
        )

    resolved_fragment = fragment_id or _lookup_fragment_for_message(conn, user_message_id)
    with transaction(conn):
        existing = conn.execute(
            f"SELECT status FROM {TOPIC_RECOVERY_TABLE} WHERE record_id = ?", (rid,)
        ).fetchone()
        if existing is None:
            conn.execute(
                f"INSERT INTO {TOPIC_RECOVERY_TABLE} ("
                "record_id, kind, status, turn_id, topic_id, from_topic_id, fragment_id, "
                "user_message_id, operation_id, param_version, base_nav_version, "
                "recovery_nav_version, failed_stage, message, stages_json, answer_key, "
                "answer_id, answer_text, last_error, claim_owner, claim_until, "
                "attempt_count, created_at, updated_at"
                ") VALUES (?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
                "NULL, NULL, NULL, NULL, NULL, 0, ?, ?)",
                (
                    rid,
                    RECOVERY_KIND,
                    str(turn_id or ""),
                    str(topic_id or ""),
                    from_topic_id,
                    resolved_fragment,
                    user_message_id,
                    operation_id,
                    param_version,
                    base_nav_version,
                    recovery_nav_version,
                    str(failed_stage or ""),
                    str(message or ""),
                    json.dumps(stage_payload, ensure_ascii=False),
                    key,
                    now,
                    now,
                ),
            )
        elif str(existing["status"]) == "answered":
            # F04：已完成不被迟到失败/事件重放覆盖（保留 answered 与 answer_id）。
            logger.info(
                "ignore late topic recovery failure for answered record %s", rid
            )
        else:
            conn.execute(
                f"UPDATE {TOPIC_RECOVERY_TABLE} SET "
                "topic_id = ?, from_topic_id = COALESCE(?, from_topic_id), "
                "fragment_id = COALESCE(?, fragment_id), "
                "user_message_id = COALESCE(?, user_message_id), "
                "operation_id = COALESCE(?, operation_id), "
                "param_version = COALESCE(?, param_version), "
                "base_nav_version = COALESCE(?, base_nav_version), "
                "recovery_nav_version = COALESCE(?, recovery_nav_version), "
                "failed_stage = ?, message = ?, stages_json = ?, "
                "answer_key = COALESCE(answer_key, ?), updated_at = ? "
                "WHERE record_id = ?",
                (
                    str(topic_id or ""),
                    from_topic_id,
                    resolved_fragment,
                    user_message_id,
                    operation_id,
                    param_version,
                    base_nav_version,
                    recovery_nav_version,
                    str(failed_stage or ""),
                    str(message or ""),
                    json.dumps(stage_payload, ensure_ascii=False),
                    key,
                    now,
                    rid,
                ),
            )
    record = get_record(scope, rid)
    if record is not None:
        return record
    # 极端情况下读不回来：仍给调用方一条可用的失败记录形态（不假装持久化成功）。
    return {
        "record_id": rid,
        "kind": RECOVERY_KIND,
        "status": "pending",
        "turn_id": str(turn_id or ""),
        "topic_id": str(topic_id or ""),
        "from_topic_id": from_topic_id,
        "fragment_id": resolved_fragment,
        "user_message_id": user_message_id,
        "operation_id": operation_id,
        "param_version": param_version,
        "base_nav_version": base_nav_version,
        "recovery_nav_version": recovery_nav_version,
        "failed_stage": str(failed_stage or ""),
        "message": str(message or ""),
        "stages": stage_payload,
        "answer_key": key,
        "answer_id": None,
        "message_id": None,
        "answer_text": None,
        "last_error": None,
        "claim_owner": None,
        "claim_until": None,
        "attempt_count": 0,
        "created_at": now,
        "updated_at": now,
        "last_attempt_at": now,
    }


def record_topic_answer_failure(scope: Any, **kwargs) -> dict:
    """回答阶段失败：单独记 `failed_stage="answer"`（不得与上下文失败互相覆盖）。"""
    kwargs.setdefault("failed_stage", "answer")
    return record_topic_failure(scope, **kwargs)


# ── 读取 / 更新（兼容旧 API）─────────────────────────────────────────


def get_record(scope: Any, record_id: str) -> dict | None:
    conn = _conn_of(scope)
    if conn is not None:
        record = _get_from_db(conn, record_id)
        if record is not None:
            return record
    return _memory_get(scope, record_id)


def list_records(scope: Any, *, status: str | None = None) -> list[dict]:
    records: list[dict] = []
    conn = _conn_of(scope)
    if conn is not None and _table_available(conn):
        sql = f"SELECT {_RECORD_COLUMNS} FROM {TOPIC_RECOVERY_TABLE}"
        params: list[Any] = []
        if status is not None:
            sql += " WHERE status = ?"
            params.append(str(status))
        sql += " ORDER BY created_at, record_id"
        try:
            records.extend(_row_to_record(row) for row in conn.execute(sql, params))
        except sqlite3.Error:  # noqa: BLE001 - 读失败退回内存兼容（不假装空）
            logger.warning("failed to list topic recovery records", exc_info=True)
    seen = {item["record_id"] for item in records}
    for item in _memory_list(scope):
        if item.get("record_id") in seen:
            continue
        if status is not None and item.get("status") != status:
            continue
        records.append(dict(item))
    return records


_UPDATE_COLUMN_MAP = {
    "status": "status",
    "fragment_id": "fragment_id",
    "user_message_id": "user_message_id",
    "operation_id": "operation_id",
    "param_version": "param_version",
    "base_nav_version": "base_nav_version",
    "recovery_nav_version": "recovery_nav_version",
    "failed_stage": "failed_stage",
    "message": "message",
    "answer_key": "answer_key",
    "answer_id": "answer_id",
    "answer_text": "answer_text",
    "last_error": "last_error",
    "claim_owner": "claim_owner",
    "claim_until": "claim_until",
    "attempt_count": "attempt_count",
    "created_at": "created_at",
}


def update_record(scope: Any, record_id: str, **fields: Any) -> dict | None:
    """按字段更新记录（旧 API 兼容；状态变化用 claim/release/save 的专用入口）。"""
    conn = _conn_of(scope)
    if conn is not None and _table_available(conn):
        updates: dict[str, Any] = {}
        for key, value in fields.items():
            if key == "stages":
                updates["stages_json"] = json.dumps(value or {}, ensure_ascii=False)
            elif key == "answer":
                updates["answer_text"] = value
            elif key == "message_id":
                updates["answer_id"] = value
            elif key == "last_attempt_at":
                updates.setdefault("updated_at", value)
            elif key in _UPDATE_COLUMN_MAP:
                updates[_UPDATE_COLUMN_MAP[key]] = value
        updates.setdefault("updated_at", _now_iso())
        assignments = ", ".join(f"{column} = ?" for column in updates)
        params = [*updates.values(), str(record_id or "")]
        with transaction(conn):
            conn.execute(
                f"UPDATE {TOPIC_RECOVERY_TABLE} SET {assignments} WHERE record_id = ?",
                params,
            )
        return get_record(scope, record_id)

    store = getattr(scope, TOPIC_RECOVERY_ATTR, None)
    if not isinstance(store, dict):
        return None
    record = store.get(str(record_id or ""))
    if not isinstance(record, dict):
        return None
    record.update(fields)
    return dict(record)


def find_pending(
    scope: Any, *, turn_id: str | None = None, user_message_id: str | None = None
) -> dict | None:
    for record in list_records(scope, status="pending"):
        if turn_id is not None and record.get("turn_id") != str(turn_id):
            continue
        if user_message_id is not None and record.get("user_message_id") != user_message_id:
            continue
        return record
    return None


def mark_answered(scope: Any, record_id: str, answer: str) -> dict | None:
    """旧兼容入口：把记录收口为 answered（新路径用 save_answer 的同事务收口）。"""
    return update_record(
        scope,
        record_id,
        status="answered",
        answer_text=str(answer or ""),
        failed_stage="",
    )


# ── 领取 / 租约 ───────────────────────────────────────────────────────


def _new_claim_owner(ctx: Any) -> str:
    instance = str(getattr(ctx, "instance_id", None) or "local")
    return f"{instance}|{uuid.uuid4().hex[:12]}"


def _owner_instance(claim_owner: Any) -> str | None:
    text = str(claim_owner or "")
    if not text:
        return None
    return text.split("|", 1)[0] or None


def _lease_active(record: dict) -> bool:
    if not record.get("claim_owner"):
        return False
    deadline = _parse_ts(record.get("claim_until"))
    if deadline is None:
        return False
    return deadline > datetime.now(timezone.utc)


def claim_record(
    conn: sqlite3.Connection,
    record_id: str,
    *,
    owner: str,
    lease_seconds: float = LEASE_SECONDS,
) -> dict | None:
    """原子领取一条 pending 记录（条件 UPDATE + 租约），成功返回领取后的记录。

    条件：`status='pending' AND (claim_until IS NULL OR claim_until < now)`。
    同一时刻只有一个执行者能领到；模型调用期间不持有写事务。
    """
    now_dt = datetime.now(timezone.utc)
    now = now_dt.isoformat()
    until = (now_dt + timedelta(seconds=float(lease_seconds))).isoformat()
    rid = str(record_id or "")
    with transaction(conn):
        # CAS 用「读到的 claim_until 原值」做条件（IS 同时支持 NULL / 文本）：
        # 不依赖时间字符串的字典序比较（SQLite datetime('now') 与 ISO 时区串
        # 的格式不同，直接比字符串会把「有效租约」误判成过期而重复生成）。
        current = conn.execute(
            f"SELECT status, claim_until FROM {TOPIC_RECOVERY_TABLE} WHERE record_id = ?",
            (rid,),
        ).fetchone()
        if current is None or str(current["status"]) != "pending":
            return None
        previous_until = current["claim_until"]
        previous_deadline = _parse_ts(previous_until)
        if previous_until and previous_deadline is not None and previous_deadline > now_dt:
            return None
        cursor = conn.execute(
            f"UPDATE {TOPIC_RECOVERY_TABLE} SET claim_owner = ?, claim_until = ?, "
            "attempt_count = attempt_count + 1, updated_at = ? "
            "WHERE record_id = ? AND status = 'pending' AND claim_until IS ?",
            (str(owner), until, now, rid, previous_until),
        )
        if cursor.rowcount != 1:
            return None
        row = _select_row(conn, rid)
    return _row_to_record(row) if row is not None else None


def release_claim(
    conn: sqlite3.Connection,
    record_id: str,
    *,
    claim_owner: str | None,
    last_error: str | None = None,
    failed_stage: str | None = None,
    stages: dict | None = None,
) -> dict | None:
    """释放租约（只释放自己的；已被别人重新领取时不动别人的租约）。

    失败事实只在 `status='pending'` 时写：已完成记录不会被迟到的失败覆盖。
    """
    now = _now_iso()
    assignments = ["claim_owner = NULL", "claim_until = NULL", "updated_at = ?"]
    params: list[Any] = [now]
    if last_error is not None:
        assignments.append("last_error = ?")
        params.append(str(last_error))
    if failed_stage is not None:
        assignments.append("failed_stage = ?")
        params.append(str(failed_stage))
    if stages is not None:
        assignments.append("stages_json = ?")
        params.append(json.dumps(stages, ensure_ascii=False))
    params.extend([str(record_id or ""), str(claim_owner or "")])
    with transaction(conn):
        conn.execute(
            f"UPDATE {TOPIC_RECOVERY_TABLE} SET {', '.join(assignments)} "
            "WHERE record_id = ? AND status = 'pending' "
            "AND (claim_owner IS NULL OR claim_owner = ?)",
            params,
        )
    return get_record(conn, record_id)


def _release_claim_safely(
    conn: sqlite3.Connection,
    record_id: str,
    claim_owner: str,
    *,
    last_error: str | None = None,
    failed_stage: str | None = None,
    stages: dict | None = None,
) -> None:
    try:
        release_claim(
            conn,
            record_id,
            claim_owner=claim_owner,
            last_error=last_error,
            failed_stage=failed_stage,
            stages=stages,
        )
    except sqlite3.Error as exc:  # noqa: BLE001 - 释放失败由租约过期兜底，不能吞掉主流程
        logger.warning(
            "failed to release topic recovery claim %s: %s",
            record_id,
            _error_detail(exc),
        )


# ── 答案写入 / 读取（唯一权威位置：messages 表）──────────────────────


def _fragment_row(conn: sqlite3.Connection, fragment_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT id, topic_id, closed_at, start_message_id, end_message_id "
        "FROM fragments WHERE id = ?",
        (str(fragment_id or ""),),
    ).fetchone()


def write_recovered_answer(
    conn: sqlite3.Connection,
    *,
    answer_key: str,
    topic_id: str,
    fragment_id: str,
    turn_id: str | None,
    user_message_id: str | None,
    content: str,
    model: str | None,
    recovery_record_id: str,
    created_at: str | None = None,
) -> str:
    """F13：专用迟到答案写入（调用方负责事务边界）。

    * 关联原轮次（turn_id）/ 原用户消息（late_answer_of_message_id）/ 固定片段；
    * 片段已封存 → **不改 fragments 行**（closed_at 不变、不重开）；片段仍开放 →
      按普通追加维护 end_message_id；
    * 仍然只是一行普通 `messages`：历史加载 / 检索 / 导出 / 删除清理天然统一。
    """
    fragment = _fragment_row(conn, fragment_id)
    if fragment is None:
        raise RecoveryPersistError(f"恢复记录的原片段不存在：{fragment_id}")
    if str(fragment["topic_id"]) != str(topic_id):
        raise RecoveryPersistError(
            f"原片段 {fragment_id} 不属于话题 {topic_id}（不猜归属，拒绝写入）"
        )
    now = created_at or _now_iso()
    message_id = _new_message_id()
    raw = {
        "recovered": True,
        "late_answer": True,
        "recovery_record_id": recovery_record_id,
        "late_answer_of_message_id": user_message_id,
        "generated_at": now,
    }
    conn.execute(
        "INSERT INTO messages (id, fragment_id, role, content, content_type, model, raw, "
        "created_at, storage_tier, turn_id, answer_key, late_answer_of_message_id) "
        "VALUES (?, ?, 'assistant', ?, 'text', ?, ?, ?, 'hot', ?, ?, ?)",
        (
            message_id,
            str(fragment_id),
            str(content or ""),
            model,
            json.dumps(raw, ensure_ascii=False),
            now,
            (str(turn_id or "") or None),
            str(answer_key),
            (str(user_message_id or "") or None),
        ),
    )
    if fragment["closed_at"] is None:
        if fragment["start_message_id"] is None:
            conn.execute(
                "UPDATE fragments SET start_message_id = ? WHERE id = ?",
                (message_id, str(fragment_id)),
            )
        conn.execute(
            "UPDATE fragments SET end_message_id = ? WHERE id = ?",
            (message_id, str(fragment_id)),
        )
    return message_id


def _insert_answer_message(
    conn: sqlite3.Connection,
    *,
    record: dict,
    content: str,
    model: str | None,
    answer_key: str,
) -> str:
    """把答案写进恢复记录固定下来的原片段（F13；普通轮仍走 MemoryWriter）。"""
    fragment_id = record.get("fragment_id") or _lookup_fragment_for_message(
        conn, record.get("user_message_id")
    )
    if not fragment_id:
        raise RecoveryPersistError(
            "恢复记录没有固定片段（缺少 fragment_id 且原用户消息不存在），拒绝写当前片段"
        )
    return write_recovered_answer(
        conn,
        answer_key=answer_key,
        topic_id=str(record.get("topic_id") or ""),
        fragment_id=str(fragment_id),
        turn_id=record.get("turn_id"),
        user_message_id=record.get("user_message_id"),
        content=content,
        model=model,
        recovery_record_id=str(record.get("record_id") or ""),
    )


def _find_answer_by_key(
    conn: sqlite3.Connection, answer_key: str | None
) -> sqlite3.Row | None:
    if not answer_key:
        return None
    return conn.execute(
        "SELECT id, content FROM messages WHERE answer_key = ? LIMIT 1",
        (str(answer_key),),
    ).fetchone()


def _message_content(
    conn: sqlite3.Connection, message_id: str | None
) -> str | None:
    if not message_id:
        return None
    row = conn.execute(
        "SELECT content, storage_tier FROM messages WHERE id = ?", (str(message_id),)
    ).fetchone()
    if row is None:
        return None
    if str(row["storage_tier"]) == "cold":
        try:
            from agent.storage.archive import restore_message_content

            return restore_message_content(conn, str(message_id))
        except Exception:  # noqa: BLE001 - 归档不可用时不伪装正文
            return None
    return row["content"]


def _mark_answered_row(
    conn: sqlite3.Connection, record_id: str, answer_id: str
) -> None:
    """事务内收口：记录 answered + answer_id，清空暂存正文与租约。"""
    conn.execute(
        f"UPDATE {TOPIC_RECOVERY_TABLE} SET status = 'answered', answer_id = ?, "
        "answer_text = '', failed_stage = '', last_error = NULL, "
        "claim_owner = NULL, claim_until = NULL, updated_at = ? WHERE record_id = ?",
        (str(answer_id), _now_iso(), str(record_id or "")),
    )


def _save_answer(
    conn: sqlite3.Connection,
    record: dict,
    answer: str,
    *,
    model: str | None,
) -> dict:
    """把答案写入唯一权威位置，并在**同一事务**把记录置 answered。"""
    record_id = str(record.get("record_id") or "")
    answer_key = str(record.get("answer_key") or "") or answer_key_for(
        str(record.get("turn_id") or ""),
        user_message_id=record.get("user_message_id"),
        operation_id=record.get("operation_id"),
    )
    stored_answer = answer
    with transaction(conn):
        row = _find_answer_by_key(conn, answer_key)
        if row is not None:
            message_id = str(row["id"])
            stored_answer = str(row["content"] or answer)
        else:
            try:
                message_id = _insert_answer_message(
                    conn,
                    record=record,
                    content=answer,
                    model=model,
                    answer_key=answer_key,
                )
            except sqlite3.IntegrityError:
                # 并发下另一个请求先写入同一逻辑答案：唯一约束保证只有一份正文。
                row = _find_answer_by_key(conn, answer_key)
                if row is None:
                    raise
                message_id = str(row["id"])
                stored_answer = str(row["content"] or answer)
        _mark_answered_row(conn, record_id, message_id)
    return {"answer_id": message_id, "answer": stored_answer}


def _stash_answer_failure(
    conn: sqlite3.Connection,
    record_id: str,
    answer: str,
    failure: str,
) -> None:
    """生成成功但保存失败：暂存正文（后续重试只补存，不重复生成）+ 释放租约。"""
    with transaction(conn):
        conn.execute(
            f"UPDATE {TOPIC_RECOVERY_TABLE} SET answer_text = ?, last_error = ?, "
            "claim_owner = NULL, claim_until = NULL, updated_at = ? "
            "WHERE record_id = ? AND status = 'pending'",
            (str(answer or ""), str(failure or ""), _now_iso(), str(record_id or "")),
        )


# ── 恢复入口：只重做未完成步骤（F03）──────────────────────────────────


def _current_navigation_version(ctx: Any) -> int | None:
    """导航版本（C 的 navigation_version.current）；模块未就绪时明确返回 None。"""
    try:
        from agent.services.navigation_version import current as _current
    except Exception:  # noqa: BLE001 - 迁移未就绪时如实「无法校验」，不猜数字
        return None
    conn = _conn_of(ctx)
    if conn is None:
        return None
    try:
        return int(_current(conn))
    except Exception:  # noqa: BLE001 - 读版本失败同样不当作「变过」
        logger.warning("failed to read navigation version", exc_info=True)
        return None


def _stale_result(record: dict, actual: int | None) -> dict:
    message = (
        "目标位置在你后来的操作中已经改变，恢复不会覆盖你的新选择。"
        "请回到目标话题后重新发起，或忽略这条恢复记录。"
    )
    return {
        "ok": False,
        "conflict": True,
        "code": "topic_application_stale",
        "message": message,
        "record": dict(record),
        "expected_nav_version": (
            record.get("recovery_nav_version")
            if record.get("recovery_nav_version") is not None
            else record.get("base_nav_version")
        ),
        "actual_nav_version": actual,
    }


async def _generate_answer(adapter: Any, messages: list) -> str:
    """只生成回答：按同一内容角色协议解析，声明标记不进正文。"""
    from agent import prompts
    from agent.adapters.base import ChatMessage
    from agent.core.loop import _redact_published, _split_declared_answer

    request_messages = list(messages or [])
    if not getattr(adapter, "protocol_in_prompt", False):
        request_messages = [
            *request_messages,
            ChatMessage(role="system", content=prompts.answer_protocol()),
        ]
    completion = await adapter.complete(request_messages, [])
    content = completion.message.content or ""
    declared, body = _split_declared_answer(content)
    text = body if declared else content
    return _redact_published(text, holdback=False).strip()


def _answered_result(
    ctx: Any, record: dict, *, failed_stage: str | None = None
) -> dict:
    conn = _conn_of(ctx)
    answer = None
    if conn is not None:
        answer = _message_content(conn, record.get("answer_id"))
    if answer is None:
        answer = record.get("answer_text")
    return {
        "ok": True,
        "answered": True,
        "record_id": record["record_id"],
        "turn_id": record.get("turn_id"),
        "topic_id": record.get("topic_id"),
        "answer": answer,
        "answer_id": record.get("answer_id"),
        "failed_stage": (
            failed_stage if failed_stage is not None else record.get("failed_stage")
        ),
        "navigation_version_check": "ok",
    }


def _in_progress_result(record: dict) -> dict:
    return {
        "ok": True,
        "answered": False,
        "in_progress": True,
        "code": "recovery_in_progress",
        "record_id": record["record_id"],
        "status": str(record.get("status") or "pending"),
        "message": "这条恢复正在处理中（已有执行者领取租约），请稍后刷新。",
        "status_code": 202,
    }


def _conflict(code: str, message: str, record: dict | None = None) -> dict:
    result: dict[str, Any] = {
        "ok": False,
        "conflict": True,
        "code": code,
        "message": message,
        "status_code": 409,
    }
    if record is not None:
        result["record"] = record
    return result


async def retry_topic_answer(
    ctx: Any,
    record_id: str,
    *,
    expected_class: str | None = None,
    expected_status: str | None = None,
    lease_seconds: float | None = None,
) -> dict:
    """重试一条话题回答恢复记录（幂等；只重做未完成步骤；原子领取 + 租约）。

    * context_prepare 失败 → 只重装配上下文 + 回答生成（不重跑 create/switch、
      不新增用户消息、不新建重复轮次）；
    * answer 失败 → 只重做回答；
    * 已成功 → 直接返回既有结果（不重复生成 / 不复制消息）；
    * 已有其他执行者持有效租约 → 202 `recovery_in_progress`（不重复生成）；
    * 恢复前复核最新导航版本：目标位置已被用户后来的选择改变 → 如实失败、
      不覆盖，记录保持可恢复（stale ≠ 拒绝）；
    * 生成成功但保存失败 → 正文暂存进记录，后续重试只补存（不重复生成）。
    """
    record = get_record(ctx, record_id)
    if record is None:
        return {
            "ok": False,
            "code": "recovery_not_found",
            "message": "没有这条恢复记录",
            "status_code": 404,
        }
    if expected_class and str(record.get("kind")) != str(expected_class):
        return _conflict(
            "recovery_class_conflict",
            "恢复记录类型与你看到的不一致，请刷新后重试",
            record,
        )
    status = str(record.get("status") or "")
    if expected_status and status != str(expected_status) and status != "answered":
        return _conflict(
            "recovery_status_conflict", "恢复记录状态已经变化，请刷新后重试", record
        )
    if status == "answered":
        return _answered_result(ctx, record)
    if status not in _ALLOWED_STATUSES:
        return _conflict(
            "recovery_status_conflict", "恢复记录当前状态不能重试，请刷新后重试", record
        )

    conn = _conn_of(ctx)
    if conn is None or not _table_available(conn):
        # 旧内存记录仅作迁移期兼容读取：没有持久化存储时如实拒绝（不假成功）。
        return {
            "ok": False,
            "code": "recovery_unavailable",
            "message": (
                "这条恢复记录没有持久化存储（旧版本内存记录只做兼容读取），"
                "无法在当前进程里重试；请重启到新版本后从原轮次重新发起。"
            ),
            "record_id": record["record_id"],
            "status_code": 503,
        }

    claim_owner = _new_claim_owner(ctx)
    claimed = claim_record(
        conn,
        record["record_id"],
        owner=claim_owner,
        lease_seconds=LEASE_SECONDS if lease_seconds is None else float(lease_seconds),
    )
    if claimed is None:
        latest = get_record(ctx, record["record_id"]) or record
        if str(latest.get("status") or "") == "answered":
            return _answered_result(ctx, latest)
        return _in_progress_result(latest)

    try:
        return await _execute_claim(ctx, conn, claimed, claim_owner)
    except asyncio.CancelledError:
        _release_claim_safely(
            conn,
            record["record_id"],
            claim_owner,
            last_error="cancelled",
        )
        raise
    except Exception as exc:  # noqa: BLE001 - 未预期异常：释放租约，可再重试
        failure = _error_detail(exc)
        _release_claim_safely(
            conn,
            record["record_id"],
            claim_owner,
            last_error=failure,
        )
        raise


async def _execute_claim(
    ctx: Any,
    conn: sqlite3.Connection,
    record: dict,
    claim_owner: str,
) -> dict:
    record_id = str(record["record_id"])
    failed_stage_at_claim = str(record.get("failed_stage") or "")

    # 1) 唯一逻辑答案已经存在（并发 / 重复恢复记录）：直接收口，不重复生成。
    existing = _find_answer_by_key(conn, record.get("answer_key"))
    if existing is not None:
        with transaction(conn):
            _mark_answered_row(conn, record_id, str(existing["id"]))
        return _answered_result(
            ctx, get_record(ctx, record_id) or record, failed_stage=failed_stage_at_claim
        )

    # 2) 生成成功但保存失败时暂存的正文：只补存，不重复调用模型。
    stash = str(record.get("answer_text") or "")
    if stash:
        try:
            saved = _save_answer(conn, record, stash, model=None)
        except Exception as exc:  # noqa: BLE001 - 保存仍失败：保留暂存，可再重试
            failure = _error_detail(exc)
            _release_claim_safely(
                conn, record_id, claim_owner, last_error=failure
            )
            return _persist_failed(record_id, failure)
        return _answered_from_saved(
            ctx, record, saved, failed_stage=failed_stage_at_claim
        )

    # 3) 恢复前复核最新导航版本：用户后来的选择不被覆盖（stale ≠ 拒绝）。
    navigation_version_check = "ok"
    actual_version = _current_navigation_version(ctx)
    baseline_version = record.get("recovery_nav_version")
    if baseline_version is None:
        baseline_version = record.get("base_nav_version")
    if baseline_version is not None:
        if actual_version is None:
            navigation_version_check = "unavailable"
        elif int(actual_version) != int(baseline_version):
            _release_claim_safely(
                conn, record_id, claim_owner, last_error="nav_version_conflict"
            )
            result = _stale_result(get_record(ctx, record_id) or record, actual_version)
            result["status_code"] = 409
            return result

    # 4) 归属失败（F06）：恢复顺序是「先修归属 → 再装配目标上下文 → 再回答」。
    #    只搬本轮这一条用户消息与绑定：不重跑已成功的 create/switch、不复制用户消息、
    #    不新建重复轮次、不推进导航版本（由 orchestrator 的唯一恢复入口负责）。
    orchestrator = getattr(ctx, "turn_orchestrator", None)
    if str(record.get("failed_stage") or "") == "ownership":
        repair = getattr(orchestrator, "repair_topic_ownership", None)
        if not callable(repair):
            _release_claim_safely(
                conn, record_id, claim_owner, failed_stage="ownership"
            )
            return {
                "ok": False,
                "code": "recovery_unavailable",
                "recoverable": True,
                "failed_stage": "ownership",
                "message": "归属恢复入口暂时不可用（没有 repair_topic_ownership 通道）",
                "record_id": record_id,
                "status_code": 503,
            }
        try:
            await repair(
                turn_id=str(record.get("turn_id") or ""),
                topic_id=str(record.get("topic_id") or ""),
                user_message_id=record.get("user_message_id"),
            )
        except Exception as exc:  # noqa: BLE001 - 修不好仍是可恢复失败，记录保留
            failure = _error_detail(exc)
            _release_claim_safely(
                conn,
                record_id,
                claim_owner,
                failed_stage="ownership",
                last_error=failure,
            )
            return {
                "ok": False,
                "code": "topic_ownership_failed",
                "recoverable": True,
                "failed_stage": "ownership",
                "message": "话题归属修复失败：" + failure,
                "record_id": record_id,
                "status_code": 503,
            }
        # 归属修好后原用户消息落在目标话题的真实片段：固定片段随真实归属更新
        # （仍然只有一条原消息，不复制、不新建轮次）。
        repaired_fragment = _lookup_fragment_for_message(conn, record.get("user_message_id"))
        if repaired_fragment and repaired_fragment != record.get("fragment_id"):
            update_record(ctx, record_id, fragment_id=repaired_fragment)
            record = dict(record)
            record["fragment_id"] = repaired_fragment

    # 5) 装配上下文（只读；不在模型调用期间持有写事务）。
    prepare = getattr(orchestrator, "prepare_topic_answer_messages", None)
    if not callable(prepare):
        _release_claim_safely(conn, record_id, claim_owner)
        return {
            "ok": False,
            "code": "recovery_unavailable",
            "message": "恢复入口暂时不可用（没有上下文装配通道）",
            "record_id": record_id,
            "status_code": 503,
        }
    try:
        messages = await prepare(
            turn_id=str(record.get("turn_id") or ""),
            topic_id=str(record.get("topic_id") or ""),
            user_message_id=record.get("user_message_id"),
        )
    except Exception as exc:  # noqa: BLE001 - 装配失败仍是可恢复失败，记录保留
        failure = _error_detail(exc)
        _release_claim_safely(
            conn,
            record_id,
            claim_owner,
            failed_stage="context_prepare",
            last_error=failure,
        )
        return {
            "ok": False,
            "code": "topic_context_prepare_failed",
            "recoverable": True,
            "failed_stage": "context_prepare",
            "message": "话题已切换，回答准备失败：" + failure,
            "record_id": record_id,
            "status_code": 503,
        }
    if not messages:
        _release_claim_safely(
            conn,
            record_id,
            claim_owner,
            failed_stage="context_prepare",
            last_error="empty_context",
        )
        return {
            "ok": False,
            "code": "topic_context_prepare_failed",
            "recoverable": True,
            "failed_stage": "context_prepare",
            "message": "话题已切换，回答准备失败：目标话题上下文为空",
            "record_id": record_id,
            "status_code": 503,
        }

    # 5) 模型通道（外部调用，不持写事务）。
    builder = getattr(ctx, "build_adapter", None)
    adapter = None
    if callable(builder):
        try:
            adapter = await builder()
        except Exception as exc:  # noqa: BLE001 - 模型通道失败：如实失败，记录保留
            failure = _error_detail(exc)
            _release_claim_safely(conn, record_id, claim_owner, last_error=failure)
            return {
                "ok": False,
                "code": "model_unavailable",
                "message": "回答生成失败（模型通道不可用）：" + failure,
                "record_id": record_id,
                "status_code": 503,
            }
    if adapter is None:
        _release_claim_safely(conn, record_id, claim_owner, last_error="model_unavailable")
        return {
            "ok": False,
            "code": "model_unavailable",
            "message": "回答生成失败（没有可用的模型通道）",
            "record_id": record_id,
            "status_code": 503,
        }
    try:
        answer = await _generate_answer(adapter, messages)
    except Exception as exc:  # noqa: BLE001 - 回答失败单独记 answer 阶段，单独恢复
        failure = _error_detail(exc)
        stages = _stages_from_record(record)
        stages.failed_stage = "answer"
        stages.failure = failure
        _release_claim_safely(
            conn,
            record_id,
            claim_owner,
            failed_stage="answer",
            last_error=failure,
            stages=stages.as_dict(),
        )
        return {
            "ok": False,
            "code": "topic_answer_failed",
            "recoverable": True,
            "failed_stage": "answer",
            "message": "话题已切换，回答生成失败：" + failure,
            "record_id": record_id,
            "status_code": 503,
        }
    if not answer.strip():
        _release_claim_safely(
            conn, record_id, claim_owner, failed_stage="answer", last_error="empty_answer"
        )
        return {
            "ok": False,
            "code": "topic_answer_failed",
            "recoverable": True,
            "failed_stage": "answer",
            "message": "话题已切换，回答生成失败：模型没有给出可交付正文",
            "record_id": record_id,
            "status_code": 503,
        }

    # 6) 事务收口：答案行与记录 answered 同一事务。
    try:
        saved = _save_answer(conn, record, answer, model=getattr(adapter, "model", None))
    except Exception as exc:  # noqa: BLE001 - 写失败：暂存正文，记录保持 pending 可再重试
        failure = _error_detail(exc)
        try:
            _stash_answer_failure(conn, record_id, answer, failure)
        except sqlite3.Error:  # noqa: BLE001 - 暂存也失败：如实说明，下次可能再调模型
            logger.warning(
                "failed to stash recovered answer for %s: %s",
                record_id,
                _error_detail(exc),
            )
        else:
            _release_claim_safely(conn, record_id, claim_owner)
        return _persist_failed(record_id, failure)

    result = _answered_from_saved(
        ctx, record, saved, failed_stage=failed_stage_at_claim
    )
    result["navigation_version_check"] = navigation_version_check
    return result


def _persist_failed(record_id: str, failure: str) -> dict:
    return {
        "ok": False,
        "code": "recovery_persist_failed",
        "recoverable": True,
        "message": "回答已生成但持久化失败：" + failure,
        "record_id": record_id,
        "status_code": 503,
    }


def _answered_from_saved(
    ctx: Any, record: dict, saved: dict, *, failed_stage: str | None
) -> dict:
    return {
        "ok": True,
        "answered": True,
        "record_id": record["record_id"],
        "turn_id": record.get("turn_id"),
        "topic_id": record.get("topic_id"),
        "answer": saved.get("answer"),
        "answer_id": saved.get("answer_id"),
        "failed_stage": failed_stage,
        "navigation_version_check": "ok",
    }


def _stages_from_record(record: dict):
    stages = TopicStages()
    payload = record.get("stages") or {}
    if isinstance(payload, dict):
        for name in TopicStages.__dataclass_fields__:  # type: ignore[attr-defined]
            if name in payload:
                setattr(stages, name, payload[name])
    return stages


# ── 恢复清单（F04-backend：进既有 GET /api/recovery/records）───────────


def _inbox_item(record: dict) -> dict:
    """一条恢复记录 → 既有 RecoveryRecord 形状 + 话题恢复专属字段（只读）。"""
    active = _lease_active(record)
    answered = str(record.get("status") or "") == "answered"
    if answered:
        state_class = "answered"
    else:
        state_class = STATE_ANSWER_IN_PROGRESS if active else STATE_ANSWER_PENDING
    item = {
        "record_id": record["record_id"],
        "kind": RECOVERY_KIND,
        "state_class": state_class,
        "status": record["status"],
        "message": record.get("message"),
        "topic_id": record.get("topic_id") or None,
        "reason": record.get("failed_stage") or record.get("last_error"),
        "created_at": record.get("created_at") or "",
        "updated_at": record.get("updated_at"),
        "owner_instance_id": _owner_instance(record.get("claim_owner")),
        "owner_state": "alive" if active else "none",
        "owner_note": (
            "正在重新生成回答（租约有效，请稍后刷新）"
            if active
            else (
                "这一轮的回答已经完成"
                if answered
                else "这一轮的回答还没有完成，可以重试"
            )
        ),
        "claim_generation": None,
        "attempts": record["attempt_count"],
        "last_error": record.get("last_error"),
        "confirmed_stopped": False,
        "actions": [] if answered else [
            {
                "id": ACTION_RETRY_ANSWER,
                "label": "重试回答",
                "enabled": not active,
                "reason": (
                    "这条恢复正在处理中（已有执行者领取租约），请稍后刷新"
                    if active
                    else ""
                ),
            }
        ],
        # 话题恢复专属字段（不破坏既有 RecoveryRecord 字段集合）。
        "turn_id": record.get("turn_id"),
        "from_topic_id": record.get("from_topic_id"),
        "fragment_id": record.get("fragment_id"),
        "failed_stage": record.get("failed_stage"),
        "operation_id": record.get("operation_id"),
        "param_version": record.get("param_version"),
        "base_nav_version": record.get("base_nav_version"),
        "recovery_nav_version": record.get("recovery_nav_version"),
        "answer_id": record.get("answer_id"),
        "attempt_count": record["attempt_count"],
        "claim_until": record.get("claim_until"),
        "retryable": (not answered) and (not active),
        "answered": answered,
    }
    return item


def list_pending_inbox_records(conn: sqlite3.Connection) -> list[dict]:
    """未完成的话题回答恢复事项，按既有 RecoveryRecord 形状 + 额外字段返回。

    只读；不改状态、不领取、不续期。`state_class`：
    * `answer_pending`    → 现在可以重试；
    * `answer_in_progress` → 有有效租约（正在生成），动作禁用并给出原因。
    """
    if not _table_available(conn):
        return []
    try:
        rows = conn.execute(
            f"SELECT {_RECORD_COLUMNS} FROM {TOPIC_RECOVERY_TABLE} "
            "WHERE status = 'pending' ORDER BY created_at DESC, record_id DESC"
        ).fetchall()
    except sqlite3.Error as exc:  # noqa: BLE001 - 读失败必须让调用方明确失败（F05 口径）
        raise RuntimeError(f"topic_recovery_records 读取失败：{exc}") from exc
    return [_inbox_item(_row_to_record(row)) for row in rows]


def inbox_record(conn: sqlite3.Connection, record_id: str) -> dict | None:
    """单条恢复事项详情（pending / answered 都可查；只读）。"""
    if not _table_available(conn):
        return None
    try:
        row = _select_row(conn, record_id)
    except sqlite3.Error as exc:  # noqa: BLE001 - 读失败必须让调用方明确失败
        raise RuntimeError(f"topic_recovery_records 读取失败：{exc}") from exc
    if row is None:
        return None
    return _inbox_item(_row_to_record(row))
