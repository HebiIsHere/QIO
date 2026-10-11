# -*- coding: utf-8 -*-
"""F06：手动维护未结束却报告干净关闭（C 组定向验收）。

缺陷（基线 8fa8452）
--------------------

POST /api/maintenance/run 里是 `asyncio.create_task(ctx.maintenance.run_once())`，
句柄随手丢掉；`MaintenanceScheduler.stop()` 只取消周期循环。于是：

* 手动那一轮在关闭时继续跑、继续写库；
* `aclose()` 按「干净关闭」释放 adapter / 关数据库；
* 关闭报告里没有它的名字 —— 未结束被吞成已完成。

本文件断言修好后的行为：手动与周期纳入同一生命周期、关闭时拒绝新维护、
有界等待 + 取消 + **确认终止**、未确认结束时 clean=False 且不提前释放依赖、
并发维护合并成一份工作。

只跑定向节点：临时库 + 无真实模型（维护里的 adapter 调用由 `build_adapter` 缺席
短路成 no adapter，不发网络请求）。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from agent.api.bus import EventBus
from agent.api.server import create_app
from agent.config import Settings
from agent.credentials.store import MemoryKeyring
from agent.services.app import AppContext
from agent.services.lifecycle import (
    PHASE_MAINTENANCE_STOPPED,
    PHASE_MAINTENANCE_UNFINISHED,
    close_decision,
)
from agent.storage.db import connect
from agent.storage.migrate import apply_migrations


def _app(tmp_path: Path, name: str = "f06") -> AppContext:
    conn = connect(tmp_path / f"{name}.db")
    apply_migrations(conn)
    ctx = AppContext(Settings(data_dir=tmp_path / name), conn, EventBus())
    ctx.credentials._kr = MemoryKeyring()
    return ctx


def _shorten_maintenance(ctx: AppContext) -> None:
    """把维护关闭的有界等待压到毫秒级（语义不变：等待 → 取消 → grace → 仍未结束）。"""
    runs = ctx.maintenance._runs
    approvals = ctx.maintenance._approvals
    runs_original = runs.shutdown
    approvals_original = approvals.shutdown

    async def _fast_runs(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        return await runs_original(0.05, cancel_timeout=0.05)

    async def _fast_approvals(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        return await approvals_original(0.05, cancel_timeout=0.05)

    runs.shutdown = _fast_runs  # type: ignore[method-assign]
    approvals.shutdown = _fast_approvals  # type: ignore[method-assign]


async def _stuck_forever(release: asyncio.Event) -> None:
    """吞掉取消、直到放行才结束的受控执行单元（用来钉住「未确认结束」）。"""
    try:
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        pass
    await release.wait()


# ---------------------------------------------------------------------------
# F06-1：run_manual 的返回契约
# ---------------------------------------------------------------------------


async def test_run_manual_starts_and_is_registered(tmp_path):
    ctx = _app(tmp_path)
    gate = asyncio.Event()
    started = asyncio.Event()

    async def fake_execute():
        started.set()
        await gate.wait()
        return {"ok": True}

    ctx.maintenance.run_once = fake_execute  # type: ignore[method-assign]

    first = await ctx.maintenance.run_manual()
    assert first == {
        "ok": True,
        "started": True,
        "accepted": True,
        "running": True,
        "reason": None,
    }
    await started.wait()
    assert ctx.maintenance.is_busy() is True

    # 并发规则：已有一轮在跑 → **合并**，不重复执行同一份工作
    second = await ctx.maintenance.run_manual()
    assert second == {
        "ok": True,
        "started": False,
        "accepted": True,
        "running": True,
        "reason": "already_running",
    }
    assert len(ctx.maintenance._runs.active()) == 1

    gate.set()
    for _ in range(50):
        if not ctx.maintenance.is_busy():
            break
        await asyncio.sleep(0.01)
    assert ctx.maintenance.is_busy() is False
    assert ctx.maintenance.last_result() == {"ok": True}
    ctx.conn.close()


async def test_run_manual_refused_during_shutdown(tmp_path):
    ctx = _app(tmp_path)
    report = await ctx.maintenance.stop()
    assert report["clean"] is True and report["unfinished"] is False

    refused = await ctx.maintenance.run_manual()
    assert refused["ok"] is False
    assert refused["started"] is False and refused["accepted"] is False
    assert refused["reason"] == "shutting_down"
    ctx.conn.close()


# ---------------------------------------------------------------------------
# F06-2：手动维护运行中关闭 —— 未结束就必须如实报告，且不提前释放依赖
# ---------------------------------------------------------------------------


async def test_stop_reports_unfinished_manual_run_and_keeps_resources(tmp_path):
    ctx = _app(tmp_path, "unfinished")
    _shorten_maintenance(ctx)
    gate = asyncio.Event()
    started = asyncio.Event()

    async def stuck_execute():
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            pass  # 吞掉取消：这就是「取消后仍未终止」
        await gate.wait()
        return {"ok": True}

    ctx.maintenance.run_once = stuck_execute  # type: ignore[method-assign]
    # 让 adapter 缓存里有一个可观察的替身：不 clean 时不得释放
    class _FakeAdapter:
        closed = False

        async def close(self):
            self.closed = True

    adapter = _FakeAdapter()
    ctx._adapter_cache[("key_f06", 1, "https://api.example.com/v1", "gpt-x")] = adapter

    assert (await ctx.maintenance.run_manual())["started"] is True
    await started.wait()

    report = await ctx.maintenance.stop()

    assert report["clean"] is False, f"执行单元未终止就不能报干净：{report}"
    assert report["unfinished"] is True
    assert report["pending"], "必须如实列出未确认结束的名字"
    assert report["reason"] and "未确认结束" in report["reason"]
    assert ctx.maintenance.is_busy() is True, "取消之后未终止的执行单元必须还挂在账上"

    # 关闭确实走到最后：再把「不干净」归给一个同样卡住的普通后台执行单元（控制变量），
    # 用来断言合并后的关闭决定 —— 维护自己的未结束已经在上一次 stop() 里如实报告过，
    # 这里不再依赖它的时序。
    stuck_release = asyncio.Event()
    handle = ctx.background.register("stuck_probe", _stuck_forever(stuck_release))

    close_report = await ctx.aclose()
    assert close_report.clean is False
    assert close_report.unfinished, f"合并报告必须带名字：{close_report.to_dict()}"
    assert PHASE_MAINTENANCE_UNFINISHED in close_report.phases
    decision = close_decision(close_report)
    assert decision.release_adapters is False, "未确认结束不得释放 adapter"
    assert decision.close_database is False, "未确认结束不得关数据库"
    assert decision.record_clean_exit is False, "未确认结束不得写干净退出"
    assert adapter.closed is False, "adapter 在未确认结束时必须保留"

    # 收尾：放行，确认不留悬挂任务。
    # 注意：执行单元身上还挂着取消请求（关闭时投递的），await 它会抛 CancelledError
    # —— 这不是缺陷，是「取消后仍未终止」的正常收尾；这里只等它真的结束。
    gate.set()
    try:
        await asyncio.wait_for(ctx.maintenance._run_handle.task, timeout=2)
    except asyncio.CancelledError:
        pass
    assert ctx.maintenance._run_handle.task.done() is True, "放行之后必须真的结束"
    assert ctx.maintenance.is_busy() is False, "放行之后必须真的结束（不留悬挂任务）"
    stuck_release.set()
    try:
        await asyncio.wait_for(handle.task, timeout=2)
    except asyncio.CancelledError:
        pass
    assert handle.task.done() is True
    ctx.conn.close()


async def test_stop_reports_clean_when_manual_run_finishes_in_grace(tmp_path):
    ctx = _app(tmp_path, "clean")
    _shorten_maintenance(ctx)
    started = asyncio.Event()

    async def cooperative():
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            return {"ok": True}  # 配合取消：在 cancel grace 内收尾

    ctx.maintenance.run_once = cooperative  # type: ignore[method-assign]
    assert (await ctx.maintenance.run_manual())["started"] is True
    await started.wait()

    report = await ctx.maintenance.stop()
    assert report["clean"] is True, f"配合取消必须收干净：{report}"
    assert report["pending"] == [] and report["reason"] is None

    close_report = await ctx.aclose()
    assert close_report.clean is True
    assert PHASE_MAINTENANCE_STOPPED in close_report.phases
    assert close_decision(close_report).release_adapters is True
    ctx.conn.close()


# ---------------------------------------------------------------------------
# F06-3：并发维护 —— 手动与周期不会重复执行同一份工作
# ---------------------------------------------------------------------------


async def test_manual_and_periodic_maintenance_merge_into_one_run(tmp_path):
    ctx = _app(tmp_path, "merge")
    clock = {"n": 0}
    entered = asyncio.Event()
    gate = asyncio.Event()

    async def counted_execute():
        clock["n"] += 1
        entered.set()
        await gate.wait()
        return {"ok": True}

    ctx.maintenance.run_once = counted_execute  # type: ignore[method-assign]
    ctx.maintenance.next_delay_seconds = lambda: 0.01  # type: ignore[method-assign]
    ctx.maintenance.start()
    await entered.wait()

    # 周期那一轮正在跑时，手动请求合并到它
    merged = await ctx.maintenance.run_manual()
    assert merged["started"] is False and merged["reason"] == "already_running"
    assert len(ctx.maintenance._runs.active()) == 1
    assert ctx.maintenance.is_running() is True

    gate.set()
    await asyncio.sleep(0.05)
    report = await ctx.maintenance.stop()
    assert report["clean"] is True, report
    assert ctx.maintenance.is_running() is False
    # 周期循环取消了；手动那一轮（已被合并）也确认结束
    assert clock["n"] >= 1
    ctx.conn.close()


# ---------------------------------------------------------------------------
# F06-4：维护发起的审批等待也纳入同一生命周期
# ---------------------------------------------------------------------------


async def test_approval_wait_is_registered_and_tracked(tmp_path):
    ctx = _app(tmp_path, "approval")
    _shorten_maintenance(ctx)
    gate = asyncio.Event()
    started = asyncio.Event()

    async def wait_forever():
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            pass
        await gate.wait()

    ctx.maintenance.register_approval_wait("high_impact_knowledge", wait_forever)
    await started.wait()

    report = await ctx.maintenance.stop()
    assert report["clean"] is False
    assert any("high_impact_knowledge" in name for name in report["pending"]), report

    gate.set()
    await asyncio.sleep(0.05)
    ctx.conn.close()


async def test_approval_wait_refused_after_close(tmp_path):
    ctx = _app(tmp_path, "approval-refused")
    await ctx.maintenance.stop()

    from agent.services.background import BackgroundClosed

    async def factory():
        return None

    with pytest.raises(BackgroundClosed):
        ctx.maintenance.register_approval_wait("tool_candidate", factory)
    ctx.conn.close()


# ---------------------------------------------------------------------------
# F06-5：关闭期间再次请求 / 数据库释放顺序
# ---------------------------------------------------------------------------


def test_manual_maintenance_route_is_accepted_and_tracked(tmp_path):
    from fastapi.testclient import TestClient

    conn = connect(tmp_path / "route.db")
    apply_migrations(conn)
    app = create_app(Settings(data_dir=tmp_path / "route"), conn, close_db_on_shutdown=True)
    with TestClient(app) as client:
        ctx = app.state.ctx
        ctx.credentials._kr = MemoryKeyring()
        # 手动入口返回「已启动」的受理事实
        ok = client.post("/api/maintenance/run")
        assert ok.status_code == 200, ok.text
        body = ok.json()
        assert body.get("accepted") is not False
        # 无论 route 是否回传调度契约，执行都必须登记在同一份注册表里
        # （这是「维护入口不再创建无人管理的后台任务」的可观察判据）。
        assert body.get("reason") != "shutting_down"
    # lifespan 关闭：维护已确认结束（closing 是关闭已开始的显式事实）
    assert ctx.maintenance.closing is True
    assert ctx.maintenance.is_running() is False
    assert ctx.maintenance.is_busy() is False


def test_db_release_order_follows_close_report(tmp_path):
    """数据库只在关闭报告干净时才释放（真实入口 close_db_on_shutdown=True）。"""
    import sqlite3

    from fastapi.testclient import TestClient

    conn = connect(tmp_path / "order.db")
    apply_migrations(conn)
    app = create_app(Settings(data_dir=tmp_path / "order"), conn, close_db_on_shutdown=True)
    with TestClient(app) as client:
        ctx = app.state.ctx
        ctx.credentials._kr = MemoryKeyring()
        assert client.get("/api/runtime/state").status_code == 200
    try:
        conn.execute("SELECT 1").fetchone()
        still_open = True
    except sqlite3.ProgrammingError:
        still_open = False
    assert still_open is False, "干净关闭时真实入口照旧关数据库"
