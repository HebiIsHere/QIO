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
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from typing import Any, Callable, TypeVar

logger = logging.getLogger(__name__)

# 并发上限：本机 CPU 推理 + 数据库读，2 个线程足够把「一次重活」从事件循环里
# 拿开，又不会和交互请求抢满 CPU。这个数字是**上限**，不是并发目标。
DEFAULT_MAX_WORKERS = 2

T = TypeVar("T")


class HeavyWork:
    """有并发上限的「重活」执行器（线程池 + 事件循环侧的结果提交约定）。"""

    def __init__(self, max_workers: int = DEFAULT_MAX_WORKERS) -> None:
        workers = max(1, int(max_workers))
        self._pool = ThreadPoolExecutor(
            max_workers=workers, thread_name_prefix="qio-heavy"
        )
        self._max_workers = workers
        self._closed = False

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
        return await loop.run_in_executor(self._pool, partial(fn, *args, **kwargs))

    async def shutdown(self) -> None:
        """停止接收新任务并让池子退出（不等已排队的任务跑完）。"""
        self._closed = True
        self._pool.shutdown(wait=False, cancel_futures=True)
