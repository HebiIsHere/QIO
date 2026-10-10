# -*- coding: utf-8 -*-
"""F 组独立验证：R01（实例归属 / 启动恢复）、R04（持久接受）、R06（重发关联）。

这些用例**只依据可观察行为**断言，不引用任何组的内部实现细节：

* R01 —— 两个真实后端上下文共享同一个临时库时，「还活着」的实例的运行中记录
  不得被后启动的上下文标成 interrupted；并且它之后仍然能正常收口成 completed。
* R04 —— 在第二条消息的 journal 写入处注入一次真实 SQLite 写失败：HTTP 必须
  如实返回非 200（503）且 accepted=false，不产生内存里的假接受项，运行器不执行。
* R06 —— 「提交前退出」与「提交后、派发前退出」两个检查点：重启后至少留下一条
  可操作的恢复记录，重发成功时关联关系不悬空（recovered_by 非空），且重发是一次性。

模型一律 fake：R04/R06 的 runner 被换成只计数的桩，不触网、不调用任何真实 provider。
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent.api.server import create_app
from agent.config import Settings
from agent.storage.db import connect
from agent.storage.migrate import apply_migrations
from agent.storage.turn_journal import TurnJournal

# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


def _freshen(db_path: Path) -> sqlite3.Connection:
    conn = connect(db_path)
    apply_migrations(conn)
    return conn


def _journal_row(conn: sqlite3.Connection, turn_id: str) -> dict | None:
    row = conn.execute(
        "SELECT status, recovered_at, recovered_by FROM turn_journal WHERE turn_id = ?",
        (turn_id,),
    ).fetchone()
    return dict(row) if row is not None else None


def _journal_status(conn: sqlite3.Connection, turn_id: str) -> str | None:
    row = _journal_row(conn, turn_id)
    return None if row is None else str(row["status"])


async def _await_status(
    conn: sqlite3.Connection,
    turn_id: str,
    wanted: tuple[str, ...],
    *,
    attempts: int = 300,
) -> str | None:
    """有界等待（不随机 sleep）：每 10ms 看一次，最多约 3 秒。"""
    status: str | None = None
    for _ in range(attempts):
        status = _journal_status(conn, turn_id)
        if status in wanted:
            return status
        await asyncio.sleep(0.01)
    return status


def _wait_until(predicate, *, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return bool(predicate())


# ---------------------------------------------------------------------------
# R01：活跃实例的运行中记录不得被第二个后端上下文打断
# ---------------------------------------------------------------------------


def test_r01_second_context_does_not_interrupt_a_live_instances_turn(tmp_path: Path):
    """A 实例有一轮正在跑时启动 B（共享同一个库）：A 的记录必须保持 running，
    并且 A 之后仍然能正常收口成 completed。"""
    db_path = tmp_path / "shared.db"
    conn_a = _freshen(db_path)
    app_a = create_app(Settings(data_dir=tmp_path / "a"), conn_a)
    conn_b: sqlite3.Connection | None = None
    try:
        with TestClient(app_a) as ca:
            ctx_a = ca.app.state.ctx
            held: list[asyncio.Event] = []

            async def arm() -> str:
                gate = asyncio.Event()
                held.append(gate)

                async def blocked(ctx) -> None:  # noqa: ANN001 - 只做闸门
                    await gate.wait()

                ctx_a.turns.set_runner(blocked)
                return ctx_a.turns.submit("正在执行的消息").turn_id

            turn_id = ca.portal.call(arm)
            started = ca.portal.call(_await_status, ctx_a.conn, turn_id, ("running",))
            assert started == "running", f"这一轮应当已经在跑：{started}"

            # 第二个真实后端上下文（同一个临时库）
            conn_b = connect(db_path)
            ca.portal.call(create_app, Settings(data_dir=tmp_path / "b"), conn_b)

            assert _journal_status(conn_a, turn_id) == "running", (
                "A 仍然是活跃实例，B 启动不得把 A 的运行中记录标成 interrupted；"
                f"实际 {_journal_status(conn_a, turn_id)!r}"
            )

            ca.portal.call(held[0].set)
            final = ca.portal.call(
                _await_status,
                ctx_a.conn,
                turn_id,
                ("completed", "interrupted", "failed", "cancelled"),
            )
            assert final == "completed", (
                "被 B 打断的记录无法再收口：这一轮必须正常走到 completed，"
                f"实际 {final!r}"
            )
    finally:
        if conn_b is not None:
            conn_b.close()
        conn_a.close()


# ---------------------------------------------------------------------------
# R04：journal 写入失败 → 非 200、无假接受、运行器未执行
# ---------------------------------------------------------------------------


class _FlakyJournalConn(sqlite3.Connection):
    """可注入一次真实 SQLite 写失败的连接（只在 turn_journal 的受理写入上触发）。

    匹配方式很关键：真实 SQL 是 `INSERT OR IGNORE INTO turn_journal …`，
    所以用「以 insert 开头 + 语句里出现 turn_journal」；写成
    `startswith("insert into turn_journal")` 会一次都匹配不到，注入形同虚设。
    """

    inject_journal_failure = False

    def execute(self, sql, *args, **kwargs):  # type: ignore[override]
        text = " ".join(str(sql).split()).lower()
        if self.inject_journal_failure and text.startswith("insert") and "turn_journal" in text:
            self.inject_journal_failure = False  # 只注入一次
            raise sqlite3.OperationalError("disk I/O error (injected by rm-f)")
        return super().execute(sql, *args, **kwargs)


def _flaky_connect(db_path: Path) -> _FlakyJournalConn:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), check_same_thread=False, factory=_FlakyJournalConn)
    conn.isolation_level = None
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def test_r04_journal_write_failure_is_not_reported_as_accepted(tmp_path: Path):
    conn = _flaky_connect(tmp_path / "flaky.db")
    apply_migrations(conn)
    app = create_app(Settings(data_dir=tmp_path / "data"), conn)
    ran: list[str] = []
    try:
        with TestClient(app) as client:
            ctx = client.app.state.ctx

            async def counting(turn_ctx) -> None:  # noqa: ANN001 - 只计数
                ran.append(turn_ctx.turn_id)

            ctx.turns.set_runner(counting)

            first = client.post("/api/turns", json={"message": "第一条消息"})
            assert first.status_code == 200, first.text
            assert _wait_until(lambda: len(ran) == 1), "第一条消息应当被执行"

            conn.inject_journal_failure = True
            second = client.post("/api/turns", json={"message": "第二条消息"})

            assert second.status_code == 503, (
                "journal 写失败时不得装作已经受理：HTTP 必须是 503（非 200）；"
                f"实际 {second.status_code} / {second.text}"
            )
            body = second.json()
            assert body.get("accepted") is False, f"响应不得声称 accepted=true：{body}"

            # 不给内存里的假接受项留位置
            _wait_until(lambda: len(ran) > 1, timeout=0.5)
            assert len(ran) == 1, "持久化失败的消息不得进入执行"

            queue = client.get("/api/turns/queue").json()
            assert queue["queued"] == [], f"持久化失败的消息不得留在内存队列里：{queue}"
            assert queue["running"] is None, f"持久化失败的消息不得被运行：{queue}"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# R06：两个退出检查点 → 重启后可操作、关系不悬空、重发一次性
# ---------------------------------------------------------------------------


def _counting_runner(ran: list[str]):
    async def run(turn_ctx) -> None:  # noqa: ANN001
        ran.append(turn_ctx.turn_id)

    return run


def test_r06_accepted_but_never_dispatched_record_is_recoverable(tmp_path: Path):
    """检查点一：受理之后、派发之前退出 → 重启后必须能重发，且关联不悬空。"""
    conn = _freshen(tmp_path / "pre.db")
    journal = TurnJournal(conn)
    journal.accepted(turn_id="turn_pre", message="排队中就退出的消息")
    journal.interrupt_stale()
    assert [r["turn_id"] for r in journal.unfinished()] == ["turn_pre"]
    try:
        app = create_app(Settings(data_dir=tmp_path / "data"), conn)
        with TestClient(app) as client:
            ran: list[str] = []
            client.app.state.ctx.turns.set_runner(_counting_runner(ran))

            state = client.get("/api/runtime/state").json()
            assert "turn_pre" in [t["turn_id"] for t in state["interrupted_turns"]]

            resp = client.post("/api/turns/turn_pre/resend")
            assert resp.status_code == 200, resp.text
            row = _journal_row(conn, "turn_pre")
            assert row is not None
            assert row["recovered_at"], "重发后必须留下处理时间"
            assert row["recovered_by"], "重发后不得悬空：旧记录必须指向新 turn"

            again = client.post("/api/turns/turn_pre/resend")
            assert again.status_code == 409, "重发是一次性的，第二次必须明确拒绝"
    finally:
        conn.close()


def test_r06_claimed_but_undispatched_record_is_not_permanently_hidden(tmp_path: Path):
    """检查点二：抢占成功（recovered_at 已写）之后、mark_recovered 之前退出。

    这条记录既不能再被 `recoverable()` 选中（recovered_at 非空），又没有被标成
    「已处理」（recovered_by 为空）—— 基线把它永久隐藏：界面看不到、重发 409。
    契约 C3 对它的要求是：**不得永久隐藏**（权威状态里看得到）+ 有正式出口
    (`repair_orphan()`) 让它重新可重发 + 重发后关联不悬空 + 重发一次性。
    本用例逐条核对这四件事。
    """
    conn = _freshen(tmp_path / "post.db")
    journal = TurnJournal(conn)
    journal.accepted(turn_id="turn_crash", message="抢占之后崩溃的消息")
    journal.interrupt_stale()
    assert journal.claim("turn_crash") is True
    # 这里不调用 mark_recovered：模拟提交后、派发前进程退出
    try:
        app = create_app(Settings(data_dir=tmp_path / "data"), conn)
        with TestClient(app) as client:
            ctx = client.app.state.ctx
            ran: list[str] = []
            ctx.turns.set_runner(_counting_runner(ran))

            state = client.get("/api/runtime/state").json()
            interrupted = [t["turn_id"] for t in state["interrupted_turns"]]
            orphaned = [t["turn_id"] for t in state.get("orphaned_turns", [])]
            assert "turn_crash" in interrupted or "turn_crash" in orphaned, (
                "抢占后崩溃的遗留记录不得被永久隐藏：必须能在权威状态里看到；"
                f"interrupted_turns={interrupted}, orphaned_turns={orphaned}"
            )

            # 正式出口：repair_orphan() 让它重新回到「可重发」
            assert ctx.turn_journal.repair_orphan("turn_crash") is True, (
                f"孤儿记录必须能被修复为可重发；orphaned_turns={orphaned}"
            )

            resp = client.post("/api/turns/turn_crash/resend")
            assert resp.status_code == 200, resp.text
            row = _journal_row(conn, "turn_crash")
            assert row is not None
            assert row["recovered_at"], "重发后必须留下处理时间"
            assert row["recovered_by"], "重发后不得悬空：旧记录必须指向新 turn"

            again = client.post("/api/turns/turn_crash/resend")
            assert again.status_code == 409, "重发是一次性的，第二次必须明确拒绝"
    finally:
        conn.close()


if __name__ == "__main__":  # pragma: no cover - 便于单独手工运行
    raise SystemExit(pytest.main([__file__, "-q"]))
