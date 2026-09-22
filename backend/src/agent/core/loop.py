"""Agent loop state machine.

States: PLANNING -> (TOOL_EXEC -> OBSERVING -> PLANNING) | DONE
Termination: no tool calls requested, or budget exhausted (STOPPED unless
force_continue). Tool failures are isolated per call (WARNING events).
Memory hooks are placeholders until M6/M7.

工具执行走 ToolRegistry 的执行管线（tool/start → pre/execute/post → tool/result
→ tool/end）：本循环在构造时把内部事件转发为 SSE 的 TOOL_START/TOOL_END，
并在 tool/result 监听器里执行 tool_trace 审计；turn 结束时卸载监听器。

本循环**不**发 TURN_START / TURN_END：turn 的生命周期由 core/turn.py 的
TurnManager 单独负责（子 agent、维护任务也复用本循环，它们不是 turn，
以前会把主 turn 的界面状态提前结束掉）。

取消语义：`is_cancelled` 在每个模型调用 / 工具调用 / 迭代边界被检查；
一旦取消，本循环立刻停止，不再发起任何新的模型或工具调用。
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
from agent.core.narrative import parse_narrative
from agent.core.tool_state import CANCELLED, FAILED, SUCCESS, ToolExecutionState
from agent.tools.registry import ToolRegistry
from agent.tools.base import ToolResult

logger = logging.getLogger(__name__)


def _terminal_tool_status(data: dict) -> str:
    """管线结束事件 → 工具终态语义。

    取消是独立语义（`cancelled`），不能因为 `ok=False` 就并进 `failed`。
    """
    if data.get("cancelled"):
        return CANCELLED
    return SUCCESS if data.get("ok") else FAILED


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
    cancelled: bool = False


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
        turn_id: str | None = None,
        trace=None,
        tool_trace=None,
        tool_selector=None,
        max_parallel_tools: int = 4,
        is_cancelled: Callable[[], bool] | None = None,
        tool_state: ToolExecutionState | None = None,
        narrative_sink: Callable[..., Any] | None = None,
        narrative_settler: Callable[..., Any] | None = None,
    ) -> None:
        self.adapter = adapter
        self.registry = registry
        self.bus = bus
        mode = AdapterMode(adapter.mode)
        # token_budget 缺省 = 0 = 不限（见 core/budget.py 的产品决定）。
        # 迭代上限 / guard / provider 自身的上限仍然生效 —— 那是防异常循环，
        # 不是用来控制正常回答长度的。
        self.budget = IterationBudget(
            max_iterations=max_iterations or default_iterations(mode),
            token_budget=token_budget or 0,
        )
        self.force_continue = force_continue
        self.approvals = approvals
        self.guard = guard
        self.continue_batch_iterations = continue_batch_iterations
        self.continue_batch_tokens = continue_batch_tokens
        self.turn_id = turn_id
        self.trace = trace
        self._model_seq = 0
        self._halted = False
        self._warnings: list[str] = []
        self._notices: list[str] = []
        # 统一用量累计（输入 / 输出 / 总量）：供应商差异已经在 Adapter 层消掉
        self._usage_input = 0
        self._usage_output = 0
        self._usage_total = 0
        # 本 loop 派发的工具调用 id；用于过滤 registry 上的跨 loop 事件
        self._dispatched_call_ids: set[str] = set()
        # 每次工具调用的开始时刻：TOOL_END 用它给出耗时（前端卡片显示「1.2s」）。
        # 放在 loop 上而不是 registry 上：registry 是跨 loop 共享的，计时必须按调用归属。
        self._tool_started_at: dict[str, float] = {}
        # 每次工具调用的**系统事实**（终态 + 耗时）：工具卡与叙事摘要共用同一份，
        # 批次结束交给 narrative_settler 写进叙事记录。
        self._tool_facts: dict[str, dict] = {}
        # 工具执行的**权威事实**（active + recent terminal）。主 Turn 由 AppContext 注入
        # 进程级实例（这样「刚结束的 Turn」的工具结果在重连后仍查得到）；
        # 单独构造 loop（子任务 / 测试）时自建一份私有的，行为一致但不外泄。
        self.tool_state = tool_state or ToolExecutionState()
        self.tool_trace = tool_trace
        self.tool_selector = tool_selector
        # 叙事出口（可选）：主 turn 由 AppContext 注入落库 + 广播；子 agent / 维护
        # 循环不注入，因此不会往主对话里写过程说明。
        self.narrative_sink = narrative_sink
        self.narrative_settler = narrative_settler
        self.max_parallel_tools = max(1, max_parallel_tools)
        self.is_cancelled = is_cancelled or (lambda: False)
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
        call_id = data.get("call_id")
        if call_id not in self._dispatched_call_ids:
            return  # 非本 loop 的调用，忽略（跨 loop 隔离）
        import time as _time

        if call_id:
            self._tool_started_at[call_id] = _time.perf_counter()
        # 先写权威状态，再发实时事件：通知丢了也不影响最终状态可恢复。
        self.tool_state.start(self.turn_id, str(call_id or ""), str(data.get("tool") or ""))
        # 呈现（present_call）在这里也给：工具「开始执行」的卡片要有中文标题，
        # 而不是等结束才补上（否则运行中的卡显示的是原始工具名）。
        presentation = None
        tool = self.registry.get(str(data.get("tool") or ""))
        if tool is not None:
            try:
                call_present = tool.present_call(dict(data.get("arguments") or {}))
                if call_present:
                    from agent.tools.display import tool_label

                    presentation = {"title": tool_label(tool.name), "tool": tool.name}
                    presentation.update(call_present)
            except Exception:  # noqa: BLE001 - 呈现失败不能影响执行
                presentation = None
        await self._emit(
            EventType.TOOL_START,
            {
                "tool": data.get("tool"),
                # call_id 是前端「原地更新同一张卡」的匹配键：缺了就会被当成两次调用
                "call_id": call_id,
                "arguments": data.get("arguments", {}),
                "presentation": presentation,
            },
        )

    async def _on_pipeline_end(self, data: dict) -> None:
        call_id = data.get("call_id")
        if call_id not in self._dispatched_call_ids:
            return
        duration_ms = None
        tool_name = str(data.get("tool") or "")
        status = _terminal_tool_status(data)
        if call_id:
            import time as _time

            started = self._tool_started_at.pop(call_id, None)
            if started is not None:
                duration_ms = int((_time.perf_counter() - started) * 1000)
        self.tool_state.finish(
            self.turn_id,
            str(call_id or ""),
            status,
            tool_name=tool_name,
            error=data.get("error"),
        )
        if call_id:
            self._tool_facts[str(call_id)] = {
                "status": status,
                "duration_ms": duration_ms,
                "error": data.get("error"),
            }
        await self._emit(
            EventType.TOOL_END,
            {
                "tool": data.get("tool"),
                "call_id": call_id,
                "ok": data.get("ok"),
                # 结局的语义（success / failed / cancelled）随事件一起给，
                # 前端不必从 ok 反推「取消」还是「失败」。
                "status": status,
                "error": data.get("error"),
                "content_preview": data.get("content_preview", ""),
                "duration_ms": duration_ms,
                "presentation": data.get("presentation"),
            },
        )

    async def _on_pipeline_result(self, data: dict) -> None:
        result = data.get("result")
        if self.tool_trace is None or result is None:
            return
        call = data.get("call")
        if call is None or getattr(call, "id", None) not in self._dispatched_call_ids:
            return
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

    async def _emit_batch_narrative(self, calls) -> str | None:
        """一次工具批次最多输出一条叙事（批内第一条有效叙事胜出）。

        叙事只描述"这一批要做什么"：连续的低价值读取要么不写，要么合并成一句 ——
        这正是"不要求每次工具调用都提示"的落点。
        """
        if self.narrative_sink is None:
            return None
        for call in calls:
            narrative = parse_narrative(getattr(call, "narrative", None))
            if narrative is None:
                continue
            try:
                return await self.narrative_sink(
                    self.turn_id, narrative, call, [c.id for c in calls]
                )
            except Exception:  # noqa: BLE001 - 表达失败不得影响工具执行
                logger.warning("narrative emission failed", exc_info=True)
                return None
        return None

    async def _dispatch_tool_calls(self, calls) -> dict[str, Any]:
        """并发安全工具并行（受 max_parallel_tools 限制），其余串行。

        返回 {call.id: ToolResult}，顺序无关（调用方按原始顺序回填）。
        """
        for c in calls:
            self._dispatched_call_ids.add(c.id)
        # 先说明、再执行：叙事事件必须排在本次工具事件之前。
        narrative_id = await self._emit_batch_narrative(calls)
        from agent.tools.policy import Concurrency, effective_concurrency

        safe_calls = []
        for c in calls:
            tool = self.registry.get(c.name)
            if tool is not None and effective_concurrency(tool) == Concurrency.PARALLEL:
                safe_calls.append(c)
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
        # 批次结束：把系统知道的真实调用结果补写进叙事记录（失败只记日志）。
        if narrative_id and self.narrative_settler is not None:
            try:
                facts = {c.id: dict(self._tool_facts.get(c.id) or {}) for c in calls}
                await self.narrative_settler(narrative_id, results, calls, facts)
            except Exception:  # noqa: BLE001 - 结算失败不影响工具结果
                logger.warning("narrative settle failed", exc_info=True)
        return results

    async def _guarded_execute(self, call) -> Any:
        """在独立 task 中执行，登记到 _active_tool_tasks 以便 cancel()。"""
        import time as _time

        # 取消检查点：已取消就不再启动这次工具调用
        if self.is_cancelled():
            return ToolResult(ok=False, error="turn cancelled")
        task = asyncio.current_task()
        if task is not None:
            self._active_tool_tasks.add(task)
        _t0 = _time.perf_counter()
        policy: str | None = None
        try:
            result = await self.registry.execute(call)
            if self.guard is not None:
                verdict = self.guard.observe(call.name, dict(call.arguments), result.ok)
                if verdict == GuardVerdict.BLOCK:
                    policy = "block"
                    result = ToolResult(ok=False, error="guard: 重复失败已被拦截")
                elif verdict == GuardVerdict.HALT:
                    policy = "halt"
                    self._halted = True
                    self._warn("guard: 同一工具反复失败，终止本轮")
                elif verdict == GuardVerdict.WARN:
                    policy = "warn"
                    self._warn(f"guard: {call.name} 反复失败，建议换方法")
            if self.trace is not None:
                self.trace.tool_run(
                    call_id=call.id,
                    tool=call.name,
                    arguments=dict(call.arguments),
                    ok=result.ok,
                    error=result.error,
                    duration_ms=int((_time.perf_counter() - _t0) * 1000),
                    policy=policy,
                    result=result.content,
                )
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

    def active_tools(self) -> list[dict]:
        """此刻真正在执行中的工具（TOOL_START 到了、TOOL_END 还没到）。

        runtime snapshot 用它回答「现在在跑哪些工具」：客户端在 RESYNC 之后
        把不在这个列表里的「运行中」卡片收口，避免 TOOL_END 丢失后永久转圈。
        """
        return [
            record
            for record in self.tool_state.snapshot(
                active_turn_id=self.turn_id, turn_id=self.turn_id
            )
            if record["status"] == "running"
        ]

    # -- event helpers ----------------------------------------------------

    async def _emit(self, event_type: EventType, data: dict) -> None:
        if self.turn_id:
            data = {**data, "turn_id": self.turn_id}
        await self.bus.publish(make_event(event_type, data))

    def _warn(self, message: str) -> None:
        self._warnings.append(message)
        logger.warning(message)
        if self.trace is not None:
            self.trace.warning("loop", message)

    # -- main entry -------------------------------------------------------

    async def run(self, user_message: str) -> TurnResult:
        try:
            return await self._run(user_message)
        finally:
            self.dispose()

    async def _run(self, user_message: str) -> TurnResult:
        messages: list[ChatMessage] = [ChatMessage(role="user", content=user_message)]
        self._warnings = []

        phase = LoopPhase.PLANNING
        tool_calls_made = 0
        final_content: str | None = None

        while True:
            # 取消检查点：下一次模型调用之前
            if self.is_cancelled():
                phase = LoopPhase.STOPPED
                break

            if self._halted:
                phase = LoopPhase.STOPPED
                self._warn("护栏终止：同一工具反复失败，已终止本轮")
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
                    self._warn(f"本轮提前结束：{reason}")
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
            self._account_usage(completion)
            self.budget.consume_iteration()

            # 取消检查点：模型调用之后（无法物理中断已发出的 HTTP 请求，
            # 但返回结果必须被丢弃，绝不重新激活本 turn）
            if self.is_cancelled():
                phase = LoopPhase.STOPPED
                break

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
            # 取消检查点：不启动新的工具
            if self.is_cancelled():
                phase = LoopPhase.STOPPED
                break
            results = await self._dispatch_tool_calls(completion.tool_calls)
            # 取消检查点：工具返回之后不再进入推理循环
            if self.is_cancelled():
                phase = LoopPhase.STOPPED
                break
            for call in completion.tool_calls:
                tool_calls_made += 1
                result = results[call.id]
                if not result.ok:
                    self._warn(f"tool {call.name} failed: {result.error}")
                messages.append(
                    ChatMessage(role="tool", tool_call_id=call.id, content=result.content)
                )
            phase = LoopPhase.OBSERVING

        cancelled = self.is_cancelled()
        if cancelled:
            phase = LoopPhase.STOPPED
        usage = {
            "iterations": self.budget.used_iterations,
            # 向后兼容字段：`tokens` 一直是「输出 token」（而不是总量）
            "tokens": self.budget.used_tokens,
            "tool_calls": tool_calls_made,
            "input_tokens": self._usage_input,
            "output_tokens": self._usage_output,
            "total_tokens": self._usage_total,
        }
        await self._emit(EventType.USAGE, usage)
        return TurnResult(
            final_content=final_content,
            phase=phase,
            iterations_used=self.budget.used_iterations,
            tokens_used=self.budget.used_tokens,
            tool_calls_made=tool_calls_made,
            warnings=list(self._warnings),
            cancelled=cancelled,
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
        import time as _time

        self._model_seq += 1
        _t0 = _time.perf_counter()
        try:
            completion = await self.adapter.complete(messages, tools)
            if self.trace is not None:
                usage = completion.usage or {}
                self.trace.model_call(
                    seq=self._model_seq,
                    adapter_mode=str(getattr(self.adapter, "mode", "")),
                    model=getattr(self.adapter, "model", None),
                    input_tokens=self._input_tokens_of(completion),
                    output_tokens=self._output_tokens_of(completion),
                    latency_ms=int((_time.perf_counter() - _t0) * 1000),
                    tool_calls=len(completion.tool_calls or []),
                )
            return completion
        except Exception as exc:  # adapter-level failure ends the turn
            if self.trace is not None:
                self.trace.model_call(
                    seq=self._model_seq,
                    adapter_mode=str(getattr(self.adapter, "mode", "")),
                    model=getattr(self.adapter, "model", None),
                    latency_ms=int((_time.perf_counter() - _t0) * 1000),
                    error=f"{type(exc).__name__}: {exc}"[:200],
                )
            self._warn(f"planning failed: {exc}")
            await self._emit(
                EventType.ERROR,
                {"code": "planning_failed", "message": str(exc)[:200], "recoverable": False},
            )
            raise

    def _tokens_of(self, completion: Completion) -> int:
        """该次模型调用的**输出** token 数。

        只认统一语义里的 output_tokens：以前回退到 total_tokens 会把输入也算进去，
        导致 Anthropic（只给 input/output）被过早限流。
        """
        return self._output_tokens_of(completion)

    @staticmethod
    def _input_tokens_of(completion: Completion) -> int:
        usage = completion.usage
        return int(usage.input_tokens) if usage is not None else 0

    @staticmethod
    def _output_tokens_of(completion: Completion) -> int:
        usage = completion.usage
        return int(usage.output_tokens) if usage is not None else 0

    def _account_usage(self, completion: Completion) -> None:
        """累计本轮用量：输出闸与 UI/Trace 统计共用同一份归一化数据。"""
        usage = completion.usage
        if usage is not None:
            self._usage_input += int(usage.input_tokens)
            self._usage_output += int(usage.output_tokens)
            self._usage_total += int(usage.total_tokens)
        self.budget.consume_output_tokens(self._tokens_of(completion))
