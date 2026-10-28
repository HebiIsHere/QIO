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
from agent.tools import security_gate
from agent.tools.base import TOOL_RESULT_PROTOCOL_VERSION, Tool, ToolResult
from agent.tools.contracts import json_schema_for
from agent.services.usage_target import UsageUnavailable
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


def _scope_to_payload(scope: Any) -> dict | None:
    """AgentScope → 可持久化载荷（审批执行器按同一子集重建，不扩大范围）。"""
    if scope is None:
        return None
    grants: list[dict] = []
    for grant in getattr(scope, "grants", ()) or ():
        as_dict = getattr(grant, "as_dict", None)
        if callable(as_dict):
            try:
                grants.append(dict(as_dict()))
                continue
            except Exception:  # noqa: BLE001 - 退回逐字段读取
                pass
        grants.append(
            {
                "grant_id": getattr(grant, "grant_id", ""),
                "instruction_id": getattr(grant, "instruction_id", ""),
                "operations": sorted(getattr(grant, "operations", ()) or ()),
                "objects": [
                    {"kind": getattr(obj, "kind", "path"), "value": getattr(obj, "value", "")}
                    for obj in getattr(grant, "objects", ()) or ()
                ],
                "purpose": getattr(grant, "purpose", ""),
                "source": getattr(grant, "source", "user_explicit"),
                "created_at": getattr(grant, "created_at", ""),
                "revoked": bool(getattr(grant, "revoked", False)),
                "requirement_id": getattr(grant, "requirement_id", None),
                "requirement_version": getattr(grant, "requirement_version", None),
                "source_record_id": getattr(grant, "source_record_id", None),
                "fingerprint": getattr(grant, "fingerprint", None),
                "limits": list(getattr(grant, "limits", ()) or ()),
            }
        )
    return {
        "parent_instruction_id": getattr(scope, "parent_instruction_id", None),
        "grants": grants,
        # 诊断/测试用的摘要（重建时以 grants 为准，不靠 id 反推授权）。
        "parent_grant_ids": [
            str(item.get("grant_id") or "") for item in grants if item.get("grant_id")
        ],
        "grant_count": len(grants),
        "depth": int(getattr(scope, "depth", 0) or 0),
        "purpose": str(getattr(scope, "purpose", "") or ""),
        "label": str(getattr(scope, "label", "") or ""),
    }


def _optional_int(value: Any) -> int | None:
    """宽松整数（只用于重建已提交审批载荷里的版本号；非法值按 None，不猜测）。"""
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _scope_from_payload(data: Any) -> Any:
    """审批载荷 → AgentScope（与提交审批时是同一批 grant，只能缩小不能扩大）。"""
    if not isinstance(data, dict) or not data:
        return None
    try:
        from agent.security.instructions import Grant, Scope
    except Exception:  # noqa: BLE001 - 没有权威类型时不伪造 scope
        return None
    grants: list[Any] = []
    for item in data.get("grants") or []:
        if not isinstance(item, dict):
            continue
        grants.append(
            Grant(
                grant_id=str(item.get("grant_id") or ""),
                instruction_id=str(item.get("instruction_id") or ""),
                operations=frozenset(
                    str(op) for op in (item.get("operations") or ())
                ),
                objects=[
                    Scope(
                        kind=str(obj.get("kind") or "path"),
                        value=str(obj.get("value") or ""),
                    )
                    for obj in item.get("objects") or []
                    if isinstance(obj, dict)
                ],
                purpose=str(item.get("purpose") or ""),
                source=str(item.get("source") or "user_explicit"),
                created_at=str(item.get("created_at") or ""),
                revoked=bool(item.get("revoked", False)),
                requirement_id=item.get("requirement_id"),
                requirement_version=_optional_int(item.get("requirement_version")),
                source_record_id=item.get("source_record_id"),
                fingerprint=item.get("fingerprint"),
                limits=tuple(str(value) for value in (item.get("limits") or ())),
            )
        )
    return security_gate.make_agent_scope(
        grants=grants,
        parent_instruction_id=data.get("parent_instruction_id"),
        depth=int(data.get("depth") or 1),
        purpose=str(data.get("purpose") or ""),
        label=str(data.get("label") or ""),
    )


