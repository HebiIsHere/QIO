"""派生任务的持久队列（阶段 2 + R05：实例归属 / 认领代次 / 迟到结果丢弃）。

职责边界很清楚：

* **封存片段**是对话状态，事务内立刻完成，不等模型；
* **摘要 / 索引 / 实体 / 知识**是可重试的派生数据，落在这张表里慢慢做。

为什么要持久化：进程在摘要前退出、模型调用失败、网络抖动，都不该让
「片段已经封存」这件事回退，也不该让用户看到对话不可用。失败退避 + 幂等键
（kind, fragment_id, content_version）保证重试不会重复制造知识、实体或索引。

R05 的三件事（契约 C7）
-----------------------

1. **归属与存活**：认领时写 `owner_instance_id`，恢复时用 `InstanceRegistry.owner_alive()`
   判定归属者是否**确认已退出**。unknown（判不出来）一律不改状态 —— 判不出来就
   什么都不做，等下一次维护重判。旧记录（没有归属）只在**明确超期**后才放回队列，
   保留 R05 原有的时限兜底。
2. **认领代次**：每次认领 `claim_generation + 1` 并写回；完成 / 失败 / 释放都必须带
   `expected_generation`，不匹配就是迟到结果，直接丢弃（不改状态、不覆盖新认领）。
3. **恢复核对不能只在启动跑一次**：`claim_due()` 每次都会顺带核对一遍 running 行，
   所以「刚认领就重启（未超 300s）」也能在下一轮 drain/claim 里被回收。

并发口径：连接是 autocommit（`isolation_level = None`），所以多步写入一律用
`agent.storage.db.transaction`（或条件 UPDATE 的 rowcount 判定）保证「只有一个认领者」。
"""

from __future__ import annotations

import logging
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from agent.storage.db import transaction

logger = logging.getLogger(__name__)

STATE_PENDING = "pending"
STATE_RUNNING = "running"
STATE_COMPLETED = "completed"
STATE_FAILED = "failed"

KIND_SUMMARY = "summary"
KIND_INDEX = "index"
KIND_ENTITIES = "entities"
KIND_KNOWLEDGE = "knowledge"

# 失败退避：5s → 15s → 60s → 5min → 30min → 1h（上限）。避免「永不结束的高频重试」。
_BACKOFF_SECONDS = (5, 15, 60, 300, 1800, 3600)
_STALE_RUNNING_SECONDS = 300

# ---------------------------------------------------------------------------
# 受控时钟：测试用它制造「超期」「退避到点」而不真的等待。
# 生产路径下 _CLOCK 为 None，一律走系统时钟。
# ---------------------------------------------------------------------------

_CLOCK: Callable[[], datetime] | None = None

# 本进程的实例身份 + 存活判定器。由启动路径绑定一次
# （`bind_instance(registry.instance_id, registry)`）；未绑定时 owner 写 NULL，
# 恢复退化为「只在明确超期后回收」的旧行为（诚实降级，不猜谁死了）。
_INSTANCE_ID: str | None = None
_REGISTRY: Any = None


def set_clock(clock: Callable[[], datetime] | None) -> None:
    """注入受控时钟（测试用；传 None 恢复正常时钟）。"""
    global _CLOCK
    _CLOCK = clock


def reset_clock() -> None:
    set_clock(None)


def bind_instance(instance_id: str | None, registry: Any = None) -> None:
    """绑定本进程身份与存活判定器（`registry.owner_alive(instance_id)`）。

    启动路径调用一次即可；传 None 解绑（测试收尾用）。
    """
    global _INSTANCE_ID, _REGISTRY
    _INSTANCE_ID = str(instance_id) if instance_id else None
    _REGISTRY = registry


def bound_instance_id() -> str | None:
    return _INSTANCE_ID


def _now() -> datetime:
    if _CLOCK is not None:
        moment = _CLOCK()
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return moment
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.isoformat()


def _parse(value: Any) -> datetime | None:
    if value is None:
        return None
    try:
        moment = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment


def backoff_delay(attempts: int) -> int:
    index = min(max(attempts - 1, 0), len(_BACKOFF_SECONDS) - 1)
    return _BACKOFF_SECONDS[index]


