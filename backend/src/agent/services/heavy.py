"""把同步重活移出事件循环：有并发上限的执行器 + 提交前的代次校验。

为什么需要它：上下文装配里直接调同步话题预测与向量检索（ONNX 推理 + 向量比较
+ 数据库读写），单次可能几十到几百毫秒。这些调用跑在事件循环线程上时，期间
**所有**交互请求（健康探测、取消请求、事件流）都得排队等它 —— 界面上就是卡顿。

边界（契约 WS3 §3 的红线，必须守住）：

* **不把 `AppContext` / 整套 predictor / selector / 整个上下文装配塞进线程**：
  它们持有共享数据库连接（autocommit）、可变缓存（向量矩阵、selector 索引）
  与业务顺序。这里只允许把**纯计算**（ONNX 推理、余弦排序、分词打分）搬进
  线程；输入读取与结果提交留在事件循环一侧（见 predict.py 的 plan/compute/finish
  三段拆分与 turn_orchestrator 的调用点）。
* **提交前校验代次**：算完回来先看这一轮有没有被取消 / 版本有没有变，过时的
  结果直接丢弃，绝不提交（调用点在 await 之后检查 `ctx.cancelled`）。
* **并发有上限**：重计算不能把机器吃满，也不能把交互请求挤在后面 —— 池子只有
  几个线程，且**绝不**用一把大锁把请求路径串起来。
* **取消语义要说清**：等待期间这一轮被取消时，`await` 抛 `CancelledError`；
  线程本身停不下来（Python 不能强杀线程），但**结果不会被提交**：调用点在拿到
  结果之前就已经离开了。这是「取消后过时结果不提交」的实现方式。
"""

from __future__ import annotations

import asyncio
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from typing import Any, Callable, TypeVar

logger = logging.getLogger(__name__)

# 并发上限：本机 CPU 推理 + 数据库读，2 个线程足够把「一次重活」从事件循环里
# 拿开，又不会和交互请求抢满 CPU。这个数字是**上限**，不是并发目标。
DEFAULT_MAX_WORKERS = 2

T = TypeVar("T")

# 存活的执行器强引用登记：测试/收尾路径需要在关闭共享 SQLite 连接之前，
# 等待所有工作线程真正结束（弱引用会在任务尚未结束时被回收，等不到）。
_INSTANCES: list["HeavyWork"] = []
_INSTANCES_LOCK = threading.Lock()


async def wait_for_heavy_work(timeout: float = 30.0) -> None:
    """等待所有存活 HeavyWork 的**已在执行**任务结束（关闭连接前的安全闸）。

    只等待当前登记在案的执行器；调用方随后可以安全关闭它们共享的数据库连接。
    超时由调用方决定是否容忍（测试收尾给有界值，生产关闭走 shutdown(wait=True)）。
    """
    async def _wait_all() -> None:
        with _INSTANCES_LOCK:
            instances = list(_INSTANCES)
        await asyncio.gather(*(item.wait_idle() for item in instances))

    await asyncio.wait_for(_wait_all(), timeout=timeout)


class HeavyWork:
    """有并发上限的「重活」执行器（线程池 + 事件循环侧的结果提交约定）。"""

    def __init__(self, max_workers: int = DEFAULT_MAX_WORKERS) -> None:
        workers = max(1, int(max_workers))
        self._pool = ThreadPoolExecutor(
            max_workers=workers, thread_name_prefix="qio-heavy"
        )
        self._max_workers = workers
        self._closed = False
        # 真正在工作线程里执行中的任务数（不是 event loop 侧的 await 数：
        # await 被取消后线程仍可能继续跑，必须由工作线程自己的 finally 递减）。
        self._active = 0
        self._idle = threading.Condition()
        with _INSTANCES_LOCK:
            _INSTANCES.append(self)

    @property
    def max_workers(self) -> int:
        return self._max_workers

    @property
    def closed(self) -> bool:
        return self._closed

    async def run(self, fn: Callable[..., T], /, *args: Any, **kwargs: Any) -> T:
        """在线程里跑一个**纯计算**函数，等它回来（结果由调用方决定是否提交）。

        关闭之后不再往池里丢任务：就地同步执行 —— 收尾路径不该因为执行器已经
        关掉而抛异常，也不该静默丢掉这次计算。
        """
        if self._closed:
            return fn(*args, **kwargs)
        loop = asyncio.get_running_loop()
        with self._idle:
            self._active += 1

        def _wrapped() -> T:
            try:
                return fn(*args, **kwargs)
            finally:
                with self._idle:
                    self._active -= 1
                    self._idle.notify_all()

        return await loop.run_in_executor(self._pool, _wrapped)

    async def wait_idle(self) -> None:
        """等待工作线程里正在执行的任务真正结束（不关闭池子）。"""
        while True:
            with self._idle:
                active = self._active
            if active <= 0:
                return
            await asyncio.to_thread(self._wait_idle_blocking)

    def _wait_idle_blocking(self) -> None:
        with self._idle:
            while self._active > 0:
                self._idle.wait(timeout=0.05)

    async def shutdown(self) -> None:
        """停止接收新任务，并**等待已在跑的纯计算结束**后退出。

        阶段四修复：旧实现 wait=False 会立刻返回，而线程里仍在执行的函数可能继续
        访问调用方随后要关闭的资源（最典型：共享 SQLite 连接 → use-after-free，
        Windows access violation / Linux SIGSEGV）。这里的任务按契约只是纯计算，
        等待有界；取消排队中尚未开始的任务。
        """
        self._closed = True
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None,
            lambda: self._pool.shutdown(wait=True, cancel_futures=True),
        )
        with _INSTANCES_LOCK:
            if self in _INSTANCES:
                _INSTANCES.remove(self)
