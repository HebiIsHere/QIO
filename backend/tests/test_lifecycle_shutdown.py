"""R9 §11 桌面退出收尾：有界、幂等、不谎报、不误伤有效待审批。

守的事情（与契约 §11 逐条对应）：

* 路由有界等待 `aclose()`，超时如实返回 `timeout: true`（不谎报 done）；
* 重复调用幂等：`aclose()` 只执行一次，退出钩子只安排一次；
* 退出开始后新工作一律 503（读仍放行，退出接口本身可重复调用）；
* 关闭前「真的有人在等」的审批不会被取消 turn 连带结算成 cancelled；
* lifespan 与路由共用同一份关闭任务 —— 后端 aclose 一定获得执行机会。
"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from agent.api.lifecycle_routes import (
    SHUTDOWN_GRACE_SECONDS,
    ShutdownCoordinator,
    _restore_waiting_approvals,
    _snapshot_waiting_approvals,
)
from agent.api.server import create_app
from agent.config import Settings
from agent.services.lifecycle import CloseReport

UTC = timezone.utc


class _FakeCtx:
    """只提供 aclose / approvals / conn 的最小替身。"""

    def __init__(self, *, report: CloseReport | None = None, delay: float = 0.0) -> None:
        self.conn = None
        self.approvals = None
        self.closed = 0
        self.delay = delay
        self._report = report or CloseReport(
            clean=True, phases=("background_stopped",), detail="fake clean"
        )

    async def aclose(self) -> CloseReport:
        self.closed += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        return self._report


async def test_shutdown_is_idempotent_and_arms_exit_once() -> None:
    ctx = _FakeCtx()
    coordinator = ShutdownCoordinator(grace_seconds=1.0)
    armed: list[int] = []
    coordinator.set_exit_hook(lambda: armed.append(1))

    first = await coordinator.request_shutdown(ctx)
    assert first["done"] is True
    assert first["already"] is False
    assert first["timeout"] is False
    assert first["report"]["clean"] is True
    assert ctx.closed == 1

    second = await coordinator.request_shutdown(ctx)
    assert second["done"] is True
    assert second["already"] is True
    assert ctx.closed == 1, "重复调用不得再次执行 aclose"

    await asyncio.sleep(0.4)  # 退出钩子按「响应先写完」延后 0.25 秒
    assert armed == [1], "退出钩子只能安排一次"
    state = coordinator.snapshot()
    assert state["shutting_down"] is True and state["done"] is True


async def test_timeout_is_reported_honestly_and_close_task_keeps_running() -> None:
    gate = asyncio.Event()

    class SlowCtx(_FakeCtx):
        async def aclose(self) -> CloseReport:  # type: ignore[override]
            self.closed += 1
            await gate.wait()
            return CloseReport(clean=True, detail="late clean")

    ctx = SlowCtx()
    coordinator = ShutdownCoordinator(grace_seconds=0.05)
    out = await coordinator.request_shutdown(ctx)
    assert out["done"] is False
    assert out["timeout"] is True, "超过预算必须如实说超时"
    assert out["report"]["clean"] is False
    assert coordinator.is_shutting_down() is True

    # 超时不取消收尾任务：放行之后它仍然会完成（后端生命周期收尾获得执行机会）。
    gate.set()
    report = await coordinator.aclose_for_lifespan(ctx)
    assert report.clean is True
    assert coordinator.snapshot()["done"] is True


async def test_shutdown_wait_budget_is_the_contract_default() -> None:
    # 契约 §11 初值 8.0 秒：改动必须改契约，不能悄悄漂移。
    assert SHUTDOWN_GRACE_SECONDS == 8.0
    assert ShutdownCoordinator().grace_seconds == 8.0


# ---------------------------------------------------------------------------
# 有效待审批：关闭不是「用户取消」
# ---------------------------------------------------------------------------


def _insert_pending(
    conn: sqlite3.Connection,
    approval_id: str,
    *,
    created_at: str,
    expires_at: str,
) -> None:
    conn.execute(
        "INSERT INTO pending_approvals "
        "(approval_id, kind, payload, turn_id, session_id, created_at, expires_at, "
        " status, owner_instance_id) "
        "VALUES (?, 'fs.write', '{}', 'turn-1', 'session-1', ?, ?, 'pending', 'inst-test')",
        (approval_id, created_at, expires_at),
    )
    conn.commit()


def _status(conn: sqlite3.Connection, approval_id: str) -> tuple[str, str | None]:
    row = conn.execute(
        "SELECT status, resolved_at FROM pending_approvals WHERE approval_id = ?",
        (approval_id,),
    ).fetchone()
    return str(row[0]), row[1]


def test_snapshot_reads_only_real_waiters() -> None:
    class _Approvals:
        def pending(self) -> list[dict]:
            return [
                {"approval_id": "appr_a", "expires_at": "2030-01-01T00:00:00+00:00",
                 "created_at": "2026-10-22T00:00:00+00:00"},
                {"approval_id": "", "expires_at": "2030-01-01T00:00:00+00:00"},
                {"not": "a waiter"},
            ]

    ctx = SimpleNamespace(approvals=_Approvals())
    snapshot = _snapshot_waiting_approvals(ctx)
    assert [item["approval_id"] for item in snapshot] == ["appr_a"]


def test_restore_waiting_approvals_only_touches_valid_shutdown_cancellations(
    db_conn: sqlite3.Connection,
) -> None:
    now = datetime.now(UTC)
    started = now.isoformat()
    later = (now + timedelta(seconds=1)).isoformat()
    expired = (now - timedelta(seconds=5)).isoformat()
    future = (now + timedelta(minutes=10)).isoformat()

    _insert_pending(db_conn, "appr_valid", created_at=started, expires_at=future)
    _insert_pending(db_conn, "appr_expired", created_at=started, expires_at=expired)
    _insert_pending(db_conn, "appr_user_cancelled", created_at=started, expires_at=future)
    _insert_pending(db_conn, "appr_approved", created_at=started, expires_at=future)

    # 模拟 aclose 取消在跑的 turn 时 base 审批做的结算。
    db_conn.execute(
        "UPDATE pending_approvals SET status = 'cancelled', resolved_at = ?", (later,)
    )
    # 用户在此前主动取消的：不该被复活。
    db_conn.execute(
        "UPDATE pending_approvals SET resolved_at = ? WHERE approval_id = 'appr_user_cancelled'",
        ((now - timedelta(seconds=30)).isoformat(),),
    )
    # 已经批准的不该被改回 pending。
    db_conn.execute(
        "UPDATE pending_approvals SET status = 'approved', resolved_at = ? "
        "WHERE approval_id = 'appr_approved'",
        (later,),
    )
    db_conn.commit()

    snapshot = [
        {"approval_id": "appr_valid", "expires_at": future, "created_at": started},
        {"approval_id": "appr_expired", "expires_at": expired, "created_at": started},
        {"approval_id": "appr_user_cancelled", "expires_at": future, "created_at": started},
        {"approval_id": "appr_approved", "expires_at": future, "created_at": started},
    ]
    ctx = SimpleNamespace(conn=db_conn, approvals=None)
    restored = _restore_waiting_approvals(ctx, snapshot, shutdown_started_at=started)

    assert restored == 1
    assert _status(db_conn, "appr_valid") == ("pending", None)
    assert _status(db_conn, "appr_expired")[0] == "cancelled", "已过期不复活"
    assert _status(db_conn, "appr_user_cancelled")[0] == "cancelled", "用户此前取消不复活"
    assert _status(db_conn, "appr_approved")[0] == "approved", "已批准不被改回等待"


# ---------------------------------------------------------------------------
# 真实应用：退出接口 + 503 闸门 + 幂等 + 退出钩子
# ---------------------------------------------------------------------------


def _client(app) -> TestClient:  # noqa: ANN001 - 测试辅助
    from agent.credentials.store import MemoryKeyring

    app.state.ctx.credentials._kr = MemoryKeyring()
    return TestClient(app)


def test_shutdown_route_runs_aclose_once_and_rejects_new_work(settings: Settings, db_conn) -> None:
    app = create_app(settings, db_conn)
    ctx = app.state.ctx
    coordinator = app.state.shutdown_coordinator
    armed: list[int] = []
    coordinator.set_exit_hook(lambda: armed.append(1))

    with _client(app) as client:
        health = client.get("/api/health")
        assert health.status_code == 200

        first = client.post("/api/lifecycle/shutdown")
        assert first.status_code == 200
        body = first.json()
        assert body["done"] is True and body["timeout"] is False
        assert body["report"]["clean"] is True, body
        assert body["approvals_restored"] == 0

        # 退出开始后：写请求 503（新工作不再被受理），读请求仍放行。
        blocked = client.put("/api/settings/ui", json={"typewriter_cps": 75})
        assert blocked.status_code == 503
        assert blocked.json()["code"] == "shutting_down"
        assert client.get("/api/lifecycle/state").status_code == 200

        # 重复调用：幂等，不重新收尾；后端 aclose 只执行一次。
        second = client.post("/api/lifecycle/shutdown")
        assert second.status_code == 200
        assert second.json()["already"] is True
        assert second.json()["done"] is True

    assert ctx.turns._closed is True, "后端 aclose 必须获得执行机会"
    assert ctx.task_manager.is_closed() is True


def test_shutdown_route_arms_injected_exit_hook(settings: Settings, db_conn) -> None:
    app = create_app(settings, db_conn)
    coordinator = app.state.shutdown_coordinator
    armed: list[int] = []
    coordinator.set_exit_hook(lambda: armed.append(1))

    with _client(app) as client:
        assert client.post("/api/lifecycle/shutdown").status_code == 200
        # 给「响应先写完」的延迟留出时间（0.25 秒）。
        import time as _time

        deadline = _time.monotonic() + 2.0
        while not armed and _time.monotonic() < deadline:
            _time.sleep(0.05)

    assert armed == [1], "退出收尾完成后必须（且只能）请求进程退出一次"
