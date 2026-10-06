"""上传作业：接收端 / 工作线程 / 收尾共享同一份可观察终态（round 4 问题三）。

2026-10-07 审计真缺陷（plan §0 第 3 条）：`api/server.py` 的 `_upload_queue_put` 只循环等
队列空位、不看工作线程是否已经结束；结束/中止哨兵还走同一个队列 —— 工作线程一死，
「队列满 + 没有消费者」就让请求永远等下去，附件停在 `prepared`。

本模块把队列、工作线程与收尾收进一个作业对象，**接收端 / 工作线程 / 取消清理都观察同一份
终态**（`state ∈ running|done|failed|cancelled` + 原因 + 工作线程句柄）：

* 接收端每次排队前先看终态；队列满时等空位**同时**等终态，谁先到谁解除等待；
* 结束 / 中止**不依赖满队列里的哨兵**：工作线程已经结束时哨兵根本不需要（`close_input`
  直接返回），取块循环每一轮也只看终态与队列，有界超时保证取消 / 服务关闭能在有限时间内
  把阻塞在 `queue.get` 上的工作线程唤醒；
* 两个方向都通：**取消 → 解除工作线程的阻塞读取**；**工作线程失败 → 解除接收端的排队等待**。

线程纪律（与 services/attachments.py 一致，不变）：这里只碰文件 I/O 与作业状态，
**数据库动作一律由事件循环线程的调用方完成**（begin_upload / apply_outcome / delete）。
"""

from __future__ import annotations

import asyncio
import logging
import queue
import threading
from typing import Callable, Iterator

from agent.services.attachments import (
    DiskOutcome,
    STATE_CANCELLED as DISK_STATE_CANCELLED,
    STATE_CHANGED,
    STATE_READY,
    UploadAborted,
    UploadTooLarge,
)
from agent.trace.redact import redact_text

logger = logging.getLogger(__name__)

#: 有界桥接队列深度（内存上界 = 深度 × 单块大小；**禁止**改成无限队列）
UPLOAD_QUEUE_DEPTH = 4
#: 工作线程取块 / 接收端等空位的轮询上限：只用来让「终态 / 取消」在有限时间内被观察到，
#: 正常路径靠事件唤醒（终态事件、空位事件），不靠这个数字。
POLL_SECONDS = 0.05
#: 请求被取消后等工作线程收尾的上界（清理临时文件 + 退出）
SETTLE_SECONDS = 5.0

#: 哨兵：只有「队列还没满、工作线程还活着」时才需要它们
SENTINEL_END = None
SENTINEL_ABORT = object()

STATE_RUNNING = "running"
STATE_DONE = "done"
STATE_FAILED = "failed"
STATE_CANCELLED = "cancelled"


class UploadJobEnded(Exception):
    """接收端在排队时发现工作线程已经到终态：立即结束接收（不再等空位）。"""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


_ACTIVE: dict[str, "UploadJob"] = {}
_ACTIVE_LOCK = threading.Lock()


def active_jobs() -> list["UploadJob"]:
    """当前还没收尾的上传作业（诊断 / 测试用：收尾后必须为空）。"""
    with _ACTIVE_LOCK:
        return list(_ACTIVE.values())


