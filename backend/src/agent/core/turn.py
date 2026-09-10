"""Turn runtime boundary: per-turn state + single-flight scheduler.

Rule of thumb:
    process-level services live on AppContext / RuntimeServices;
    per-turn state lives on TurnContext.

TurnManager guarantees `active_main_turn <= 1` and keeps the rest in a FIFO
queue, so overlapping HTTP requests cannot clobber each other's active loop,
pipeline listeners, notices or cancellation target.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


TurnRunner = Callable[["TurnContext"], Awaitable[None]]


@dataclass
class TurnContext:
    """All state that belongs to exactly one turn."""

    turn_id: str
    message: str
    created_at: str = field(default_factory=_now)
    initial_topic: str | None = None
    current_topic: str | None = None
    status: str = "queued"  # queued | running | done | failed | cancelled
    user_message_id: str | None = None
    prediction: Any = None
    loop: Any = None
    cancelled: bool = False
    notices: list[str] = field(default_factory=list)
    knowledge_snapshot: list[dict] = field(default_factory=list)
    final_content: str | None = None
    error: str | None = None
    result: dict | None = None
    notify: bool = False  # system-driven (e.g. subagent completion) turn


class TurnManager:
    """Single-flight main-turn scheduler with a FIFO queue."""

    def __init__(
        self,
        runner: TurnRunner | None = None,
        publisher: Callable[[dict], Awaitable[None]] | None = None,
    ) -> None:
        self._runner = runner
        self._publisher = publisher
        self._queue: asyncio.Queue[TurnContext] = asyncio.Queue()
        self._active: TurnContext | None = None
        self._pending: list[TurnContext] = []
        self._futures: dict[str, asyncio.Future] = {}
        self._worker: asyncio.Task | None = None
        self._closed = False

    # -- wiring -----------------------------------------------------------

    def set_runner(self, runner: TurnRunner) -> None:
        self._runner = runner

    def set_publisher(self, publisher: Callable[[dict], Awaitable[None]]) -> None:
        self._publisher = publisher

    # -- queue snapshot ---------------------------------------------------

    def snapshot(self) -> dict:
        active = self._active
        return {
            "running": (
                {"turn_id": active.turn_id, "message": active.message[:120]}
                if active is not None
                else None
            ),
            "queued": [
                {"turn_id": c.turn_id, "message": c.message[:120]}
                for c in self._pending
            ],
        }

    def _schedule_emit(self) -> None:
        if self._publisher is None:
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        asyncio.create_task(self._emit_queue())

    async def _emit_queue(self) -> None:
        if self._publisher is None:
            return
        try:
            await self._publisher(self.snapshot())
        except Exception:  # noqa: BLE001 - queue broadcast must not break turns
            pass

    # -- submission -------------------------------------------------------

    def submit(
        self, message: str, topic_id: str | None = None, *, notify: bool = False
    ) -> TurnContext:
        ctx = TurnContext(
            turn_id=f"turn_{uuid.uuid4().hex[:12]}",
            message=message,
            initial_topic=topic_id,
            current_topic=topic_id,
            notify=notify,
        )
        try:
            loop = asyncio.get_running_loop()
            self._futures[ctx.turn_id] = loop.create_future()
        except RuntimeError:
            pass  # no running loop: enqueue without an awaitable result
        self._pending.append(ctx)
        self._queue.put_nowait(ctx)
        self._ensure_worker()
        self._schedule_emit()
        return ctx

    async def wait(self, turn_id: str, timeout: float | None = None) -> dict | None:
        fut = self._futures.get(turn_id)
        if fut is None:
            return None
        try:
            if timeout is None:
                return await fut
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            return None

    def _ensure_worker(self) -> None:
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._work())

    # -- worker: single-flight FIFO --------------------------------------

    async def _work(self) -> None:
        while not self._closed:
            ctx = await self._queue.get()
            if ctx in self._pending:
                self._pending.remove(ctx)
            self._active = ctx
            ctx.status = "running"
            self._schedule_emit()
            try:
                if ctx.cancelled:
                    ctx.status = "cancelled"
                else:
                    await self._runner(ctx)
                    if ctx.cancelled:
                        ctx.status = "cancelled"
                    elif ctx.status == "running":
                        ctx.status = "done"
            except asyncio.CancelledError:
                ctx.status = "cancelled"
                raise
            except Exception as exc:  # noqa: BLE001 - turn isolation boundary
                ctx.status = "failed"
                ctx.error = f"{type(exc).__name__}: {exc}"
            finally:
                if self._active is ctx:
                    self._active = None
                fut = self._futures.pop(ctx.turn_id, None)
                if fut is not None and not fut.done():
                    fut.set_result(ctx.result)
                self._schedule_emit()

    # -- introspection / control -----------------------------------------

    @property
    def active(self) -> TurnContext | None:
        return self._active

    def active_loop(self) -> Any:
        return self._active.loop if self._active is not None else None

    def push_notice(self, text: str) -> bool:
        """Route a notice to the active turn's loop; False when none."""
        loop = self.active_loop()
        if loop is None:
            return False
        loop.push_notice(text)
        return True

    def cancel_active(self) -> bool:
        """Cancel the active turn's in-flight work; False when idle."""
        ctx = self._active
        if ctx is None:
            return False
        ctx.cancelled = True
        if ctx.loop is not None:
            ctx.loop.cancel()
        self._schedule_emit()
        return True

    def cancel(self, turn_id: str) -> bool:
        """Cancel a specific turn — active or still queued."""
        active = self._active
        if active is not None and active.turn_id == turn_id:
            return self.cancel_active()
        for i, c in enumerate(self._pending):
            if c.turn_id == turn_id:
                c.cancelled = True
                c.status = "cancelled"
                self._pending.pop(i)
                self._schedule_emit()
                return True
        return False

    def queued_count(self) -> int:
        return len(self._pending)

    async def shutdown(self) -> None:
        self._closed = True
        worker = self._worker
        self._worker = None
        if worker is not None:
            worker.cancel()
            try:
                await worker
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001 - shutdown must not raise
                pass