@dataclass(frozen=True)
class DerivedTask:
    id: str
    kind: str
    fragment_id: str
    content_version: int
    state: str
    attempts: int
    last_error: str | None
    run_after: str | None
    owner_instance_id: str | None = None
    claim_generation: int = 0


def _columns(conn: sqlite3.Connection) -> set[str]:
    """派生任务表当前有哪些列（迁移未落地时降级，不假装有归属列）。"""
    try:
        return {
            str(row["name"])
            for row in conn.execute("PRAGMA table_info(derived_tasks)").fetchall()
        }
    except sqlite3.Error:
        return set()


def _has_ownership(conn: sqlite3.Connection) -> bool:
    cols = _columns(conn)
    return "owner_instance_id" in cols and "claim_generation" in cols


def _has_record_owners(conn: sqlite3.Connection) -> bool:
    """A 的迁移里有 record_owners（契约 C1：一张表覆盖 turn / approval / 派生任务）。

    认领时顺带镜像一条归属行，A 的启动恢复（recover_confirmed_dead）就能按
    「确认已退出的实例」找到这些派生任务，而不是只认我们自己那一列。
    """
    try:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'record_owners'"
        ).fetchone()
    except sqlite3.Error:
        return False
    return row is not None


def _mirror_ownership(conn: sqlite3.Connection, task_id: str, instance_id: str | None) -> None:
    """把认领写进 record_owners（无该表 / 无归属时什么都不做）。"""
    if not instance_id or not _has_record_owners(conn):
        return
    conn.execute(
        "INSERT OR REPLACE INTO record_owners (record_type, record_id, instance_id) "
        "VALUES ('derived_task', ?, ?)",
        (str(task_id), str(instance_id)),
    )


def _clear_ownership(conn: sqlite3.Connection, task_id: str) -> None:
    """任务终结 / 放回队列：归属行跟着消失（记录不再「在谁手上」）。"""
    if not _has_record_owners(conn):
        return
    conn.execute(
        "DELETE FROM record_owners WHERE record_type = 'derived_task' AND record_id = ?",
        (str(task_id),),
    )


def owned_task_ids(conn: sqlite3.Connection, instance_id: str) -> list[str]:
    """某个实例名下仍在跑的派生任务 id（启动恢复报告用；无表则空）。"""
    if not _has_record_owners(conn):
        return []
    rows = conn.execute(
        "SELECT record_id FROM record_owners WHERE record_type = 'derived_task' "
        "AND instance_id = ?",
        (str(instance_id),),
    ).fetchall()
    return [str(row["record_id"]) for row in rows]


def _from_row(row: sqlite3.Row) -> DerivedTask:
    try:
        keys = set(row.keys())
    except AttributeError:  # pragma: no cover - 非 Row 工厂时的保守回退
        keys = set()
    return DerivedTask(
        id=str(row["id"]),
        kind=str(row["kind"]),
        fragment_id=str(row["fragment_id"]),
        content_version=int(row["content_version"]),
        state=str(row["state"]),
        attempts=int(row["attempts"]),
        last_error=row["last_error"],
        run_after=row["run_after"],
        owner_instance_id=row["owner_instance_id"] if "owner_instance_id" in keys else None,
        claim_generation=int(row["claim_generation"]) if "claim_generation" in keys else 0,
    )


def enqueue(
    conn: sqlite3.Connection, kind: str, fragment_id: str, content_version: int
) -> tuple[str, bool]:
    """登记一个派生任务。同 (kind, fragment, 内容版本) 幂等：返回 (task_id, 是否新建)。"""
    existing = conn.execute(
        "SELECT id FROM derived_tasks WHERE kind = ? AND fragment_id = ? AND content_version = ?",
        (kind, fragment_id, content_version),
    ).fetchone()
    if existing is not None:
        return str(existing["id"]), False
    task_id = f"task_{uuid.uuid4().hex[:12]}"
    now = _iso(_now())
    with transaction(conn):
        conn.execute(
            "INSERT INTO derived_tasks "
            "(id, kind, fragment_id, content_version, state, attempts, last_error, run_after, "
            " created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, 0, NULL, NULL, ?, ?)",
            (task_id, kind, fragment_id, content_version, STATE_PENDING, now, now),
        )
    return task_id, True


