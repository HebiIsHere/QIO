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
2. **原子领取 + 租约**：`retry_topic_answer` 先用一条条件 UPDATE 做**记录级**
   预检/领取（`status='pending' AND 租约缺失/已过期`），真正的执行权在下方第 4b 条
   （按 `answer_key` 的 `topic_answer_runs`）收口；**模型调用期间不持有 SQLite
   写事务**。并发/重复请求：已有答案 → 返回同一 `answer_id`；正在执行 →
   结构化「处理中」（202 + `code=recovery_in_progress`），不重复生成。
   不同 answer_key 的恢复事项不全局串行（各自一条执行权行）。
3. **唯一逻辑答案**：`answer_key = sha256(turn_id + ":" + user_message_id)`
   （没有 user_message_id 时退化为 turn_id+operation_id）。`messages.answer_key`
   上的部分唯一索引保证多个恢复记录/并发请求最终只产生**一份**正式答案。
4. **事务收口**：答案行写入与记录置 `answered`（含 answer_id）在**同一事务**提交。
   R16-D（契约 §6 / §14.6）：暂存正文的**唯一权威**是 `topic_answer_runs` 的
   `staged_answer/staged_at/staged_model`（按 answer_key 共享）；生成成功但保存失败后，
   **任意**同 answer_key 的恢复记录（哪怕另一条记录）都能取得共享暂存结果补存，不再调用模型。
   记录列 `answer_text` 不再是新暂存的权威：新代码只写 run 行；旧版本残留只作**只读兜底**
   （读到即同事务转写 run 行并清记录，保证只有一份权威）。崩溃发生在外部模型调用与
   暂存之间时，**可能再次调用模型**——不声称外部调用 exactly-once。
4a. **phase 状态机（迁移 83 列）**：waiting（等待处理）→ preparing（归属/上下文装配）
   → generating（模型生成中）→ staged（已生成、保存失败待补存）→ answered（正式保存）；
   失败走 failed（可恢复失败，记录保持 pending 可重试）。phase 与 status 同步写入
   （waiting/staged/failed→pending、preparing/generating→running、answered→answered）。
   暂存 / 补存 / 保存 / 成功收口 / 失败更新 / 释放全部校验执行权；迟到写一律拒绝（I11）。
4b. **逻辑答案执行权（R15-D / R4，契约 §6）**：租约不再只按 recovery record 领取，
   而是按 `answer_key` 收口到持久表 `topic_answer_runs`（迁移 81）。同一逻辑答案的
   多条恢复记录共享一个执行权：第二个请求要么 202 `recovery_in_progress`（不调模型），
   要么在**确认旧实例已退出**后接管（generation+1）。模型调用期间由心跳按
   `answer_key+generation+owner` 条件续租（不持写事务、不全局锁）。暂存 / 保存 /
   成功收口 / 失败更新 / 释放全部校验执行权；失去执行权的旧执行者返回
   `{ok:false, code:"recovery_execution_superseded", superseded:true}` 且不改新任务状态。
5. **迟到答案（F13）**：答案写回失败时刻原用户消息所在的**固定片段**，不重开已封存
   片段（closed_at 不变）、不写当前开放片段、不搬动原问题、不新建重复轮次；仍然是
   普通 `messages` 行（+`late_answer_of_message_id` 标记），所以历史加载 / 检索 /
   导出 / 删除清理天然统一，长期只保留一份正文。
6. 恢复前复核导航版本（`recovery_nav_version`）：用户后来的手动选择不会被覆盖；
   恢复本身不推进导航版本。日志/错误文本一律过 `agent/trace/redact.py`。
7. **普通正式回答接入统一答案身份（R17-D / 契约 §2.5）**：:func:`save_normal_answer` 与恢复
   回答共用 :func:`answer_key_for`；正式保存事务内先查既有正式答案（含旧库 answer_key 为空的
   **可靠归属**：同 turn / 同原用户消息 / 该轮恰好一条正式 assistant 候选 / 同片段同话题，
   不满足当作不存在、不猜），已存在则复用原 answer_id、不新增第二条；保存成功与同 answer_key
   的 pending 恢复记录、执行行（answered / answer_id / 清暂存）**同一事务**提交；保存失败先
   写共享暂存（run 行 staged_*，phase=staged）再抛**原异常**，任意同 answer_key 的恢复记录之后
   只补存、不再调用模型；旧库可靠命中时同事务补写 answer_key（默认不新增迁移：SQLite 无
   sha256，运行时懒关联比批量迁移更可靠）。

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
    "ANSWER_RUN_TABLE",
    "EXECUTION_SUPERSEDED_CODE",
    "LEASE_SECONDS",
    "PHASE_WAITING",
    "PHASE_PREPARING",
    "PHASE_GENERATING",
    "PHASE_STAGED",
    "PHASE_ANSWERED",
    "PHASE_FAILED",
    "ALLOWED_PHASES",
    "PHASE_STATUS",
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
    "save_normal_answer",
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
#: 逻辑答案执行权表（迁移 81）：answer_key 主键，同一逻辑答案只有一个执行者。
ANSWER_RUN_TABLE = "topic_answer_runs"
#: 旧执行者迟到写入被拒时的结构化结果码（契约 §6.2.3）。
EXECUTION_SUPERSEDED_CODE = "recovery_execution_superseded"
#: app 上挂**旧内存记录**的属性名（迁移期兼容读取；有 conn 时一律以 SQLite 为准）。
TOPIC_RECOVERY_ATTR = "topic_recovery_records"

#: 单次领取的租约时长（秒）。模型调用期间不持写事务，靠租约防重复生成；
#: 进程崩溃留下的租约到期后可被重新领取。故意给足（生成可能较慢）。
LEASE_SECONDS = 600.0

#: 逻辑答案阶段（迁移 83 列；契约 §6 / §14.6）。phase 与 status 同步写入：
#: waiting→pending / preparing→running / generating→running / staged→pending /
#: answered→answered / failed→pending（可恢复失败，记录保持 pending 可重试）。
PHASE_WAITING = "waiting"
PHASE_PREPARING = "preparing"
PHASE_GENERATING = "generating"
PHASE_STAGED = "staged"
PHASE_ANSWERED = "answered"
PHASE_FAILED = "failed"
ALLOWED_PHASES = (
    PHASE_WAITING,
    PHASE_PREPARING,
    PHASE_GENERATING,
    PHASE_STAGED,
    PHASE_ANSWERED,
    PHASE_FAILED,
)
#: phase → 既有 status 语义（同步写入的唯一口径）。
PHASE_STATUS = {
    PHASE_WAITING: "pending",
    PHASE_PREPARING: "running",
    PHASE_GENERATING: "running",
    PHASE_STAGED: "pending",
    PHASE_ANSWERED: "answered",
    PHASE_FAILED: "pending",
}

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
    "created_at, updated_at, execution_generation, execution_owner, last_heartbeat_at"
)


# ── 小工具 ─────────────────────────────────────────────────────────────


def _now_utc() -> datetime:
    """租约时间的**唯一来源**（契约 §6.2.5 冻结测试缝）。

    测试 monkeypatch 本函数模拟时间推进（领取 / 续租 / 到期 / 接管全部走这里），
    不用 sleep 构造时序。生产路径就是真实 UTC 时钟。
    """
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now_utc().isoformat()


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
        # R15-D / R4（迁移 81）：这条记录当前绑定的执行权（写侧 CAS 校验它）。
        # 老库/半截迁移没有这三列时如实 None/0，不猜。
        "execution_generation": int(_row_value(row, "execution_generation") or 0),
        "execution_owner": _row_value(row, "execution_owner"),
        "last_heartbeat_at": _row_value(row, "last_heartbeat_at"),
    }
    return record


