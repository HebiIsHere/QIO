"""P2-A 定向测试的共享夹具（只放 helper；用例在 test_p2_a_*.py 里）。

模型一律 fake：FakeRunner 是 async 替身，不联网、不碰真实 API Key。
迁移：先跑既有的 apply_migrations（到 45），再应用本分支的迁移 46
（storage/migration_p2_a.py）—— 与 Lead 整合后的真实路径等价。
"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from agent.maintenance.changes import record_effective_change
from agent.maintenance.models import MaintenanceRunOutcome
from agent.storage import migration_p2_a
from agent.storage.db import connect
from agent.storage.migrate import apply_migrations


def migrated_conn(tmp_path: Path, name: str = "p2a.db") -> sqlite3.Connection:
    """建库并应用迁移（含本分支的迁移 46）。"""
    conn = connect(tmp_path / name)
    apply_migrations(conn)
    migration_p2_a.apply(conn)
    return conn


class FakeSettings(dict):
    """settings 替身：dict.get + get_int，与 SettingsStore 的读取口径一致。"""

    def get_int(self, key: str, default: int) -> int:
        try:
            return int(self.get(key, default))
        except (TypeError, ValueError):
            return default


class FakeClock:
    """受控时钟：默认返回 datetime；测试用 advance() 制造超时 / 时间前进。"""

    def __init__(self, moment: datetime | None = None) -> None:
        self.moment = moment or datetime(2026, 10, 21, 4, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.moment

    def advance(self, *, seconds: float = 0, minutes: float = 0, hours: float = 0) -> datetime:
        self.moment = self.moment + timedelta(seconds=seconds, minutes=minutes, hours=hours)
        return self.moment


class FakeRunner:
    """可编程 async runner：记录收到的快照，可返回结果 / 抛错 / 阻塞（并发测试）。"""

    def __init__(self, outcome: MaintenanceRunOutcome | None = None) -> None:
        self.calls: list[Any] = []
        self.outcome = outcome if outcome is not None else outcome_ok()
        self.error: BaseException | None = None
        self.gate: asyncio.Event | None = None
        self.on_call: Any = None

    async def run_batch(self, snapshot):
        self.calls.append(snapshot)
        if self.on_call is not None:
            self.on_call(snapshot)
        if self.gate is not None:
            await self.gate.wait()
        if self.error is not None:
            raise self.error
        return self.outcome


def outcome_ok(**fields: Any) -> MaintenanceRunOutcome:
    payload: dict[str, Any] = {
        "ok": True,
        "status": "completed",
        "batch_id": None,
        "processed_change_seq": None,
        "applied": 0,
        "asked": 0,
        "rejected": 0,
        "error": None,
    }
    payload.update(fields)
    return MaintenanceRunOutcome(**payload)


def outcome_failed(error: str = "fake failure", **fields: Any) -> MaintenanceRunOutcome:
    payload: dict[str, Any] = {
        "ok": False,
        "status": "failed",
        "batch_id": None,
        "processed_change_seq": None,
        "error": error,
    }
    payload.update(fields)
    return MaintenanceRunOutcome(**payload)


def add_changes(
    conn: sqlite3.Connection,
    count: int,
    *,
    start: int = 1,
    source: str = "background_extraction",
) -> list[int]:
    """通过正式接口批量造变化；返回 knowledge 序号。"""
    created: list[int] = []
    for index in range(start, start + count):
        recorded = record_effective_change(
            conn,
            knowledge_id=f"kid-{index}",
            chain_id=f"chain-{index}",
            version=1,
            source=source,
        )
        assert recorded is True
        created.append(index)
    return created


def add_confirmed_turns(conn: sqlite3.Connection, scheduler, count: int, *, prefix: str = "turn") -> None:
    """按真实成功用户 turn 的调用方式累计等待轮次。"""
    for index in range(count):
        scheduler.note_user_turn_completed(f"{prefix}-{index}")


def change_seq(conn: sqlite3.Connection, knowledge_id: str) -> int:
    row = conn.execute(
        "SELECT seq FROM knowledge_change_log WHERE knowledge_id = ?", (knowledge_id,)
    ).fetchone()
    assert row is not None
    return int(row["seq"])


def pending_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM knowledge_change_log ORDER BY seq"
    ).fetchall()


def state_row(conn: sqlite3.Connection) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM knowledge_maintenance_state WHERE id = 1").fetchone()
    assert row is not None
    return row
