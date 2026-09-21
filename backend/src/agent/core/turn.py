"""Turn runtime boundary: per-turn state + single-flight scheduler.

Rule of thumb:
    process-level services live on AppContext / RuntimeServices;
    per-turn state lives on TurnContext.

TurnManager guarantees `active_main_turn <= 1` and keeps the rest in a FIFO
queue, so overlapping HTTP requests cannot clobber each other's active loop,
pipeline listeners, notices or cancellation target.

TurnManager is also the **single source of truth for the turn lifecycle**:

    accepted ──▶ queued ──▶ running ──┬──▶ completed
          └──────────────────────────▶├──▶ failed
                                      ├──▶ cancelled
                                      └──▶ unavailable

契约（前端与后端共同遵守）：

* `accepted` 只表示「后端已经受理」——它**不是** active；
* 必须等待前一个主 turn 结束时是 `queued`；
* 只有真的开始执行才是 `running`，也只有 worker 在真正开跑时才会发
  `TURN_START`；
* 终态（`TERMINAL_STATUSES`）单向：进入之后不再变化，更不允许回到 `running`。

被取消的 queued turn 会变成 **tombstone**：它仍然躺在底层 `asyncio.Queue`
里（`asyncio.Queue` 不支持安全删除），但 worker 取到它时会直接跳过 ——
既不会被设成 `_active`，也不会发出任何 turn 生命周期事件。

Every accepted turn gets exactly one `TURN_START` and exactly one `TURN_END`
(the latter in a `finally`), whatever happens inside the runner. Nested loops
(subagents, maintenance, tool development) must never emit turn events — they
are not turns, and a subagent's `TURN_END` used to end the user's turn.
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
EventEmitter = Callable[[str, dict], Awaitable[None]]

TURN_START = "TURN_START"
TURN_END = "TURN_END"

# 终态：进入其中之一后不再变化。
TERMINAL_STATUSES = ("completed", "failed", "cancelled", "unavailable")


def _terminal(ctx: "TurnContext") -> bool:
    return ctx.status in TERMINAL_STATUSES


@dataclass
class TurnContext:
    """All state that belongs to exactly one turn."""

    turn_id: str
    message: str
    created_at: str = field(default_factory=_now)
    initial_topic: str | None = None
    current_topic: str | None = None
    # accepted | queued | running | completed | failed | cancelled | unavailable
    status: str = "accepted"
    user_message_id: str | None = None
    prediction: Any = None
    loop: Any = None
    cancelled: bool = False
    notices: list[str] = field(default_factory=list)
    knowledge_snapshot: list[dict] = field(default_factory=list)
    final_content: str | None = None
    error: str | None = None
    usage: dict | None = None
    result: dict | None = None
    notify: bool = False  # system-driven (e.g. subagent completion) turn
    turn_start_emitted: bool = False
    turn_end_emitted: bool = False
    # 阶段 1：这一轮的归属在**提交时**就捕获接续意图，在**开始执行时**落实成绑定。
    # 提交之后用户再做的新选择只影响后续提交（排队消息不被追溯改向）。
    intent_id: str | None = None
    bound_topic: str | None = None
    bound_fragment_id: str | None = None
    bound_intent_version: int | None = None
    explicit_target: str | None = None


class TurnManager:
    """Single-flight main-turn scheduler with a FIFO queue."""

    def __init__(
        self,
        runner: TurnRunner | None = None,
        publisher: Callable[[dict], Awaitable[None]] | None = None,
        emitter: EventEmitter | None = None,
    ) -> None:
        self._runner = runner
        self._publisher = publisher
        self._emitter = emitter
        self._queue: asyncio.Queue[TurnContext] = asyncio.Queue()
        self._active: TurnContext | None = None
        self._pending: list[TurnContext] = []
        self._cancelled: list[dict] = []  # 最近被取消的 turn（有界）
        self._futures: dict[str, asyncio.Future] = {}
        self._worker: asyncio.Task | None = None
        self._closed = False
        # 队列快照的版本号：每一次影响快照的状态变化都 +1。
        # 前端据此丢弃「比已知状态更旧」的快照 —— 快照是权威的，
        # 但**旧**的权威快照不能覆盖更新的事件（例如 TURN_START 之后晚到的 running=null）。
        self._revision = 0
        # 后端实例标识（由 create_app 注入）：进程重启后 revision 会从头计数，
        # 前端必须靠它判断「基准已经换了」，而不是把新实例的低 revision 当成旧状态。
        self.instance_id: str | None = None

    # -- wiring -----------------------------------------------------------

    def _bump_revision(self) -> int:
        self._revision += 1
        return self._revision

    def set_runner(self, runner: TurnRunner) -> None:
        self._runner = runner

    def set_publisher(self, publisher: Callable[[dict], Awaitable[None]]) -> None:
        self._publisher = publisher

    def set_emitter(self, emitter: EventEmitter) -> None:
        """Wire the SSE emitter; kept as a callable so core/ never imports api/."""
        self._emitter = emitter

    # -- queue snapshot ---------------------------------------------------

    def snapshot(self) -> dict:
        active = self._active
        return {
            "instance_id": self.instance_id,
            "revision": self._revision,
            "running": (
                {"turn_id": active.turn_id, "message": active.message[:120]}
                if active is not None
                else None
            ),
            "queued": [
                {"turn_id": c.turn_id, "message": c.message[:120]}
                for c in self._pending
            ],
            "cancelled": list(self._cancelled),
        }

    def _record_cancelled(self, ctx: TurnContext) -> None:
        self._cancelled.append({"turn_id": ctx.turn_id, "message": ctx.message[:120]})
        del self._cancelled[:-5]  # 只保留最近 5 条

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
        self,
        message: str,
        topic_id: str | None = None,
        *,
        notify: bool = False,
        intent_id: str | None = None,
    ) -> TurnContext:
        if self._closed:
            # worker 已经停了：再收下这个 turn，它只会躺在队列里永远不被执行
            # （调用方还会一直 await 一个永远不会兑现的 future）。
            raise RuntimeError("TurnManager is closed; it no longer accepts new turns")
        # 提交成功 ≠ 开始执行：前面还有主 turn 或已经排着队时，它就是 queued。
        waits = self._active is not None or bool(self._pending)
        ctx = TurnContext(
            turn_id=f"turn_{uuid.uuid4().hex[:12]}",
            message=message,
            initial_topic=topic_id,
            current_topic=topic_id,
            notify=notify,
            intent_id=intent_id,
            status="queued" if waits else "accepted",
        )
        try:
            loop = asyncio.get_running_loop()
            self._futures[ctx.turn_id] = loop.create_future()
        except RuntimeError:
            pass  # no running loop: enqueue without an awaitable result
        self._pending.append(ctx)
        self._queue.put_nowait(ctx)
        self._bump_revision()
        self._ensure_worker()
        self._schedule_emit()
        return ctx

    async def wait(self, turn_id: str, timeout: float | None = None) -> dict | None:
        """等待某个 turn 的结果。

        `wait` 是一次**等待操作**，和 turn 本身的生命周期是两件事：

        * 超时只结束这一次等待 —— 不删除、也不取消 turn 的 completion future；
        * 调用方被取消（任务取消）同样只结束这一次等待；
        * completion future 只在 turn 进入终态、被兑现之后清理（见 `_resolve`）。

        所以这里必须用 `asyncio.shield`：`wait_for` / 任务取消只会取消 shield 的外层，
        不会把内层 future 一起取消掉（否则 turn 结束时结果就没有地方落地了）。
        """
        fut = self._futures.get(turn_id)
        if fut is None:
            return None
        try:
            if timeout is None:
                return await asyncio.shield(fut)
            return await asyncio.wait_for(asyncio.shield(fut), timeout)
        except asyncio.TimeoutError:
            return None

    def _resolve(self, ctx: TurnContext, payload: dict) -> None:
        """兑现某个 turn 的等待者（幂等：已经兑现过的不再重复设置）。"""
        fut = self._futures.pop(ctx.turn_id, None)
        if fut is not None and not fut.done():
            fut.set_result(payload)

    def _ensure_worker(self) -> None:
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._work())

    # -- worker: single-flight FIFO --------------------------------------

    async def _work(self) -> None:
        while not self._closed:
            ctx = await self._queue.get()
            if ctx in self._pending:
                self._pending.remove(ctx)
            # tombstone：排队期间被取消（或已进入终态）的 turn 直接跳过。
            # 它从未开始执行，因此既不能占用 active，也不能发 TURN_START。
            if ctx.cancelled or _terminal(ctx):
                self._resolve(ctx, {"ok": False, "reason": "cancelled"})
                self._schedule_emit()
                continue
            self._active = ctx
            ctx.status = "running"
            self._bump_revision()
            self._schedule_emit()
            await self._emit_turn_start(ctx)
            try:
                if ctx.cancelled:
                    ctx.status = "cancelled"
                else:
                    await self._runner(ctx)
                    if ctx.cancelled:
                        ctx.status = "cancelled"
                    elif ctx.status in ("running", "accepted"):
                        ctx.status = "completed"
            except asyncio.CancelledError:
                ctx.status = "cancelled"
                raise
            except Exception as exc:  # noqa: BLE001 - turn isolation boundary
                ctx.status = "failed"
                ctx.error = f"{type(exc).__name__}: {exc}"
            finally:
                await self._emit_turn_end(ctx)
                if self._active is ctx:
                    self._active = None
                    self._bump_revision()
                self._resolve(
                    ctx,
                    ctx.result if ctx.result is not None else {"ok": False, "reason": ctx.status},
                )
                self._schedule_emit()

    # -- lifecycle events -------------------------------------------------

    async def _emit_turn_start(self, ctx: TurnContext) -> None:
        if ctx.turn_start_emitted:
            return
        ctx.turn_start_emitted = True
        await self._emit_event(
            TURN_START,
            {
                "turn_id": ctx.turn_id,
                "instance_id": self.instance_id,
                # revision 让前端能把「新的 turn 状态」和「旧的队列快照」比较：
                # 晚到的旧快照不得把这一轮清掉。
                "revision": self._revision,
                "message": ctx.message[:200],
            },
        )

    async def _emit_turn_end(self, ctx: TurnContext) -> None:
        """一个 accepted turn 必须且只能有一个 TURN_END（含异常路径）。"""
        if ctx.status not in TERMINAL_STATUSES:
            ctx.status = "completed"
        if ctx.turn_end_emitted:
            return
        ctx.turn_end_emitted = True
        payload: dict[str, Any] = {
            "turn_id": ctx.turn_id,
            "instance_id": self.instance_id,
            "revision": self._revision,
            "status": ctx.status,
            "final_content": ctx.final_content,
            "error": ctx.error,
        }
        if ctx.usage:
            payload.update(ctx.usage)
        await self._emit_event(TURN_END, payload)

    async def _emit_event(self, name: str, data: dict) -> None:
        if self._emitter is None:
            return
        try:
            await self._emitter(name, data)
        except Exception:  # noqa: BLE001 - 广播失败不能影响 turn 收尾
            pass

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
        self._record_cancelled(ctx)
        self._bump_revision()
        self._schedule_emit()
        return True

    def cancel(self, turn_id: str) -> bool:
        """Cancel a specific turn — active or still queued."""
        active = self._active
        if active is not None and active.turn_id == turn_id:
            return self.cancel_active()
        for i, c in enumerate(self._pending):
            if c.turn_id == turn_id:
                if _terminal(c):
                    return False
                c.cancelled = True
                c.status = "cancelled"
                self._pending.pop(i)
                self._record_cancelled(c)
                # 排队项的结局不依赖 worker：立刻兑现等待者，
                # 之后 worker 取到这个 tombstone 只会跳过。
                self._resolve(c, {"ok": False, "reason": "cancelled"})
                self._bump_revision()
                self._schedule_emit()
                return True
        return False

    def queued_count(self) -> int:
        return len(self._pending)

    async def shutdown(self) -> None:
        """应用关闭：不留下任何永远等不到结果的 wait。

        顺序：停止受理新任务 → 处理排队 turn（置终态 + 兑现等待者）
        → 取消 worker（active turn 的收尾在 worker 的 finally 里完成）
        → 清理仍然挂着的等待者与队列对象。
        """
        self._closed = True
        self._bump_revision()

        pending, self._pending = list(self._pending), []
        for ctx in pending:
            ctx.cancelled = True
            if not _terminal(ctx):
                ctx.status = "cancelled"
            self._resolve(ctx, {"ok": False, "reason": "shutdown"})

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

        # 兜底：worker 已经不在，任何还挂着的等待者都必须以终态结束，而不是永远等待。
        for turn_id, fut in list(self._futures.items()):
            if not fut.done():
                fut.set_result({"ok": False, "reason": "cancelled"})
            self._futures.pop(turn_id, None)

        # 丢弃底层队列里剩下的 tombstone / 未执行对象，避免 shutdown 之后仍被引用。
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
                self._queue.task_done()
            except asyncio.QueueEmpty:  # pragma: no cover - 竞态兜底
                break
        self._active = None