def _due(run_after: Any, now: datetime) -> bool:
    if run_after is None or str(run_after) == "":
        return True
    moment = _parse(run_after)
    if moment is None:
        return True  # 时间坏掉的行不该永远卡在队列外
    return moment <= now


def _owner_clause(
    conn: sqlite3.Connection, instance_id: str | None
) -> tuple[str, list[object]]:
    """完成/失败/释放时的「所有者」条件（与认领代次一起构成双校验）。

    M06 要求取消与迟到提交都过**所有者 + 版本**校验：代次已经唯一标识了一次认领，
    归属者这一层是纵深防御（同一个代次不可能由别的实例产生，但显式核对更诚实）。
    未绑定实例身份时不加这一层（诚实降级，不用假身份去卡）。
    """
    owner = instance_id if instance_id is not None else _INSTANCE_ID
    if owner and _has_ownership(conn):
        return " AND owner_instance_id = ?", [str(owner)]
    return "", []


# ---------------------------------------------------------------------------
# 存活判定
# ---------------------------------------------------------------------------


def _owner_alive(registry: Any, instance_id: str) -> bool | None:
    """归属者是否还活着。

    返回 False 只代表**确认已退出**；None = 判不出来（unknown），调用方不得当死。
    registry 既接受 `InstanceRegistry`（有 `owner_alive` 方法），也接受一个裸 callable
    （受控测试用最少的替身即可，不必构造整个实例表）。
    """
    if registry is None:
        return None
    checker = getattr(registry, "owner_alive", None)
    if checker is None:
        if callable(registry):
            checker = registry
        else:
            return None
    try:
        result = checker(instance_id)
    except Exception:  # noqa: BLE001 - 判不出来就是 unknown，绝不能当死
        return None
    if result is True:
        return True
    if result is False:
        return False
    return None


def _should_reclaim(
    owner: str | None,
    *,
    my_instance: str | None,
    registry: Any,
    stale_by_time: bool,
) -> bool:
    """这条 running 该不该放回队列。

    * 无归属（旧记录 / 未绑定实例）：判不出来 —— 只有**明确超期**才放回（时限兜底）；
    * 归属者是本实例：进程内卡住，超期才放回（同实例不与自己抢）；
    * 归属者是别的实例：`owner_alive() is False` 立刻放回（刚认领就重启也能恢复），
      True 不动（另一活实例的任务不被抢），None（unknown）保留原状态。
    """
    if not owner:
        return bool(stale_by_time)
    if my_instance and owner == my_instance:
        return bool(stale_by_time)
    alive = _owner_alive(registry, owner)
    if alive is False:
        return True
    return False


def recover_stale(
    conn: sqlite3.Connection,
    *,
    timeout_seconds: int = _STALE_RUNNING_SECONDS,
    instance_id: str | None = None,
    registry: Any = None,
) -> int:
    """把「卡在 running」的任务放回可重试状态（幂等，安全重复调用）。

    判定顺序：归属者**确认已退出** → 立刻回收；unknown → 保留；无归属 / 本实例 →
    只在超期后回收。`claim_due()` 每次都会顺带调用它，所以恢复核对不是「只在启动
    跑一次」。
    """
    now = _now()
    my_instance = instance_id if instance_id is not None else _INSTANCE_ID
    checker = registry if registry is not None else _REGISTRY
    owns = _has_ownership(conn)

    rows = conn.execute(
        "SELECT * FROM derived_tasks WHERE state = ?", (STATE_RUNNING,)
    ).fetchall()
    reclaimed: list[str] = []
    for row in rows:
        owner = (row["owner_instance_id"] if owns else None) or None
        updated = _parse(row["updated_at"])
        stale_by_time = updated is None or (now - updated).total_seconds() >= timeout_seconds
        if _should_reclaim(
            owner, my_instance=my_instance, registry=checker, stale_by_time=stale_by_time
        ):
            reclaimed.append(str(row["id"]))
    if not reclaimed:
        return 0

    stamp = _iso(now)
    recovered = 0
    with transaction(conn):
        for task_id in reclaimed:
            cursor = conn.execute(
                "UPDATE derived_tasks SET state = ?, run_after = NULL, updated_at = ? "
                "WHERE id = ? AND state = ?",
                (STATE_PENDING, stamp, task_id, STATE_RUNNING),
            )
            if int(cursor.rowcount or 0) > 0:
                _clear_ownership(conn, task_id)
            recovered += int(cursor.rowcount or 0)
    if recovered:
        logger.info("派生任务恢复：%d 条 running 放回队列（归属者已确认退出或超期）", recovered)
    return recovered


