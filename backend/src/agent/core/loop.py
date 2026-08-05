"""Agent loop state machine.

States: PLANNING -> (TOOL_EXEC -> OBSERVING -> PLANNING) | DONE
Termination: no tool calls requested, or budget exhausted (STOPPED unless
force_continue). Tool failures are isolated per call (WARNING events).
Memory hooks are placeholders until M6/M7.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum

from agent.adapters.base import AdapterMode, BaseAdapter, ChatMessage, Completion
from agent.api.events import EventType, make_event
from agent.api.server import EventBus
from agent.core.budget import IterationBudget, default_iterations
from agent.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)


class LoopPhase(str, Enum):
    PLANNING = "planning"
    TOOL_EXEC = "tool_exec"
    OBSERVING = "observing"
    DONE = "done"
    STOPPED = "stopped"


@dataclass
class TurnResult:
    final_content: str | None
    phase: LoopPhase
    iterations_used: int
    tokens_used: int
    tool_calls_made: int
    warnings: list[str] = field(default_factory=list)


class AgentLoop:
    def __init__(
        self,
        adapter: BaseAdapter,
        registry: ToolRegistry,
        bus: EventBus,
        *,
        max_iterations: int | None = None,
        token_budget: int | None = None,
        force_continue: bool = False,
    ) -> None:
        self.adapter = adapter
        self.registry = registry
        self.bus = bus
        mode = AdapterMode(adapter.mode)
        self.budget = IterationBudget(
            max_iterations=max_iterations or default_iterations(mode),
            token_budget=token_budget or 0,
        ) if token_budget else IterationBudget(
            max_iterations=max_iterations or default_iterations(mode),
        )
        self.force_continue = force_continue
        self._warnings: list[str] = []

    # -- event helpers ----------------------------------------------------

    async def _emit(self, event_type: EventType, data: dict) -> None:
        await self.bus.publish(make_event(event_type, data))

    def _warn(self, message: str) -> None:
        self._warnings.append(message)
        logger.warning(message)

    # -- main entry -------------------------------------------------------

    async def run(self, user_message: str) -> TurnResult:
        messages: list[ChatMessage] = [ChatMessage(role="user", content=user_message)]
        self._warnings = []
        await self._emit(
            EventType.TURN_START,
            {"turn": 1, "user_message": user_message[:200]},
        )

        phase = LoopPhase.PLANNING
        tool_calls_made = 0
        final_content: str | None = None

        while True:
            if self.budget.exhausted and not self.force_continue:
                phase = LoopPhase.STOPPED
                break

            # PLANNING
            completion = await self._plan(messages)
            self.budget.consume_tokens(self._tokens_of(completion))
            self.budget.consume_iteration()

            if not completion.tool_calls:
                phase = LoopPhase.DONE
                final_content = completion.message.content
                break

            # TOOL_EXEC + OBSERVING
            phase = LoopPhase.TOOL_EXEC
            # append the assistant message (with tool_calls) so the
            # following tool messages are valid per the API contract
            messages.append(completion.message)
            for call in completion.tool_calls:
                tool_calls_made += 1
                await self._emit(
                    EventType.TOOL_START,
                    {"tool": call.name, "arguments": call.arguments},
                )
                result = await self.registry.execute(call)
                await self._emit(
                    EventType.TOOL_END,
                    {
                        "tool": call.name,
                        "ok": result.ok,
                        "error": result.error,
                        "content_preview": result.content[:200],
                    },
                )
                if not result.ok:
                    self._warn(f"tool {call.name} failed: {result.error}")
                messages.append(
                    ChatMessage(role="tool", tool_call_id=call.id, content=result.content)
                )
            phase = LoopPhase.OBSERVING

        usage = {
            "iterations": self.budget.used_iterations,
            "tokens": self.budget.used_tokens,
            "tool_calls": tool_calls_made,
        }
        await self._emit(
            EventType.TURN_END,
            {"phase": phase.value, "final_content": final_content, **usage},
        )
        await self._emit(EventType.USAGE, usage)
        return TurnResult(
            final_content=final_content,
            phase=phase,
            iterations_used=self.budget.used_iterations,
            tokens_used=self.budget.used_tokens,
            tool_calls_made=tool_calls_made,
            warnings=list(self._warnings),
        )

    # -- steps ------------------------------------------------------------

    async def _plan(self, messages: list[ChatMessage]) -> Completion:
        try:
            return await self.adapter.complete(messages, self.registry.specs())
        except Exception as exc:  # adapter-level failure ends the turn
            self._warn(f"planning failed: {exc}")
            await self._emit(
                EventType.ERROR,
                {"code": "planning_failed", "message": str(exc)[:200], "recoverable": False},
            )
            raise

    def _tokens_of(self, completion: Completion) -> int:
        usage = completion.usage or {}
        return int(usage.get("total_tokens", 0) or 0)