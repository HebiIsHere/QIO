"""Agent loop state machine.

States: PLANNING -> (TOOL_EXEC -> OBSERVING -> PLANNING) | DONE
Termination: no tool calls requested, or budget exhausted (STOPPED unless
force_continue). Tool failures are isolated per call (WARNING events).
Memory hooks are placeholders until M6/M7.

工具执行走 ToolRegistry 的执行管线（tool/start → pre/execute/post → tool/result
→ tool/end）：本循环在构造时把内部事件转发为 SSE 的 TOOL_START/TOOL_END，
并在 tool/result 监听器里执行 tool_trace 审计；turn 结束时卸载监听器。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

from agent.adapters.base import AdapterMode, BaseAdapter, ChatMessage, Completion
from agent.api.events import EventType, make_event
from agent.api.bus import EventBus
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
        tool_trace=None,
        tool_selector=None,
        max_parallel_tools: int = 4,
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
        self._notices: list[str] = []
        self.tool_trace = tool_trace
        self.tool_selector = tool_selector
        self.max_parallel_tools = max(1, max_parallel_tools)
        self._active_tool_tasks: set[asyncio.Task] = set()
        self._disposers: list[Callable[[], None]] = []
        self._bind_pipeline()

    # -- pipeline wiring ---------------------------------------------------

    def _bind_pipeline(self) -> None:
        """把内部工具事件转发到 SSE，并在 tool/result 上做审计。"""
        register = getattr(self.registry, "register_policy", None)
        if register is None:
            return
        self._disposers.append(register("tool/start", self._on_pipeline_start))
        self._disposers.append(register("tool/end", self._on_pipeline_end))
        if self.tool_trace is not None:
            self._disposers.append(register("tool/result", self._on_pipeline_result))

    def dispose(self) -> None:
        """卸载本轮注册的管线监听器。"""
        for disposer in self._disposers:
            try:
                disposer()
            except Exception:  # noqa: BLE001 - cleanup must never break the turn
                logger.warning("pipeline disposer failed", exc_info=True)
        self._disposers.clear()

    async def _on_pipeline_start(self, data: dict) -> None:
        await self._emit(
            EventType.TOOL_START,
            {"tool": data.get("tool"), "arguments": data.get("arguments", {})},
        )

    async def _on_pipeline_end(self, data: dict) -> None:
        await self._emit(
            EventType.TOOL_END,
            {
                "tool": data.get("tool"),
                "ok": data.get("ok"),
                "error": data.get("error"),
                "content_preview": data.get("content_preview", ""),
                "presentation": data.get("presentation"),
            },
        )

    async def _on_pipeline_result(self, data: dict) -> None:
        result = data.get("result")
        if self.tool_trace is None or result is None:
            return
        call = data.get("call")
        try:
            self.tool_trace({
                "tool_name": data.get("tool"),
                "arguments": getattr(call, "arguments", {}),
                "ok": result.ok,
                "result": result.content,
            })
        except Exception:  # noqa: BLE001 - tracing must not break the loop
            logger.warning("tool trace failed for %s", data.get("tool"), exc_info=True)

    # -- parallel dispatch & cancellation -----------------------------------

    async def _dispatch_tool_calls(self, calls) -> dict[str, Any]:
        """并发安全工具并行（受 max_parallel_tools 限制），其余串行。

        返回 {call.id: ToolResult}，顺序无关（调用方按原始顺序回填）。
        """
        safe_calls = [
            c for c in calls
            if getattr(self.registry.get(c.name), "is_concurrency_safe", False)
        ]
        safe_ids = {c.id for c in safe_calls}
        unsafe_calls = [c for c in calls if c.id not in safe_ids]
        results: dict[str, Any] = {}

        # 非安全：严格串行（保持顺序）
        for call in unsafe_calls:
            results[call.id] = await self._guarded_execute(call)

        if safe_calls:
            semaphore = asyncio.Semaphore(self.max_parallel_tools)

            async def _run_safe(call):
                async with semaphore:
                    return call.id, await self._guarded_execute(call)

            gathered = await asyncio.gather(*(_run_safe(c) for c in safe_calls))
            for call_id, result in gathered:
                results[call_id] = result
        return results

    async def _guarded_execute(self, call) -> Any:
        """在独立 task 中执行，登记到 _active_tool_tasks 以便 cancel()。"""
        task = asyncio.current_task()
        if task is not None:
            self._active_tool_tasks.add(task)
        try:
            return await self.registry.execute(call)
        finally:
            if task is not None:
                self._active_tool_tasks.discard(task)

    def cancel(self) -> None:
        """取消本轮所有在途工具调用；registry 会把 CancelledError 转成 aborted。"""
        for task in list(self._active_tool_tasks):
            task.cancel()

    def push_notice(self, text: str) -> None:
        """Queue a system notice; injected before the next PLANNING step."""
        self._notices.append(text)

    # -- event helpers ----------------------------------------------------

    async def _emit(self, event_type: EventType, data: dict) -> None:
        await self.bus.publish(make_event(event_type, data))

    def _warn(self, message: str) -> None:
        self._warnings.append(message)
        logger.warning(message)

    # -- main entry -------------------------------------------------------

    async def run(self, user_message: str) -> TurnResult:
        try:
            return await self._run(user_message)
        finally:
            self.dispose()

    async def _run(self, user_message: str) -> TurnResult:
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
                # 预算耗尽也要让用户知道为什么没有继续（而不是静默停止）
                reason = (
                    f"迭代次数达到上限（{self.budget.used_iterations}/{self.budget.max_iterations}）"
                    if self.budget.used_iterations >= self.budget.max_iterations
                    else f"token 预算耗尽（{self.budget.used_tokens}/{self.budget.token_budget}）"
                )
                self._warn(f"turn stopped: {reason}")
                await self._emit(
                    EventType.WARNING,
                    {"code": "budget_exhausted", "message": reason, "recoverable": True},
                )
                break

            if self._notices:
                messages.append(
                    ChatMessage(role="system", content="\n".join(self._notices))
                )
                self._notices.clear()

            # PLANNING
            completion = await self._plan(messages)
            self.budget.consume_tokens(self._tokens_of(completion))
            self.budget.consume_iteration()

            # native 模式：模型在工具调用前先说话时，把内容作为 interim 事件推给前端
            if (
                self.adapter.mode == AdapterMode.NATIVE
                and completion.tool_calls
                and completion.message.content
                and completion.message.content.strip()
            ):
                await self._emit(
                    EventType.ASSISTANT,
                    {"content": completion.message.content, "interim": True},
                )

            if not completion.tool_calls:
                phase = LoopPhase.DONE
                final_content = completion.message.content
                break

            # TOOL_EXEC + OBSERVING（执行走 registry 管线，事件由监听器转发；
            # 并发安全工具分组并行，其余串行，结果按原始顺序回填）
            phase = LoopPhase.TOOL_EXEC
            messages.append(completion.message)
            results = await self._dispatch_tool_calls(completion.tool_calls)
            for call in completion.tool_calls:
                tool_calls_made += 1
                result = results[call.id]
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
        tools = self.registry.specs()
        if self.tool_selector is not None:
            # route tools by the current query context (last user + tool message)
            query_parts: list[str] = []
            for m in reversed(messages):
                if m.role == "user" and m.content:
                    query_parts.append(m.content)
                    break
            for m in reversed(messages):
                if m.role == "tool" and m.content:
                    query_parts.append(m.content[:200])
                    break
            query = "\n".join(reversed(query_parts))[:500]
            try:
                tools = self.tool_selector(query)
            except Exception:  # noqa: BLE001 - routing must never break planning
                logger.warning("tool routing failed; falling back to full set", exc_info=True)
        try:
            return await self.adapter.complete(messages, tools)
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