# ---------------------------------------------------------------------------
# 认领 / 完成 / 失败 / 释放
# ---------------------------------------------------------------------------


def claim_due(
    conn: sqlite3.Connection,
    *,
    limit: int = 5,
    kinds: tuple[str, ...] | None = None,
    instance_id: str | None = None,
    registry: Any = None,
    timeout_seconds: int = _STALE_RUNNING_SECONDS,
) -> list[DerivedTask]:
    """取到期的任务并置为 running（递增认领代次）。

    同一个任务不会被两个执行者同时拿走：条件 UPDATE 的 rowcount 在同一个事务里判定。
    每次调用都先顺带 `recover_stale()`（恢复核对不只在启动跑一次）。
    """
    recover_stale(
        conn,
        timeout_seconds=timeout_seconds,
        instance_id=instance_id,
        registry=registry,
    )

    now_moment = _now()
    now = _iso(now_moment)
    owner = instance_id if instance_id is not None else _INSTANCE_ID
    owns = _has_ownership(conn)

    sql = "SELECT * FROM derived_tasks WHERE state IN (?, ?)"
    params: list[object] = [STATE_PENDING, STATE_FAILED]
    if kinds:
        sql += f" AND kind IN ({','.join('?' for _ in kinds)})"
        params.extend(kinds)
    sql += " ORDER BY created_at, rowid"  # rowid 兜底：受控时钟下 created_at 可能相同
    rows = conn.execute(sql, params).fetchall()
    due = [row for row in rows if _due(row["run_after"], now_moment)][: max(0, int(limit))]

    claimed: list[DerivedTask] = []
    with transaction(conn):
        for row in due:
            if owns:
                cursor = conn.execute(
                    "UPDATE derived_tasks SET state = ?, owner_instance_id = ?, "
                    "claim_generation = claim_generation + 1, updated_at = ? "
                    "WHERE id = ? AND state IN (?, ?)",
                    (STATE_RUNNING, owner, now, row["id"], STATE_PENDING, STATE_FAILED),
                )
            else:
                cursor = conn.execute(
                    "UPDATE derived_tasks SET state = ?, updated_at = ? "
                    "WHERE id = ? AND state IN (?, ?)",
                    (STATE_RUNNING, now, row["id"], STATE_PENDING, STATE_FAILED),
                )
            if int(cursor.rowcount or 0) != 1:
                # 另一个执行者抢先了：这一条不归我，继续下一条
                continue
            fresh = conn.execute(
                "SELECT * FROM derived_tasks WHERE id = ?", (row["id"],)
            ).fetchone()
            _mirror_ownership(conn, str(row["id"]), owner)
            claimed.append(_from_row(fresh if fresh is not None else row))
    return claimed


def complete(
    conn: sqlite3.Connection, task_id: str, *, expected_generation: int | None = None
) -> bool:
    """完成一条任务。带 `expected_generation` 时做代次校验：不匹配即丢弃迟到结果。

    `expected_generation=None` 只为兼容旧调用点（没有认领代次的路径）；新代码必须传。
    返回是否真的写回了状态。
    """
    stamp = _iso(_now())
    if expected_generation is None:
        cursor = conn.execute(
            "UPDATE derived_tasks SET state = ?, last_error = NULL, updated_at = ? WHERE id = ?",
            (STATE_COMPLETED, stamp, task_id),
        )
        applied = int(cursor.rowcount or 0) > 0
        if applied:
            _clear_ownership(conn, task_id)
        return applied
    owns = _has_ownership(conn)
    if not owns:
        return complete(conn, task_id)
    owner_sql, owner_params = _owner_clause(conn, None)
    cursor = conn.execute(
        "UPDATE derived_tasks SET state = ?, last_error = NULL, updated_at = ? "
        "WHERE id = ? AND state = ? AND claim_generation = ?" + owner_sql,
        (STATE_COMPLETED, stamp, task_id, STATE_RUNNING, int(expected_generation), *owner_params),
    )
    applied = int(cursor.rowcount or 0) > 0
    if applied:
        _clear_ownership(conn, task_id)
    else:
        logger.info("派生任务迟到完成被丢弃：task=%s generation=%s", task_id, expected_generation)
    return applied


