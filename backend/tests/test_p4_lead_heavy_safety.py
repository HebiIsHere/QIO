# -*- coding: utf-8 -*-
"""阶段四 CI 崩溃根因的回归守卫。

根因：上下文装配（build_context_blocks）会读写共享 SQLite 连接，却跑在
HeavyWork 线程池里；应用关闭时旧实现 HeavyWork.shutdown 是 wait=False ——
它在工作线程还在执行时就返回，随后数据库连接被关闭，工作线程继续使用它，
CPython sqlite3 C 层 use-after-free（Windows access violation / Linux SIGSEGV）。

修复不牺牲 WS3 的性能契约（慢检索仍不阻塞事件循环），而是收紧生命周期：
HeavyWork.shutdown 必须等待已在执行的纯计算结束后才返回，再由关闭方关闭 DB。
"""

from __future__ import annotations

import asyncio
import inspect
import threading
import time

from agent.services import app as app_module
from agent.services import turn_orchestrator
from agent.services.heavy import HeavyWork


def test_heavy_shutdown_waits_for_running_compute():
    async def scenario() -> None:
        heavy = HeavyWork(max_workers=1)
        started = threading.Event()
        finished = threading.Event()

        def slow_compute() -> str:
            started.set()
            time.sleep(0.2)
            finished.set()
            return "done"

        task = asyncio.create_task(heavy.run(slow_compute))
        while not started.is_set():
            await asyncio.sleep(0.005)
        await heavy.shutdown()
        # shutdown 返回时任务必须已经真正结束（旧实现 wait=False 会在这里失败）
        assert finished.is_set()
        assert await task == "done"

    asyncio.run(scenario())


def test_cancelled_await_still_waits_for_worker_and_drain_waits():
    """真实的取消路径：await 被取消后工作线程仍在跑；drain 必须等线程真正结束。"""
    from agent.services.heavy import wait_for_heavy_work

    async def scenario() -> None:
        heavy = HeavyWork(max_workers=1)
        started = threading.Event()
        finished = threading.Event()

        def slow_compute() -> None:
            started.set()
            time.sleep(0.2)
            finished.set()

        task = asyncio.create_task(heavy.run(slow_compute))
        while not started.is_set():
            await asyncio.sleep(0.005)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        # 取消只结束了 event-loop 侧的 await，工作线程还没结束
        assert not finished.is_set()
        await wait_for_heavy_work(timeout=5.0)
        assert finished.is_set()
        await heavy.shutdown()

    asyncio.run(scenario())


def test_app_close_drains_heavy_work_before_returning():
    """AppContext.aclose 必须先停 turns/tasks，再 drains heavy，最后才由调用方关 DB。"""
    source = inspect.getsource(app_module)
    compact = " ".join(source.split())
    assert "await self.heavy.shutdown()" in compact
    # heavy 收尾必须在 turn shutdown 之后（不在 turn 运行期间就关池子）
    turn_pos = compact.find("await self.turns.shutdown()")
    heavy_pos = compact.find("await self.heavy.shutdown()")
    assert 0 <= turn_pos < heavy_pos


def test_context_assembly_still_offloaded_for_ws3():
    """性能契约：慢检索仍在 heavy 池里跑（不能为了修生命周期把它挪回事件循环）。"""
    source = inspect.getsource(turn_orchestrator)
    compact = " ".join(source.split())
    assert "await app.heavy.run( builder," in compact
