"""进程级共享的有界阻塞执行器（契约 4）。

把「纯 CPU 计算」（嵌入批量、余弦）与「真实文件 I/O」移出事件循环：
调用方一律用 loop.run_in_executor(get_blocking_executor(), fn, ...)。

设计要点：
- **有界**：max_workers=4。无界线程池在高并发工具批次下会放大调度与内存压力，
  也让「线程身份断言」失去意义；
- **进程级共享**：所有模块（tool_router / fs_tools / …）复用同一个池，
  线程总量可控；不按 loop、不按工具各建池；
- **生命周期**：shutdown_blocking_executor() 由应用关闭路径调用（Lead 接线）；
  关闭后再次获取会惰性重建一个全新可用实例（测试与脚本友好）；
- **职责边界**：执行器只做纯计算 / 文件 I/O —— 数据库连接、审批调用、事件提交
  等有状态操作留在事件循环上（契约明令不搬进线程）。
"""

from __future__ import annotations

import concurrent.futures
import threading

MAX_BLOCKING_WORKERS = 4

_executor: concurrent.futures.ThreadPoolExecutor | None = None
_lock = threading.Lock()


def get_blocking_executor() -> concurrent.futures.ThreadPoolExecutor:
    """返回进程级共享的有界线程池（惰性创建；关闭后重建）。"""
    global _executor
    with _lock:
        if _executor is None or _executor._shutdown:  # noqa: SLF001 - 重建判定
            _executor = concurrent.futures.ThreadPoolExecutor(
                max_workers=MAX_BLOCKING_WORKERS,
                thread_name_prefix="qio-blocking",
            )
        return _executor


def shutdown_blocking_executor(wait: bool = True) -> None:
    """关闭共享执行器（应用关闭时调用；Lead 接线点）。

    幂等：重复调用安全。之后 get_blocking_executor() 会重建新实例。
    """
    global _executor
    with _lock:
        if _executor is not None:
            _executor.shutdown(wait=wait)
            _executor = None
