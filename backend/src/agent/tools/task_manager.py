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

    # -- submit -----------------------------------------------------------

    def submit(
        self,
        tool_name: str,
        coro_factory: Callable[[], Awaitable[ToolResult]],
        *,
        task_id: str | None = None,
    ) -> str:
        task_id = task_id or f"task_{uuid.uuid4().hex[:12]}"
        record = TaskRecord(task_id=task_id, tool=tool_name, status="queued")
        self._records[task_id] = record
        self.prune()
        task = asyncio.create_task(self._run(task_id, coro_factory))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task_id

    # -- lifecycle --------------------------------------------------------

    async def _run(
        self, task_id: str, coro_factory: Callable[[], Awaitable[ToolResult]]
    ) -> None:
        record = self._records.get(task_id)
        if record is None:  # 未完成任务不会被回收，这里只是防御
            return
        # 提交即 queued：抢到并发额度之前不得声称在跑，否则前端会把排队当成执行中。
        await self._emit(task_id)
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
        record.finished_at = _now()
        await self._emit(task_id)
        for fut in self._waiters.pop(task_id, []):
            if not fut.done():
                fut.set_result(record)
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
        """Returns (status, result): done/failed/not_found, or running on timeout."""
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
                await fut
            else:
                await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            # 超时必须摘掉自己注册的 waiter，否则每次超时都留下永久残留。
            return ("running", None)
        finally:
            waiters = self._waiters.get(task_id)
            if waiters is not None:
                if fut in waiters:
                    waiters.remove(fut)
                if not waiters:
                    self._waiters.pop(task_id, None)
        return (record.status, record.result)

    def register_notify(self, task_id: str, cb: NOTIFY_CB) -> bool:
        """Subscribe a completion callback; returns False when already done."""
        record = self._records.get(task_id)
        if record is None or record.done:
            return False
        self._notify.setdefault(task_id, []).append(cb)
        return True

    def record_info(self, task_id: str) -> TaskRecord | None:
        return self._records.get(task_id)

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
