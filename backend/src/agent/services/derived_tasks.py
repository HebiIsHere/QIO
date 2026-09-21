"""派生任务的持久队列（阶段 2）。

职责边界很清楚：

* **封存片段**是对话状态，事务内立刻完成，不等模型；
* **摘要 / 索引 / 实体 / 知识**是可重试的派生数据，落在这张表里慢慢做。

为什么要持久化：进程在摘要前退出、模型调用失败、网络抖动，都不该让
「片段已经封存」这件事回退，也不该让用户看到对话不可用。失败退避 + 幂等键
（kind, fragment_id, content_version）保证重试不会重复制造知识、实体或索引。
"""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from agent.storage.db import transaction

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


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.isoformat()


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


def _from_row(row: sqlite3.Row) -> DerivedTask:
    return DerivedTask(
        id=str(row["id"]),
        kind=str(row["kind"]),
        fragment_id=str(row["fragment_id"]),
        content_version=int(row["content_version"]),
        state=str(row["state"]),
        attempts=int(row["attempts"]),
        last_error=row["last_error"],
        run_after=row["run_after"],
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


def claim_due(
    conn: sqlite3.Connection, *, limit: int = 5, kinds: tuple[str, ...] | None = None
) -> list[DerivedTask]:
    """取到期的任务并置为 running（一次短事务；同一任务不会被两个执行者同时拿走）。"""
    now = _iso(_now())
    sql = (
        "SELECT * FROM derived_tasks "
        "WHERE state IN (?, ?) AND (run_after IS NULL OR run_after <= ?)"
    )
    params: list[object] = [STATE_PENDING, STATE_FAILED, now]
    if kinds:
        sql += f" AND kind IN ({','.join('?' for _ in kinds)})"
        params.extend(kinds)
    sql += " ORDER BY created_at LIMIT ?"
    params.append(limit)
    with transaction(conn):
        rows = conn.execute(sql, params).fetchall()
        claimed: list[DerivedTask] = []
        for row in rows:
            conn.execute(
                "UPDATE derived_tasks SET state = ?, updated_at = ? WHERE id = ?",
                (STATE_RUNNING, now, row["id"]),
            )
            claimed.append(_from_row(row))
    return claimed


def complete(conn: sqlite3.Connection, task_id: str) -> None:
    conn.execute(
        "UPDATE derived_tasks SET state = ?, last_error = NULL, updated_at = ? WHERE id = ?",
        (STATE_COMPLETED, _iso(_now()), task_id),
    )


def fail(conn: sqlite3.Connection, task_id: str, error: str) -> None:
    """失败：记原因、次数 +1、安排下一次尝试时间（退避）。"""
    row = conn.execute("SELECT attempts FROM derived_tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        return
    attempts = int(row["attempts"]) + 1
    run_after = _iso(_now() + timedelta(seconds=backoff_delay(attempts)))
    conn.execute(
        "UPDATE derived_tasks SET state = ?, attempts = ?, last_error = ?, run_after = ?, "
        "updated_at = ? WHERE id = ?",
        (STATE_FAILED, attempts, error[:500], run_after, _iso(_now()), task_id),
    )


def recover_stale(
    conn: sqlite3.Connection, *, timeout_seconds: int = _STALE_RUNNING_SECONDS
) -> int:
    """进程重启后：把「卡在 running」的任务放回可重试状态（幂等，安全重复调用）。"""
    deadline = _iso(_now() - timedelta(seconds=timeout_seconds))
    cursor = conn.execute(
        "UPDATE derived_tasks SET state = ?, updated_at = ? "
        "WHERE state = ? AND updated_at <= ?",
        (STATE_PENDING, _iso(_now()), STATE_RUNNING, deadline),
    )
    return int(cursor.rowcount or 0)


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


def task_for(
    conn: sqlite3.Connection, kind: str, fragment_id: str, content_version: int
) -> DerivedTask | None:
    row = conn.execute(
        "SELECT * FROM derived_tasks WHERE kind = ? AND fragment_id = ? AND content_version = ?",
        (kind, fragment_id, content_version),
    ).fetchone()
    return _from_row(row) if row is not None else None
