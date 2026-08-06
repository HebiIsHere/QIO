"""Task manager: async subagent task lifecycle, concurrency cap, notify.

Tasks run in the background with a hard concurrency cap (extra tasks queue
behind the semaphore). Results are cached in memory (never truncated).
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
NOTIFY_CB = Callable[[str, "TaskRecord"], Awaitable[None]]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


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


class TaskManager:
    def __init__(
        self,
        bus,
        *,
        max_concurrent: int = DEFAULT_MAX_CONCURRENT,
    ) -> None:
        self.bus = bus
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._records: dict[str, TaskRecord] = {}
        self._waiters: dict[str, list[asyncio.Future]] = {}
        self._notify: dict[str, list[NOTIFY_CB]] = {}

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
        asyncio.create_task(self._run(task_id, coro_factory))
        return task_id

    # -- lifecycle --------------------------------------------------------

    async def _run(
        self, task_id: str, coro_factory: Callable[[], Awaitable[ToolResult]]
    ) -> None:
        record = self._records[task_id]
        record.status = "running"
        await self._emit(task_id)
        async with self._semaphore:
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
        if timeout is None:
            await fut
        else:
            try:
                await asyncio.wait_for(fut, timeout=timeout)
            except asyncio.TimeoutError:
                return ("running", None)
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
