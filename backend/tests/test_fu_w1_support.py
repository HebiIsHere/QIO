"""W1 受控验收的共享夹具（只放 helper，用例在 test_fu_w1_*.py 里）。

规矩：**不结束任何真实进程**、不联网、不碰真实模型；实例存活一律用
`exited_at` / 心跳 / 注入的 `pid_alive` 替身来判定，绝不依赖本机真实进程。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from agent.storage.db import connect
from agent.storage.instance_registry import InstanceRegistry
from agent.storage.migrate import apply_migrations

HOST = "w1-test-host"


def migrated_conn(tmp_path: Path, name: str = "app.db") -> sqlite3.Connection:
    conn = connect(tmp_path / name)
    apply_migrations(conn)
    return conn


def shadow_conn(conn: sqlite3.Connection) -> sqlite3.Connection:
    """同一份库的第二条连接（模拟第二个实例 / 第二个并发请求）。"""
    path = None
    for row in conn.execute("PRAGMA database_list").fetchall():
        if str(row[1]) == "main" and row[2]:
            path = str(row[2])
    if path is None:  # pragma: no cover - 夹具只用于文件库
        raise AssertionError("shadow_conn 需要文件库")
    other = sqlite3.connect(path)
    other.row_factory = sqlite3.Row
    other.isolation_level = None
    other.execute("PRAGMA busy_timeout = 5000")
    return other


def add_instance(
    conn: sqlite3.Connection,
    instance_id: str,
    *,
    exited: bool = False,
    pid: int = 4242,
    host: str = HOST,
    heartbeat_seconds_ago: float = 0.0,
) -> str:
    """在 instances 表里登记一个实例。

    `exited=True` → 显式退出（`owner_alive` 必须返回 False，确定性，不探测 pid）；
    否则心跳新鲜 → 活（同样不探测 pid）。两种都**不依赖本机真实进程**。
    """
    moment = datetime.now(timezone.utc) - timedelta(seconds=heartbeat_seconds_ago)
    conn.execute(
        "INSERT OR REPLACE INTO instances "
        "(instance_id, pid, host, started_at, last_heartbeat, exited_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            instance_id,
            int(pid),
            host,
            moment.isoformat(),
            moment.isoformat(),
            moment.isoformat() if exited else None,
        ),
    )
    return instance_id


def add_turn(
    conn: sqlite3.Connection,
    turn_id: str,
    *,
    message: str = "没有执行完的消息",
    topic_id: str | None = None,
    notify: int = 0,
    status: str = "queued",
    owner: str | None = None,
    recovered_at: str | None = None,
    recovered_by: str | None = None,
    reason: str | None = None,
    created_at: str | None = None,
    publish_owner: bool = True,
) -> str:
    moment = created_at or datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO turn_journal "
        "(turn_id, message, topic_id, notify, status, created_at, updated_at, reason, "
        " owner_instance_id, recovered_at, recovered_by) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            turn_id,
            message,
            topic_id,
            int(notify),
            status,
            moment,
            moment,
            reason,
            owner,
            recovered_at,
            recovered_by,
        ),
    )
    if owner and publish_owner:
        conn.execute(
            "INSERT OR REPLACE INTO record_owners (record_type, record_id, instance_id) "
            "VALUES ('turn', ?, ?)",
            (turn_id, owner),
        )
    return turn_id


def add_derived(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    state: str = "running",
    owner: str | None = None,
    claim_generation: int = 0,
    attempts: int = 0,
    last_error: str | None = None,
    kind: str = "summary",
    created_at: str | None = None,
    publish_owner: bool = True,
) -> str:
    moment = created_at or datetime.now(timezone.utc).isoformat()
    columns = (
        "id, kind, fragment_id, content_version, state, attempts, last_error, run_after, "
        "created_at, updated_at, owner_instance_id, claim_generation"
    )
    conn.execute(
        f"INSERT INTO derived_tasks ({columns}) VALUES (?, ?, ?, 1, ?, ?, ?, NULL, ?, ?, ?, ?)",
        (
            task_id,
            kind,
            f"frag_{task_id}",
            state,
            int(attempts),
            last_error,
            moment,
            moment,
            owner,
            int(claim_generation),
        ),
    )
    if owner and publish_owner:
        conn.execute(
            "INSERT OR REPLACE INTO record_owners (record_type, record_id, instance_id) "
            "VALUES ('derived_task', ?, ?)",
            (task_id, owner),
        )
    return task_id


def confirm_legacy_stopped(
    conn: sqlite3.Connection, kind: str, record_id: str, instance_id: str = "me"
) -> str:
    """F01：受控用例里显式记下「旧执行者已停止」的确认（等价于用户点那一次确认）。

    无归属记录的 continue / ignore / repair / requeue 都要求先有这个确认 ——
    旧版本不写任何生命周期记录，库里没有证据说明它的执行者停了。
    """
    from agent.services.recovery import LegacyStopConfirmations

    return LegacyStopConfirmations(conn).confirm(
        kind, record_id, instance_id=instance_id, note="受控用例"
    )


def register_self(conn: sqlite3.Connection, instance_id: str = "me") -> InstanceRegistry:
    """把「本实例」登记成活着（生产路径里 AppContext 会 start() 它）。"""
    registry = InstanceRegistry(conn, instance_id, pid=4242, host=HOST)
    add_instance(conn, instance_id, exited=False)
    return registry


def table_snapshot(conn: sqlite3.Connection, table: str) -> list[tuple]:
    rows = conn.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
    return [tuple(row) for row in rows]


class FakeTurns:
    """最小的派发替身：记录每一次 submit，返回一个带 turn_id / status 的对象。"""

    class _Turn:
        def __init__(self, turn_id: str, status: str = "accepted") -> None:
            self.turn_id = turn_id
            self.status = status

    def __init__(self, status: str = "accepted") -> None:
        self.calls: list[dict[str, Any]] = []
        self._status = status

    def submit(
        self,
        message: str,
        topic_id: str | None = None,
        *,
        notify: bool = False,
        intent_id: str | None = None,
        turn_id: str | None = None,
    ) -> "FakeTurns._Turn":
        self.calls.append(
            {
                "message": message,
                "topic_id": topic_id,
                "notify": notify,
                "intent_id": intent_id,
                "turn_id": turn_id,
            }
        )
        return FakeTurns._Turn(str(turn_id), self._status)

    @property
    def submitted(self) -> list[str]:
        return [str(call["turn_id"]) for call in self.calls]