# ---------------------------------------------------------------------------
# R9-H13 §3.H（F6）：可委派范围（当前有效要求 + 模式 + 已核实授权）与结构化申请
# ---------------------------------------------------------------------------


def _current_requirement_binding() -> Any:
    """当前操作依据的具体用户要求绑定（子智能体继承父绑定，不回退"最近一条"）。"""
    try:
        from agent.security.context import current_requirement

        return current_requirement.get()
    except Exception:  # noqa: BLE001 - 上下文未初始化按没有绑定
        return None


def _binding_ids(binding: Any) -> tuple[str | None, int | None]:
    """要求绑定 → (requirement_id, version)；字段缺失时如实返回 None。"""
    if binding is None:
        return None, None
    if isinstance(binding, dict):
        requirement_id = binding.get("requirement_id")
        version = binding.get("version")
    else:
        requirement_id = getattr(binding, "requirement_id", None)
        version = getattr(binding, "version", None)
    if not requirement_id and isinstance(binding, str):
        requirement_id = binding
    try:
        normalized_version = int(version) if version is not None else None
    except (TypeError, ValueError):
        normalized_version = None
    text = str(requirement_id or "").strip()
    return (text or None), normalized_version


def _grant_description(scope: Any) -> list[dict]:
    """结构化 grant 事实（操作/对象/来源/版本/用途边界）；无权威类型时不伪造。"""
    try:
        from agent.security.instructions import describe_scope

        return list(describe_scope(scope).get("grants") or ())
    except Exception:  # noqa: BLE001
        return []


def _flatten_scope_objects(grants: Any) -> list[dict]:
    """grant 事实里出现过的对象去重（只用于展示/核对，不产生授权）。"""
    objects: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for grant in grants or ():
        for item in (grant.get("objects") or ()) if isinstance(grant, dict) else ():
            kind = str(item.get("kind") or "")
            value = str(item.get("value") or "")
            key = (kind, value)
            if value and key not in seen:
                seen.add(key)
                objects.append({"kind": kind, "value": value})
    return objects


