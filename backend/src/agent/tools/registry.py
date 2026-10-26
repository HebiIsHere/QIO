"""Tool registry: registration + an internal event-driven execution pipeline.

每次调用沿管线执行：
    tool/start (emit) → tool/pre-execute (waterfall，可拒/替换输入) →
    tool/execute (waterfall，terminal = 默认执行器) →
    tool/post-execute (waterfall，可处理结果) →
    tool/result (emit，观察/审计) → tool/end (emit)

执行后按 tool.output_schema 轻量校验，失败返回失败结果；tool/end 事件携带
presentation（present_call/present_result 的合并结果）。SSE 事件与审计由
register_policy(...) 注册的监听器转发（见 AgentLoop / AppContext）。
"""

from __future__ import annotations

import asyncio
import logging
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any, Callable

from agent.adapters.base import ToolCall, ToolSpec
from agent.tools.approval_results import (
    begin_collection,
    reset_collection,
    settle as settle_legacy_approvals,
)
from agent.tools.base import (
    TOOL_RESULT_PROTOCOL_VERSION,
    Tool,
    ToolResult,
    ToolStatus,
)

# 这里**不能**在模块级 `from agent.core.narrative import ...`：那会让
# tools → lifecycle → registry → core → core.loop → tools.registry 围成一个圈，
# 先导入 agent.tools.* 的调用方（比如单独跑某个测试文件）直接 ImportError。
# 运行期用到的名字在各函数里局部导入；`Narrative` 只用于标注，而模块开了
# `from __future__ import annotations`，标注不求值，所以放在 TYPE_CHECKING 下即可。
if TYPE_CHECKING:
    from agent.core.narrative import Narrative

logger = logging.getLogger(__name__)

EVENT_START = "tool/start"
EVENT_PRE_EXECUTE = "tool/pre-execute"
EVENT_EXECUTE = "tool/execute"
EVENT_POST_EXECUTE = "tool/post-execute"
EVENT_RESULT = "tool/result"
EVENT_END = "tool/end"
EVENT_UNREGISTERED = "tool/unregistered"

# 当前正在执行的工具调用所携带的叙事（见 core/narrative.py）。
# 用途只有一个：工具内部发起审批时（tool_execution / computer / create_topic），
# ApprovalService 可以取到模型写的 explanation。并行调用各自 task 隔离，不会串味。
_current_narrative: ContextVar[Narrative | None] = ContextVar(
    "qio_current_narrative", default=None
)

#: 已批准操作的延迟执行标记：tool.invoke 执行器重跑管线时不再重复弹审批
#: （真实执行前的模式/对象重核对已由审批执行器完成，见 tools/approval.py）。
_approved_execution: ContextVar[str | None] = ContextVar(
    "qio_approved_execution", default=None
)


def current_narrative() -> Narrative | None:
    """当前工具调用携带的叙事（没有则 None）。"""
    return _current_narrative.get()


def current_approved_execution() -> str | None:
    """当前是否处在「用户已批准」的重跑管线（返回 approval_id；否则 None）。

    工具内部自带的授权入口（如自建工具每次调用重新判定）据此跳过**重复**审批；
    审批执行器在领取前已完成模式/对象重核对，工具自己的只读兜底仍然生效。
    """
    return _approved_execution.get()


def _exception_text(exc: BaseException) -> str:
    """把异常写成「类型 + 说明」；说明为空时给出可查的替代信息。

    以前是 `f"{type(exc).__name__}: {exc}"`：底层异常没有文本时只剩
    「ConnectError: 」这半句（真实事故里出现 22 次），模型和用户都读不出下一步。
    """
    detail = str(exc).strip()
    if not detail:
        cause = exc.__cause__ or exc.__context__
        detail = str(cause).strip() if cause is not None else ""
    if not detail:
        detail = "（底层错误没有给出说明）"
    return f"{type(exc).__name__}: {detail}"