def fail(
    conn: sqlite3.Connection,
    task_id: str,
    error: str,
    *,
    expected_generation: int | None = None,
) -> bool:
    """失败：记原因、次数 +1、安排下一次尝试时间（退避）。

    带 `expected_generation` 时，代次不匹配就是迟到结果：不改状态、不递增 attempts、
    也不覆盖新认领的 last_error。

    原因入库前先过统一脱敏（agent/trace/redact.py）：last_error 会进状态接口与
    界面，属于「错误信息」的硬性约束范围 —— 派生失败原因可能夹带模型输入片段。
    """
    from agent.trace.redact import redact_text

    owns = _has_ownership(conn)
    columns = (
        "attempts, state, claim_generation, owner_instance_id" if owns else "attempts, state"
    )
    row = conn.execute(
        f"SELECT {columns} FROM derived_tasks WHERE id = ?", (task_id,)
    ).fetchone()
    if row is None:
        return False
    owner_expected = _INSTANCE_ID
    if expected_generation is not None and owns:
        owner_mismatch = bool(owner_expected) and str(row["owner_instance_id"] or "") != str(
            owner_expected
        )
        if (
            str(row["state"]) != STATE_RUNNING
            or int(row["claim_generation"]) != int(expected_generation)
            or owner_mismatch
        ):
            logger.info(
                "派生任务迟到失败被丢弃：task=%s generation=%s", task_id, expected_generation
            )
            return False

    attempts = int(row["attempts"]) + 1
    now = _now()
    run_after = _iso(now + timedelta(seconds=backoff_delay(attempts)))
    stamp = _iso(now)
    if expected_generation is not None and owns:
        owner_sql, owner_params = _owner_clause(conn, None)
        cursor = conn.execute(
            "UPDATE derived_tasks SET state = ?, attempts = ?, last_error = ?, run_after = ?, "
            "updated_at = ? WHERE id = ? AND state = ? AND claim_generation = ?" + owner_sql,
            (
                STATE_FAILED,
                attempts,
                redact_text(error)[:500],
                run_after,
                stamp,
                task_id,
                STATE_RUNNING,
                int(expected_generation),
                *owner_params,
            ),
        )
        applied = int(cursor.rowcount or 0) > 0
        if applied:
            _clear_ownership(conn, task_id)
        return applied
    conn.execute(
        "UPDATE derived_tasks SET state = ?, attempts = ?, last_error = ?, run_after = ?, "
        "updated_at = ? WHERE id = ?",
        (STATE_FAILED, attempts, redact_text(error)[:500], run_after, stamp, task_id),
    )
    _clear_ownership(conn, task_id)
    return True


def release(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    expected_generation: int | None = None,
    reason: str | None = None,
) -> bool:
    """把一条 running 任务放回队列（**不是失败**：取消 / 关闭收尾）。

    与完成/失败同一套代次校验：关掉之后才回来的「迟到释放」不得动新认领。
    attempts 不递增，run_after 清空（下一次 drain 就能接着做）。
    """
    stamp = _iso(_now())
    if expected_generation is None:
        cursor = conn.execute(
            "UPDATE derived_tasks SET state = ?, run_after = NULL, updated_at = ? "
            "WHERE id = ? AND state = ?",
            (STATE_PENDING, stamp, task_id, STATE_RUNNING),
        )
        applied = int(cursor.rowcount or 0) > 0
    elif _has_ownership(conn):
        owner_sql, owner_params = _owner_clause(conn, None)
        cursor = conn.execute(
            "UPDATE derived_tasks SET state = ?, run_after = NULL, updated_at = ? "
            "WHERE id = ? AND state = ? AND claim_generation = ?" + owner_sql,
            (STATE_PENDING, stamp, task_id, STATE_RUNNING, int(expected_generation), *owner_params),
        )
        applied = int(cursor.rowcount or 0) > 0
    else:
        return release(conn, task_id)
    if applied:
        _clear_ownership(conn, task_id)
        if reason:
            logger.info("派生任务释放回队列：task=%s 原因=%s", task_id, reason)
    return applied