def _row_value(row: sqlite3.Row, key: str, default: Any = None) -> Any:
    """sqlite3.Row 的容错读取：列不存在时返回 default（半截迁移的旧库）。"""
    try:
        return row[key]
    except (IndexError, KeyError):
        return default


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
    return deadline > _now_utc()


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
    注意：这只是**记录级**租约；真正的执行权收口在 :func:`_acquire_execution`
    （按 answer_key 的 `topic_answer_runs`，迁移 81）。
    """
    now_dt = _now_utc()
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


def _release_record_claim_safely(
    conn: sqlite3.Connection, record_id: str, claim_owner: str
) -> None:
    """只释放**记录级**租约（执行权尚未领取 / 已由 release_execution 处理时用）。"""
    try:
        release_claim(conn, record_id, claim_owner=claim_owner)
    except sqlite3.Error as exc:  # noqa: BLE001 - 释放失败由租约过期兜底，不能吞掉主流程
        logger.warning(
            "failed to release topic recovery claim %s: %s",
            record_id,
            _error_detail(exc),
        )


# ── 逻辑答案执行权（迁移 81 / 契约 §6.2）──────────────────────────────
#
# 写入权 = (answer_key, generation, owner)。领取 / 接管都在一个短事务里对
# topic_answer_runs 做 CAS；模型调用期间**不持有** SQLite 写事务，靠心跳条件 UPDATE
# 续租（answer_key + generation + owner）。旧执行者的任何迟到写入先校验执行权，
# 失败即放弃（返回 recovery_execution_superseded），绝不改新任务状态。


class _ExecutionLost(RuntimeError):
    """当前执行者已经失去执行权（被接管 / 世代被推进）：必须放弃写入。"""


class _RecordChanged(RuntimeError):
    """领取执行权后记录状态又被别人改掉（已完成 / 被别的执行者绑定）。"""


def _run_table_available(conn: sqlite3.Connection) -> bool:
    try:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (ANSWER_RUN_TABLE,),
        ).fetchone()
    except sqlite3.Error:  # noqa: BLE001 - 读不动按「不可用」处理（如实 503，不假成功）
        return False
    return row is not None


#: 执行权行读取列（迁移 83 追加 phase / staged_*；暂存权威就在这一行，按 answer_key）。
_EXECUTION_COLUMNS = (
    "answer_key, status, generation, owner, owner_instance, claim_until, "
    "record_id, answer_id, created_at, updated_at, "
    "phase, staged_answer, staged_at, staged_model"
)


def _execution_row(
    conn: sqlite3.Connection, answer_key: str
) -> sqlite3.Row | None:
    return conn.execute(
        f"SELECT {_EXECUTION_COLUMNS} FROM {ANSWER_RUN_TABLE} WHERE answer_key = ?",
        (str(answer_key),),
    ).fetchone()


def _row_to_execution(row: sqlite3.Row) -> dict:
    return {
        "answer_key": str(row["answer_key"]),
        "status": str(row["status"] or "pending"),
        "generation": int(row["generation"] or 0),
        "owner": row["owner"],
        "owner_instance": row["owner_instance"],
        "claim_until": row["claim_until"],
        "record_id": row["record_id"],
        "answer_id": row["answer_id"],
        # 迁移 83：phase 与共享暂存（唯一权威）。旧库行由迁移回填 phase。
        "phase": str(_row_value(row, "phase") or PHASE_WAITING),
        "staged_answer": str(_row_value(row, "staged_answer") or ""),
        "staged_at": _row_value(row, "staged_at"),
        "staged_model": _row_value(row, "staged_model"),
    }


def _ensure_execution_row(
    conn: sqlite3.Connection, answer_key: str
) -> sqlite3.Row | None:
    """在**调用方事务内**取 / 建执行权行（answer_key 主键，并发首建幂等）。

    先 `INSERT OR IGNORE`（写锁用最短事务拿到，避免「先读后写」在 WAL 下撞
    snapshot 升级），再读回行；既有行时这条语句是无害空操作。
    """
    now = _now_iso()
    conn.execute(
        f"INSERT OR IGNORE INTO {ANSWER_RUN_TABLE} "
        "(answer_key, status, generation, owner, owner_instance, claim_until, "
        "record_id, answer_id, created_at, updated_at, phase) "
        "VALUES (?, 'pending', 0, NULL, NULL, NULL, NULL, NULL, ?, ?, ?)",
        (str(answer_key), now, now, PHASE_WAITING),
    )
    return _execution_row(conn, answer_key)


def _instance_registry(ctx: Any) -> Any:
    """复用 ctx 上既有的 InstanceRegistry（不引入第二套存活机制）。"""
    for attr in ("instance_registry", "instances"):
        registry = getattr(ctx, attr, None)
        if registry is not None and callable(getattr(registry, "owner_alive", None)):
            return registry
    return None


def _owner_alive(ctx: Any, instance_id: Any) -> bool | None:
    """归属实例是否还活着（True 活 / False 确认已退出 / None 判不出来）。

    判不出来一律不接管（与 InstanceRegistry.owner_alive 的「unknown 不改状态」一致）。
    """
    if not instance_id:
        return None
    registry = _instance_registry(ctx)
    if registry is None:
        return None
    try:
        return registry.owner_alive(str(instance_id))
    except Exception:  # noqa: BLE001 - 存活判定失败按 unknown 处理，不误接管
        logger.warning("failed to judge execution owner alive", exc_info=True)
        return None


def _acquire_execution(
    conn: sqlite3.Connection,
    ctx: Any,
    *,
    answer_key: str,
    owner: str,
    lease_seconds: float,
) -> tuple[dict | None, str]:
    """按 answer_key 原子领取 / 接管逻辑答案执行权。

    返回 `(execution, disposition)`：
    * `"acquired"`：拿到执行权（generation 已 +1）；
    * `"in_progress"`：别人的执行权有效，或租约到期但旧实例存活性判不出来（不接管）；
    * `"answered"`：执行权已收口为 answered（调用方补记录收口）；
    * `"unavailable"`：执行权表不可用 / 读失败（如实 503）。
    """
    now = _now_utc()
    now_iso = now.isoformat()
    try:
        with transaction(conn):
            row = _ensure_execution_row(conn, answer_key)
            if row is None:
                return None, "unavailable"
            current = _row_to_execution(row)
            if current["status"] == "answered":
                return None, "answered"
            if current["status"] == "running" and current["owner"]:
                deadline = _parse_ts(current.get("claim_until"))
                if deadline is not None and deadline > now:
                    return None, "in_progress"
                if deadline is None:
                    # 连到期时间都读不出来：无法证明「租约已到期」，按 unknown 不接管。
                    return None, "in_progress"
                # 租约到期：只有能**证明旧实例已失效**（owner_alive is False）才接管；
                # True（还活着，可能心跳暂缓）与 None（判不出来）都不接管。
                if _owner_alive(ctx, current["owner_instance"]) is not False:
                    return None, "in_progress"
            generation = int(current["generation"] or 0) + 1
            until = (now + timedelta(seconds=float(lease_seconds))).isoformat()
            cursor = conn.execute(
                f"UPDATE {ANSWER_RUN_TABLE} SET status = 'running', phase = ?, "
                "generation = ?, owner = ?, owner_instance = ?, claim_until = ?, "
                "record_id = NULL, updated_at = ? WHERE answer_key = ? AND generation = ? "
                "AND status != 'answered'",
                (
                    PHASE_PREPARING,
                    generation,
                    str(owner),
                    _owner_instance(owner),
                    until,
                    now_iso,
                    str(answer_key),
                    int(current["generation"] or 0),
                ),
            )
            if cursor.rowcount != 1:
                return None, "in_progress"
            execution = {
                "answer_key": str(answer_key),
                "generation": generation,
                "owner": str(owner),
                "owner_instance": _owner_instance(owner),
                "lease_seconds": float(lease_seconds),
                "record_id": "",
            }
            return execution, "acquired"
    except sqlite3.Error as exc:  # noqa: BLE001 - 执行权表读/写失败：如实不可用
        logger.warning(
            "failed to acquire topic answer execution right: %s", _error_detail(exc)
        )
        return None, "unavailable"


def _bind_record_execution(
    conn: sqlite3.Connection,
    execution: dict,
    record_id: str,
) -> None:
    """把执行权令牌绑定到恢复记录（记录侧写操作之后都 CAS 它）。

    同一事务里同时更新 `topic_answer_runs.record_id` 与记录的
    `execution_generation/execution_owner/last_heartbeat_at`。
    执行权已丢 -> :class:`_ExecutionLost`；记录状态已变 -> :class:`_RecordChanged`。
    """
    now = _now_utc()
    now_iso = now.isoformat()
    until = (now + timedelta(seconds=float(execution["lease_seconds"]))).isoformat()
    with transaction(conn):
        run_cursor = conn.execute(
            f"UPDATE {ANSWER_RUN_TABLE} SET record_id = ?, updated_at = ? "
            "WHERE answer_key = ? AND generation = ? AND owner = ? "
            "AND status = 'running'",
            (
                str(record_id),
                now_iso,
                execution["answer_key"],
                execution["generation"],
                execution["owner"],
            ),
        )
        if run_cursor.rowcount != 1:
            raise _ExecutionLost(str(record_id))
        record_cursor = conn.execute(
            f"UPDATE {TOPIC_RECOVERY_TABLE} SET execution_generation = ?, "
            "execution_owner = ?, last_heartbeat_at = ?, claim_owner = ?, "
            "claim_until = ?, updated_at = ?, answer_key = COALESCE(answer_key, ?) "
            "WHERE record_id = ? AND status = 'pending'",
            (
                execution["generation"],
                execution["owner"],
                now_iso,
                execution["owner"],
                until,
                now_iso,
                execution["answer_key"],
                str(record_id),
            ),
        )
        if record_cursor.rowcount != 1:
            raise _RecordChanged(str(record_id))


def _renew_execution(conn: sqlite3.Connection, execution: dict) -> bool:
    """心跳续租（条件 UPDATE answer_key + generation + owner）。

    **不持有** SQLite 写事务跨越模型调用：这里只是一个短路事务。续租失败说明
    执行权已被接管（或 run 行已收口），返回 False，心跳任务退出。
    """
    now = _now_utc()
    now_iso = now.isoformat()
    until = (now + timedelta(seconds=float(execution["lease_seconds"]))).isoformat()
    with transaction(conn):
        run_cursor = conn.execute(
            f"UPDATE {ANSWER_RUN_TABLE} SET claim_until = ?, updated_at = ? "
            "WHERE answer_key = ? AND generation = ? AND owner = ? "
            "AND status = 'running'",
            (
                until,
                now_iso,
                execution["answer_key"],
                execution["generation"],
                execution["owner"],
            ),
        )
        if run_cursor.rowcount != 1:
            return False
        conn.execute(
            f"UPDATE {TOPIC_RECOVERY_TABLE} SET claim_until = ?, "
            "last_heartbeat_at = ?, updated_at = ? WHERE record_id = ? "
            "AND status = 'pending' AND claim_owner = ? "
            "AND execution_owner = ? AND execution_generation = ?",
            (
                until,
                now_iso,
                now_iso,
                str(execution.get("record_id") or ""),
                execution["owner"],
                execution["owner"],
                execution["generation"],
            ),
        )
    return True


def _release_execution(
    conn: sqlite3.Connection,
    execution: dict,
    *,
    last_error: str | None = None,
    failed_stage: str | None = None,
    stages: dict | None = None,
    phase: str = PHASE_FAILED,
) -> bool:
    """释放执行权（CAS：只释放自己的；已接管时不动新执行者的租约）。

    失败事实只在记录仍 `pending` 且执行权仍属于自己时写；返回 False = 已失去
    执行权（旧执行者的迟到释放不得清新任务租约 / 改写新任务状态）。
    """
    now = _now_iso()
    record_id = str(execution.get("record_id") or "")
    assignments = [
        "claim_owner = NULL",
        "claim_until = NULL",
        "execution_owner = NULL",
        "updated_at = ?",
    ]
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
    params.extend([record_id, execution["owner"], execution["generation"]])
    release_phase = str(phase or PHASE_FAILED)
    if release_phase not in (PHASE_FAILED, PHASE_STAGED):
        raise ValueError(f"释放阶段只能是 failed / staged：{phase!r}")
    with transaction(conn):
        run_cursor = conn.execute(
            f"UPDATE {ANSWER_RUN_TABLE} SET status = 'pending', phase = ?, owner = NULL, "
            "owner_instance = NULL, claim_until = NULL, updated_at = ? "
            "WHERE answer_key = ? AND generation = ? AND owner = ? "
            "AND status = 'running'",
            (
                release_phase,
                now,
                execution["answer_key"],
                execution["generation"],
                execution["owner"],
            ),
        )
        if run_cursor.rowcount != 1:
            return False
        conn.execute(
            f"UPDATE {TOPIC_RECOVERY_TABLE} SET {', '.join(assignments)} "
            "WHERE record_id = ? AND status = 'pending' AND execution_owner = ? "
            "AND execution_generation = ?",
            params,
        )
    return True


def _release_execution_safely(
    conn: sqlite3.Connection,
    execution: dict,
    *,
    last_error: str | None = None,
    failed_stage: str | None = None,
    stages: dict | None = None,
    phase: str = PHASE_FAILED,
) -> bool:
    try:
        return _release_execution(
            conn,
            execution,
            last_error=last_error,
            failed_stage=failed_stage,
            stages=stages,
            phase=phase,
        )
    except sqlite3.Error as exc:  # noqa: BLE001 - 释放失败由租约过期兜底，不能吞主流程
        logger.warning(
            "failed to release topic answer execution right: %s", _error_detail(exc)
        )
        return False


def _execution_still_owned(conn: sqlite3.Connection, execution: dict) -> bool:
    """在**调用方事务内**校验执行权仍然属于自己（run 行 + 记录侧令牌一致）。"""
    row = conn.execute(
        f"SELECT status, generation, owner FROM {ANSWER_RUN_TABLE} "
        "WHERE answer_key = ?",
        (execution["answer_key"],),
    ).fetchone()
    if row is None:
        return False
    if str(row["status"]) != "running":
        return False
    if int(row["generation"] or 0) != int(execution["generation"]):
        return False
    if str(row["owner"] or "") != str(execution["owner"]):
        return False
    record_id = str(execution.get("record_id") or "")
    if record_id:
        record = conn.execute(
            f"SELECT status, execution_generation, execution_owner "
            f"FROM {TOPIC_RECOVERY_TABLE} WHERE record_id = ?",
            (record_id,),
        ).fetchone()
        if record is None or str(record["status"]) != "pending":
            return False
        if int(record["execution_generation"] or 0) != int(execution["generation"]):
            return False
        if str(record["execution_owner"] or "") != str(execution["owner"]):
            return False
    return True


def _run_staged_answer(conn: sqlite3.Connection, answer_key: str) -> str:
    """共享暂存正文（run 行 = 唯一权威；没有返回空串）。"""
    row = _execution_row(conn, answer_key)
    if row is None:
        return ""
    return str(_row_value(row, "staged_answer") or "")


def _record_legacy_answer_text(conn: sqlite3.Connection, record_id: str) -> str:
    """旧版本残留的暂存正文（只读兜底；不是新暂存的权威）。"""
    row = conn.execute(
        f"SELECT answer_text FROM {TOPIC_RECOVERY_TABLE} WHERE record_id = ?",
        (str(record_id or ""),),
    ).fetchone()
    return str(row["answer_text"] or "") if row is not None else ""


def _migrate_legacy_stash(
    conn: sqlite3.Connection, execution: dict, record_id: str, answer: str
) -> None:
    """旧 record.answer_text -> run.staged_answer 的**同事务**转写，并清记录列。

    保证迁移期暂存只有一份权威；执行权已丢 -> _ExecutionLost（零行改动，I11）。
    """
    now = _now_iso()
    with transaction(conn):
        if not _execution_still_owned(conn, execution):
            raise _ExecutionLost(str(record_id))
        run_cursor = conn.execute(
            f"UPDATE {ANSWER_RUN_TABLE} SET staged_answer = ?, staged_at = ?, "
            "staged_model = NULL, updated_at = ? "
            "WHERE answer_key = ? AND generation = ? AND owner = ? AND status = 'running'",
            (
                str(answer or ""),
                now,
                now,
                execution["answer_key"],
                execution["generation"],
                execution["owner"],
            ),
        )
        if run_cursor.rowcount != 1:
            raise _ExecutionLost(str(record_id))
        record_cursor = conn.execute(
            f"UPDATE {TOPIC_RECOVERY_TABLE} SET answer_text = '', updated_at = ? "
            "WHERE record_id = ? AND status = 'pending' AND execution_owner = ? "
            "AND execution_generation = ?",
            (now, str(record_id), execution["owner"], execution["generation"]),
        )
        if record_cursor.rowcount != 1:
            raise _ExecutionLost(str(record_id))


def _set_active_phase(
    conn: sqlite3.Connection, execution: dict, phase: str
) -> bool:
    """执行中阶段推进（preparing / generating）：CAS 执行权，失败 False（I11）。

    只用于 status=running 的执行中阶段；终态（staged/answered/failed）由
    暂存 / 收口 / 释放路径与 status 一起原子写入（见 PHASE_STATUS）。
    """
    if str(phase) not in (PHASE_PREPARING, PHASE_GENERATING):
        raise ValueError(f"不是执行中阶段：{phase!r}")
    with transaction(conn):
        cursor = conn.execute(
            f"UPDATE {ANSWER_RUN_TABLE} SET phase = ?, updated_at = ? "
            "WHERE answer_key = ? AND generation = ? AND owner = ? AND status = 'running'",
            (
                str(phase),
                _now_iso(),
                execution["answer_key"],
                execution["generation"],
                execution["owner"],
            ),
        )
    return cursor.rowcount == 1


def _finalize_answer_locked(
    conn: sqlite3.Connection, execution: dict, answer_id: str
) -> None:
    """在**调用方事务内**收口：所有同 answer_key 的 pending 记录 + run 行置 answered。

    保存事务内先校验执行权（:func:`_execution_still_owned`），再写答案行（调用方），
    然后这里把记录与执行权一起收口；任何一步失败整体回滚。
    """
    answer_key = str(execution["answer_key"])
    pending = conn.execute(
        f"SELECT record_id FROM {TOPIC_RECOVERY_TABLE} "
        "WHERE status = 'pending' AND answer_key = ?",
        (answer_key,),
    ).fetchall()
    for row in pending:
        _mark_answered_row(conn, str(row["record_id"]), str(answer_id))
    now = _now_iso()
    cursor = conn.execute(
        f"UPDATE {ANSWER_RUN_TABLE} SET status = 'answered', phase = ?, answer_id = ?, "
        "staged_answer = '', staged_at = NULL, staged_model = NULL, "
        "owner = NULL, owner_instance = NULL, claim_until = NULL, updated_at = ? "
        "WHERE answer_key = ? AND generation = ? AND owner = ? "
        "AND status = 'running'",
        (
            PHASE_ANSWERED,
            str(answer_id),
            now,
            answer_key,
            execution["generation"],
            execution["owner"],
        ),
    )
    if cursor.rowcount != 1:
        raise _ExecutionLost(answer_key)


def _finalize_answered_answer_key(
    conn: sqlite3.Connection, answer_key: str
) -> str | None:
    """执行权已 answered：把仍 pending 的同 answer_key 记录补收口（只读/补写记录）。"""
    row = _execution_row(conn, answer_key)
    if row is None or str(row["status"] or "") != "answered":
        return None
    answer_id = row["answer_id"]
    if not answer_id:
        return None
    with transaction(conn):
        pending = conn.execute(
            f"SELECT record_id FROM {TOPIC_RECOVERY_TABLE} "
            "WHERE status = 'pending' AND answer_key = ?",
            (str(answer_key),),
        ).fetchall()
        for item in pending:
            _mark_answered_row(conn, str(item["record_id"]), str(answer_id))
    return str(answer_id)


def _start_execution_heartbeat(
    conn: sqlite3.Connection,
    execution: dict,
    *,
    interval: float,
    wait: Any = None,
) -> tuple[asyncio.Task, asyncio.Event]:
    """起心跳任务：每 `interval` 秒（或由测试 `wait` 事件驱动）续租一次。

    * 生产不传 `wait`：用 `asyncio.wait_for(stop.wait(), timeout=interval)` 计时；
    * 测试传 `heartbeat_wait`（async () -> None）：每次续租前调用，用事件控制节奏，
      不依赖 sleep。
    """
    stop = asyncio.Event()

    async def _loop() -> None:
        while not stop.is_set():
            if wait is not None:
                await wait()
            else:
                try:
                    await asyncio.wait_for(stop.wait(), timeout=float(interval))
                    return
                except asyncio.TimeoutError:
                    pass
            if stop.is_set():
                return
            try:
                renewed = _renew_execution(conn, execution)
            except sqlite3.Error as exc:  # noqa: BLE001 - 续租失败记日志，主流程 CAS 兜底
                logger.warning(
                    "topic answer lease heartbeat failed: %s", _error_detail(exc)
                )
                renewed = True
            if not renewed:
                # 执行权已被接管 / 已收口：心跳退出，主流程的写入会被 CAS 拒绝。
                return
        return

    return asyncio.create_task(_loop(), name="topic-answer-lease-heartbeat"), stop


async def _stop_execution_heartbeat(
    task: asyncio.Task | None, stop: asyncio.Event | None
) -> None:
    if task is None:
        return
    if stop is not None:
        stop.set()
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):  # noqa: BLE001 - 收尾不掩盖主结果
        pass


def _superseded_result(record_id: str) -> dict:
    """旧执行者迟到写入被拒：不改任何行，如实说明执行权已被接管。"""
    return {
        "ok": False,
        "superseded": True,
        "code": EXECUTION_SUPERSEDED_CODE,
        "message": (
            "这条逻辑答案已由另一执行者接管；本次迟到的生成结果已放弃写入，"
            "不会覆盖新任务的状态、暂存结果或租约。"
        ),
        "record_id": str(record_id or ""),
        "status_code": 409,
    }


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


def _answer_attribution_problem(
    conn: sqlite3.Connection, record: dict
) -> tuple[str, str] | None:
    """I14 归属前提复核：固定片段 / 话题 / 原用户消息 / 原轮次必须一致。

    返回 (code, detail) 或 None；code 冻结为 answer_attribution_mismatch（契约 §14.6）。
    只读；不猜归属、不搬动原问题。
    """
    topic_id = str(record.get("topic_id") or "")
    fragment_id = record.get("fragment_id") or _lookup_fragment_for_message(
        conn, record.get("user_message_id")
    )
    if not fragment_id:
        return (
            "answer_attribution_mismatch",
            "恢复记录没有固定片段（缺少 fragment_id 且原用户消息不存在）",
        )
    fragment = _fragment_row(conn, str(fragment_id))
    if fragment is None:
        return ("answer_attribution_mismatch", f"固定片段不存在：{fragment_id}")
    if str(fragment["topic_id"]) != topic_id:
        return (
            "answer_attribution_mismatch",
            f"固定片段 {fragment_id} 不属于话题 {topic_id}",
        )
    user_message_id = record.get("user_message_id")
    if user_message_id:
        row = conn.execute(
            "SELECT fragment_id, turn_id FROM messages WHERE id = ?",
            (str(user_message_id),),
        ).fetchone()
        if row is None:
            return (
                "answer_attribution_mismatch",
                f"原用户消息不存在：{user_message_id}",
            )
        message_fragment = row["fragment_id"]
        if message_fragment and str(message_fragment) != str(fragment_id):
            return ("answer_attribution_mismatch", "原用户消息不属于固定片段")
        record_turn = record.get("turn_id")
        message_turn = row["turn_id"]
        if message_turn and record_turn and str(message_turn) != str(record_turn):
            return ("answer_attribution_mismatch", "原用户消息不属于记录轮次")
    return None


#: 既有正式答案读取列（R17-D：统一答案身份需要轮次归属做一致性复核）。
_ANSWER_ROW_COLUMNS = "id, content, turn_id, answer_key"


def _find_answer_by_key(
    conn: sqlite3.Connection, answer_key: str | None
) -> sqlite3.Row | None:
    if not answer_key:
        return None
    return conn.execute(
        f"SELECT {_ANSWER_ROW_COLUMNS} FROM messages WHERE answer_key = ? LIMIT 1",
        (str(answer_key),),
    ).fetchone()


def _messages_answer_key_available(conn: sqlite3.Connection) -> bool:
    """messages.answer_key（迁移 79）是否可用。

    R17 契约 §14.11：迁移列缺失时**不得静默降级**（不能假装有统一答案身份），
    这里只给调用方一个明确的 False，由调用方抛可恢复错误。
    """
    try:
        rows = conn.execute("PRAGMA table_info(messages)").fetchall()
    except sqlite3.Error:  # noqa: BLE001 - 读不动同样按「不可用」处理
        return False
    return any(str(row[1]) == "answer_key" for row in rows)


def _reliable_legacy_answer(
    conn: sqlite3.Connection,
    *,
    topic_id: str,
    turn_id: str,
    user_message_id: str | None,
    fragment_id: str | None = None,
) -> sqlite3.Row | None:
    """旧库 answer_key 为空的历史正式回答：只有**可靠归属**才返回候选行，否则 None。

    R17 契约 §2.5 D4 的必要条件（全部满足才关联，不猜）：
    * 原用户消息存在、属于同一轮次、属于目标话题的同一片段；
    * 显式 fragment_id 必须与原用户消息所在片段一致；
    * 该轮**恰好一条**正式 assistant 候选（answer_key 为空、且不是迟到答案补写行）；
    * 该轮没有任何带 answer_key 的答案（已有其它逻辑答案 -> 不关联，避免双份正文）。
    """
    user_message = str(user_message_id or "")
    turn = str(turn_id or "")
    if not user_message or not turn:
        return None
    user_row = conn.execute(
        "SELECT fragment_id, turn_id FROM messages WHERE id = ? AND role = 'user'",
        (user_message,),
    ).fetchone()
    if user_row is None:
        return None
    if str(user_row["turn_id"] or "") != turn:
        return None
    user_fragment = str(user_row["fragment_id"] or "")
    expected_fragment = str(fragment_id or "") or user_fragment
    if not expected_fragment or expected_fragment != user_fragment:
        return None
    fragment = _fragment_row(conn, expected_fragment)
    if fragment is None or str(fragment["topic_id"]) != str(topic_id or ""):
        return None
    keyed = conn.execute(
        "SELECT 1 FROM messages WHERE turn_id = ? AND answer_key IS NOT NULL LIMIT 1",
        (turn,),
    ).fetchone()
    if keyed is not None:
        return None
    candidates = conn.execute(
        "SELECT id, content FROM messages WHERE role = 'assistant' AND turn_id = ? "
        "AND answer_key IS NULL AND late_answer_of_message_id IS NULL",
        (turn,),
    ).fetchall()
    if len(candidates) != 1:
        return None
    return candidates[0]


def _backfill_legacy_answer_key(
    conn: sqlite3.Connection, answer_key: str, message_id: str
) -> str:
    """旧库可靠命中：在**调用方事务内**只补 messages.answer_key 一列（不动正文/归属）。

    并发唯一性仍由 messages.answer_key 部分唯一索引兜底：撞唯一约束时回查既有一份。
    """
    try:
        cursor = conn.execute(
            "UPDATE messages SET answer_key = ? WHERE id = ? AND answer_key IS NULL",
            (str(answer_key), str(message_id)),
        )
    except sqlite3.IntegrityError:
        existing = _find_answer_by_key(conn, answer_key)
        if existing is None:
            raise
        return str(existing["id"])
    if cursor.rowcount != 1:
        existing = _find_answer_by_key(conn, answer_key)
        if existing is None:
            raise RecoveryPersistError(
                "旧库正式答案补写 answer_key 失败（行已被并发改动，不猜）"
            )
        return str(existing["id"])
    return str(message_id)


def _resolve_existing_answer(
    conn: sqlite3.Connection,
    answer_key: str,
    *,
    topic_id: str,
    turn_id: str,
    user_message_id: str | None,
    fragment_id: str | None = None,
    backfill: bool = True,
) -> sqlite3.Row | None:
    """既有正式答案的统一查找（普通保存与恢复路径共用，R17 契约 §2.5 / I12）。

    * 先按 answer_key 查（新代码路径）；
    * 再按旧库可靠归属规则查（answer_key 为空的历史答案）；backfill=True 时
      在**调用方事务内**补写 answer_key；
    * 两条都不成立 -> None（不猜）。
    """
    row = _find_answer_by_key(conn, answer_key)
    if row is not None:
        return row
    legacy = _reliable_legacy_answer(
        conn,
        topic_id=topic_id,
        turn_id=turn_id,
        user_message_id=user_message_id,
        fragment_id=fragment_id,
    )
    if legacy is None:
        return None
    if not backfill:
        return legacy
    message_id = _backfill_legacy_answer_key(conn, answer_key, str(legacy["id"]))
    return _find_answer_by_key(conn, answer_key) or conn.execute(
        f"SELECT {_ANSWER_ROW_COLUMNS} FROM messages WHERE id = ?", (str(message_id),)
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
    """事务内收口：记录 answered + answer_id，清空暂存正文与租约。

    调用方保证在**同一事务**里先完成执行权校验（收口是唯一允许跨记录写的路径：
    所有同 answer_key 的 pending 记录一起收口）。`execution_generation` 保留为
    审计值，`execution_owner` 清空表示当前没有执行者持有写入权。
    """
    conn.execute(
        f"UPDATE {TOPIC_RECOVERY_TABLE} SET status = 'answered', answer_id = ?, "
        "answer_text = '', failed_stage = '', last_error = NULL, "
        "claim_owner = NULL, claim_until = NULL, execution_owner = NULL, "
        "updated_at = ? WHERE record_id = ?",
        (str(answer_id), _now_iso(), str(record_id or "")),
    )


def _save_answer(
    conn: sqlite3.Connection,
    record: dict,
    answer: str,
    *,
    model: str | None,
    execution: dict,
) -> dict:
    """把答案写入唯一权威位置，并在**同一事务**把记录置 answered。

    事务内第一步再校验一次执行权（契约 §6.2.7）：旧执行者的迟到保存 / 接管与保存
    竞争时只会整体回滚（抛 :class:`_ExecutionLost`），不会覆盖新执行权状态。
    """
    answer_key = str(execution["answer_key"])
    stored_answer = answer
    with transaction(conn):
        if not _execution_still_owned(conn, execution):
            raise _ExecutionLost(answer_key)
        # R17-D：既有答案查找与普通正式保存共用同一套可靠规则（含旧库空 answer_key 的
        # 可靠归属 + 同事务补写）；共享暂存 / 写入权校验语义不变（I10 / I11 / I12）。
        row = _resolve_existing_answer(
            conn,
            answer_key,
            topic_id=str(record.get("topic_id") or ""),
            turn_id=str(record.get("turn_id") or ""),
            user_message_id=record.get("user_message_id"),
            fragment_id=record.get("fragment_id"),
        )
        if row is not None:
            message_id = str(row["id"])
            stored_answer = str(row["content"] or answer)
        else:
            # I14：写入前再复核一次归属前提（补存路径也走这里）；不成立 -> 结构化拒绝。
            problem = _answer_attribution_problem(conn, record)
            if problem is not None:
                raise RecoveryPersistError(problem[1])
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
        _finalize_answer_locked(conn, execution, message_id)
    return {"answer_id": message_id, "answer": stored_answer}


# ── 普通正式回答：统一答案身份保存入口（R17-D / P2-4，契约 §2.5）────────


def _validate_normal_answer_attribution(
    conn: sqlite3.Connection,
    *,
    topic_id: str,
    turn_id: str,
    user_message_id: str | None,
    fragment_id: str | None,
) -> None:
    """I14：普通正式回答落库前的归属前提（原轮次 / 原用户消息 / 片段 / 话题一致）。

    不成立时**不得**伪装成正式回答落库：抛 RecoveryPersistError（可恢复，正文由调用方
    写入共享暂存，之后任意恢复记录只补存）。不猜归属、不搬原问题。
    """
    user_message = str(user_message_id or "")
    if not user_message:
        return
    row = conn.execute(
        "SELECT role, fragment_id, turn_id FROM messages WHERE id = ?",
        (user_message,),
    ).fetchone()
    if row is None:
        raise RecoveryPersistError("普通回答的原用户消息不存在（不猜归属）")
    if str(row["role"]) != "user":
        raise RecoveryPersistError("普通回答的原用户消息不是 user 角色（不猜归属）")
    message_turn = str(row["turn_id"] or "")
    if message_turn and str(turn_id or "") and message_turn != str(turn_id):
        raise RecoveryPersistError("普通回答的原用户消息不属于该轮次（不猜归属）")
    message_fragment = str(row["fragment_id"] or "")
    if fragment_id and message_fragment and str(fragment_id) != message_fragment:
        raise RecoveryPersistError("普通回答显式片段与原用户消息所在片段不一致（不猜归属）")
    if not message_fragment:
        raise RecoveryPersistError("普通回答的原用户消息没有片段归属（不猜归属）")
    fragment = _fragment_row(conn, message_fragment)
    if fragment is None:
        raise RecoveryPersistError("普通回答的原用户消息片段不存在（不猜归属）")
    if topic_id and str(fragment["topic_id"]) != str(topic_id):
        raise RecoveryPersistError("普通回答的原用户消息不属于目标话题（不猜归属）")


def _insert_normal_answer_message(
    conn: sqlite3.Connection,
    *,
    memory: Any,
    topic_id: str,
    turn_id: str,
    content: str,
    content_type: str,
    model: str | None,
    raw: dict | None,
    fragment_id: str | None,
    answer_key: str,
) -> str:
    """按普通回答原有语义写一条 assistant 消息：片段由轮次绑定解析 + 统一 answer_key。

    仍走 MemoryWriter（归属 / 绑定 / 片段边界与旧行为一致），只额外携带 answer_key。
    """
    memory_conn = getattr(memory, "conn", None)
    if memory_conn is not None and memory_conn is not conn:
        raise RecoveryPersistError("memory 写入通道与当前数据库连接不一致（拒绝写入）")
    message_id, _closed = memory.append_message(
        topic_id=str(topic_id),
        role="assistant",
        content=str(content or ""),
        content_type=str(content_type or "text"),
        model=model,
        raw=raw,
        fragment_id=fragment_id,
        turn_id=str(turn_id or "") or None,
        answer_key=str(answer_key),
    )
    return str(message_id)


def _finalize_normal_answer_locked(
    conn: sqlite3.Connection, answer_key: str, answer_id: str
) -> list[str]:
    """在**调用方事务内**收口：同 answer_key 的 pending 恢复记录 + 执行行置 answered。

    普通正式回答也接入同一答案身份（I12）：成功后不得留下同 answer_key 的 pending
    恢复记录或 running 执行行；旧执行者随后迟到的写入由写入权 CAS 拒绝（I11）。
    """
    closed_record_ids: list[str] = []
    if _table_available(conn):
        pending = conn.execute(
            f"SELECT record_id FROM {TOPIC_RECOVERY_TABLE} "
            "WHERE status = 'pending' AND answer_key = ? ORDER BY record_id",
            (str(answer_key),),
        ).fetchall()
        for row in pending:
            record_id = str(row["record_id"])
            _mark_answered_row(conn, record_id, str(answer_id))
            closed_record_ids.append(record_id)
    if _run_table_available(conn):
        # 执行行以 answer_key 为主键：普通回答也留下「这份逻辑答案已收口」的持久事实，
        # 之后的任意恢复入口直接拿到 answered / answer_id，不再尝试生成或补存。
        _ensure_execution_row(conn, answer_key)
        conn.execute(
            f"UPDATE {ANSWER_RUN_TABLE} SET status = 'answered', phase = ?, answer_id = ?, "
            "staged_answer = '', staged_at = NULL, staged_model = NULL, owner = NULL, "
            "owner_instance = NULL, claim_until = NULL, updated_at = ? "
            "WHERE answer_key = ?",
            (PHASE_ANSWERED, str(answer_id), _now_iso(), str(answer_key)),
        )
    return closed_record_ids


def _save_normal_answer_locked(
    conn: sqlite3.Connection,
    memory: Any,
    *,
    answer_key: str,
    topic_id: str,
    turn_id: str,
    user_message_id: str | None,
    content: str,
    content_type: str,
    model: str | None,
    raw: dict | None,
    fragment_id: str | None,
) -> dict:
    """正式保存事务：唯一答案写入 / 复用 + 记录与执行行收口，全部原子提交。"""
    created = False
    with transaction(conn):
        row = _resolve_existing_answer(
            conn,
            answer_key,
            topic_id=str(topic_id),
            turn_id=str(turn_id or ""),
            user_message_id=user_message_id,
            fragment_id=fragment_id,
        )
        if row is not None:
            stored_turn = str(row["turn_id"] or "")
            if stored_turn and str(turn_id or "") and stored_turn != str(turn_id):
                raise RecoveryPersistError(
                    "既有逻辑答案的轮次归属与本次保存不一致（拒绝复用，避免错挂）"
                )
            message_id = str(row["id"])
        else:
            _validate_normal_answer_attribution(
                conn,
                topic_id=str(topic_id),
                turn_id=str(turn_id or ""),
                user_message_id=user_message_id,
                fragment_id=fragment_id,
            )
            try:
                message_id = _insert_normal_answer_message(
                    conn,
                    memory=memory,
                    topic_id=topic_id,
                    turn_id=str(turn_id or ""),
                    content=content,
                    content_type=content_type,
                    model=model,
                    raw=raw,
                    fragment_id=fragment_id,
                    answer_key=answer_key,
                )
                created = True
            except sqlite3.IntegrityError:
                # 并发下另一执行者先写入同一逻辑答案：部分唯一索引保证只有一份正文，
                # 回查既有行复用（不新增第二条）。
                row = _find_answer_by_key(conn, answer_key)
                if row is None:
                    raise
                message_id = str(row["id"])
        closed_record_ids = _finalize_normal_answer_locked(
            conn, answer_key, message_id
        )
    return {
        "answer_id": str(message_id),
        "created": created,
        "existing": not created,
        "closed_record_ids": closed_record_ids,
        "answer_key": str(answer_key),
        "user_message_id": user_message_id,
    }


def _stash_normal_answer_failure(
    conn: sqlite3.Connection,
    *,
    answer_key: str,
    answer: str,
    failure: str,
    model: str | None,
) -> bool:
    """普通保存失败：把正文写进**共享暂存唯一权威**（run 行）后由调用方抛出原异常。

    返回 False = 没有安全写入（执行行已 answered / 另一个活跃执行者持有写入权 /
    执行权表不可用）：这时**不覆盖**别人的暂存与租约（I11），调用方仍抛原异常。
    """
    if not _run_table_available(conn):
        return False
    now = _now_iso()
    with transaction(conn):
        row = _ensure_execution_row(conn, answer_key)
        if row is None:
            return False
        current = _row_to_execution(row)
        if current["status"] == "answered":
            return False
        if current["status"] == "running" and current["owner"]:
            logger.warning(
                "save_normal_answer: 跳过暂存（answer_key=%s 有活跃执行者持有写入权）：%s",
                str(answer_key)[:12],
                str(failure or "")[:200],
            )
            return False
        cursor = conn.execute(
            f"UPDATE {ANSWER_RUN_TABLE} SET status = 'pending', phase = ?, "
            "staged_answer = ?, staged_at = ?, staged_model = ?, owner = NULL, "
            "owner_instance = NULL, claim_until = NULL, updated_at = ? "
            "WHERE answer_key = ? AND status != 'answered'",
            (PHASE_STAGED, str(answer or ""), now, model, now, str(answer_key)),
        )
        if cursor.rowcount != 1:
            return False
        if _table_available(conn):
            # 记录列 answer_text 不再是新暂存的权威：清掉迁移期残留，保证只有一份权威。
            conn.execute(
                f"UPDATE {TOPIC_RECOVERY_TABLE} SET answer_text = '', updated_at = ? "
                "WHERE status = 'pending' AND answer_key = ? AND answer_text IS NOT NULL",
                (now, str(answer_key)),
            )
    return True


def save_normal_answer(
    scope: Any,
    *,
    topic_id: str,
    turn_id: str,
    user_message_id: str | None,
    content: str,
    content_type: str = "text",
    model: str | None = None,
    raw: dict | None = None,
    fragment_id: str | None = None,
) -> dict:
    """R17 冻结接口：普通正式回答的统一保存入口（契约 §2.5 / P2-4）。

    与恢复回答复用 answer_key_for：answer_key = answer_key_for(turn_id,
    user_message_id=user_message_id)（与恢复入口同一函数；普通轮必须有原用户消息，
    缺失时按既有 operation_id 退化规则并记录，不静默造假）。

    * 正式保存事务内先按 answer_key 查既有正式答案（含旧库可靠归属规则）→ 已存在则
      复用原 answer_id（不新增第二条），并把同 answer_key 的 pending 恢复记录与执行行
      **同一事务**收口；不存在则按普通回答原有语义写入一条 assistant 消息（片段由轮次
      绑定解析、content_type / model / raw / 原轮次全部保留，且带 answer_key）。
    * 并发唯一性靠既有 messages.answer_key 部分唯一索引，冲突时回查既有一份。
    * 保存失败时先把正文写入共享暂存唯一权威（run 行 staged_*，phase=staged）再抛出
      **原异常**；暂存本身失败不掩盖原异常；另一个活跃执行者的暂存 / 租约不覆盖（I11）。

    返回：{"answer_id": str, "created": bool, "existing": bool,
    "closed_record_ids": list[str], "answer_key": str}。
    """
    conn = _conn_of(scope)
    if conn is None:
        raise RecoveryPersistError("没有可用的数据库连接，普通正式回答无法保存")
    app = scope if hasattr(scope, "memory") else getattr(scope, "app", scope)
    memory = getattr(app, "memory", None)
    turn = str(turn_id or "")
    user_message = str(user_message_id or "") or None
    if user_message:
        answer_key = answer_key_for(turn, user_message_id=user_message)
    else:
        # 契约 §2.5：普通轮必须有原用户消息；缺失时按既有 operation_id 退化规则，
        # 并留下可观察记录（不静默造假）。
        logger.warning(
            "save_normal_answer: 缺少原用户消息（turn_id=%s），按退化规则生成 answer_key",
            turn,
        )
        answer_key = answer_key_for(turn, user_message_id=None)
    try:
        if memory is None:
            raise RecoveryPersistError(
                "没有可用的 memory 写入通道，普通正式回答无法保存"
            )
        if not _messages_answer_key_available(conn):
            raise RecoveryPersistError(
                "messages.answer_key 列不可用（迁移 79 未应用），"
                "普通正式回答无法统一答案身份"
            )
        return _save_normal_answer_locked(
            conn,
            memory,
            answer_key=answer_key,
            topic_id=str(topic_id),
            turn_id=turn,
            user_message_id=user_message,
            content=str(content or ""),
            content_type=str(content_type or "text"),
            model=model,
            raw=raw,
            fragment_id=fragment_id,
        )
    except Exception as original:  # noqa: BLE001 - 失败路径：先暂存正文，再抛原异常
        try:
            _stash_normal_answer_failure(
                conn,
                answer_key=answer_key,
                answer=str(content or ""),
                failure=_error_detail(original),
                model=model,
            )
        except Exception:  # noqa: BLE001 - 暂存异常不得掩盖原异常
            logger.warning(
                "save_normal_answer: 失败暂存也失败（原异常优先抛出）", exc_info=True
            )
        raise


def _stash_answer_failure(
    conn: sqlite3.Connection,
    execution: dict,
    answer: str,
    failure: str,
    *,
    model: str | None = None,
) -> bool:
    """生成成功但保存失败：把正文写进**共享暂存唯一权威**（run 行，按 answer_key）并释放执行权。

    * 暂存只写 run.staged_answer/staged_at/staged_model + phase=staged；**不再**把正文写进
      恢复记录列（记录列只清空迁移期残留，保证只有一份权威）；
    * 返回 False = 已失去执行权：旧执行者的迟到暂存不得覆盖新任务的暂存 / 租约 / 阶段
      （I11：零行改动）。
    """
    record_id = str(execution.get("record_id") or "")
    now = _now_iso()
    try:
        with transaction(conn):
            if not _execution_still_owned(conn, execution):
                return False
            run_cursor = conn.execute(
                f"UPDATE {ANSWER_RUN_TABLE} SET status = 'pending', phase = ?, "
                "staged_answer = ?, staged_at = ?, staged_model = ?, owner = NULL, "
                "owner_instance = NULL, claim_until = NULL, updated_at = ? "
                "WHERE answer_key = ? AND generation = ? AND owner = ? "
                "AND status = 'running'",
                (
                    PHASE_STAGED,
                    str(answer or ""),
                    now,
                    model,
                    now,
                    execution["answer_key"],
                    execution["generation"],
                    execution["owner"],
                ),
            )
            if run_cursor.rowcount != 1:
                return False
            record_cursor = conn.execute(
                f"UPDATE {TOPIC_RECOVERY_TABLE} SET answer_text = '', last_error = ?, "
                "claim_owner = NULL, claim_until = NULL, execution_owner = NULL, "
                "updated_at = ? WHERE record_id = ? AND status = 'pending' "
                "AND execution_owner = ? AND execution_generation = ?",
                (
                    str(failure or ""),
                    now,
                    record_id,
                    execution["owner"],
                    execution["generation"],
                ),
            )
            if record_cursor.rowcount != 1:
                # 记录侧执行令牌不再属于自己：整体回滚，零行改动（I11）。
                raise _ExecutionLost(record_id)
    except _ExecutionLost:
        return False
    return True


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


def _update_record_under_execution(
    conn: sqlite3.Connection, execution: dict, record_id: str, **fields: Any
) -> bool:
    """记录侧小字段更新也必须校验执行权（旧执行者不得改新任务已绑定的记录）。"""
    mapping = {
        "fragment_id": "fragment_id",
        "failed_stage": "failed_stage",
        "last_error": "last_error",
        "answer_text": "answer_text",
    }
    updates: list[str] = []
    params: list[Any] = []
    for key, value in fields.items():
        column = mapping.get(key)
        if column is None:
            continue
        updates.append(f"{column} = ?")
        params.append(value)
    if not updates:
        return True
    updates.append("updated_at = ?")
    params.append(_now_iso())
    params.extend([str(record_id), execution["owner"], execution["generation"]])
    cursor = conn.execute(
        f"UPDATE {TOPIC_RECOVERY_TABLE} SET {', '.join(updates)} "
        "WHERE record_id = ? AND status = 'pending' AND execution_owner = ? "
        "AND execution_generation = ?",
        params,
    )
    return cursor.rowcount == 1


def _released_or_superseded(
    conn: sqlite3.Connection, execution: dict, record_id: str, **failures: Any
) -> dict | None:
    """失败路径的收口：释放执行权成功 -> None（调用方返回结构化失败）；失败 -> superseded。"""
    if _release_execution_safely(conn, execution, **failures):
        return None
    return _superseded_result(record_id)


async def retry_topic_answer(
    ctx: Any,
    record_id: str,
    *,
    expected_class: str | None = None,
    expected_status: str | None = None,
    lease_seconds: float | None = None,
    heartbeat_interval: float | None = None,
    heartbeat_wait: Any = None,
) -> dict:
    """重试一条话题回答恢复记录（幂等；只重做未完成步骤；按 answer_key 收口执行权）。

    * context_prepare 失败 → 只重装配上下文 + 回答生成（不重跑 create/switch、
      不新增用户消息、不新建重复轮次）；
    * answer 失败 → 只重做回答；
    * 已成功 → 直接返回既有结果（不重复生成 / 不复制消息）；
    * 同一 answer_key 已有有效执行权 → 202 `recovery_in_progress`（不重复生成）；
    * 活任务靠心跳续租，不因固定租约到期被重开；执行权租约到期且**确认旧实例
      已退出**（InstanceRegistry.owner_alive is False）才接管（generation+1）；
    * 恢复前复核最新导航版本：目标位置已被用户后来的选择改变 → 如实失败、
      不覆盖，记录保持可恢复（stale ≠ 拒绝）；
    * 生成成功但保存失败 → 正文暂存进记录，后续重试只补存（不重复生成）。

    测试缝（契约 §6.2.5，生产不传）：`lease_seconds` / `heartbeat_interval` /
    `heartbeat_wait`（`async () -> None`，每次续租前调用；测试用事件控制节奏，
    不依赖 sleep）。租约时间唯一来源是模块级 :func:`_now_utc`。
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
    if not _run_table_available(conn):
        # 迁移 81 之前的库：不能安全协调「同一逻辑答案只生成一次」，如实拒绝。
        return {
            "ok": False,
            "code": "recovery_unavailable",
            "message": (
                "执行权表不可用（迁移 81 未应用），无法安全协调同一逻辑答案的生成；"
                "请先完成数据库迁移后重试。"
            ),
            "record_id": record["record_id"],
            "status_code": 503,
        }

    lease = LEASE_SECONDS if lease_seconds is None else float(lease_seconds)
    interval = (
        float(heartbeat_interval)
        if heartbeat_interval is not None
        else max(1.0, lease / 3.0)
    )
    answer_key = str(record.get("answer_key") or "") or answer_key_for(
        str(record.get("turn_id") or ""),
        user_message_id=record.get("user_message_id"),
        operation_id=record.get("operation_id"),
    )

    # 1) 记录级租约预检：别人持有的有效租约 = 处理中（既有语义保留，不写任何行）。
    if _lease_active(record):
        latest = get_record(ctx, record["record_id"]) or record
        if str(latest.get("status") or "") == "answered":
            return _answered_result(ctx, latest)
        return _in_progress_result(latest)

    claim_owner = _new_claim_owner(ctx)

    # 2) 先按逻辑答案身份领取执行权：同一 answer_key 只允许一个生成执行者。
    #    领不到（别人的执行有效 / 旧实例还活着 / 判不出来）时**不写记录租约**，直接 202。
    execution, disposition = _acquire_execution(
        conn, ctx, answer_key=answer_key, owner=claim_owner, lease_seconds=lease
    )
    if execution is None:
        if disposition == "answered":
            _finalize_answered_answer_key(conn, answer_key)
            latest = get_record(ctx, record["record_id"]) or record
            return _answered_result(ctx, latest)
        if disposition == "unavailable":
            return {
                "ok": False,
                "code": "recovery_unavailable",
                "message": "执行权表暂时不可用，无法安全协调同一逻辑答案的生成",
                "record_id": record["record_id"],
                "status_code": 503,
            }
        latest = get_record(ctx, record["record_id"]) or record
        if str(latest.get("status") or "") == "answered":
            return _answered_result(ctx, latest)
        return _in_progress_result(latest)

    # 3) 执行权到手后才做记录级原子领取（CAS + 租约）；抢不到就释放执行权。
    claimed = claim_record(
        conn, record["record_id"], owner=claim_owner, lease_seconds=lease
    )
    if claimed is None:
        _release_execution_safely(conn, execution)
        latest = get_record(ctx, record["record_id"]) or record
        if str(latest.get("status") or "") == "answered":
            return _answered_result(ctx, latest)
        return _in_progress_result(latest)

    execution["record_id"] = str(record["record_id"])
    try:
        _bind_record_execution(conn, execution, record["record_id"])
    except _ExecutionLost:
        _release_record_claim_safely(conn, record["record_id"], claim_owner)
        return _superseded_result(record["record_id"])
    except _RecordChanged:
        # 绑定失败：run 事务已回滚（我方仍持有），释放执行权 + 记录级租约后如实处理。
        _release_execution_safely(conn, execution)
        _release_record_claim_safely(conn, record["record_id"], claim_owner)
        latest = get_record(ctx, record["record_id"]) or claimed
        if str(latest.get("status") or "") == "answered":
            return _answered_result(ctx, latest)
        return _in_progress_result(latest)
    except sqlite3.Error as exc:  # noqa: BLE001 - 绑定失败：释放两边后可再重试
        failure = _error_detail(exc)
        _release_execution_safely(conn, execution, last_error=failure)
        _release_record_claim_safely(conn, record["record_id"], claim_owner)
        raise

    heartbeat_task: asyncio.Task | None = None
    heartbeat_stop: asyncio.Event | None = None
    try:
        # 3) 心跳：模型调用 / 装配期间按租约间隔条件续租（不持写事务、不全局锁）。
        heartbeat_task, heartbeat_stop = _start_execution_heartbeat(
            conn, execution, interval=interval, wait=heartbeat_wait
        )
        current = get_record(ctx, record["record_id"]) or claimed
        return await _execute_claim(ctx, conn, current, execution)
    except asyncio.CancelledError:
        _release_execution_safely(conn, execution, last_error="cancelled")
        raise
    except _ExecutionLost:
        return _superseded_result(record["record_id"])
    except Exception as exc:  # noqa: BLE001 - 未预期异常：释放执行权，可再重试
        failure = _error_detail(exc)
        _release_execution_safely(conn, execution, last_error=failure)
        raise
    finally:
        await _stop_execution_heartbeat(heartbeat_task, heartbeat_stop)


