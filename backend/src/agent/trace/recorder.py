"""TurnTracer: thin per-turn handle over TraceStore for loop/orchestrator."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator

from agent.trace.phases import PhaseTimer
from agent.trace.redact import preview
from agent.trace.store import TraceStore


class TurnTracer:
    """一轮的计时/记录句柄。

    `phase()` 是「时间花在哪」的权威入口（见 trace/phases.py）：调用方按阶段开
    上下文，收口时 store 自动带上整本阶段账。`note()` 记与时间轴无关的事实
    （例如排队等待时长）。
    """

    def __init__(self, store: TraceStore, turn_id: str) -> None:
        self.store = store
        self.turn_id = turn_id
        self.timer = PhaseTimer()
        store.register_tracer(self)

    @property
    def enabled(self) -> bool:
        return self.store.enabled

    # -- phase timing ------------------------------------------------------

    @contextmanager
    def phase(self, name: str, detail: str | None = None) -> Iterator[None]:
        with self.timer.phase(name, detail):
            yield

    def note(self, key: str, value: Any) -> None:
        self.timer.note(key, value)

    def record_after_turn(self, name: str, ms: int, detail: str | None = None) -> None:
        """登记一段 turn 结束后的后台工作耗时，并立刻落库（阶段账本会重算 residual）。

        后台任务在这一轮 finish() 之后才结束，所以它自己负责把这一段补写回去。
        """
        self.timer.after_turn(name, ms, detail)
        self.store.set_phases(self.turn_id, self.timer.payload())

    def take_phases(self, *, duration_ms: int | None = None) -> dict:
        """收口并交出阶段账本（幂等：第二次拿到的是同一份快照）。"""
        self.timer.stop()
        return self.timer.payload(duration_ms=duration_ms)

    def flush_phases(self) -> None:
        """兜底落库：没走到 store.finish 的路径（例如凭据不可用）也不丢时间去向。"""
        self.store.flush_phases(self.turn_id, self)

    # -- loop-facing ------------------------------------------------------

    def model_call(self, **info) -> None:
        self.store.record_model_call(self.turn_id, info)

    def tool_run(
        self,
        *,
        call_id: str,
        tool: str,
        arguments=None,
        ok: bool,
        error: str | None,
        duration_ms: int,
        policy: str | None = None,
        result: str | None = None,
    ) -> None:
        self.store.record_tool_run(
            self.turn_id,
            {
                "call_id": call_id,
                "tool": tool,
                "args_preview": preview(arguments, 300),
                "ok": ok,
                "error": error,
                "duration_ms": duration_ms,
                "policy": policy,
                "result_preview": preview(result, 300),
            },
        )

    def warning(self, code: str, message: str) -> None:
        self.store.add_warning(self.turn_id, code, message)

    # -- orchestrator-facing ---------------------------------------------

    def topic(self, **info) -> None:
        self.store.set_topic(self.turn_id, info)

    def injection(self, **info) -> None:
        self.store.set_injection(self.turn_id, info)

    def write(self, kind: str, value: str) -> None:
        self.store.record_write(self.turn_id, kind, value)
