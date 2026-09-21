"""应用级 startup / shutdown：后台的东西必须真的启动，也必须真的结束。

真实缺陷：

* `create_app()` 里同步调用 `maintenance.start()`，没有 running loop 时抛 RuntimeError
  又被 `except RuntimeError: pass` 吞掉 —— 所以后台维护**从来没有真正启动过**，
  而且没人知道；
* `MaintenanceScheduler._loop` 引用了并不存在的 `ctx._active_loop`，
  异常还发生在 try 之外 —— 真跑起来也会被自己弄死；
* `AppContext.aclose()` 只关 adapter：TurnManager / TaskManager / DB 都没进关机流程；
* `TurnManager` / `TaskManager` 关闭之后仍然接受新任务，新任务永远不会被执行。
"""

from __future__ import annotations

import asyncio
import sqlite3

import pytest
from fastapi.testclient import TestClient

from agent.api.server import create_app
from agent.config import Settings
from agent.core.turn import TurnManager


@pytest.fixture()
def app_settings(tmp_path) -> Settings:
    return Settings(data_dir=tmp_path)


def test_maintenance_runs_after_real_startup(app_settings, db_conn):
    from agent.credentials.store import MemoryKeyring

    app = create_app(app_settings, db_conn)
    ctx = app.state.ctx
    ctx.credentials._kr = MemoryKeyring()

    assert ctx.maintenance.is_running() is False, "构造应用不等于启动维护"

    with TestClient(app):
        assert ctx.maintenance.is_running() is True, "lifespan startup 必须真的启动维护"

    assert ctx.maintenance.is_running() is False, "shutdown 之后不能还剩维护任务"


def test_shutdown_closes_turns_tasks_adapters_and_db(app_settings, db_conn):
    from agent.credentials.store import MemoryKeyring

    app = create_app(app_settings, db_conn, close_db_on_shutdown=True)
    ctx = app.state.ctx
    ctx.credentials._kr = MemoryKeyring()

    with TestClient(app):
        pass

    assert ctx.turns._closed is True, "TurnManager.shutdown() 必须真的被调用"
    assert ctx.task_manager.is_closed() is True, "TaskManager 必须有可靠 shutdown"
    assert ctx._adapter_cache == {}, "adapter 缓存要清空"
    with pytest.raises(sqlite3.ProgrammingError):
        db_conn.execute("SELECT 1")


async def test_maintenance_loop_survives_a_failing_tick(monkeypatch):
    """调度器不能因为一次检查抛错就永久死亡。"""
    from agent.api.bus import EventBus
    from agent.services.app import AppContext
    from agent.storage.db import connect
    from agent.storage.migrate import apply_migrations
    from pathlib import Path
    import tempfile

    tmp = Path(tempfile.mkdtemp())
    conn = connect(tmp / "app.db")
    apply_migrations(conn)
    ctx = AppContext(Settings(data_dir=tmp), conn, EventBus())
    scheduler = ctx.maintenance

    ticks = {"n": 0}

    def explode():
        raise RuntimeError("状态来源坏了")

    async def fake_run_once():
        ticks["n"] += 1
        return {"ok": True}

    monkeypatch.setattr(ctx.turns, "active_loop", explode)
    monkeypatch.setattr(scheduler, "run_once", fake_run_once)
    monkeypatch.setattr(scheduler, "next_delay_seconds", lambda: 0.01)

    scheduler.start()
    await asyncio.sleep(0.1)
    assert scheduler.is_running() is True, "一次 tick 抛错不能杀死调度器"

    monkeypatch.setattr(ctx.turns, "active_loop", lambda: None)
    await asyncio.sleep(0.1)
    assert ticks["n"] > 0, "错误隔离之后仍然要继续跑正常 tick"

    await scheduler.stop()
    assert scheduler.is_running() is False
    conn.close()


async def test_turn_manager_rejects_submit_after_shutdown():
    async def runner(ctx):
        return None

    tm = TurnManager(runner)
    await tm.shutdown()
    with pytest.raises(RuntimeError, match="closed"):
        tm.submit("A")


async def test_task_manager_shutdown_cancels_running_and_rejects_new_tasks():
    from agent.tools.base import ToolResult
    from agent.tools.task_manager import TaskManager

    class _Bus:
        async def publish(self, event) -> None:  # noqa: ANN001
            return None

    manager = TaskManager(_Bus(), max_concurrent=2)
    gate = asyncio.Event()
    started = asyncio.Event()

    async def slow() -> ToolResult:
        started.set()
        await gate.wait()
        return ToolResult(ok=True, content="never")

    task_id = manager.submit("t", slow)
    await started.wait()

    await manager.shutdown()

    assert manager.is_closed() is True
    assert manager.record_info(task_id) is None or manager.record_info(task_id).done
    # 等待者必须被兑现，不能留下悬挂的 await
    status, _ = await asyncio.wait_for(manager.await_result(task_id, timeout=1), timeout=2)
    assert status in ("not_found", "failed", "done")
    with pytest.raises(RuntimeError, match="closed"):
        manager.submit("t", slow)
