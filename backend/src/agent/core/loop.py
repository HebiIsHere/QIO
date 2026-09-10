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
from agent.core.guard import GuardVerdict, RunawayGuard
from agent.tools.registry import ToolRegistry
from agent.tools.base import ToolResult

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
        approvals=None,
        guard: RunawayGuard | None = None,
        continue_batch_iterations: int = 32,
        continue_batch_tokens: int = 12800,
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
        self.approvals = approvals
        self.guard = guard
        self.continue_batch_iterations = continue_batch_iterations
        self.continue_batch_tokens = continue_batch_tokens
        self._halted = False
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
            result = await self.registry.execute(call)
            if self.guard is not None:
                verdict = self.guard.observe(call.name, dict(call.arguments), result.ok)
                if verdict == GuardVerdict.BLOCK:
                    return ToolResult(ok=False, error="guard: 重复失败已被拦截")
                if verdict == GuardVerdict.HALT:
                    self._halted = True
                    self._warn("guard: 同一工具反复失败，终止本轮")
                elif verdict == GuardVerdict.WARN:
                    self._warn(f"guard: {call.name} 反复失败，建议换方法")
            return result
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
            if self._halted:
                phase = LoopPhase.STOPPED
                self._warn("guard halt: 同一工具反复失败，已终止本轮")
                await self._emit(
                    EventType.WARNING,
                    {"code": "guard_halt", "message": "同一工具反复失败，已终止本轮", "recoverable": True},
                )
                break

            if self.budget.exhausted and not self.force_continue:
                if self.approvals is None:
                    # 无审批服务：旧的静默停止行为
                    phase = LoopPhase.STOPPED
                    reason = (
                        f"迭代次数达到上限（{self.budget.used_iterations}/{self.budget.max_iterations}）"
                        if self.budget.used_iterations >= self.budget.max_iterations
                        else f"输出 token 预算耗尽（{self.budget.used_tokens}/{self.budget.token_budget}）"
                    )
                    self._warn(f"turn stopped: {reason}")
                    await self._emit(
                        EventType.WARNING,
                        {"code": "budget_exhausted", "message": reason, "recoverable": True},
                    )
                    break
                # 有审批服务：挂起等用户决定「继续/停止」
                decision = await self.approvals.request(
                    "continue",
                    {
                        "used_iterations": self.budget.used_iterations,
                        "max_iterations": self.budget.max_iterations,
                        "used_tokens": self.budget.used_tokens,
                        "token_budget": self.budget.token_budget,
                    },
                )
                if decision.decision == "approved":
                    self.budget.raise_limits(
                        self.continue_batch_iterations, self.continue_batch_tokens
                    )
                    continue
                phase = LoopPhase.STOPPED
                self._warn("预算耗尽，用户选择停止")
                break

            if self._notices:
                messages.append(
                    ChatMessage(role="system", content="\n".join(self._notices))
                )
                self._notices.clear()

            # PLANNING
            completion = await self._plan(messages)
            self.budget.consume_output_tokens(self._tokens_of(completion))
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
        # 输出 token 闸：优先 completion_tokens；缺失时回退 total_tokens
        return int(usage.get("completion_tokens", usage.get("total_tokens", 0)) or 0)
