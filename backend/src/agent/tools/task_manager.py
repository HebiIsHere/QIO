"""Task manager: async subagent task lifecycle, concurrency cap, notify.

Tasks run in the background with a hard concurrency cap (extra tasks queue
behind the semaphore; only the task holding a slot is `running`, the rest stay
`queued`). Results are cached in memory but not forever: finished records
expire by TTL and by count, and eviction releases their result buffers.
Completion wakes awaiters and fires registered notify callbacks, and a
SUBAGENT_STATUS event is emitted for the frontend.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Awaitable, Callable

from agent.api.events import EventType, make_event
from agent.tools.base import ToolResult

logger = logging.getLogger(__name__)

DEFAULT_MAX_CONCURRENT = 4
DEFAULT_MAX_RECORDS = 200
DEFAULT_RECORD_TTL_SECONDS = 1800.0
NOTIFY_CB = Callable[[str, "TaskRecord"], Awaitable[None]]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


@dataclass
class TaskRecord:
    task_id: str
    tool: str
    status: str  # queued | running | done | failed
    created_at: str = field(default_factory=_now)
    finished_at: str | None = None
    result: ToolResult | None = None
    full_content: str | None = None  # untruncated result for pointer reads
    iterations: int = 0
    tokens: int = 0
    tool_calls: int = 0

    @property
    def done(self) -> bool:
        return self.status in ("done", "failed")

    def release(self) -> None:
        """释放结果占用（长结果 full_content 可能是几十 KB 级）。"""
        self.result = None
        self.full_content = None


class TaskManager:
    def __init__(
        self,
        bus,
        *,
        max_concurrent: int = DEFAULT_MAX_CONCURRENT,
        max_records: int = DEFAULT_MAX_RECORDS,
        record_ttl_seconds: float | None = DEFAULT_RECORD_TTL_SECONDS,
    ) -> None:
        self.bus = bus
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._records: dict[str, TaskRecord] = {}
        self._waiters: dict[str, list[asyncio.Future]] = {}
        self._notify: dict[str, list[NOTIFY_CB]] = {}
        # 持有后台任务引用：事件循环只持弱引用，不保存的话任务可能被 GC 提前回收
        self._tasks: set[asyncio.Task] = set()
        self._max_records = max(1, max_records)
        self._record_ttl = record_ttl_seconds
        self._closed = False

    # -- submit -----------------------------------------------------------

    def submit(
        self,
        tool_name: str,
        coro_factory: Callable[[], Awaitable[ToolResult]],
        *,
        task_id: str | None = None,
    ) -> str:
        if self._closed:
            # 关闭之后收下的任务永远不会被执行（事件循环要走了）
            raise RuntimeError("TaskManager is closed; it no longer accepts new tasks")
        task_id = task_id or f"task_{uuid.uuid4().hex[:12]}"
        record = TaskRecord(task_id=task_id, tool=tool_name, status="queued")
        self._records[task_id] = record
        self.prune()
        task = asyncio.create_task(self._run(task_id, coro_factory))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task_id

    # -- shutdown ---------------------------------------------------------

    def is_closed(self) -> bool:
        return self._closed

    async def shutdown(self) -> None:
        """应用关闭：停止收新任务、取消在跑的任务、兑现所有等待者。

        顺序上要保证「等待者先被兑现、任务再被取消完」，否则调用方会永远
        等一个不会再有人设置结果的 future。
        """
        self._closed = True

        # 排队中的任务：取消对应的后台任务（它们在等 semaphore，从未真正执行）
        running, self._tasks = list(self._tasks), set()
        for task in running:
            task.cancel()

        for task in running:
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001 - 关闭阶段不能再抛
                logger.warning("task did not stop cleanly", exc_info=True)

        # 兜底：任何仍然不是终态的记录都收口（例如任务在别处被取消过）
        for record in self._records.values():
            if not record.done:
                record.status = "failed"
                record.result = ToolResult(ok=False, error="task cancelled: app shutting down")
                record.finished_at = _now()

        # 兜底：任何仍然挂着的等待者都以终态结束
        for task_id, waiters in list(self._waiters.items()):
            record = self._records.get(task_id)
            outcome = (
                (record.status, record.result)
                if record is not None
                else ("not_found", None)
            )
            for fut in waiters:
                if not fut.done():
                    fut.set_result(outcome)
            self._waiters.pop(task_id, None)
        for task_id, callbacks in list(self._notify.items()):
            self._notify.pop(task_id, None)

    # -- lifecycle --------------------------------------------------------

    async def _run(
        self, task_id: str, coro_factory: Callable[[], Awaitable[ToolResult]]
    ) -> None:
        record = self._records.get(task_id)
        if record is None:  # 未完成任务不会被回收，这里只是防御
            return
        # 提交即 queued：抢到并发额度之前不得声称在跑，否则前端会把排队当成执行中。
        await self._emit(task_id)
        try:
            async with self._semaphore:
                record.status = "running"
                await self._emit(task_id)
                try:
                    result = await coro_factory()
                    record.result = result
                    record.status = "done" if result.ok else "failed"
                except Exception as exc:  # noqa: BLE001 - task isolation
                    logger.warning("subagent task %s failed: %s", task_id, exc)
                    record.result = ToolResult(ok=False, error=f"{type(exc).__name__}: {exc}")
                    record.status = "failed"
        except asyncio.CancelledError:
            # 关闭时的取消也是终态：把记录留在 running 会让「谁在跑」永远失真。
            # 这里要覆盖两种取消点：等 semaphore 时被取消（还没开始跑）与执行中被取消。
            if not record.done:
                record.status = "failed"
                record.result = ToolResult(ok=False, error="task cancelled: shutting down")
            record.finished_at = _now()
            await self._emit(task_id)
            raise
        record.finished_at = _now()
        await self._emit(task_id)
        # 兑现 waiter 时传**结局快照**（status + result），而不是记录对象本身：
        # 紧接着的 prune() 可能把这条记录回收并 release()（清空 result/full_content），
        # 而 waiter 一定是在 prune 之后才被调度 —— 传对象就会让它拿到
        # 「done 但没有内容」的假结论。
        outcome = (record.status, record.result)
        for fut in self._waiters.pop(task_id, []):
            if not fut.done():
                fut.set_result(outcome)
        for cb in list(self._notify.pop(task_id, [])):
            try:
                await cb(task_id, record)
            except Exception:  # noqa: BLE001 - notify isolation
                logger.warning("notify callback failed for %s", task_id, exc_info=True)
        self.prune()

    # -- await / notify ---------------------------------------------------

    async def await_result(
        self, task_id: str, timeout: float = 120.0
    ) -> tuple[str, ToolResult | None]:
        """Returns (status, result): 终态 / 仍等待中的**真实**状态 / not_found。

        超时的含义只有一个：在这次等待窗口内任务没有进入终态。
        它**不**代表任务正在运行 —— 排队中的任务超时后必须报 `queued`。
        所以超时返回的是记录当前的真实状态（并在边界情况下重新读一次，
        因为 queued → running / 终态 可能恰好发生在这个窗口里）。
        """
        record = self._records.get(task_id)
        if record is None:
            return ("not_found", None)
        if record.done:
            return (record.status, record.result)
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self._waiters.setdefault(task_id, []).append(fut)
        try:
            if timeout is None:
                return await fut
            else:
                return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            # 超时必须摘掉自己注册的 waiter，否则每次超时都留下永久残留；
            # 返回前重读一次记录，避免把边界上的真实状态报成过期值。
            current = self._records.get(task_id)
            if current is None:
                return ("not_found", None)
            if current.done:
                return (current.status, current.result)
            return (current.status, None)
        finally:
            waiters = self._waiters.get(task_id)
            if waiters is not None:
                if fut in waiters:
                    waiters.remove(fut)
                if not waiters:
                    self._waiters.pop(task_id, None)

    def register_notify(self, task_id: str, cb: NOTIFY_CB) -> bool:
        """Subscribe a completion callback; returns False when already done."""
        record = self._records.get(task_id)
        if record is None or record.done:
            return False
        self._notify.setdefault(task_id, []).append(cb)
        return True

    def record_info(self, task_id: str) -> TaskRecord | None:
        return self._records.get(task_id)

    def snapshot(self) -> list[dict]:
        """仍在跑 / 仍在排队的任务（重连后据此恢复「独立任务」卡）。

        已完成的任务不进快照：它们的结果已经通过 SUBAGENT_STATUS 或收尾消息
        体现过了，重连时再列一遍只会把已经结束的卡重新点亮。
        字段与 SUBAGENT_STATUS 事件保持一致，前端可以用同一套渲染。
        """
        out: list[dict] = []
        for record in self._records.values():
            if record.done:
                continue
            result = record.result
            out.append(
                {
                    "task_id": record.task_id,
                    "tool": record.tool,
                    "status": record.status,
                    "ok": result.ok if result else None,
                    "content_preview": (result.content or "")[:200] if result else "",
                    "error": (result.error or "")[:200] if result and not result.ok else None,
                    "iterations": record.iterations,
                    "tokens": record.tokens,
                    "tool_calls": record.tool_calls,
                }
            )
        return out

    # -- retention --------------------------------------------------------

    def prune(self) -> None:
        """回收已完成记录：先按 TTL，再按最大条数（永不回收运行中 / 排队中的任务）。"""
        now = datetime.now(timezone.utc)
        if self._record_ttl is not None:
            for task_id, record in list(self._records.items()):
                if not record.done:
                    continue
                finished = _parse_ts(record.finished_at) or _parse_ts(record.created_at)
                if finished is not None and (
                    now - finished
                ).total_seconds() > self._record_ttl:
                    self._discard(task_id)
        while len(self._records) > self._max_records:
            victim = self._oldest_finished()
            if victim is None:
                # 剩下的全是活任务：宁可短暂超限，也不能丢活的子任务。
                break
            self._discard(victim)

    def _oldest_finished(self) -> str | None:
        oldest: str | None = None
        oldest_key = ""
        for task_id, record in self._records.items():
            if not record.done:
                continue
            key = record.finished_at or record.created_at
            if oldest is None or key < oldest_key:
                oldest, oldest_key = task_id, key
        return oldest

    def _discard(self, task_id: str) -> None:
        record = self._records.pop(task_id, None)
        if record is not None:
            record.release()

    # -- events -----------------------------------------------------------

    async def _emit(self, task_id: str) -> None:
        record = self._records.get(task_id)
        if record is None:
            return
        result = record.result
        await self.bus.publish(
            make_event(
                EventType.SUBAGENT_STATUS,
                {
                    "task_id": task_id,
                    "tool": record.tool,
                    "status": record.status,
                    "ok": result.ok if result else None,
                    "content_preview": (result.content or "")[:200] if result else "",
                    "error": (result.error or "")[:200] if result and not result.ok else None,
                    "iterations": record.iterations,
                    "tokens": record.tokens,
                    "tool_calls": record.tool_calls,
                },
            )
        )