class RequestAgentScopeTool(Tool):
    """子智能体权限不足时向主智能体提交结构化范围申请（不直接请求用户）。

    契约 §3.H（F6）：子权限不足先向主智能体申请；主智能体也没有权限时才由主
    智能体请求用户。请求/授予/重试/撤销用结构化状态（ScopeRequestLedger），
    不把"一句错误文字"当申请。
    """

    name = "request_agent_scope"
    description = (
        "当本次子任务需要超出当前分配范围的操作/对象权限时，向主智能体提交结构化"
        "范围申请（说明操作、对象、原因）；不会直接向用户弹窗。主智能体会核对当前"
        "要求/模式与已核实授权后决定是否扩大你的范围，并可能让用户确认。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "operations": {
                "type": "array",
                "items": {"type": "string"},
                "description": "需要的操作名（read/write/create/delete/execute/install_deps/network_egress）",
            },
            "objects": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "kind": {"type": "string", "enum": ["path", "path_prefix", "program", "url_host"]},
                        "value": {"type": "string"},
                    },
                    "required": ["kind", "value"],
                },
                "description": "需要的具体对象（路径/前缀/程序/接收服务）",
            },
            "reason": {"type": "string", "description": "为什么完成本次子任务需要这些权限"},
        },
        "required": ["operations", "objects", "reason"],
    }
    max_operations = 20
    max_objects = 20
    max_text_chars = 400

    def __init__(self, *, security: Any, purpose: str) -> None:
        self.security = security
        self.purpose = str(purpose or "")

    def _invalid(self, message: str) -> ToolResult:
        return ToolResult(
            ok=False,
            status="failed",
            code="invalid_scope_request",
            category="permission",
            recoverable=True,
            error=message,
            data={"state": "denied", "reason": message},
        )

    async def run(self, **kwargs: Any) -> ToolResult:
        try:
            from agent.security.instructions import (
                Scope,
                scope_request_ledger,
            )
            from agent.security.review import record_is_active as _record_is_active
        except Exception:  # noqa: BLE001 - 没有权威类型时不伪造申请
            return self._invalid("当前环境没有可用的授权台账，无法形成范围申请")

        operations = [
            str(item).strip()
            for item in (kwargs.get("operations") or ())
            if str(item).strip()
        ]
        objects: list[Scope] = []
        for item in kwargs.get("objects") or ():
            if isinstance(item, dict):
                kind = str(item.get("kind") or "path").strip().lower()
                value = str(item.get("value") or "").strip()
            else:
                kind, value = "path", str(item or "").strip()
            if kind not in {"path", "path_prefix", "program", "url_host", "service"}:
                return self._invalid("范围申请的对象类别不合法：" + kind)
            if value:
                objects.append(Scope(kind, value))
        reason = str(kwargs.get("reason") or "").strip()
        if not operations or not objects:
            return self._invalid("范围申请必须说明需要的操作与对象")
        if (
            len(operations) > self.max_operations
            or len(objects) > self.max_objects
            or len(reason) > self.max_text_chars
            or any(len(item.value) > self.max_text_chars for item in objects)
        ):
            return self._invalid("范围申请超出允许的规模（操作/对象/文字过长）")

        requirement_id, bound_version = _binding_ids(_current_requirement_binding())
        requirement = None
        requirements = getattr(self.security, "requirements", None)
        if requirement_id and requirements is not None:
            try:
                requirement = requirements.by_id(str(requirement_id))
            except Exception:  # noqa: BLE001
                requirement = None
        if requirement is None:
            return self._invalid("当前没有可绑定的用户要求，主智能体无法据此核实授权")
        version = int(getattr(requirement, "version", bound_version or 1) or 1)
        mode = str(security_gate.mode_of(self.security) or "")
        escalation = "main_agent" if mode == "restricted" else "user"
        ledger = scope_request_ledger()
        request = ledger.request(
            requirement_id=str(requirement_id),
            requirement_version=version,
            mode=mode,
            purpose=self.purpose,
            operations=operations,
            objects=objects,
            reason=reason,
            escalation=escalation,
        )
        scope = security_gate.current_agent_scope()
        grants = tuple(getattr(scope, "grants", ()) or ()) if scope is not None else ()
        synced = ledger.sync(
            requirement_id=str(requirement_id),
            requirement_version=version,
            purpose=self.purpose,
            grants=grants,
            requirement_active=str(getattr(requirement, "status", "")) == "active",
            requirement_current_version=version,
            grants_detail=_grant_description(scope),
        )
        if synced:
            request = synced[0]
        content = (
            "已向主智能体提交结构化权限申请（不会直接请求用户）。\n"
            f"申请编号: {request.request_id}\n"
            f"状态: {request.state}\n"
            f"需要操作: {'、'.join(operations)}\n"
            "需要对象: "
            + "、".join(item.value for item in objects[:8])
            + ("等" if len(objects) > 8 else "")
            + "\n主智能体会先核对当前要求、模式与已核实的授权；主智能体也没有权限时"
            "才会请求用户确认。"
        )
        return ToolResult(
            ok=True,
            status="completed",
            code="scope_requested",
            content=content,
            data=request.as_dict(),
            protocol_version=TOOL_RESULT_PROTOCOL_VERSION,
        )


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

    # security = 统一授权网关（契约 §2.4）；None = 旧接线/单测回退路径。
    inject = ["security"]

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
        security=None,
        usage_assignment: Callable[[], tuple[str, str | None] | None] | None = None,
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
        self.security = security
        # 设置页改版 §8：返回「子任务」用途的 (credential_ref, model)；
        # 返回 None = 没有显式/默认指定（照旧走基线自主选择）；
        # 显式指定但不可用时抛 UsageUnavailable（由 run() 转成可读失败）。
        self.usage_assignment = usage_assignment

    def _origin(self) -> dict[str, Any]:
        provider = self.origin_provider or _ORIGIN_PROVIDER
        if provider is None:
            return {}
        try:
            value = provider()
        except Exception:  # noqa: BLE001 - 程序绑定的 origin 读失败不阻断任务
            return {}
        return dict(value or {}) if isinstance(value, dict) else {}

    # -- F6：可委派范围（当前要求 + 模式 + 已核实授权）与结构化范围申请 -------

    def _ledger_available(self) -> bool:
        security = self.security
        return bool(
            security is not None
            and getattr(security, "authorizations", None) is not None
            and getattr(security, "requirements", None) is not None
        )

    def _verified_context(self) -> dict[str, Any] | None:
        """当前要求绑定的可委派事实（只读台账；没有绑定返回 None，不回退历史）。"""
        if not self._ledger_available():
            return None
        requirement_id, bound_version = _binding_ids(_current_requirement_binding())
        if not requirement_id:
            return None
        requirements = self.security.requirements
        try:
            requirement = requirements.by_id(str(requirement_id))
        except Exception:  # noqa: BLE001 - 台账不可用按没有绑定事实
            requirement = None
        version = int(getattr(requirement, "version", bound_version or 1) or 1)
        records: tuple = ()
        finder = getattr(self.security.authorizations, "records_for_requirement", None)
        if callable(finder):
            try:
                records = tuple(finder(str(requirement_id)) or ())
            except Exception:  # noqa: BLE001 - 查询失败按没有记录（不伪造）
                records = ()
        return {
            "requirement_id": requirement_id,
            "version": version,
            "requirement": requirement,
            "records": records,
            "active": bool(
                requirement is not None
                and str(getattr(requirement, "status", "")) == "active"
            ),
        }

    def _active_check_factory(self, requirement_id: str):
        """核实授权的实时有效性核对：记录撤销/过期/要求改版后立即失效。"""

        def factory(record_id: str):
            def check() -> bool:
                try:
                    from agent.security.review import record_is_active

                    finder = getattr(
                        self.security.authorizations, "records_for_requirement", None
                    )
                    rows = (
                        tuple(finder(str(requirement_id)) or ())
                        if callable(finder)
                        else ()
                    )
                except Exception:  # noqa: BLE001 - 核对失败按失效（失败关闭）
                    return False
                for row in rows:
                    if str(getattr(row, "record_id", "") or "") == str(record_id):
                        return bool(record_is_active(row))
                return False

            return check

        return factory

    def _enrich_scope(self, scope: Any, context: dict[str, Any] | None) -> Any:
        """给 verified grant 补上要求版本/记录 id/内容指纹/实时撤销核对。"""
        if scope is None or context is None:
            return scope
        try:
            from agent.security.instructions import bind_verified_grants
        except Exception:  # noqa: BLE001 - 没有权威类型时保持原 scope（不伪造）
            return scope
        grants = bind_verified_grants(
            getattr(scope, "grants", ()) or (),
            context["records"],
            requirement_id=str(context["requirement_id"]),
            requirement_version=int(context["version"]),
            active_check_factory=self._active_check_factory(
                str(context["requirement_id"])
            ),
        )
        try:
            import dataclasses as _dc

            if "grants" in {item.name for item in _dc.fields(scope)}:
                return _dc.replace(scope, grants=tuple(grants))
        except Exception:  # noqa: BLE001 - 非 dataclass 时退回适配器重建
            pass
        try:
            return security_gate.make_agent_scope(
                grants=tuple(grants),
                parent_instruction_id=getattr(scope, "parent_instruction_id", None),
                depth=int(getattr(scope, "depth", 1) or 1),
                purpose=str(getattr(scope, "purpose", "") or ""),
                label=str(getattr(scope, "label", "") or ""),
            )
        except Exception:  # noqa: BLE001 - 无法重建时保留原 scope（不伪造）
            return scope

    def _usable_grants(self, scope: Any) -> list:
        """子范围里真正可用于委派核对的 grant（未撤销 + 有操作 + 有对象）。"""
        try:
            from agent.security.instructions import grant_is_active
        except Exception:  # noqa: BLE001
            grant_is_active = lambda grant: not bool(  # noqa: E731
                getattr(grant, "revoked", False)
            )
        usable: list = []
        for grant in getattr(scope, "grants", ()) or ():
            if not grant_is_active(grant):
                continue
            if not getattr(grant, "operations", None) or not getattr(
                grant, "objects", None
            ):
                continue
            usable.append(grant)
        return usable

    def _scope_delegation_facts(
        self, scope: Any, context: dict[str, Any] | None
    ) -> tuple[dict, list[dict]]:
        """子范围摘要 + 结构化委派状态 + 申请状态（结果/审批载荷/审计共用）。"""
        summary = security_gate.scope_summary(scope) if scope is not None else {}
        if scope is None:
            return summary, []
        grants = _grant_description(scope)
        operations = sorted(
            {str(op) for item in grants for op in (item.get("operations") or ())}
        )
        objects = _flatten_scope_objects(grants)
        summary.update(
            {
                "grant_count": len(grants),
                "grants": grants,
                "operations": operations,
                "objects": objects,
                "revocable": True,
            }
        )
        mode = str(security_gate.mode_of(self.security) or "")
        escalation = "main_agent" if mode == "restricted" else "user"
        usable = self._usable_grants(scope)
        delegation: dict[str, Any] = {
            "state": "granted" if usable else "requested",
            "requirement_id": context["requirement_id"] if context else None,
            "requirement_version": context["version"] if context else None,
            "mode": mode,
            "purpose": str(self.definition.description or ""),
            "escalation": escalation,
            "revocable": True,
            "grants": grants,
            "operations": operations,
            "objects": objects,
        }
        requests: list[dict] = []
        if context is not None:
            try:
                from agent.security.instructions import scope_request_ledger

                ledger = scope_request_ledger()
                purpose = str(self.definition.description or "")
                if usable and not ledger.for_requirement(
                    str(context["requirement_id"]), purpose
                ):
                    # 已形成的委派也进入同一状态机：granted → revoked 可追踪，
                    # 不把一次许可当成长期通配授权。
                    ledger.request(
                        requirement_id=str(context["requirement_id"]),
                        requirement_version=int(context["version"]),
                        mode=mode,
                        purpose=purpose,
                        operations=operations,
                        objects=objects,
                        reason="当前要求下已核实的可委派范围",
                        escalation=escalation,
                    )
                synced = ledger.sync(
                    requirement_id=str(context["requirement_id"]),
                    requirement_version=int(context["version"]),
                    purpose=purpose,
                    grants=getattr(scope, "grants", ()) or (),
                    requirement_active=bool(context["active"]),
                    requirement_current_version=int(context["version"]),
                    grants_detail=grants,
                )
                requests = [item.as_dict() for item in synced]
                if any(item["state"] == "revoked" for item in requests):
                    delegation["state"] = "revoked"
                elif not context["active"]:
                    delegation["state"] = "superseded"
            except Exception:  # noqa: BLE001 - 状态机不可用时不伪造状态
                requests = []
        summary["delegation"] = delegation
        return summary, requests

    def _prepare_scope(self, scope: Any) -> tuple[Any, dict[str, Any] | None]:
        """形成显式子范围（当前要求 + 模式 + 已核实授权），并登记结构化状态。

        没有可委派事实时**不阻止**子任务创建（研究型子任务本来就不需要范围），
        但会形成结构化申请状态（requested），子智能体运行中权限不足时可用
        request_agent_scope 向主智能体申请——不直接向用户弹窗。
        """
        context = self._verified_context() if self._ledger_available() else None
        scope = self._enrich_scope(scope, context)
        if context is None:
            return scope, None
        if self._usable_grants(scope):
            return scope, context
        mode = str(security_gate.mode_of(self.security) or "")
        escalation = "main_agent" if mode == "restricted" else "user"
        try:
            from agent.security.instructions import scope_request_ledger

            ledger = scope_request_ledger()
            purpose = str(self.definition.description or "")
            if not ledger.for_requirement(str(context["requirement_id"]), purpose):
                ledger.request(
                    requirement_id=str(context["requirement_id"]),
                    requirement_version=int(context["version"]),
                    mode=mode,
                    purpose=purpose,
                    operations=(),
                    objects=(),
                    reason="当前要求下没有可委派的已核实授权范围",
                    escalation=escalation,
                )
        except Exception:  # noqa: BLE001 - 状态机不可用时仍按事实返回
            pass
        return scope, context

    async def run(self, **kwargs: Any) -> ToolResult:
        if self.adapter_factory is None:
            return ToolResult(ok=False, error="子任务没有可用的适配器工厂")
        try:
            control, business = split_subtask_control(kwargs)
        except SubtaskControlError as exc:
            return ToolResult(ok=False, error=str(exc))

        # 旧 credential_key 只在这里归一化；双参不一致直接拒绝（不猜）。
        legacy_key = business.pop("credential_key", None)
        # 用途分配（设置页改版 §8）：只有**没有**任何显式指定时才生效
        # （工具调用里的 credential_ref / 旧 credential_key / 工具定义上的 credential_ref
        # 都是更具体的指定，优先级更高）。
        assignment_ref: str | None = None
        assignment_model: str | None = None
        if self.usage_assignment is not None and not (
            (control is not None and control.credential_ref)
            or legacy_key
            or self.definition.credential_ref
        ):
            try:
                assigned = self.usage_assignment()
            except UsageUnavailable as exc:
                # 显式指定不可用：如实失败，不静默换用别的 Key 或别的模型。
                return ToolResult(
                    ok=False,
                    error=f"子任务用途不可用（{exc.reason_code}）：{exc.message}",
                    code="credential_unavailable",
                    status="failed",
                )
            if assigned is not None:
                assignment_ref, assignment_model = assigned
        try:
            choice = credential_selection.resolve_credential_choice(
                self.credentials,
                credential_ref=(
                    (control.credential_ref if control is not None else None)
                    or assignment_ref
                ),
                credential_key=legacy_key,
                purpose="subagent",
                fallback_ref=self.definition.credential_ref,
            )
            model = credential_selection.choose_model(
                choice,
                (control.model if control is not None else None) or assignment_model,
            )
        except credential_selection.CredentialSelectionError as exc:
            return ToolResult(
                ok=False,
                error=str(exc),
                code="credential_unavailable",
                status="failed",
            )

        # 契约 §2.3/§2.4：子智能体只拿到父级 grant 的**子集**，深度只有一层；
        # 创建本身走统一授权入口；超出父级范围由主智能体统一向用户申请（单层），
        # 子智能体自己绝不弹窗。scope 先算出来：批准后的执行器要按同一个子集重建。
        scope = None
        delegation_facts: dict[str, Any] | None = None
        scope_requests: list[dict] = []
        if self.security is not None:
            scope = await security_gate.create_child_scope(
                self.security, purpose=self.definition.description
            )
            if scope is None:
                return ToolResult(
                    ok=False,
                    error=(
                        "子智能体不能继续扩权：当前上下文无法生成父授权的子集"
                        "（或已超过允许的深度），本次没有创建。"
                    ),
                    category="permission",
                    recoverable=False,
                )
            # R9-H13 §3.H（F6）：依据当前有效要求、模式与已核实授权形成明确子集。
            # 没有可委派事实时不阻止创建（研究型子任务不需要范围），但形成结构化
            # 申请状态；子智能体运行中权限不足时用 request_agent_scope 向主智能体
            # 申请，主智能体也没有权限时才请求用户（不直接弹用户窗）。
            scope, delegation_context = self._prepare_scope(scope)
            delegation_facts, scope_requests = self._scope_delegation_facts(
                scope, delegation_context
            )
            effect = security_gate.subagent_effect(
                self.name, definition_name=self.definition.name
            )
            decision = await security_gate.authorize(self.security, effect)
            verdict = security_gate.verdict_of(decision)
            if verdict == "deny":
                return security_gate.denied_result(
                    decision, "当前模式不允许创建子智能体"
                )
            if verdict == "approve":
                operation = security_gate.build_pending_operation(
                    kind="subagent.create",
                    label=f"创建子智能体 {self.name}",
                    payload={
                        "tool": self.name,
                        "definition": self.definition.name,
                        # 审批执行器要用的完整执行参数（可序列化、可恢复）：
                        "arguments": dict(business),
                        "control": control.model_dump() if control is not None else None,
                        "credential_ref": choice["credential_ref"],
                        "model": model,
                        "agent_scope": _scope_to_payload(scope),
                    },
                    effect=effect,
                    fingerprint={
                        "tool": self.name,
                        "definition": self.definition.name,
                        "arguments": dict(business),
                    },
                    purpose="创建子智能体（能力不超过父级授权范围）",
                    mode_at_request=security_gate.mode_of(self.security),
                    instruction=security_gate.instruction_snapshot(self.security),
                )
                ticket = await security_gate.submit_pending(self.security, operation)
                if ticket is not None:
                    return security_gate.pending_result(
                        ticket, operation_label=f"创建子智能体 {self.name}"
                    )
                return ToolResult(
                    ok=False,
                    error=(
                        "创建子智能体需要你的批准，但当前没有可用的审批执行器："
                        "本次没有创建（执行边界暂不可用）。"
                    ),
                    category="permission",
                    recoverable=False,
                )
        adapter = await self.adapter_factory(choice["credential_ref"], model)
        if adapter is None:
            return ToolResult(ok=False, error="为子任务构建适配器失败")
        budget = _merged_budget(self.definition.subagent_budget, control)
        origin = self._origin()
        task_id = f"task_{uuid.uuid4().hex[:12]}"
        self.task_manager.submit(
            self.name,
            lambda: self._execute(
                task_id, adapter, business, control, budget, choice, scope
            ),
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
                "agent_scope": (
                    delegation_facts
                    if delegation_facts is not None
                    else security_gate.scope_summary(scope)
                ),
                "scope_requests": scope_requests,
                "next": (
                    "需要结果时调用 await_task(task_id=..., notify=true) 取回；"
                    "结果较长时用 read_task_result 分段读取全文。"
                ),
            },
            protocol_version=TOOL_RESULT_PROTOCOL_VERSION,
            status="queued",
            code="queued",
        )

    async def execute_after_approval(self, payload: dict[str, Any]) -> ToolResult:
        """审批执行器入口（契约 §5.3）：按审批单参数 + AgentScope 子集重建并执行。

        审批已由用户完成（审批执行器做过模式/对象重核对）；这里不重新申请审批，
        也不扩大范围 —— 用的是提交审批时快照的同一个能力子集。
        """
        if self.adapter_factory is None:
            return ToolResult(ok=False, error="子任务没有可用的适配器工厂")
        try:
            control = (
                SubtaskControl.model_validate(payload.get("control"))
                if payload.get("control")
                else None
            )
        except ValidationError as exc:
            return ToolResult(
                ok=False,
                error=f"审批单里的子任务控制字段不合法：{exc}",
                status="failed",
            )
        business = dict(payload.get("arguments") or {})
        ref = str(payload.get("credential_ref") or "")
        model = payload.get("model")
        if not ref:
            return ToolResult(
                ok=False,
                error="审批单缺少凭据引用，无法重建子智能体",
                status="failed",
            )
        adapter = await self.adapter_factory(ref, model)
        if adapter is None:
            return ToolResult(ok=False, error="为子任务构建适配器失败", status="failed")
        scope = _scope_from_payload(payload.get("agent_scope"))
        budget = _merged_budget(self.definition.subagent_budget, control)
        task_id = f"task_{uuid.uuid4().hex[:12]}"
        result = await self._execute(
            task_id,
            adapter,
            business,
            control,
            budget,
            {"credential_ref": ref, "model": model},
            scope,
        )
        return result

    async def _execute(
        self,
        task_id: str,
        adapter,
        business: dict[str, Any],
        control: SubtaskControl | None,
        budget: SubagentBudget,
        choice: dict[str, Any],
        scope=None,
    ) -> ToolResult:
        tid = f"subagent:{task_id}"
        tracer = None
        if self.trace_store is not None:
            from agent.trace.recorder import TurnTracer

            tracer = TurnTracer(self.trace_store, tid)
            self.trace_store.begin(tid)
        try:
            # 子智能体循环内创建的所有 effect 都带这个 AgentScope（契约 §2.4）。
            with security_gate.agent_scope_context(scope):
                result = await self._run_subagent(
                    task_id, adapter, business, control, budget, choice, tid, tracer
                )
        except Exception:
            if self.trace_store is not None:
                self.trace_store.finish(tid, "failed")
            raise
        if self.trace_store is not None:
            self.trace_store.finish(tid, "done", final_preview=result.content or "")
        if scope is not None:
            # R9-H13 §3.H（F6）：子任务结果必须带结构化委派事实与申请状态
            # （哪个动作/对象被允许、要求版本、撤销/重试状态），不是一句文字。
            context = self._verified_context() if self._ledger_available() else None
            scope = self._enrich_scope(scope, context)
            facts, requests = self._scope_delegation_facts(scope, context)
            result.data = {
                **(result.data or {}),
                "agent_scope": facts,
                "scope_requests": requests,
            }
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
        # 契约 §3.H（F6）：子智能体权限不足时先向主智能体提交结构化范围申请
        # （不直接请求用户）；由主智能体统一核对当前要求/模式/已核实授权。
        if security_gate.current_agent_scope() is not None:
            sub_registry.register(
                RequestAgentScopeTool(
                    security=self.security, purpose=self.definition.description
                )
            )
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
