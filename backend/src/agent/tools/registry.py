"""Tool registry: registration + an internal event-driven execution pipeline.

每次调用沿管线执行：
    tool/start (emit) → tool/pre-execute (waterfall，可拒/替换输入) →
    tool/execute (waterfall，terminal = 默认执行器) →
    tool/post-execute (waterfall，可处理结果) →
    tool/result (emit，观察/审计) → tool/end (emit)

SSE 事件（TOOL_START/TOOL_END）与审计由 register_policy(...) 注册的监听器
转发（见 AgentLoop / AppContext）。错误按调用隔离，绝不向上抛出。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable

from agent.adapters.base import ToolCall, ToolSpec
from agent.tools.base import Tool, ToolResult

logger = logging.getLogger(__name__)

EVENT_START = "tool/start"
EVENT_PRE_EXECUTE = "tool/pre-execute"
EVENT_EXECUTE = "tool/execute"
EVENT_POST_EXECUTE = "tool/post-execute"
EVENT_RESULT = "tool/result"
EVENT_END = "tool/end"


class ToolRegistry:
    def __init__(self, approvals=None, internal_bus: Any = None) -> None:
        # 惰性导入：agent.core.events_bus 会触发 agent.core 包初始化（含 loop），
        # 顶层导入会造成 registry ↔ core 循环。
        from agent.core.events_bus import InternalEventBus

        self._tools: dict[str, Tool] = {}
        self.events = internal_bus or InternalEventBus()
        self._approvals = approvals
        # 默认执行器是 execute 管线的 terminal；策略经 register_policy 插到它之前。
        self._executor = self._default_execute
        self.events.on(EVENT_EXECUTE, self._executor)
        if approvals is not None:
            self.events.on(EVENT_PRE_EXECUTE, self._approval_policy)

    # -- registration ------------------------------------------------------

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"tool already registered: {tool.name}")
        self._tools[tool.name] = tool

    def unregister(self, name: str) -> None:
        self._tools.pop(name, None)

    def register_policy(self, event: str, handler: Callable[..., Any]) -> Callable[[], None]:
        """注册管线策略；返回可逆 disposer。

        tool/execute 的策略会插到默认执行器之前（可包装真实执行）；
        其余事件按注册顺序追加。
        """
        if event == EVENT_EXECUTE:
            return self.events.on(event, handler, before=self._executor)
        return self.events.on(event, handler)

    # -- introspection ------------------------------------------------------

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def specs(self) -> list[ToolSpec]:
        return [
            ToolSpec(
                name=t.name,
                description=t.description,
                parameters=t.parameters,
            )
            for t in self._tools.values()
        ]

    # -- pipeline -----------------------------------------------------------

    async def execute(self, call: ToolCall) -> ToolResult:
        """Run one tool call through the pipeline; never raises."""
        tool = self._tools.get(call.name)
        if tool is None:
            result = ToolResult(
                ok=False,
                error=f"unknown tool '{call.name}' (registered: {sorted(self._tools)})",
            )
            await self._finish(call, result)
            return result

        ctx: dict[str, Any] = {
            "tool": tool,
            "arguments": dict(call.arguments),
            "call": call,
        }
        await self.events.emit(EVENT_START, {"tool": call.name, "arguments": call.arguments})

        outcome = await self.events.waterfall(EVENT_PRE_EXECUTE, ctx)
        if isinstance(outcome, ToolResult):
            result = outcome
        else:
            ctx = outcome if isinstance(outcome, dict) else ctx
            result = await self.events.waterfall(EVENT_EXECUTE, ctx)
            if isinstance(result, ToolResult):
                result = await self.events.waterfall(EVENT_POST_EXECUTE, result)
            else:
                result = ToolResult(
                    ok=False,
                    error=f"tool/execute produced unexpected value: {type(result).__name__}",
                )
        await self._finish(call, result)
        return result

    async def _finish(self, call: ToolCall, result: ToolResult) -> None:
        await self.events.emit(EVENT_RESULT, {"tool": call.name, "call": call, "result": result})
        await self.events.emit(
            EVENT_END,
            {
                "tool": call.name,
                "ok": result.ok,
                "error": result.error,
                "content_preview": result.content[:200],
            },
        )

    # -- built-in policies --------------------------------------------------

    async def _approval_policy(self, ctx: dict, *, next: Callable[..., Any]) -> Any:
        tool = ctx.get("tool")
        if tool is None or not getattr(tool, "requires_approval", False) or self._approvals is None:
            return await next(ctx)
        decision = await self._approvals.request(
            "tool_execution",
            {"tool": tool.name, "arguments": ctx.get("arguments", {})},
        )
        if decision.decision == "approved":
            return await next(ctx)
        reason = "rejected by user" if decision.decision == "rejected" else "approval timed out"
        return ToolResult(ok=False, error=f"tool '{tool.name}' not executed ({reason})")

    async def _default_execute(self, ctx: dict, *, next: Callable[..., Any]) -> ToolResult:
        tool = ctx["tool"]
        arguments = ctx["arguments"]
        try:
            coro = tool.run(**arguments)
            if tool.timeout_ms:
                return await asyncio.wait_for(coro, timeout=tool.timeout_ms / 1000.0)
            return await coro
        except asyncio.TimeoutError:
            logger.warning("tool %s timed out after %sms", tool.name, tool.timeout_ms)
            return ToolResult(
                ok=False,
                error=f"tool '{tool.name}' timed out after {tool.timeout_ms}ms",
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - isolation boundary
            logger.warning("tool %s failed: %s", tool.name, exc)
            return ToolResult(ok=False, error=f"{type(exc).__name__}: {exc}")
