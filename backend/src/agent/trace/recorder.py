"""TurnTracer: thin per-turn handle over TraceStore for loop/orchestrator."""

from __future__ import annotations

from agent.trace.redact import preview
from agent.trace.store import TraceStore


class TurnTracer:
    def __init__(self, store: TraceStore, turn_id: str) -> None:
        self.store = store
        self.turn_id = turn_id

    @property
    def enabled(self) -> bool:
        return self.store.enabled

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