class ToolRegistry:
    def __init__(self, approvals=None, internal_bus: Any = None, services: Any = None) -> None:
        # 惰性导入：agent.core.events_bus 会触发 agent.core 包初始化（含 loop），
        # 顶层导入会造成 registry ↔ core 循环。
        from agent.core.events_bus import InternalEventBus

        self._tools: dict[str, Tool] = {}
        self.events = internal_bus or InternalEventBus()
        self._approvals = approvals
        self.services = services
        # 默认执行器是 execute 管线的 terminal；策略经 register_policy 插到它之前。
        self._executor = self._default_execute
        self.events.on(EVENT_EXECUTE, self._executor)
        if approvals is not None:
            self.events.on(EVENT_PRE_EXECUTE, self._approval_policy)

    # -- registration ------------------------------------------------------

    def register(self, tool: Tool) -> Callable[[], None]:
        """注册工具；返回可逆 disposer（幂等：重复调用只生效一次）。"""
        if tool.name in self._tools:
            raise ValueError(f"tool already registered: {tool.name}")
        self._tools[tool.name] = tool
        if self.services is not None:
            self.services.attach(tool)

        def dispose() -> None:
            self.unregister(tool.name)

        return dispose

    def unregister(self, name: str) -> None:
        """从注册表移除工具，并广播 tool/unregistered（尽力而为）。"""
        tool = self._tools.pop(name, None)
        if tool is not None:
            self._schedule_emit(EVENT_UNREGISTERED, {"tool": name})

    def _schedule_emit(self, event: str, data: dict) -> None:
        """disposer 是同步回调；在事件循环存在时调度异步广播。"""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(self.events.emit(event, data))

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
                # 每个工具都声明可选的叙事信封：模型据此知道自己可以说明意图，
                # 而 adapter 会在解析时把它剥离，工具本身永远看不到这个字段。
                parameters=self._with_narrative_field(self._parameters_for(t)),
            )
            for t in self._tools.values()
        ]

    @staticmethod
    def _parameters_for(tool: Tool) -> dict[str, Any]:
        """展示给模型的参数 schema：类型模型存在时与本地校验同源。

        arg_model（pydantic）存在 → json_schema_for 生成 JSON Schema
        （enum / additionalProperties / required 由类型真正执行）；
        否则沿用工具自己写的 parameters（旧工具兼容）。
        """
        model = getattr(tool, "arg_model", None)
        if model is not None:
            from agent.tools.contracts import json_schema_for

            return json_schema_for(model)
        return tool.parameters

    @staticmethod
    def _with_narrative_field(parameters: dict[str, Any]) -> dict[str, Any]:
        """在工具参数 schema 的副本里补一个可选的 `_qio` 属性。

        只改副本，不动工具自己的 parameters；不加入 required；
        已有同名属性时不覆盖（保持工具自身定义优先）。
        """
        from agent import prompts
        from agent.core.narrative import NARRATIVE_KEY, NARRATIVE_KINDS

        spec = dict(parameters or {})
        spec.setdefault("type", "object")
        properties = dict(spec.get("properties") or {})
        descriptions = prompts.NARRATIVE_ARGUMENT_DESCRIPTIONS
        properties.setdefault(
            NARRATIVE_KEY,
            {
                "type": "object",
                "description": prompts.narrative_field_description(),
                "properties": {
                    "kind": {"type": "string", "enum": list(NARRATIVE_KINDS)},
                    "text": {"type": "string", "description": descriptions["text"]},
                    "explanation": {
                        "type": "string",
                        "description": descriptions["explanation"],
                    },
                },
                "additionalProperties": False,
            },
        )
        spec["properties"] = properties
        return spec

    # -- pipeline -----------------------------------------------------------

    async def execute(
        self, call: ToolCall, *, approval_id: str | None = None
    ) -> ToolResult:
        """Run one tool call through the pipeline; never raises.

        外部取消（Task.cancel）会把 CancelledError 转成 aborted 失败结果，
        保证 per-call 隔离，并让 TOOL_END(ok=false, aborted) 照常发出。
        approval_id 非空表示这是**用户已批准**的延迟执行：不再重复弹审批，
        但仍走完整管线（参数校验 / 执行 / 输出校验 / 事件 / 审计）。
        """
        # 叙事只用于「模型想怎么表达」，不参与任何执行/风险/权限判断；
        # 在这里挂到 ContextVar 上，工具内部发起的审批可以取到 explanation。
        from agent.core.narrative import parse_narrative

        token = _current_narrative.set(parse_narrative(getattr(call, "narrative", None)))
        approved_token = _approved_execution.set(approval_id)
        # 旧 request() 审批的执行结果闭环：本次调用内被批准的 legacy 审批在这里收口
        # （统一写 history + 发 APPROVAL_RESULT，见 tools/approval_results.py）。
        collect_token = begin_collection()
        try:
            result = await self._execute_call(call)
            await settle_legacy_approvals(self._approvals, result)
            return result
        finally:
            reset_collection(collect_token)
            _approved_execution.reset(approved_token)
            _current_narrative.reset(token)

    async def _execute_call(self, call: ToolCall) -> ToolResult:
        tool = self._tools.get(call.name)
        # 阶段四（契约 §14.2）：_qio_subtask 是 subagent 工具的保留控制字段，
        # 只允许 subagent 工具消费。其它工具收到它 = 模型把控制信息混进业务参数，
        # 直接拒绝（不静默忽略、不静默转发）。字段名唯一来源
        # agent/tools/subagent_tool.py 的 SUBTASK_CONTROL_KEY。
        if (
            tool is not None
            and "_qio_subtask" in dict(call.arguments)
            and not getattr(tool, "subtask_control_allowed", False)
        ):
            result = ToolResult(
                ok=False,
                status="failed",
                code="unexpected_control_field",
                error="_qio_subtask 只允许 subagent 工具使用；本次调用已拒绝。",
            )
            await self._finish(call, tool, result)
            return result
        if tool is None:
            result = ToolResult(
                ok=False,
                error=f"未注册的工具「{call.name}」（已注册：{sorted(self._tools)}）",
            )
            await self._finish(call, None, result)
            return result

        ctx: dict[str, Any] = {
            "tool": tool,
            "arguments": dict(call.arguments),
            "call": call,
        }
        # Phase 1 类型化参数：执行前用同源 pydantic 模型校验（enum / oneOf /
        # additionalProperties 真正执行）。失败是**结构化结果**，不是中文错误句：
        # status=failed / code=invalid_arguments / data.errors 进入工具反馈。
        if getattr(tool, "arg_model", None) is not None:
            from agent.tools.contracts import validate_model

            validated, errors = validate_model(tool.arg_model, dict(call.arguments))
            if errors or validated is None:
                result = ToolResult(
                    ok=False,
                    status=ToolStatus.FAILED.value,
                    code="invalid_arguments",
                    protocol_version=TOOL_RESULT_PROTOCOL_VERSION,
                    error="工具参数校验失败：" + "；".join(errors),
                    data={
                        "tool": call.name,
                        "errors": list(errors),
                        "received_keys": sorted(dict(call.arguments).keys()),
                    },
                )
                await self._finish(call, tool, result)
                return result
            # 校验通过后按类型模型归一化后的参数交给管线（含默认值 / 枚举约束），
            # 后续工具与审批看到的是同一份已校验数据。
            ctx["arguments"] = dict(validated.model_dump(mode="python"))
        try:
            await self.events.emit(
                EVENT_START,
                {"tool": call.name, "arguments": call.arguments, "call_id": call.id},
            )

            outcome = await self.events.waterfall(EVENT_PRE_EXECUTE, ctx)
            if isinstance(outcome, ToolResult):
                result = outcome
            else:
                ctx = outcome if isinstance(outcome, dict) else ctx
                result = await self.events.waterfall(EVENT_EXECUTE, ctx)
                if isinstance(result, ToolResult):
                    result = await self.events.waterfall(EVENT_POST_EXECUTE, result)
                    result = self._validate_output(tool, result)
                else:
                    result = ToolResult(
                        ok=False,
                        error=f"工具执行返回了非预期类型：{type(result).__name__}",
                    )
        except asyncio.CancelledError:
            result = ToolResult(ok=False, error=f"工具「{call.name}」已被取消")
            cancelled = True
        else:
            cancelled = False
        await self._finish(call, tool, result, cancelled=cancelled)
        return result

    async def _finish(
        self,
        call: ToolCall,
        tool: Tool | None,
        result: ToolResult,
        *,
        cancelled: bool = False,
    ) -> None:
        """广播一次调用的结果。

        `cancelled` 是显式语义而不是「失败的一种」：下层的权威状态
        （见 `core/tool_state.py`）据此区分 cancelled 与 failed，
        界面上「用户取消」不能被说成「执行失败」。
        """
        await self.events.emit(EVENT_RESULT, {"tool": call.name, "call": call, "result": result})
        await self.events.emit(
            EVENT_END,
            {
                "tool": call.name,
                "call_id": call.id,
                "ok": result.ok,
                "error": result.error,
                "content_preview": result.content[:200],
                "presentation": self._present(tool, call, result),
                "cancelled": cancelled,
            },
        )

    # -- validation & presentation ------------------------------------------

    def _validate_output(self, tool: Tool, result: ToolResult) -> ToolResult:
        schema = getattr(tool, "output_schema", None)
        if not schema:
            return result
        if not result.ok:
            return result
        try:
            value = result.content
            parsed = value
            if isinstance(value, str):
                try:
                    import json

                    parsed = json.loads(value)
                except (ValueError, TypeError):
                    parsed = value
            from agent.tools.schema import validate

            errors = validate(parsed, schema)
        except Exception as exc:  # noqa: BLE001 - validation must never break the loop
            logger.warning("output schema validation failed for %s: %s", tool.name, exc)
            return ToolResult(
                ok=False,
                error=f"工具输出不符合 schema：{type(exc).__name__}: {exc}",
            )
        if errors:
            return ToolResult(ok=False, error="工具输出不符合 schema：" + "；".join(errors))
        return result

    def _present(self, tool: Tool | None, call: ToolCall, result: ToolResult) -> dict[str, Any] | None:
        if tool is None:
            return None
        from agent.tools.display import tool_label

        presentation: dict[str, Any] = {}
        try:
            call_present = tool.present_call(dict(call.arguments))
            if call_present:
                presentation.update(call_present)
            result_present = tool.present_result(result)
            if result_present:
                presentation.update(result_present)
        except Exception:  # noqa: BLE001 - presentation must never break execution
            logger.warning("tool presentation failed for %s", tool.name, exc_info=True)
            return None
        # 界面文案统一中文：标题给中文展示名；原始工具名作为附加字段（前端只在
        # 悬停 title 里用）。状态不在这里补默认值：卡片本身已有 ✓/✕ 表达成败，
        # 再挂一个「成功」徽标只是噪声；需要语义状态的工具可以自己设置（中文）。
        presentation.setdefault("title", tool_label(tool.name))
        presentation.setdefault("tool", tool.name)
        return presentation

    # -- built-in policies --------------------------------------------------

    async def _approval_policy(self, ctx: dict, *, next: Callable[..., Any]) -> Any:
        tool = ctx.get("tool")
        if tool is None or not getattr(tool, "requires_approval", False) or self._approvals is None:
            return await next(ctx)
        # 已批准的延迟执行：审批已在用户点「允许」时完成，不再重复弹。
        if _approved_execution.get():
            return await next(ctx)
        engine = getattr(self._approvals, "engine", None)
        store = getattr(self._approvals, "store", None)
        if engine is not None and store is not None:
            return await self._deferred_approval(ctx, tool)
        # 旧装配（没有统一授权引擎 / 没有持久化审批）：保持原同步审批行为。
        # 审批内容要能被普通用户看懂（spec 第 66~70 条）：把这次具体调用翻译成
        # 「想做什么 / 会访问什么 / 影响 / 一次性还是长期」，而不是把工具名和
        # 参数原样丢给用户。原始 tool/arguments 保留给「高级详情」。
        from agent.tools.approval_present import describe_tool_call

        arguments = ctx.get("arguments", {}) or {}
        payload: dict[str, Any] = {"tool": tool.name, "arguments": arguments}
        try:
            payload.update(describe_tool_call(tool.name, dict(arguments)))
        except Exception:  # noqa: BLE001 - 描述失败不能让审批本身失效
            logger.warning("approval description failed for %s", tool.name, exc_info=True)
        decision = await self._approvals.request(
            "tool_execution",
            payload,
        )
        if decision.decision == "approved":
            return await next(ctx)
        from agent.tools.approval import refusal_reason

        return ToolResult(
            ok=False,
            error=f"工具「{tool.name}」未执行（{refusal_reason(decision.decision)}）",
        )

    async def _deferred_approval(self, ctx: dict, tool) -> Any:
        """统一授权 + 非阻塞审批（契约 §2.1/§5.1）。

        ALLOW：直接执行；DENY：中文原因短路；APPROVE：持久化待执行操作并立即
        返回 pending 结果 —— 不在工具调用栈里等用户点按钮（TurnManager 是单飞，
        同步等待会占住唯一 active turn）。批准后由审批执行器重跑本管线。
        """
        from agent.security.effects import Verdict
        from agent.security.operations import PendingOperation

        arguments = ctx.get("arguments", {}) or {}
        call = ctx.get("call")
        declared = None
        effects_fn = getattr(tool, "effects", None)
        if callable(effects_fn):
            try:
                declared = list(effects_fn(dict(arguments)))
            except Exception:  # noqa: BLE001 - 效果声明失败按未声明（保守）
                logger.warning("tool effects declaration failed for %s", tool.name, exc_info=True)
                declared = None
        decision = await self._approvals.engine.authorize_tool_invocation(
            tool_name=tool.name,
            arguments=dict(arguments),
            declared_effects=declared,
        )
        if decision.verdict == Verdict.ALLOW:
            return await next(ctx)
        if decision.verdict == Verdict.DENY:
            return ToolResult(
                ok=False,
                status=ToolStatus.FAILED.value,
                code="authorization_denied",
                error=f"工具「{tool.name}」未执行（{decision.reason}）",
            )
        label = ""
        try:
            from agent.tools.approval_present import describe_tool_call

            described = describe_tool_call(tool.name, dict(arguments))
            label = str(described.get("description") or described.get("summary") or "")
        except Exception:  # noqa: BLE001
            label = ""
        if not label:
            label = "调用工具「" + str(tool.name) + "」"
        operation = PendingOperation(
            kind="tool.invoke",
            label=label[:200],
            payload={
                "tool": tool.name,
                "arguments": dict(arguments),
                "call_id": getattr(call, "id", None),
            },
            effect=self._effect_dict(decision.effects),
            fingerprint={"tool": tool.name, "arguments": dict(arguments)},
            purpose=decision.reason,
            source="tool",
        )
        ticket = await self._approvals.submit(operation)
        return ToolResult(
            ok=True,
            status=ToolStatus.AWAITING_APPROVAL.value,
            code="pending_approval",
            protocol_version=TOOL_RESULT_PROTOCOL_VERSION,
            content="已提交审批，批准后会自动执行（审批 id：" + ticket.approval_id + "）",
            data={"pending_approval": ticket.as_dict()},
        )

    @staticmethod
    def _effect_dict(effects) -> dict:
        if not effects:
            return {}
        effect = effects[0]
        return {
            "kind": effect.kind.value,
            "purpose": effect.purpose,
            "tool": effect.tool,
            "targets": [
                {
                    "kind": target.kind,
                    "value": target.value,
                    "identity": target.identity,
                    "exists": target.exists,
                }
                for target in effect.targets
            ],
            "detail": dict(effect.detail),
        }

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
                error=f"工具「{tool.name}」超时（{tool.timeout_ms} 毫秒）",
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - isolation boundary
            logger.warning("tool %s failed: %s", tool.name, exc)
            return ToolResult(ok=False, error=_exception_text(exc))