def release_running(
    conn: sqlite3.Connection, *, instance_id: str | None = None, reason: str | None = None
) -> int:
    """把某实例名下所有 running 任务放回队列（干净退出前的兜底收尾）。

    `instance_id=None` 表示本实例；传 "*" 表示不区分归属。
    """
    owns = _has_ownership(conn)
    if instance_id == "*" or not owns:
        rows = conn.execute(
            "SELECT id FROM derived_tasks WHERE state = ?", (STATE_RUNNING,)
        ).fetchall()
    else:
        owner = instance_id if instance_id is not None else _INSTANCE_ID
        if not owner:
            return 0
        rows = conn.execute(
            "SELECT id FROM derived_tasks WHERE state = ? AND owner_instance_id = ?",
            (STATE_RUNNING, owner),
        ).fetchall()
    released = 0
    for row in rows:
        if release(conn, str(row["id"]), expected_generation=None, reason=reason):
            released += 1
    return released


# ---------------------------------------------------------------------------
# 查询
# ---------------------------------------------------------------------------


def pending_count(conn: sqlite3.Connection, kind: str | None = None) -> int:
    if kind is None:
        row = conn.execute(
            "SELECT COUNT(*) c FROM derived_tasks WHERE state != ?", (STATE_COMPLETED,)
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT COUNT(*) c FROM derived_tasks WHERE state != ? AND kind = ?",
            (STATE_COMPLETED, kind),
        ).fetchone()
    return int(row["c"])


def running_count(conn: sqlite3.Connection, *, instance_id: str | None = None) -> int:
    owns = _has_ownership(conn)
    if instance_id and owns:
        row = conn.execute(
            "SELECT COUNT(*) c FROM derived_tasks WHERE state = ? AND owner_instance_id = ?",
            (STATE_RUNNING, instance_id),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT COUNT(*) c FROM derived_tasks WHERE state = ?", (STATE_RUNNING,)
        ).fetchone()
    return int(row["c"])


def task_for(
    conn: sqlite3.Connection, kind: str, fragment_id: str, content_version: int
) -> DerivedTask | None:
    row = conn.execute(
        "SELECT * FROM derived_tasks WHERE kind = ? AND fragment_id = ? AND content_version = ?",
        (kind, fragment_id, content_version),
    ).fetchone()
    return _from_row(row) if row is not None else None


def fragments_missing_entity_tasks(
    conn: sqlite3.Connection, *, limit: int = 2
) -> list[tuple[str, int]]:
    """「摘要已完成、但实体提炼没有任务行」的片段（M05 有边界补派）。

    只认 completed 的 summary 任务：新代码在摘要落库的同一个事务里就会登记实体任务，
    所以这里命中的是**本轮之前**已经完成摘要、而实体那一环缺失的历史片段。
    每个 (片段, 内容版本) 只会补派一次（enqueue 的 UNIQUE 幂等），再调用不会重复。
    """
    rows = conn.execute(
        "SELECT s.fragment_id AS fragment_id, s.content_version AS content_version "
        "FROM derived_tasks s "
        "WHERE s.kind = ? AND s.state = ? "
        "  AND NOT EXISTS (SELECT 1 FROM derived_tasks e WHERE e.kind = ? "
        "                  AND e.fragment_id = s.fragment_id "
        "                  AND e.content_version = s.content_version) "
        "  AND EXISTS (SELECT 1 FROM fragments f WHERE f.id = s.fragment_id "
        "              AND COALESCE(f.summary, '') != '') "
        "ORDER BY s.updated_at LIMIT ?",
        (KIND_SUMMARY, STATE_COMPLETED, KIND_ENTITIES, max(0, int(limit))),
    ).fetchall()
    return [(str(row["fragment_id"]), int(row["content_version"])) for row in rows]
