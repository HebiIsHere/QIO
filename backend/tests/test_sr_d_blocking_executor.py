"""契约 4（D）：进程级共享的有界阻塞执行器（backend/src/agent/tools/blocking.py）。

验收点：
- get_blocking_executor() 返回进程级共享实例，max_workers=4（有界）；
- shutdown_blocking_executor() 关闭后再次获取可用全新实例；
- 并发提交多个任务时，线程数不超过 max_workers（真正有界）；
- 线程名字可辨识（便于证据输出）。
"""

from __future__ import annotations

import threading
import time

from agent.tools import blocking


def _task(ident_out: list, idx: int) -> int:
    ident_out.append((idx, threading.get_ident(), threading.current_thread().name))
    return idx


def test_get_blocking_executor_shared_and_bounded():
    ex = blocking.get_blocking_executor()
    assert ex is not None
    assert blocking.get_blocking_executor() is ex  # 进程级共享：同一实例
    assert ex._max_workers == 4  # 有界：max_workers=4


def test_shutdown_then_reacquire_creates_fresh_executor():
    ex1 = blocking.get_blocking_executor()
    blocking.shutdown_blocking_executor(wait=True)
    assert ex1._shutdown
    ex2 = blocking.get_blocking_executor()
    assert ex2 is not ex1
    assert not ex2._shutdown
    # 新实例可用
    ident: list = []
    fut = ex2.submit(_task, ident, 1)
    assert fut.result(timeout=5) == 1
    assert len(ident) == 1


def test_executor_is_actually_bounded_under_concurrency():
    """10 个任务并发提交：实际动用的线程数不得超过 4。"""
    ex = blocking.get_blocking_executor()
    ident: list = []
    futures = [ex.submit(_slow_task, ident, i) for i in range(10)]
    for f in futures:
        f.result(timeout=15)
    threads = {t for _, t, _ in ident}
    assert len(threads) <= 4, f"expected bounded pool (<=4 threads), saw {len(threads)}"


def _slow_task(ident_out: list, idx: int) -> int:
    ident_out.append((idx, threading.get_ident(), threading.current_thread().name))
    time.sleep(0.05)
    return idx


def test_executor_threads_are_named_for_evidence():
    ex = blocking.get_blocking_executor()
    ident: list = []
    ex.submit(_task, ident, 0).result(timeout=5)
    name = ident[0][2]
    assert "qio" in name.lower() or "blocking" in name.lower(), (
        f"executor thread should be identifiable, got {name!r}"
    )
