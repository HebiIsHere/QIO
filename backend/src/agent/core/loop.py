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

from agent.adapters.base import AdapterMode, BaseAdapter, ChatMessage, Completion, ToolSpec
from agent.api.events import EventType, make_event
from agent.api.bus import EventBus
from agent.core.budget import IterationBudget, default_iterations
from agent.core.guard import GuardVerdict, RunawayGuard
from agent.core.narrative import parse_narrative
from agent.core import tool_feedback
from agent.core.tool_state import CANCELLED, FAILED, SUCCESS, ToolExecutionState
from agent.core.turn_facts import TurnFacts
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
    # 没来得及读的系统通知（见 AgentLoop.push_notice）：宣布的时候还有下一轮
    # planning 可读，等这一轮结束时却已经没有了。它们必须由上层变成自己的一轮，
    # 否则子任务结果会无声消失。
    unread_notices: list[str] = field(default_factory=list)


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
        usage_sink: Callable[[int, int], None] | None = None,
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
        # 为什么停下来的人话说明：护栏终止 / 预算停止时记下来，收尾时若一个字
        # 都没产生就把它当回答写出去（空回答等于静默失败）。
        self._stop_note: str | None = None
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
        # 工具调用历史：本轮内第几次调用（历史卡片按它排序）+ 待结算的调用/结果
        self._call_seq: dict[str, int] = {}
        self._call_seq_next = 0
        self._pending_tool_io: dict[str, dict] = {}
        # 本轮的「后端事实」台账（见 core/turn_facts.py）：工具终态 + 开发任务状态。
        # 每轮在 `_run` 里新建，收尾时用它决定要不要给最终答复补事实说明。
        self.turn_facts = TurnFacts()
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
        # 用量归因：每次模型调用把 (进, 出) 报给调用方（由它记到对应凭据上）。
        # 放在每次调用之后而不是整轮结束 —— 中途失败/取消的那部分也已经计费。
        self.usage_sink = usage_sink
        self.max_parallel_tools = max(1, max_parallel_tools)
        self.is_cancelled = is_cancelled or (lambda: False)
        self._active_tool_tasks: set[asyncio.Task] = set()
        # 「停止」要能掐掉正在等待的模型请求，而不是等它自然返回（见 cancel/_await_completion）。
        self._cancel_event = asyncio.Event()
        self._request_aborted = False
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
        pending = self._pending_tool_io.pop(str(call_id or ""), None)
        # 结果事实（类别 / 可重试）随 TOOL_END 一起给：界面不必从 ok 反推失败类型。
        result_facts = (
            tool_feedback.facts(pending["result"]) if pending and pending.get("result") else {}
        )
        record_id = self._record_tool_call(tool_name, data, status, duration_ms, pending)
        if call_id:
            # 合并写入：`record_id` 由上面刚写下的历史记录给出，整条覆盖会把它丢掉
            self._tool_facts[str(call_id)] = {
                **(self._tool_facts.get(str(call_id)) or {}),
                "status": status,
                "duration_ms": duration_ms,
                "error": data.get("error"),
                "record_id": record_id,
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
                "category": result_facts.get("category"),
                "category_label": result_facts.get("category_label"),
                "recoverable": result_facts.get("recoverable"),
                "content_preview": data.get("content_preview", ""),
                "duration_ms": duration_ms,
                "presentation": data.get("presentation"),
                # 工具调用历史的记录 id：实时卡片靠它取全文（与历史卡片同一条路径）
                "record_id": record_id,
            },
        )

    async def _on_pipeline_result(self, data: dict) -> None:
        result = data.get("result")
        if result is None:
            return
        call = data.get("call")
        if call is None or getattr(call, "id", None) not in self._dispatched_call_ids:
            return
        # 终态（含「取消」）在 tool/end 才权威，而完整输出只有这里拿得到 → 先暂存
        self._pending_tool_io[str(call.id)] = {"call": call, "result": result}

    def _record_tool_call(self, tool_name: str, data: dict, status: str,
                          duration_ms: int | None, pending: dict | None) -> str | None:
        """把这次调用交给审计 / 历史回调；返回记录 id（没有回调或失败则 None）。

        审计与历史都不得影响工具结果，也不得让这一轮失败。
        """
        if self.tool_trace is None or pending is None:
            return None
        call = pending["call"]
        result = pending["result"]
        try:
            record_id = self.tool_trace({
                "tool_name": tool_name,
                "arguments": getattr(call, "arguments", {}),
                "ok": result.ok,
                "result": result.content,
                # 失败原因也要进轨迹：只落输出正文时，失败的调用落下来是空白
                "error": result.error,
                "call_id": str(data.get("call_id") or ""),
                "turn_id": self.turn_id,
                "seq": self._call_seq.get(str(data.get("call_id") or ""), 0),
                "duration_ms": duration_ms,
                "status": status,
            })
            return str(record_id) if record_id else None
        except Exception:  # noqa: BLE001 - tracing must not break the loop
            logger.warning("tool trace failed for %s", tool_name, exc_info=True)
            return None

    def _record_turn_facts(self, call, result: ToolResult) -> None:
        """把一次调用的终态与工具上报的事实记进本轮台账（只记账，不改执行）。

        见 core/turn_facts.py：台账是「最终答复事实校正」的唯一依据，
        它只认后端已经知道的事实（工具终态 + 开发工具上报的任务状态）。
        """
        self.turn_facts.record_tool(
            call_id=str(getattr(call, "id", "") or ""),
            tool_name=str(getattr(call, "name", "") or ""),
            ok=bool(result.ok),
            status=tool_feedback.status_of(result),
            category=result.category,
            error=result.error,
        )
        self.turn_facts.record_facts(result.facts)

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
            self._call_seq_next += 1
            self._call_seq[c.id] = self._call_seq_next
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
                    # 阈值到了不再直接终止本轮：先问用户要不要继续（2026-09-22 起）。
                    policy = "halt"
                    failures = self.guard.failures_for(call.name)
                    if self.approvals is None:
                        # 没有审批通道（子任务 / 单元测试）：退回旧的终止行为，不静默继续
                        self._halted = True
                        self._stop_note = self._halt_stop_note(call.name, failures, result.error)
                        self._warn(
                            f"guard: {call.name} 累计失败 {failures} 次，已终止本轮（无审批通道）"
                        )
                    else:
                        decision = await self.approvals.request(
                            "continue",
                            {
                                "reason": "tool_failures",
                                "tool": call.name,
                                "failures": failures,
                                # 复用预算那条「继续/停止」的通道：把当前预算一并给出，
                                # 界面不必为"为什么问"单独做一套 UI。
                                "used_iterations": self.budget.used_iterations,
                                "max_iterations": self.budget.max_iterations,
                                "used_tokens": self.budget.used_tokens,
                                "token_budget": self.budget.token_budget,
                            },
                        )
                        if decision.decision == "approved":
                            self.guard.reset_tool(call.name)
                            self._warn(
                                f"guard: {call.name} 累计失败 {failures} 次，用户选择继续（计数已清零）"
                            )
                        else:
                            self._halted = True
                            self._stop_note = self._halt_stop_note(call.name, failures, result.error)
                            self._warn(
                                f"guard: {call.name} 累计失败 {failures} 次，用户选择停止本轮"
                            )
                elif verdict == GuardVerdict.WARN:
                    policy = "warn"
                    self._warn(
                        f"guard: {call.name} 已累计失败 {self.guard.failures_for(call.name)} 次，建议换方法"
                    )
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
        """取消本轮：在途工具调用 + 正在等待的模型请求。

        工具那条走 registry（它会把 CancelledError 转成 aborted）；
        模型请求那条由 `_await_completion` 的竞速负责 —— 只设标志位的话，
        请求还是会跑完，用户按了停止却要一直等。
        """
        self._cancel_event.set()
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

    @staticmethod
    def _halt_stop_note(tool: str, failures: int, error: str | None) -> str:
        """护栏终止时的收尾文本：哪个工具、失败几次、最后一次为什么失败。"""
        return (
            f"本轮没有产生回答：工具 {tool} 连续失败 {failures} 次后已停止。"
            f"最后一次失败原因：{error or '（没有更多说明）'}。"
        )

    # -- main entry -------------------------------------------------------

    async def run(self, user_message: str) -> TurnResult:
        try:
            return await self._run(user_message)
        finally:
            self.dispose()

    async def _run(self, user_message: str) -> TurnResult:
        messages: list[ChatMessage] = [ChatMessage(role="user", content=user_message)]
        self._warnings = []
        # 每轮一份新台账：上一轮的失败不能算到这一轮头上。
        self.turn_facts = TurnFacts()

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
                reason = (
                    f"迭代次数达到上限（{self.budget.used_iterations}/{self.budget.max_iterations}）"
                    if self.budget.used_iterations >= self.budget.max_iterations
                    else f"输出 token 预算耗尽（{self.budget.used_tokens}/{self.budget.token_budget}）"
                )
                if self.approvals is None:
                    # 无审批服务：旧的静默停止行为
                    phase = LoopPhase.STOPPED
                    self._stop_note = f"本轮没有产生回答：{reason}，已停止。"
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
                self._stop_note = f"本轮没有产生回答：{reason}，按你的选择停下来了。"
                self._warn("预算耗尽，用户选择停止")
                break

            if self._notices:
                messages.append(
                    ChatMessage(role="system", content="\n".join(self._notices))
                )
                self._notices.clear()

            # PLANNING
            completion = await self._plan(messages)
            if completion is None:
                # 这一轮在等待模型时被用户停掉：请求已经中断，没有结果可用。
                phase = LoopPhase.STOPPED
                break
            self._account_usage(completion)
            self.budget.consume_iteration()

            # 取消检查点：模型调用之后（无法物理中断已发出的 HTTP 请求，
            # 但返回结果必须被丢弃，绝不重新激活本 turn）
            if self.is_cancelled():
                phase = LoopPhase.STOPPED
                break

            # native 模式：模型在工具调用前先说话时，把内容作为 interim 事件推给前端
            # 但这一批工具已经带了叙事（`_qio`）时不再推 interim：同一阶段只保留一种
            # 过程表达，否则用户会看到「◈ 过程」气泡和叙事行各说一遍（2026-09-22 合并规则）。
            batch_has_narrative = any(
                parse_narrative(getattr(c, "narrative", None)) is not None
                for c in (completion.tool_calls or [])
            )
            if (
                self.adapter.mode == AdapterMode.NATIVE
                and completion.tool_calls
                and completion.message.content
                and completion.message.content.strip()
                and not batch_has_narrative
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
                # 台账记的是**权威事实**：这一次调用最终是成功还是失败，以及工具
                # 上报的「操作之后」的开发任务状态（见 core/turn_facts.py）。
                self._record_turn_facts(call, result)
                messages.append(
                    # 统一反馈：失败也要把类别/原因/是否可重试交给模型，
                    # 不能只回 content —— 失败且 content 为空时模型会收到空正文。
                    ChatMessage(
                        role="tool",
                        tool_call_id=call.id,
                        content=tool_feedback.message(
                            result, tool_name=call.name, call_id=call.id
                        ),
                    )
                )
            phase = LoopPhase.OBSERVING

        cancelled = self.is_cancelled()
        if self._request_aborted:
            # 请求被用户中断：这一轮的语义就是「被取消」，不能落成「模型没说话」。
            cancelled = True
        if cancelled:
            phase = LoopPhase.STOPPED
        if not cancelled and not (final_content or "").strip():
            # 静默失败收口：护栏终止 / 预算停止 / 模型什么都没说，都必须留下人话。
            # 取消是用户自己的动作，界面已有「已停止」状态行，这里不补文本。
            final_content = self._stop_note or "本轮没有产生回答，也没有给出原因。"
        if not cancelled:
            # 后端事实校正：本轮存在没有通过验证的失败时，在答复末尾补一段事实说明。
            # 它不改写、不删除模型写过的字；模型正文照原样留在前面。
            note = self.turn_facts.annotation()
            if note:
                final_content = f"{final_content or ''}\n\n{note}"
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
        # 迟到的系统通知：这一轮已经不会再 planning 了，留在手上的必须交还给上层
        # （否则「子任务完成了」这句话会被静默丢掉，用户永远等不到结果）。
        unread_notices = list(self._notices)
        self._notices.clear()
        return TurnResult(
            final_content=final_content,
            phase=phase,
            iterations_used=self.budget.used_iterations,
            tokens_used=self.budget.used_tokens,
            tool_calls_made=tool_calls_made,
            warnings=list(self._warnings),
            cancelled=cancelled,
            unread_notices=unread_notices,
        )

    # -- steps ------------------------------------------------------------

    async def _plan(self, messages: list[ChatMessage]) -> Completion | None:
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
            completion = await self._await_completion(messages, tools)
            if completion is None:
                # 被用户取消：这次调用没有结果，也不再记一条「假成功」的 trace
                return None
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

    async def _await_completion(
        self, messages: list[ChatMessage], tools: list[ToolSpec]
    ) -> Completion | None:
        """跑一次模型调用；被用户取消时**中断这次请求**并返回 None。

        以前取消只是一个检查点：请求已经发出去，就只能等它回来再把结果丢掉 ——
        provider 慢的时候要白白等几十秒，而 turn 队列是单飞的，排在后面的消息
        也跟着一起等。这里把请求放进自己的 task，与取消事件竞速；取消时立刻
        取消它，底层 HTTP 连接随之中断。

        诚实边界：断开的是**客户端的等待**，服务端是否立刻停止生成由供应商决定；
        这条改动保证的是「不再占用等待时间、不再堵住下一条消息」。
        """
        if self._cancel_event.is_set():
            return None
        request = asyncio.ensure_future(self.adapter.complete(messages, tools))
        waiter = asyncio.ensure_future(self._cancel_event.wait())
        try:
            await asyncio.wait({request, waiter}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            waiter.cancel()
        if request.done() and not request.cancelled():
            # 正常返回（异常由 _plan 的 except 分支如实处理）
            return request.result()
        request.cancel()
        try:
            await request
        except (asyncio.CancelledError, Exception):  # noqa: BLE001 - 结果已被取消，丢弃
            pass
        self._request_aborted = True
        return None

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
        if self.usage_sink is not None:
            # 归因给「这把钥匙」：进 / 出分开报，由调用方决定记到哪条凭据上。
            input_tokens = self._input_tokens_of(completion)
            output_tokens = self._output_tokens_of(completion)
            if input_tokens or output_tokens:
                try:
                    self.usage_sink(input_tokens, output_tokens)
                except Exception:  # noqa: BLE001 - 记账失败不得打断回答
                    logger.warning("usage sink failed", exc_info=True)