class UploadJob:
    """一次上传的共享状态机（构造在事件循环线程）。"""

    def __init__(
        self,
        *,
        label: str,
        loop: asyncio.AbstractEventLoop,
        depth: int = UPLOAD_QUEUE_DEPTH,
        poll_seconds: float = POLL_SECONDS,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> None:
        self.label = label
        self.id = label
        self.loop = loop
        self.depth = max(1, int(depth))
        self.poll_seconds = max(0.001, float(poll_seconds))
        self.cancel_requested = cancel_requested
        self.box: queue.Queue = queue.Queue(maxsize=self.depth)
        self.state = STATE_RUNNING
        self.reason: str | None = None
        #: 线程安全终态：工作线程与接收端都读它
        self._end_flag = threading.Event()
        #: 事件循环侧：等「有空位」与「终态」（由跨线程 call_soon_threadsafe 唤醒）
        self._space = asyncio.Event()
        self._end = asyncio.Event()
        self._lock = threading.Lock()
        with _ACTIVE_LOCK:
            _ACTIVE[self.id] = self

    # -- 终态 --------------------------------------------------------------

    @property
    def terminal(self) -> bool:
        return self._end_flag.is_set()

    def snapshot(self) -> dict:
        return {
            "id": self.id,
            "state": self.state,
            "reason": self.reason,
            "queued": self.box.qsize(),
            "terminal": self.terminal,
        }

    def _mark(self, state: str, reason: str | None) -> str:
        """置终态（幂等：第一个终态说了算）。任何线程都可调用。"""
        with self._lock:
            if self._end_flag.is_set():
                return self.state
            self.state = state
            self.reason = reason
            self._end_flag.set()
        self._wake(self._end)
        return state

    def _wake(self, event: asyncio.Event) -> None:
        """从工作线程唤醒事件循环上的等待者（跨线程必须走 call_soon_threadsafe）。"""
        try:
            self.loop.call_soon_threadsafe(event.set)
        except RuntimeError:  # 事件循环已经关了（服务关闭）：没人等，忽略
            pass

    # -- 事件循环线程侧 ------------------------------------------------------

    def abort(self, reason: str, *, state: str = STATE_CANCELLED) -> str:
        """事件循环线程主动中止（超限 / 客户端断开 / 请求被取消 / 服务关闭）。"""
        return self._mark(state, reason)

    async def wait_end(self) -> None:
        await self._end.wait()

    async def put(self, chunk: bytes) -> None:
        """把一块字节交给工作线程：**每次排队前先看终态**，队列满时同时等空位与终态。"""
        while True:
            if self._end_flag.is_set():
                raise UploadJobEnded(self.reason or "上传已经结束（工作线程已退出）")
            try:
                self.box.put_nowait(chunk)
                return
            except queue.Full:
                await self._wait_space_or_end()

    async def _wait_space_or_end(self) -> None:
        if not self.box.full():
            return
        self._space.clear()
        end = asyncio.ensure_future(self._end.wait())
        space = asyncio.ensure_future(self._space.wait())
        try:
            await asyncio.wait(
                {end, space}, timeout=self.poll_seconds, return_when=asyncio.FIRST_COMPLETED
            )
        finally:
            for fut in (end, space):
                if not fut.done():
                    fut.cancel()

    async def close_input(self, *, abort: bool = False) -> None:
        """正常结束（投 None）或中止（投哨兵）。

        工作线程已经结束时**直接返回**：结束/中止不依赖一个已经没有消费者的满队列。
        """
        if self._end_flag.is_set():
            return
        try:
            await self.put(SENTINEL_ABORT if abort else SENTINEL_END)
        except UploadJobEnded:
            return

    def close(self) -> None:
        """作业收尾：从注册表移除（请求处理结束时由事件循环线程调用）。"""
        with _ACTIVE_LOCK:
            _ACTIVE.pop(self.id, None)

    # -- 工作线程侧 ----------------------------------------------------------

    def _note_space(self) -> None:
        self._wake(self._space)

    def _stop_requested(self) -> bool:
        if self._end_flag.is_set():
            return True
        if self.cancel_requested is not None:
            try:
                return bool(self.cancel_requested())
            except Exception:  # noqa: BLE001 - 读不到取消标志就按「没取消」处理
                return False
        return False

    def consume(self) -> Iterator[bytes]:
        """工作线程侧的有界取块循环：终态 / 取消一到就退出（不会无限等）。

        取消（DELETE / 服务关闭）能解除这里对 `queue.get` 的阻塞等待 —— 这是
        「取消要能解除工作线程的阻塞读取」的落点。
        """
        while True:
            if self._stop_requested():
                raise UploadAborted(self.reason or "上传已中止（取消 / 服务关闭）；没有保存任何副本")
            try:
                item = self.box.get(timeout=self.poll_seconds)
            except queue.Empty:
                continue
            self._note_space()
            if item is SENTINEL_END:
                return
            if item is SENTINEL_ABORT:
                raise UploadAborted("上传被中止（超出上限或客户端断开）；没有保存任何副本")
            yield item  # type: ignore[misc]

    # -- 工作线程汇报（只置终态，不落库） -------------------------------------

    def worker_finished(self, outcome: DiskOutcome) -> None:
        if outcome.state == STATE_READY:
            self._mark(STATE_DONE, None)
        elif outcome.state == STATE_CHANGED:
            self._mark(STATE_DONE, outcome.error)
        elif outcome.state == DISK_STATE_CANCELLED:
            self._mark(STATE_CANCELLED, outcome.error or "上传已取消")
        else:
            self._mark(STATE_FAILED, outcome.error or "上传失败")

    def worker_failed(self, exc: BaseException) -> None:
        if isinstance(exc, UploadTooLarge):
            self._mark(STATE_FAILED, str(exc))
        elif isinstance(exc, UploadAborted):
            self._mark(STATE_CANCELLED, str(exc))
        else:
            self._mark(
                STATE_FAILED,
                f"上传失败：{redact_text(type(exc).__name__)}: {redact_text(str(exc))}",
            )


def run_upload_worker(service, att, job: UploadJob, *, max_bytes: int) -> DiskOutcome:
    """工作线程入口：**只做文件 I/O**（+ 作业终态），绝不碰数据库。"""
    try:
        outcome = service.write_upload_stream(att, job.consume(), max_bytes=max_bytes)
    except BaseException as exc:  # noqa: BLE001 - 任何退出路径都要先给接收端一个准确终态
        job.worker_failed(exc)
        raise
    job.worker_finished(outcome)
    return outcome
