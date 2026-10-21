"""Subagent runtime tool: standalone loop with its own model/credential.

阶段四（契约 §14.3）：

* 动态 subagent 工具支持保留字段 _qio_subtask（pydantic 严格校验，schema 由执行
  模型生成）；credential_ref 必填；model / context / evidence_ids / budget 严格
  校验后使用；控制字段在交给业务参数之前**剥离**，只允许 subagent 工具使用。
* 凭据由 QIO 自主选择（入口 list_available_credentials）；尊重工具定义 / 用户
  明确指定与政策上限；旧 credential_key 仅在兼容层归一化，双参冲突拒绝。
* 真实终态：completed / partially_completed / failed / cancelled /
  budget_exhausted 由程序按子任务 loop 的真实结束事实填写，不把非异常退出包装成功。
* 完整结果保留（TaskRecord.full_content / total_chars）；长结果按
  read_task_result(task_id, offset, limit) 分段读取，预览不冒充全文。
* 原任务/原话题/目标版本由程序绑定（origin_provider），模型不能伪造。
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Awaitable, Callable

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from agent import prompts
from agent.credentials import selection as credential_selection
from agent.prompts import (
    SUBAGENT_PROMPT,
    SUBAGENT_RESULT_LONG,
    SUBAGENT_STARTED,
    TOOL_AWAIT_TASK_DESC,
    TOOL_READ_TASK_RESULT_DESC,
)
from agent.tools.base import TOOL_RESULT_PROTOCOL_VERSION, Tool, ToolResult
from agent.tools.contracts import json_schema_for
from agent.tools.memory_search import MemorySearchTool
from agent.tools.registry import ToolRegistry
from agent.tools.spec import SubagentBudget, ToolDefinition
from agent.tools.task_manager import (
    OUTCOME_BUDGET_EXHAUSTED,
    OUTCOME_CANCELLED,
    OUTCOME_COMPLETED,
    OUTCOME_FAILED,
    OUTCOME_PARTIALLY_COMPLETED,
)

DEFAULT_RESULT_PREVIEW_CHARS = 500
READ_CHUNK_LIMIT = 4000

#: 保留字段名：只允许 subagent 工具消费（其它工具收到必须拒绝）。
SUBTASK_CONTROL_KEY = "_qio_subtask"

MAX_CONTEXT_TEXT = 8000
MAX_CONTEXT_SUMMARY = 4000
MAX_CONTEXT_ITEMS = 32
MAX_EVIDENCE_IDS = 64
MAX_CONTEXT_TOTAL = 12000
MAX_REF_CHARS = 200

#: 程序绑定 origin 的全局提供者（由 app.py 启动时用 set_origin_provider 绑定一次；
#: 子任务在某一轮里创建时，它返回该轮的程序事实，模型不能伪造）。
_ORIGIN_PROVIDER: Callable[[], dict[str, Any] | None] | None = None


def set_origin_provider(provider: Callable[[], dict[str, Any] | None] | None) -> None:
    global _ORIGIN_PROVIDER
    _ORIGIN_PROVIDER = provider


def reset_origin_provider() -> None:
    set_origin_provider(None)


def _prompt_text(key: str, fallback: str = "") -> str:
    value = prompts.PROMPTS.get(key)
    return str(value).strip() if value is not None else fallback


class SubtaskControlError(ValueError):
    """_qio_subtask 不合法：调用方据此如实失败（不把控制字段当业务参数）。"""


class SubtaskContext(BaseModel):
    """子任务上下文：QIO 自主选择传递原文 / 摘要 / 记忆 / 材料引用。"""

    model_config = ConfigDict(extra="forbid")

    text: str = Field(default="", max_length=MAX_CONTEXT_TEXT)
    summary: str = Field(default="", max_length=MAX_CONTEXT_SUMMARY)
    items: list[str] = Field(default_factory=list, max_length=MAX_CONTEXT_ITEMS)
    evidence_ids: list[str] = Field(default_factory=list, max_length=MAX_EVIDENCE_IDS)

    @model_validator(mode="after")
    def _bounded(self) -> "SubtaskContext":
        total = len(self.text) + len(self.summary) + sum(len(item) for item in self.items)
        if total > MAX_CONTEXT_TOTAL:
            raise ValueError(f"context 内容总长不能超过 {MAX_CONTEXT_TOTAL} 字符")
        for item in self.items:
            if len(str(item)) > MAX_CONTEXT_TEXT:
                raise ValueError("context.items 单项过长")
        return self


class SubtaskBudgetOverride(BaseModel):
    """控制字段里的预算：只能收紧/对齐程序允许的执行范围。"""

    model_config = ConfigDict(extra="forbid")

    max_iterations: int | None = Field(default=None, ge=1, le=50)
    max_tokens: int | None = Field(default=None, ge=1000)
    output_limit_chars: int | None = Field(default=None, ge=100, le=50_000)


class SubtaskControl(BaseModel):
    """_qio_subtask 保留字段（schema 由此模型生成，模型不能传业务参数进来）。"""

    model_config = ConfigDict(extra="forbid")

    credential_ref: str = Field(
        min_length=1,
        max_length=MAX_REF_CHARS,
        description="从可用凭据目录中选择的 credential_ref（不是密钥）。",
    )
    model: str | None = Field(default=None, max_length=128)
    context: SubtaskContext | None = None
    evidence_ids: list[str] = Field(default_factory=list, max_length=MAX_EVIDENCE_IDS)
    budget: SubtaskBudgetOverride | None = None

    @model_validator(mode="after")
    def _check_refs(self) -> "SubtaskControl":
        for item in [self.credential_ref, *self.evidence_ids]:
            if not str(item or "").strip():
                raise ValueError("credential_ref / evidence_ids 不允许空字符串")
            if len(str(item)) > MAX_REF_CHARS:
                raise ValueError("引用 id 过长")
        return self


def subtask_control_schema() -> dict[str, Any]:
    """_qio_subtask 的模型可见 schema（由执行模型生成，不手抄）。"""
    return json_schema_for(SubtaskControl)


def inject_subtask_control(parameters: dict[str, Any] | None) -> dict[str, Any]:
    """把保留字段注入动态 subagent 工具的参数 schema（业务参数保持不变）。"""
    schema = json.loads(json.dumps(parameters or {}))
    schema.setdefault("type", "object")
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        properties = {}
        schema["properties"] = properties
    properties[SUBTASK_CONTROL_KEY] = subtask_control_schema()
    required = [str(item) for item in (schema.get("required") or [])]
    if SUBTASK_CONTROL_KEY not in required:
        required.append(SUBTASK_CONTROL_KEY)
    schema["required"] = required
    return schema


def split_subtask_control(kwargs: dict[str, Any]) -> tuple[SubtaskControl | None, dict[str, Any]]:
    """先剥离控制字段，再把剩余业务参数原样交给工具。

    返回 (control, business_kwargs)；control=None 表示旧调用点没有提供控制字段
    （兼容路径：仍按工具定义的 credential_ref / 自动选择执行，并如实标注 legacy）。
    """
    business = dict(kwargs or {})
    raw = business.pop(SUBTASK_CONTROL_KEY, None)
    if raw is None:
        return None, business
    if not isinstance(raw, dict):
        raise SubtaskControlError("_qio_subtask 必须是对象")
    try:
        control = SubtaskControl.model_validate(raw)
    except ValidationError as exc:
        first = exc.errors()[0] if exc.errors() else {}
        location = ".".join(str(part) for part in first.get("loc") or ()) or "_qio_subtask"
        raise SubtaskControlError(
            f"_qio_subtask.{location} 不合法：{first.get('msg') or '校验失败'}"
        ) from exc
    return control, business


def render_subtask_context(control: SubtaskControl | None) -> str:
    """把 QIO 选择的上下文渲染成子任务 user 消息的一部分（限长、来源保留）。"""
    if control is None:
        return ""
    parts: list[str] = []
    context = control.context
    if context is not None:
        if context.text.strip():
            parts.append("【QIO 提供的原文】\n" + context.text.strip())
        if context.summary.strip():
            parts.append("【QIO 提供的摘要】\n" + context.summary.strip())
        if context.items:
            lines = "\n".join(f"- {str(item).strip()}" for item in context.items if str(item).strip())
            if lines:
                parts.append("【QIO 提供的材料片段】\n" + lines)
    evidence: list[str] = []
    if context is not None:
        evidence.extend(context.evidence_ids)
    evidence.extend(control.evidence_ids)
    deduped = list(dict.fromkeys(str(item).strip() for item in evidence if str(item).strip()))
    if deduped:
        parts.append("【来源引用】\n" + "\n".join(f"- {item}" for item in deduped))
    if not parts:
        return ""
    parts.append(
        "以上上下文由 QIO 选择传入；记忆保留原有行动者、来源与确定程度，不假设拥有完整对话。"
    )
    return "\n\n".join(parts)


def _merged_budget(
    definition_budget: SubagentBudget | None, control: SubtaskControl | None
) -> SubagentBudget:
    """程序预算为上限：控制字段只能收紧，不能扩大执行范围。"""
    base = definition_budget or SubagentBudget()
    override = control.budget if control is not None else None
    if override is None:
        return base
    data = base.model_dump()
    if override.max_iterations is not None:
        data["max_iterations"] = min(int(base.max_iterations), int(override.max_iterations))
    if override.max_tokens is not None:
        data["max_tokens"] = min(int(base.max_tokens), int(override.max_tokens))
    if override.output_limit_chars is not None:
        data["output_limit_chars"] = min(
            int(base.output_limit_chars), int(override.output_limit_chars)
        )
    return SubagentBudget(**data)


def _outcome_for(result: Any) -> str:
    """把子任务 loop 的真实结束事实映射成精确终态（契约 §14.3）。"""
    if bool(getattr(result, "cancelled", False)):
        return OUTCOME_CANCELLED
    code = str(getattr(result, "stop_reason_code", "") or "")
    content = str(getattr(result, "final_content", "") or "").strip()
    if code == "budget":
        return OUTCOME_BUDGET_EXHAUSTED
    if code in ("length_limit", "content_filter", "incomplete_stream"):
        return OUTCOME_PARTIALLY_COMPLETED if content else OUTCOME_FAILED
    if code in ("guard_halt", "no_progress"):
        return OUTCOME_PARTIALLY_COMPLETED if content else OUTCOME_FAILED
    return OUTCOME_COMPLETED if content else OUTCOME_FAILED


class SubagentTool(Tool):
    #: 允许接收 _qio_subtask 控制字段（registry 的保留字段检查据此放行）
    subtask_control_allowed = True

    def __init__(
        self,
        definition: ToolDefinition,
        *,
        credentials,
        task_manager,
        retriever=None,
        adapter_factory: Callable[[str, str | None], Awaitable[Any]] | None = None,
        bus=None,
        toolset: list[str] | None = None,
        trace_store=None,
        origin_provider: Callable[[], dict[str, Any] | None] | None = None,
    ) -> None:
        self.definition = definition
        self.name = definition.name
        self.description = definition.description
        # 业务 schema 原样保留，只增 _qio_subtask（模型可见、由执行模型生成）
        self.parameters = inject_subtask_control(definition.parameters)
        self.credentials = credentials
        self.task_manager = task_manager
        self.retriever = retriever
        self.adapter_factory = adapter_factory
        self.bus = bus
        self.toolset = toolset or ["memory_search"]
        self.trace_store = trace_store
        self.origin_provider = origin_provider

    def _origin(self) -> dict[str, Any]:
        provider = self.origin_provider or _ORIGIN_PROVIDER
        if provider is None:
            return {}
        try:
            value = provider()
        except Exception:  # noqa: BLE001 - 程序绑定的 origin 读失败不阻断任务
            return {}
        return dict(value or {}) if isinstance(value, dict) else {}

    async def run(self, **kwargs: Any) -> ToolResult:
        if self.adapter_factory is None:
            return ToolResult(ok=False, error="子任务没有可用的适配器工厂")
        try:
            control, business = split_subtask_control(kwargs)
        except SubtaskControlError as exc:
            return ToolResult(ok=False, error=str(exc))

        # 旧 credential_key 只在这里归一化；双参不一致直接拒绝（不猜）。
        legacy_key = business.pop("credential_key", None)
        try:
            choice = credential_selection.resolve_credential_choice(
                self.credentials,
                credential_ref=(control.credential_ref if control is not None else None),
                credential_key=legacy_key,
                purpose="subagent",
                fallback_ref=self.definition.credential_ref,
            )
            model = credential_selection.choose_model(
                choice, control.model if control is not None else None
            )
        except credential_selection.CredentialSelectionError as exc:
            return ToolResult(
                ok=False,
                error=str(exc),
                code="credential_unavailable",
                status="failed",
            )
        adapter = await self.adapter_factory(choice["credential_ref"], model)
        if adapter is None:
            return ToolResult(ok=False, error="为子任务构建适配器失败")
        budget = _merged_budget(self.definition.subagent_budget, control)
        origin = self._origin()
        task_id = f"task_{uuid.uuid4().hex[:12]}"
        self.task_manager.submit(
            self.name,
            lambda: self._execute(task_id, adapter, business, control, budget, choice),
            task_id=task_id,
            origin=origin,
        )
        if self.definition.sync:
            _, res = await self.task_manager.await_result(task_id, timeout=None)
            return res if res is not None else ToolResult(ok=False, error="子任务执行失败")
        return ToolResult(
            ok=True,
            content=SUBAGENT_STARTED.format(task_id=task_id),
            data={
                "task_id": task_id,
                "state": "queued",
                "credential_ref": choice["credential_ref"],
                "model": model,
                "origin": origin,
                "next": (
                    "需要结果时调用 await_task(task_id=..., notify=true) 取回；"
                    "结果较长时用 read_task_result 分段读取全文。"
                ),
            },
            protocol_version=TOOL_RESULT_PROTOCOL_VERSION,
            status="queued",
            code="queued",
        )

    async def _execute(
        self,
        task_id: str,
        adapter,
        business: dict[str, Any],
        control: SubtaskControl | None,
        budget: SubagentBudget,
        choice: dict[str, Any],
    ) -> ToolResult:
        tid = f"subagent:{task_id}"
        tracer = None
        if self.trace_store is not None:
            from agent.trace.recorder import TurnTracer

            tracer = TurnTracer(self.trace_store, tid)
            self.trace_store.begin(tid)
        try:
            result = await self._run_subagent(
                task_id, adapter, business, control, budget, choice, tid, tracer
            )
        except Exception:
            if self.trace_store is not None:
                self.trace_store.finish(tid, "failed")
            raise
        if self.trace_store is not None:
            self.trace_store.finish(tid, "done", final_preview=result.content or "")
        return result

    async def _run_subagent(
        self,
        task_id: str,
        adapter,
        business: dict[str, Any],
        control: SubtaskControl | None,
        budget: SubagentBudget,
        choice: dict[str, Any],
        tid: str,
        tracer,
    ) -> ToolResult:
        from agent.core.loop import AgentLoop
        from agent.credentials.usage import credential_usage_sink

        sub_registry = ToolRegistry()
        if self.retriever is not None and "memory_search" in self.toolset:
            sub_registry.register(MemorySearchTool(self.retriever))
        system_text = _prompt_text(
            "SUBAGENT_SYSTEM", "你是 QIO 的子任务执行者，负责完成本次分配的明确任务。"
        )
        task_text = SUBAGENT_PROMPT.format(
            description=self.definition.description,
            kwargs=json.dumps(business, ensure_ascii=False),
            limit=budget.output_limit_chars,
        )
        context_text = render_subtask_context(control)
        prompt = system_text + "\n\n" + task_text
        if context_text:
            prompt = prompt + "\n\n" + context_text
        loop = AgentLoop(
            adapter,
            sub_registry,
            self.bus,
            usage_sink=credential_usage_sink(self.credentials, adapter),
            max_iterations=budget.max_iterations,
            token_budget=budget.max_tokens,
            turn_id=tid,
            trace=tracer,
        )
        result = await loop.run(prompt)
        # 真实终态由执行器写进记录（TaskManager 收口时不再按 ok 简单二分）。
        record = self.task_manager.record_info(task_id)
        outcome = _outcome_for(result)
        if record is not None:
            record.iterations = result.iterations_used
            record.tokens = result.tokens_used
            record.tool_calls = result.tool_calls_made
            record.outcome = outcome
            record.origin = dict(record.origin or {})
            record.origin.setdefault("credential_ref", choice["credential_ref"])
            record.origin.setdefault("model", getattr(adapter, "model", ""))
            record.origin.setdefault("budget", budget.model_dump())
        content = result.final_content or ""
        if record is not None:
            # 完整结果先落进记录：long 结果预览替换不了全文（read_task_result 只读它）。
            record.full_content = content
            record.total_chars = len(content)
        if outcome == OUTCOME_CANCELLED:
            return ToolResult(
                ok=False,
                status="cancelled",
                code="cancelled",
                error="子任务已被取消（迟到结果不再推进原任务）",
            )
        if outcome == OUTCOME_BUDGET_EXHAUSTED:
            message = SUBAGENT_RESULT_LONG.format(
                length=len(content),
                preview=content[:DEFAULT_RESULT_PREVIEW_CHARS],
                task_id=task_id,
            ) if content else "子任务因执行预算耗尽而停止；未完成的部分需要按原目标继续处理。"
            return ToolResult(
                ok=False,
                status="failed",
                code="budget_exhausted",
                error=message,
                content=content[: budget.output_limit_chars],
            )
        if outcome == OUTCOME_FAILED:
            return ToolResult(
                ok=False,
                status="failed",
                code="subtask_failed",
                error=content[: budget.output_limit_chars] or "子任务没有产出可用结果",
            )
        # completed / partially_completed：成果完整保留，长结果给可读取引用。
        status = "completed" if outcome == OUTCOME_COMPLETED else "partially_completed"
        if len(content) <= budget.output_limit_chars:
            return ToolResult(ok=True, status=status, code="ok", content=content)
        preview = content[:DEFAULT_RESULT_PREVIEW_CHARS]
        return ToolResult(
            ok=True,
            status=status,
            code="result_paginated",
            content=SUBAGENT_RESULT_LONG.format(
                length=len(content), preview=preview, task_id=task_id
            ),
            data={
                "task_id": task_id,
                "total_chars": len(content),
                "read_tool": "read_task_result",
                "offset": 0,
                "limit": READ_CHUNK_LIMIT,
            },
            protocol_version=TOOL_RESULT_PROTOCOL_VERSION,
        )


def _task_facts_text(task_id: str, facts: dict[str, Any]) -> str:
    outcome = str(facts.get("outcome") or "")
    status = str(facts.get("status") or "")
    total = int(facts.get("total_chars") or 0)
    lines = [f"task_id: {task_id}", f"状态: {status}", f"真实终态: {outcome or '未知'}"]
    if facts.get("error"):
        lines.append(f"原因: {facts['error']}")
    if total:
        lines.append(f"完整结果: {total} 字符（read_task_result 分段读取）")
    origin = facts.get("origin") or {}
    if origin.get("topic_id"):
        lines.append(f"原话题: {origin['topic_id']}")
    return "\n".join(lines)


class AwaitTaskTool(Tool):
    name = "await_task"
    description = prompts.TOOL_DESCRIPTIONS["await_task"]
    parameters = {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": prompts.argument_description("await_task", "task_id"),
            },
            "notify": {
                "type": "boolean",
                "description": prompts.argument_description("await_task", "notify"),
            },
            "timeout": {
                "type": "integer",
                "description": prompts.argument_description("await_task", "timeout"),
            },
        },
        "required": ["task_id"],
    }

    def __init__(self, task_manager, notify_handler=None) -> None:
        self.task_manager = task_manager
        self.notify_handler = notify_handler

    async def run(self, **kwargs: Any) -> ToolResult:
        task_id = str(kwargs.get("task_id") or "").strip()
        if not task_id:
            return ToolResult(ok=False, error="task_id 必填")
        notify = bool(kwargs.get("notify", False))
        timeout = int(kwargs.get("timeout", 120) or 120)
        facts = self.task_manager.task_facts(task_id)
        if facts is None:
            return ToolResult(ok=False, error=f"找不到子任务：{task_id}")
        if notify:
            if self.notify_handler is None:
                return ToolResult(ok=False, error="当前环境不支持完成通知")
            if self.task_manager.register_notify(task_id, self.notify_handler):
                return ToolResult(
                    ok=True,
                    status="queued",
                    content=f"已注册通知：任务 {task_id} 完成时将自动唤起主 agent。",
                    data={"task_id": task_id, "notify": True},
                    protocol_version=TOOL_RESULT_PROTOCOL_VERSION,
                    code="notify_registered",
                )
            # 已完成的直接取回
        status, res = await self.task_manager.await_result(task_id, timeout=timeout)
        if status == "not_found":
            return ToolResult(ok=False, error=f"找不到子任务：{task_id}")
        facts = self.task_manager.task_facts(task_id) or facts
        if res is None:
            return ToolResult(
                ok=True,
                status="running",
                content=(
                    f"任务 {task_id} 仍在运行（{status}），可稍后再次 await_task 取回；"
                    "等待超时不等于任务已经停止，不要重复轮询。"
                ),
                data={"task_id": task_id, "status": status, "outcome": facts.get("outcome")},
                protocol_version=TOOL_RESULT_PROTOCOL_VERSION,
                code="still_running",
            )
        outcome = str(facts.get("outcome") or "")
        if outcome == OUTCOME_CANCELLED or status == "cancelled":
            return ToolResult(
                ok=False,
                status="cancelled",
                code="cancelled",
                error=f"子任务 {task_id} 已被取消（不会因迟到结果复活）。",
                data={"task_id": task_id, "status": status, "outcome": outcome},
                protocol_version=TOOL_RESULT_PROTOCOL_VERSION,
            )
        if not res.ok:
            return ToolResult(
                ok=False,
                status="failed",
                code=outcome or "subtask_failed",
                error=res.error or "子任务失败",
                data={"task_id": task_id, "status": status, "outcome": outcome,
                      "facts": self.task_manager.task_facts(task_id)},
                protocol_version=TOOL_RESULT_PROTOCOL_VERSION,
            )
        content = res.content or ""
        # 长结果：提示按分页读取，绝不只转述预览就结束原工作。
        facts = self.task_manager.task_facts(task_id) or {}
        total = int(facts.get("total_chars") or 0)
        body = content
        if total and len(content) < total:
            body = (
                content
                + "\n\n【结果未完整显示】"
                + f"完整结果共 {total} 字符，请用 read_task_result(task_id=\"{task_id}\", "
                "offset=0, limit=4000) 分段读取后继续原任务。"
            )
        return ToolResult(
            ok=True,
            status=("partially_completed" if outcome == OUTCOME_PARTIALLY_COMPLETED else "completed"),
            code="ok",
            content=body,
            data={
                "task_id": task_id,
                "status": status,
                "outcome": outcome,
                "total_chars": total,
                "facts": facts,
            },
            protocol_version=TOOL_RESULT_PROTOCOL_VERSION,
        )


class ReadTaskResultTool(Tool):
    name = "read_task_result"
    description = prompts.TOOL_DESCRIPTIONS["read_task_result"]
    parameters = {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": prompts.argument_description("read_task_result", "task_id"),
            },
            "offset": {
                "type": "integer",
                "description": prompts.argument_description("read_task_result", "offset"),
            },
            "limit": {
                "type": "integer",
                "description": prompts.argument_description("read_task_result", "limit"),
            },
        },
        "required": ["task_id"],
    }

    def __init__(self, task_manager) -> None:
        self.task_manager = task_manager

    async def run(self, **kwargs: Any) -> ToolResult:
        task_id = str(kwargs.get("task_id") or "").strip()
        if not task_id:
            return ToolResult(ok=False, error="task_id 必填")
        offset = max(0, int(kwargs.get("offset", 0) or 0))
        limit = max(1, min(int(kwargs.get("limit", READ_CHUNK_LIMIT) or READ_CHUNK_LIMIT), READ_CHUNK_LIMIT))
        page = self.task_manager.read_result(task_id, offset=offset, limit=limit)
        if page is None:
            return ToolResult(ok=False, error="子任务结果暂不可用")
        data = {
            "task_id": task_id,
            "offset": page["offset"],
            "limit": page["limit"],
            "next_offset": page["next_offset"],
            "has_more": page["has_more"],
            "total_chars": page["total_chars"],
            "status": page["status"],
            "outcome": page["outcome"],
        }
        return ToolResult(
            ok=True,
            status="completed",
            code="ok",
            content=page["content"],
            data=data,
            protocol_version=TOOL_RESULT_PROTOCOL_VERSION,
        )