async def _execute_claim(
    ctx: Any,
    conn: sqlite3.Connection,
    record: dict,
    execution: dict,
) -> dict:
    record_id = str(record["record_id"])
    failed_stage_at_claim = str(record.get("failed_stage") or "")
    record = dict(record)
    record["answer_key"] = execution["answer_key"]

    # 1) 唯一逻辑答案已经存在（并发 / 重复恢复记录）：只收口，不重复生成。
    #    R17-D：与普通正式保存共用同一套可靠查找（含旧库空 answer_key 的可靠归属）；
    #    这里只读判断，answer_key 的补写在 _save_answer 的事务内完成。
    existing = _resolve_existing_answer(
        conn,
        execution["answer_key"],
        topic_id=str(record.get("topic_id") or ""),
        turn_id=str(record.get("turn_id") or ""),
        user_message_id=record.get("user_message_id"),
        fragment_id=record.get("fragment_id"),
        backfill=False,
    )
    if existing is not None:
        content = _message_content(conn, str(existing["id"])) or str(
            existing["content"] or ""
        )
        try:
            saved = _save_answer(conn, record, content, model=None, execution=execution)
        except _ExecutionLost:
            return _superseded_result(record_id)
        except Exception as exc:  # noqa: BLE001 - 收口失败：如实可恢复失败
            failure = _error_detail(exc)
            superseded = _released_or_superseded(
                conn, execution, record_id, last_error=failure
            )
            if superseded is not None:
                return superseded
            return _persist_failed(record_id, failure)
        return _answered_from_saved(
            ctx, record, saved, failed_stage=failed_stage_at_claim
        )

    # 2) 共享暂存结果（run 行 = 唯一权威，按 answer_key）：A 生成成功但保存失败后，
    #    B（任意同 answer_key 的恢复记录）只补存、不再生成；记录列 answer_text 只作
    #    旧版本只读兜底（读到即同事务转写 run 行并清记录，保证只有一份权威）。
    stash = _run_staged_answer(conn, execution["answer_key"])
    if not stash:
        legacy = _record_legacy_answer_text(conn, record_id)
        if legacy:
            try:
                _migrate_legacy_stash(conn, execution, record_id, legacy)
            except _ExecutionLost:
                return _superseded_result(record_id)
            stash = _run_staged_answer(conn, execution["answer_key"]) or legacy
    if stash:
        try:
            saved = _save_answer(conn, record, stash, model=None, execution=execution)
        except _ExecutionLost:
            return _superseded_result(record_id)
        except RecoveryPersistError as exc:
            return _attribution_mismatch_result(
                conn, record_id, execution, record, _error_detail(exc)
            )
        except Exception as exc:  # noqa: BLE001 - 保存仍失败：保留暂存，可再重试
            failure = _error_detail(exc)
            if not _stash_answer_failure(conn, execution, stash, failure):
                return _superseded_result(record_id)
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
            superseded = _released_or_superseded(
                conn, execution, record_id, last_error="nav_version_conflict"
            )
            if superseded is not None:
                return superseded
            result = _stale_result(
                get_record(ctx, record_id) or record, actual_version
            )
            result["status_code"] = 409
            return result

    # 4) 归属失败（F06）：恢复顺序是「先修归属 → 再装配目标上下文 → 再回答」。
    #    只搬本轮这一条用户消息与绑定：不重跑已成功的 create/switch、不复制用户消息、
    #    不新建重复轮次、不推进导航版本（由 orchestrator 的唯一恢复入口负责）。
    orchestrator = getattr(ctx, "turn_orchestrator", None)
    if str(record.get("failed_stage") or "") == "ownership":
        repair = getattr(orchestrator, "repair_topic_ownership", None)
        if not callable(repair):
            superseded = _released_or_superseded(
                conn, execution, record_id, failed_stage="ownership"
            )
            if superseded is not None:
                return superseded
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
            superseded = _released_or_superseded(
                conn,
                execution,
                record_id,
                failed_stage="ownership",
                last_error=failure,
            )
            if superseded is not None:
                return superseded
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
        # （仍然只有一条原消息，不复制、不新建轮次；记录侧更新仍校验执行权）。
        repaired_fragment = _lookup_fragment_for_message(
            conn, record.get("user_message_id")
        )
        if repaired_fragment and repaired_fragment != record.get("fragment_id"):
            if not _update_record_under_execution(
                conn, execution, record_id, fragment_id=repaired_fragment
            ):
                return _superseded_result(record_id)
            record = dict(record)
            record["fragment_id"] = repaired_fragment

    # 4b) I14：正式回答必须满足归属与上下文前提（固定片段 / 话题 / 原用户消息 / 原轮次）；
    #     不成立时如实 503 + 零正式答案行，不调模型、不猜归属。
    problem = _answer_attribution_problem(conn, record)
    if problem is not None:
        code, detail = problem
        superseded = _released_or_superseded(conn, execution, record_id, last_error=code)
        if superseded is not None:
            return superseded
        return {
            "ok": False,
            "code": code,
            "recoverable": True,
            "failed_stage": record.get("failed_stage"),
            "message": "恢复记录的归属前提不成立，不能作为正式回答落库：" + detail,
            "record_id": record_id,
            "status_code": 503,
        }

    # 5) 装配上下文（只读；不在模型调用期间持有写事务）。
    prepare = getattr(orchestrator, "prepare_topic_answer_messages", None)
    if not callable(prepare):
        superseded = _released_or_superseded(conn, execution, record_id)
        if superseded is not None:
            return superseded
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
        superseded = _released_or_superseded(
            conn,
            execution,
            record_id,
            failed_stage="context_prepare",
            last_error=failure,
        )
        if superseded is not None:
            return superseded
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
        # I14 冻结码（契约 §14.6）：上下文装配为空 -> answer_context_missing，零正式答案行。
        superseded = _released_or_superseded(
            conn,
            execution,
            record_id,
            failed_stage="context_prepare",
            last_error="answer_context_missing",
        )
        if superseded is not None:
            return superseded
        return {
            "ok": False,
            "code": "answer_context_missing",
            "recoverable": True,
            "failed_stage": "context_prepare",
            "message": "话题已切换，回答准备失败：目标话题上下文为空",
            "record_id": record_id,
            "status_code": 503,
        }

    # 6) 模型通道（外部调用，不持写事务；心跳在调用期间续租）。
    builder = getattr(ctx, "build_adapter", None)
    adapter = None
    if callable(builder):
        try:
            adapter = await builder()
        except Exception as exc:  # noqa: BLE001 - 模型通道失败：如实失败，记录保留
            failure = _error_detail(exc)
            superseded = _released_or_superseded(
                conn, execution, record_id, last_error=failure
            )
            if superseded is not None:
                return superseded
            return {
                "ok": False,
                "code": "model_unavailable",
                "message": "回答生成失败（模型通道不可用）：" + failure,
                "record_id": record_id,
                "status_code": 503,
            }
    if adapter is None:
        superseded = _released_or_superseded(
            conn, execution, record_id, last_error="model_unavailable"
        )
        if superseded is not None:
            return superseded
        return {
            "ok": False,
            "code": "model_unavailable",
            "message": "回答生成失败（没有可用的模型通道）",
            "record_id": record_id,
            "status_code": 503,
        }
    # 模型生成阶段：phase=generating（CAS 执行权；已失去 -> superseded，零写入）。
    if not _set_active_phase(conn, execution, PHASE_GENERATING):
        return _superseded_result(record_id)
    try:
        answer = await _generate_answer(adapter, messages)
    except Exception as exc:  # noqa: BLE001 - 回答失败单独记 answer 阶段，单独恢复
        failure = _error_detail(exc)
        stages = _stages_from_record(record)
        stages.failed_stage = "answer"
        stages.failure = failure
        superseded = _released_or_superseded(
            conn,
            execution,
            record_id,
            failed_stage="answer",
            last_error=failure,
            stages=stages.as_dict(),
        )
        if superseded is not None:
            return superseded
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
        superseded = _released_or_superseded(
            conn,
            execution,
            record_id,
            failed_stage="answer",
            last_error="empty_answer",
        )
        if superseded is not None:
            return superseded
        return {
            "ok": False,
            "code": "topic_answer_failed",
            "recoverable": True,
            "failed_stage": "answer",
            "message": "话题已切换，回答生成失败：模型没有给出可交付正文",
            "record_id": record_id,
            "status_code": 503,
        }

    # 7) 事务收口：再校验执行权 -> 写答案行 -> 所有同 answer_key 的 pending 记录
    #    + run 行置 answered，一切原子。旧执行者的迟到保存整体回滚。
    try:
        saved = _save_answer(
            conn, record, answer, model=getattr(adapter, "model", None), execution=execution
        )
    except _ExecutionLost:
        return _superseded_result(record_id)
    except RecoveryPersistError as exc:
        # 归属前提在保存时被证伪（片段被删/换话题等）：I14 冻结码，零正式答案行。
        return _attribution_mismatch_result(
            conn, record_id, execution, record, _error_detail(exc)
        )
    except Exception as exc:  # noqa: BLE001 - 写失败：暂存正文，记录保持 pending 可再重试
        failure = _error_detail(exc)
        try:
            stashed = _stash_answer_failure(
                conn, execution, answer, failure, model=getattr(adapter, "model", None)
            )
        except sqlite3.Error:  # noqa: BLE001 - 暂存也失败：如实说明，下次可能再调模型
            logger.warning(
                "failed to stash recovered answer for %s: %s",
                record_id,
                _error_detail(exc),
            )
            stashed = False
        if not stashed:
            return _superseded_result(record_id)
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


def _attribution_mismatch_result(
    conn: sqlite3.Connection,
    record_id: str,
    execution: dict,
    record: dict,
    detail: str,
) -> dict:
    """I14 归属前提不成立：释放执行权、零正式答案行、结构化 503（契约 §14.6）。

    释放本身也校验执行权：旧执行者的迟到保存不会借这里改新任务状态（I11）。
    若 run 行已有共享暂存正文（保存阶段才发现归属被证伪），释放时保持 phase=staged：
    正文仍属于该 answer_key，可由另一条归属正确的恢复记录补存（不丢已生成结果）。
    """
    release_phase = (
        PHASE_STAGED if _run_staged_answer(conn, execution["answer_key"]) else PHASE_FAILED
    )
    superseded = _released_or_superseded(
        conn,
        execution,
        record_id,
        last_error="answer_attribution_mismatch",
        phase=release_phase,
    )
    if superseded is not None:
        return superseded
    return {
        "ok": False,
        "code": "answer_attribution_mismatch",
        "recoverable": True,
        "failed_stage": record.get("failed_stage"),
        "message": "恢复记录的归属前提不成立，不能作为正式回答落库：" + str(detail or ""),
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
