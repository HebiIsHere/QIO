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


def abort_jobs_for(attachment_id: str, reason: str) -> int:
    """把某个附件正在进行的上传作业置成终态（DELETE = 取消这次上传）。

    为什么必须由路由来做：services/attachments.py 的 delete() 置位取消事件后**会把它从
    _cancel 里清掉**，事后用 is_cancel_requested() 轮询的消费者（本模块的取块循环）就再也
    看不到取消了 —— 取消信号必须在作业自己的终态上留痕，而不是依赖服务里那个临时事件。
    """
    target = str(attachment_id)
    with _ACTIVE_LOCK:
        jobs = [job for job in _ACTIVE.values() if job.id == target]
    for job in jobs:
        job.abort(reason)
    return len(jobs)


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
        generation: int | None = None,
    ) -> None:
        self.label = label
        #: N5：本作业**登记时**的代际。提交边界与落库必须用同一个代际 ——
        #: 否则「登记在前、worker 启动在后」的上传会以「启动那一刻」的代际通过提交
        #: 边界（覆盖后发起的重定位成果），而落库按登记代际丢弃，行与磁盘就此不一致。
        self.generation = generation
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
        #: 工作线程**已经开始**跑（进入取块循环之前）；诊断/验收用来确定「它已经/还没进阻塞读」
        self.worker_started = threading.Event()
        #: 已经交给写盘循环的分块数 + 是否正阻塞在 queue.get 上（验收用的确定性状态，不靠 sleep 猜）
        self.consumed = 0
        self._reading = False
        #: 工作线程**真的退出**了（临时文件清理完成）：取消/收尾的验收等它，不靠墙钟
        self.worker_done = threading.Event()
        #: 事件循环侧：等「有空位」「终态」「工作线程退出」（跨线程 call_soon_threadsafe 唤醒）
        self._space = asyncio.Event()
        self._end = asyncio.Event()
        self._worker_exit = asyncio.Event()
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
            "consumed": self.consumed,
            "reading": self._reading,
            "worker_done": self.worker_done.is_set(),
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
        """事件循环线程主动中止（超限 / 客户端断开 / 请求被取消 / 服务关闭 / 附件被删）。

        除了置终态，还要**把阻塞在 queue.get 上的工作线程叫醒** —— 取消不能靠「等下一轮
        轮询」，那是运气（CI py3.12 的取消用例就是被这个坑掉的）。
        """
        result = self._mark(state, reason)
        self._wake_reader()
        return result

    def _wake_reader(self) -> None:
        """往队列里塞一个中止哨兵，让阻塞的读取立刻返回。

        队列满时工作线程本来就有数据可读（消费完自然会在循环顶部看到终态），忽略 Full 即可。
        """
        try:
            self.box.put_nowait(SENTINEL_ABORT)
        except queue.Full:
            pass

    def worker_exited(self) -> None:
        """工作线程退出（run_upload_worker 的 finally）：置退出事件并唤醒事件循环上的等待者。"""
        self.worker_done.set()
        self._wake(self._worker_exit)

    async def wait_worker_exit(self, *, timeout: float) -> bool:
        """等工作线程真的退出：事件驱动；timeout 只用于判定失败（不是证据）。"""
        if self.worker_done.is_set():
            return True
        try:
            await asyncio.wait_for(self._worker_exit.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            return False
        return True

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
                self._reading = True
                item = self.box.get(timeout=self.poll_seconds)
            except queue.Empty:
                continue
            finally:
                self._reading = False
            self._note_space()
            if item is SENTINEL_END:
                return
            if item is SENTINEL_ABORT:
                raise UploadAborted("上传被中止（超出上限或客户端断开）；没有保存任何副本")
            self.consumed += 1
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
    job.worker_started.set()
    try:
        # N5：把**登记时**的代际交给提交边界（与 api/server.py 的 apply_outcome 同一个），
        # 工作线程启动得再晚也不会以新代际的身份覆盖更新的准备结果。
        outcome = service.write_upload_stream(
            att, job.consume(), max_bytes=max_bytes, generation=job.generation
        )
    except BaseException as exc:  # noqa: BLE001 - 任何退出路径都要先给接收端一个准确终态
        job.worker_failed(exc)
        raise
    else:
        job.worker_finished(outcome)
        return outcome
    finally:
        # 终态先落、退出事件后置（等 worker_done 的验收再读 job.terminal 时不会看到半截状态）；
        # 临时文件清理已经在 write_upload_stream 内部完成。
        job.worker_exited()
