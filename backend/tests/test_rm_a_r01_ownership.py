"""R01 受控验收：实例归属 —— 新实例启动不得动正在工作的实例。

缺陷（基线实测）：
`AppContext.__init__` 里 `TurnJournal.interrupt_stale()` 与
`ApprovalService._mark_interrupted()` 都是**无条件**把库里所有
`queued` / `running` / `pending` 标成 interrupted —— 隐含假设「库只有一个写入者」。
第二个后端实例一启动，还在跑的那个实例的任务与待确认事项就被全部标成中断；
派生任务也会被重复认领。

修好之后的判据（契约 C1，全部用受控时钟，不 sleep）：
显式 exited_at → 死；心跳新鲜 → 活；心跳过期且 pid 不存在 → 死；其余 → unknown。
**unknown 一律不改状态。**
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from agent.api.bus import EventBus
from agent.config import Settings
from agent.credentials.store import MemoryKeyring
from agent.services import derived_tasks
from agent.services.app import AppContext
from agent.storage.db import connect
from agent.storage.instance_registry import (
    HEARTBEAT_TTL_SECONDS,
    RECORD_APPROVAL,
    RECORD_DERIVED_TASK,
    RECORD_TURN,
    InstanceRegistry,
)
from agent.storage.migrate import apply_migrations
from agent.storage.turn_journal import INTERRUPTED, QUEUED, RUNNING, TurnJournal

# 一个几乎不可能存在的 pid（Linux pid 上限 4194304；Windows pid 是 4 的倍数且远小于它）
GHOST_PID = 999_999_999


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _ctx(tmp_path, name: str = "app.db") -> AppContext:
    """真实的后端上下文：共享同一份临时库时，多个实例看到的是一套数据。"""
    conn = connect(tmp_path / name)
    apply_migrations(conn)
    ctx = AppContext(Settings(data_dir=tmp_path), conn, EventBus())
    ctx.credentials._kr = MemoryKeyring()
    return ctx


def _seed_dead_instance(
    conn: sqlite3.Connection,
    instance_id: str,
    *,
    heartbeat_age: float = HEARTBEAT_TTL_SECONDS * 4,
    pid: int | None = GHOST_PID,
    exited: bool = False,
    host: str | None = None,
) -> None:
    """造一个「上一个进程留下的实例」：心跳过期 + pid 不存在（或显式退出）。"""
    last = (_now() - timedelta(seconds=heartbeat_age)).isoformat()
    exited_at = _now().isoformat() if exited else None
    conn.execute(
        "INSERT OR REPLACE INTO instances "
        "(instance_id, pid, host, started_at, last_heartbeat, exited_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (instance_id, pid, host or _host(), last, last, exited_at),
    )


def _host() -> str:
    import socket

    return socket.gethostname()


# -- 判据本体（纯函数式的受控时钟，不依赖任何真实进程）--------------------------


def test_liveness_needs_more_than_a_timestamp(db_conn: sqlite3.Connection):
    now = _now()

    def clock() -> datetime:
        return now

    registry = InstanceRegistry(db_conn, "me", pid=1, clock=clock)
    registry.start()

    # 1) 显式退出 → 死（权威判据，不看时间也不看 pid）
    _seed_dead_instance(db_conn, "exited", heartbeat_age=1, pid=1, exited=True)
    assert registry.owner_alive("exited") is False

    # 2) 心跳新鲜 → 活（哪怕 pid 看起来不存在：新鲜心跳就是「我还在」）
    _seed_dead_instance(db_conn, "fresh", heartbeat_age=1, pid=GHOST_PID)
    assert registry.owner_alive("fresh") is True

    # 3) 心跳过期 + pid 不存在 → 死
    _seed_dead_instance(db_conn, "expired_ghost", heartbeat_age=HEARTBEAT_TTL_SECONDS * 2)
    assert registry.owner_alive("expired_ghost") is False

    # 4) 心跳过期 + pid 判不出来（NULL）→ unknown（不许当死）
    _seed_dead_instance(db_conn, "expired_nopid", heartbeat_age=HEARTBEAT_TTL_SECONDS * 2, pid=None)
    assert registry.owner_alive("expired_nopid") is None

    # 5) 心跳过期 + 别的机器上的 pid → unknown（本机探测结果不能替它下结论）
    _seed_dead_instance(
        db_conn,
        "expired_otherhost",
        heartbeat_age=HEARTBEAT_TTL_SECONDS * 2,
        host="another-host",
    )
    assert registry.owner_alive("expired_otherhost") is None

    # 6) 没有实例行（旧记录）→ unknown
    assert registry.owner_alive("never_seen") is None
    assert registry.owner_alive(None) is None

    # live_instance_ids 只收「确认活着」的：unknown 不在其中
    assert set(registry.live_instance_ids()) == {"me", "fresh"}
    report = registry.classify_instances()
    assert set(report["unknown"]) == {"expired_nopid", "expired_otherhost"}
    assert set(report["alive"]) == {"me", "fresh"}
    assert set(report["dead"]) == {"exited", "expired_ghost"}


def test_heartbeat_expires_against_the_injected_clock(db_conn: sqlite3.Connection):
    """同一个实例行，只推进假时钟：新鲜 → unknown/死。时间阈值必须真的起作用。"""
    now = _now()
    state = {"now": now}
    registry = InstanceRegistry(
        db_conn, "peer", pid=GHOST_PID, clock=lambda: state["now"]
    )
    # 直接写行（不用 start：start 会用心跳时间覆盖）
    _seed_dead_instance(db_conn, "peer", heartbeat_age=1, pid=GHOST_PID)
    assert registry.owner_alive("peer") is True  # 心跳新鲜
    state["now"] = now + timedelta(seconds=HEARTBEAT_TTL_SECONDS + 5)
    assert registry.owner_alive("peer") is False  # 过期 + pid 不存在


def test_expired_heartbeat_with_live_pid_is_alive(db_conn: sqlite3.Connection):
    """心跳过期但 pid 还在本机 → 活（长任务不刷心跳不该被误杀）。"""
    import os

    registry = InstanceRegistry(db_conn, "me", pid=os.getpid())
    registry.start()
    _seed_dead_instance(
        db_conn, "slow", heartbeat_age=HEARTBEAT_TTL_SECONDS * 3, pid=os.getpid()
    )
    assert registry.owner_alive("slow") is True


# -- 两个真实后端上下文共享一份库（R01 的主验收）------------------------------


def test_second_instance_does_not_touch_a_live_instance(tmp_path):
    ctx_a = _ctx(tmp_path)
    topic = ctx_a.topics.nodes.create_topic("归属话题").id
    # A 正在工作：一条还在跑的 turn + 一条排队的 turn + 一个正在执行的派生任务
    # + 一个正在等用户决定的审批。
    ctx_a.turn_journal.accepted(turn_id="turn_a_run", message="A 正在跑", topic_id=topic)
    ctx_a.turn_journal.running("turn_a_run")
    ctx_a.turn_journal.accepted(turn_id="turn_a_queue", message="A 排着队", topic_id=topic)
    ctx_a.conn.execute(
        "INSERT INTO derived_tasks (id, kind, fragment_id, content_version, state, attempts, "
        "owner_instance_id, created_at, updated_at) "
        "VALUES ('task_a', 'summary', 'frag_a', 1, 'running', 0, ?, ?, ?)",
        (ctx_a.instance_id, _now().isoformat(), _now().isoformat()),
    )
    ctx_a.conn.execute(
        "INSERT INTO pending_approvals (approval_id, kind, payload, created_at, expires_at, "
        "status, owner_instance_id) VALUES ('appr_a', 'tool_execution', '{}', ?, ?, 'pending', ?)",
        (_now().isoformat(), (_now() + timedelta(minutes=5)).isoformat(), ctx_a.instance_id),
    )
    ctx_a.instances.claim(RECORD_DERIVED_TASK, "task_a", ctx_a.instance_id)
    ctx_a.instances.claim(RECORD_APPROVAL, "appr_a", ctx_a.instance_id)

    # B 启动（A 还活着：B 的心跳刚写，A 的也刚写）
    ctx_b = _ctx(tmp_path)
    assert ctx_b.instance_id != ctx_a.instance_id

    # 队列台账：一行都没被 B 改
    rows = {
        r["turn_id"]: (r["status"], r["reason"])
        for r in ctx_b.conn.execute(
            "SELECT turn_id, status, reason FROM turn_journal WHERE turn_id LIKE 'turn_a%'"
        ).fetchall()
    }
    assert rows == {"turn_a_run": (RUNNING, None), "turn_a_queue": (QUEUED, None)}
    assert ctx_b.turn_journal.unfinished() == []
    assert ctx_b.recovered_turns == []

    # 待确认事项：B 不许把 A 正在等的审批标成 interrupted
    status = ctx_b.conn.execute(
        "SELECT status FROM pending_approvals WHERE approval_id = 'appr_a'"
    ).fetchone()["status"]
    assert status == "pending"

    # 派生任务：B 不许把 A 正在执行的标成可重试（也就是不许重复认领）
    state = ctx_b.conn.execute(
        "SELECT state FROM derived_tasks WHERE id = 'task_a'"
    ).fetchone()["state"]
    assert state == "running"
    assert ctx_b.instance_recovery_report["handled"] == {}

    # A 的归属仍然记在 A 名下，没有被 B 抢走
    assert ctx_b.instances.owner_instance_id(RECORD_DERIVED_TASK, "task_a") == ctx_a.instance_id


def test_derived_task_is_not_reclaimed_while_owner_is_alive(tmp_path):
    """B 启动后 drain/claim 也不该拿到 A 的任务（否则就是重复执行）。"""
    ctx_a = _ctx(tmp_path)
    ctx_a.conn.execute(
        "INSERT INTO derived_tasks (id, kind, fragment_id, content_version, state, attempts, "
        "owner_instance_id, created_at, updated_at) "
        "VALUES ('task_a', 'summary', 'frag_a', 1, 'running', 0, ?, ?, ?)",
        (ctx_a.instance_id, _now().isoformat(), _now().isoformat()),
    )
    ctx_a.instances.claim(RECORD_DERIVED_TASK, "task_a", ctx_a.instance_id)
    ctx_b = _ctx(tmp_path)
    claimed = [t.id for t in derived_tasks.claim_due(ctx_b.conn, limit=10)]
    assert claimed == []


def test_unknown_owner_state_changes_nothing(tmp_path):
    """归属判不出来（心跳过期 + pid 未知）→ 一律不改状态，只计数。"""
    ctx_a = _ctx(tmp_path)
    topic = ctx_a.topics.nodes.create_topic("未知话题").id
    # A 的记录先写好，然后把 A 的行改成「判不出来」：心跳过期 + pid 为 NULL
    ctx_a.turn_journal.accepted(turn_id="turn_a", message="A 的消息", topic_id=topic)
    ctx_a.turn_journal.running("turn_a")
    _seed_dead_instance(
        ctx_a.conn, ctx_a.instance_id, heartbeat_age=HEARTBEAT_TTL_SECONDS * 3, pid=None
    )
    ctx_a.conn.execute(
        "INSERT INTO derived_tasks (id, kind, fragment_id, content_version, state, attempts, "
        "owner_instance_id, created_at, updated_at) "
        "VALUES ('task_a', 'summary', 'frag_a', 1, 'running', 0, ?, ?, ?)",
        (ctx_a.instance_id, _now().isoformat(), _now().isoformat()),
    )
    ctx_a.instances.claim(RECORD_DERIVED_TASK, "task_a", ctx_a.instance_id)

    ctx_b = _ctx(tmp_path)
    assert ctx_b.instance_recovery_report["deferred"].get(ctx_a.instance_id)
    assert ctx_b.turn_journal.unfinished() == []
    status = ctx_b.conn.execute(
        "SELECT status FROM turn_journal WHERE turn_id = 'turn_a'"
    ).fetchone()["status"]
    assert status == RUNNING, "unknown 不得改状态"
    state = ctx_b.conn.execute(
        "SELECT state FROM derived_tasks WHERE id = 'task_a'"
    ).fetchone()["state"]
    assert state == "running"
    # 归属没被清掉：等下一次维护重新判定
    assert ctx_b.instances.owner_instance_id(RECORD_DERIVED_TASK, "task_a") == ctx_a.instance_id


def test_confirmed_dead_instance_records_are_recovered(tmp_path):
    """A 真的退出（显式 exited_at）→ 下一个实例才恢复它的记录。"""
    ctx_a = _ctx(tmp_path)
    topic = ctx_a.topics.nodes.create_topic("恢复话题").id
    ctx_a.turn_journal.accepted(turn_id="turn_a", message="A 没执行完", topic_id=topic)
    ctx_a.turn_journal.running("turn_a")
    ctx_a.conn.execute(
        "INSERT INTO derived_tasks (id, kind, fragment_id, content_version, state, attempts, "
        "owner_instance_id, created_at, updated_at) "
        "VALUES ('task_a', 'summary', 'frag_a', 1, 'running', 0, ?, ?, ?)",
        (ctx_a.instance_id, _now().isoformat(), _now().isoformat()),
    )
    ctx_a.instances.claim(RECORD_DERIVED_TASK, "task_a", ctx_a.instance_id)

    # A 干净退出（真实 aclose 会写这一行；这里显式写，不启动第二个应用）
    ctx_a.instances.mark_clean_exit()

    ctx_b = _ctx(tmp_path)
    recovered = {row["turn_id"]: row for row in ctx_b.turn_journal.unfinished()}
    assert "turn_a" in recovered
    assert recovered["turn_a"]["status"] == INTERRUPTED
    assert recovered["turn_a"]["reason"] == "running_at_restart"
    assert recovered["turn_a"]["message"] == "A 没执行完"

    task = ctx_b.conn.execute(
        "SELECT state, owner_instance_id, last_error FROM derived_tasks WHERE id = 'task_a'"
    ).fetchone()
    assert task["state"] == "pending"  # 只放回可重试，不在这里执行
    assert task["owner_instance_id"] is None
    assert "已退出" in (task["last_error"] or "")


def test_clean_exit_is_written_by_aclose(tmp_path):
    """真实关闭路径：aclose() 必须写下 exited_at（否则就要靠心跳+pid 兜底）。"""
    from fastapi.testclient import TestClient

    from agent.api.server import create_app

    conn = connect(tmp_path / "app.db")
    apply_migrations(conn)
    app = create_app(Settings(data_dir=tmp_path), conn)
    with TestClient(app):
        pass
    row = conn.execute(
        "SELECT exited_at FROM instances WHERE instance_id = ?", (app.state.ctx.instance_id,)
    ).fetchone()
    assert row is not None and row["exited_at"]


def test_legacy_rows_without_owner_are_preserved(tmp_path):
    """旧记录没有归属（owner_instance_id 为 NULL）→ 保守保留 + 计数，不自动中断。"""
    ctx = _ctx(tmp_path)
    topic = ctx.topics.nodes.create_topic("旧记录").id
    ctx.conn.execute(
        "INSERT INTO turn_journal (turn_id, message, topic_id, notify, status, created_at, "
        "updated_at) VALUES ('turn_legacy', '旧版本留下的消息', ?, 0, 'running', ?, ?)",
        (topic, _now().isoformat(), _now().isoformat()),
    )
    ctx2 = _ctx(tmp_path)
    status = ctx2.conn.execute(
        "SELECT status FROM turn_journal WHERE turn_id = 'turn_legacy'"
    ).fetchone()["status"]
    assert status == "running", "没有归属的旧记录不得被自动中断"
    assert ctx2.instance_recovery_report["legacy_unowned"]["turn_journal"] >= 1
