r"""E 路独立核验探针 · F06 手动维护未结束却报告干净关闭（基线 8fa845255c118a7104b1de8e37298ca745924482）

规格来源：D:\qio-dev\_audit-findings-8fa8452.md 的 F06 段 + 冻结契约 C4：
  - 手动与周期维护纳入同一生命周期管理；
  - 关闭时拒绝新维护、有界等待/取消、确认执行单元终止；
  - 有任务未确认结束时报告 unfinished 且不干净退出，不提前释放它仍依赖的 DB/adapter。

**与其他探针的关系（独立作者、独立场景）**：本探针不复用任何既有测试的断言。它构造两个场景：
  E1 执行单元吞掉取消后**永不终止** → close 必须 clean=False / unfinished 列出名字，
     且**不得**提前释放它仍依赖的 adapter（可观测：adapter.close() 未被调用）；
  E2 执行单元吞掉取消、但在有界窗口内**自行结束** → close 必须 clean=True，
     且 adapter 被释放（说明关闭路径不是「一律报不干净」）。

运行：
  cd <被测 worktree>\backend
  $env:PYTHONPATH = '<被测 worktree>\backend\src'
  & 'D:\qio-dev\qa-lead\backend\.venv\Scripts\python.exe' -m pytest <本文件> -q -p no:cacheprovider -s -o asyncio_mode=auto
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

os.environ.pop("QIO_DATA_DIR", None)
os.environ.setdefault("QIO_DEV_INSECURE", "1")
os.environ.setdefault("QIO_DISABLE_DB_CHECK", "1")

import pytest  # noqa: E402

from agent.api.bus import EventBus  # noqa: E402
from agent.config import Settings  # noqa: E402
from agent.credentials.store import MemoryKeyring  # noqa: E402
from agent.services.app import AppContext  # noqa: E402
from agent.storage.db import connect  # noqa: E402
from agent.storage.migrate import apply_migrations  # noqa: E402


class _FakeAdapter:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


def _app(tmp_path: Path, adapter: _FakeAdapter) -> AppContext:
    conn = connect(tmp_path / "f06-e.db")
    apply_migrations(conn)
    ctx = AppContext(Settings(data_dir=tmp_path), conn, EventBus())
    ctx.credentials._kr = MemoryKeyring()
    ctx._adapter_cache[("key_e06", 1, "https://api.example.com/v1", "gpt-x")] = adapter
    return ctx


def _report_get(report, name):
    """close 报告可以是对象或 dict，两种形状都读得到。"""
    if isinstance(report, dict):
        return report.get(name)
    return getattr(report, name, None)


async def _wait_until(predicate, timeout: float = 20.0, step: float = 0.02) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(step)
    return bool(predicate())


async def test_f06_e1_unfinished_unit_is_reported_and_resources_kept(tmp_path: Path) -> None:
    adapter = _FakeAdapter()
    ctx = _app(tmp_path, adapter)
    started = asyncio.Event()
    never = asyncio.Event()

    async def stuck() -> dict:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            pass  # 吞掉取消：执行单元未终止
        await never.wait()
        return {"ok": True}

    ctx.maintenance.run_once = stuck  # type: ignore[method-assign]
    accepted = await ctx.maintenance.run_manual()
    print("\n[F06-E1] run_manual 回执 =", accepted)
    assert accepted.get("started") is True or accepted.get("accepted") is True, (
        "前置条件：手动维护必须真的被接受并开始"
    )
    await asyncio.wait_for(started.wait(), timeout=20)

    report = await ctx.maintenance.stop()
    print("[F06-E1] stop 报告 =", report)
    print("[F06-E1] adapter.closed =", adapter.closed, " is_busy =", ctx.maintenance.is_busy())

    assert _report_get(report, "clean") is False, (
        "F06 缺陷复现：执行单元未确认终止，关闭却报告干净（clean=True）。report=" + str(report)
    )
    unfinished = _report_get(report, "unfinished")
    assert unfinished, "F06：必须如实报告 unfinished（不能只说 clean=True/False）。report=" + str(report)
    pending = _report_get(report, "pending")
    print("[F06-E1] pending =", pending)
    assert pending, "F06：必须列出未确认结束的执行单元名字。report=" + str(report)
    assert adapter.closed is False, (
        "F06 缺陷复现：执行单元还没确认结束就提前释放了它仍依赖的 adapter。"
        "adapter.closed=" + str(adapter.closed)
    )

    # 收尾：放行后应当能干净收敛（否则会污染后续用例/留下常驻任务）
    never.set()
    await _wait_until(lambda: not ctx.maintenance.is_busy(), timeout=20)
    await ctx.maintenance.stop()
    close_after = await ctx.aclose()
    print("[F06-E1] 放行后 aclose 报告 =", close_after, " adapter.closed =", adapter.closed)
    ctx.conn.close()


async def test_f06_e2_unit_that_finishes_within_grace_is_clean(tmp_path: Path) -> None:
    adapter = _FakeAdapter()
    ctx = _app(tmp_path, adapter)
    started = asyncio.Event()

    async def slow_but_finishing() -> dict:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            # 吞掉取消，但自己在有界窗口内结束（模拟「取消后稍后才真正停下」）
            await asyncio.sleep(0.15)
        return {"ok": True}

    ctx.maintenance.run_once = slow_but_finishing  # type: ignore[method-assign]
    assert (await ctx.maintenance.run_manual()).get("accepted") is True
    await asyncio.wait_for(started.wait(), timeout=20)

    report = await ctx.maintenance.stop()
    print("\n[F06-E2] stop 报告 =", report, " adapter.closed =", adapter.closed)
    assert _report_get(report, "clean") is True, (
        "F06：执行单元在有界窗口内确认结束后，关闭必须报告干净（不能一律报不干净）。report="
        + str(report)
    )
    # adapter 的释放在**应用关闭**（aclose）阶段，不在 maintenance.stop()。
    # 这里验证「干净退出 ⇒ 应用关闭时确实释放」，而不是拿 maintenance.stop() 当应用关闭。
    close_report = await ctx.aclose()
    print("[F06-E2] aclose 报告 =", close_report, " adapter.closed =", adapter.closed)
    assert _report_get(close_report, "clean") is True, (
        "F06：干净退出的应用关闭也必须如实报告干净。close_report=" + str(close_report)
    )
    assert adapter.closed is True, (
        "F06：确认干净退出后应当释放 adapter（否则就是泄漏）。close_report=" + str(close_report)
    )
    ctx.conn.close()


async def test_f06_e3_new_maintenance_refused_after_shutdown_started(tmp_path: Path) -> None:
    adapter = _FakeAdapter()
    ctx = _app(tmp_path, adapter)

    async def quick() -> dict:
        return {"ok": True}

    ctx.maintenance.run_once = quick  # type: ignore[method-assign]
    first = await ctx.maintenance.stop()
    assert _report_get(first, "clean") is True, "前置条件：空闲时关闭应当干净"

    refused = await ctx.maintenance.run_manual()
    print("\n[F06-E3] 关闭后 run_manual =", refused)
    assert refused.get("ok") is False and refused.get("started") is False, (
        "F06：关闭开始后必须拒绝新维护（不得创建无人管理的后台任务）。refused=" + str(refused)
    )
    ctx.conn.close()