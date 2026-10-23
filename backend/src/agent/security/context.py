"""授权上下文传播（契约 §2.4）。

* `current_instruction`：本轮用户指令（授权依据）。每轮由 turn 运行时设置，
  指令结束即关闭；绝不跨指令继承。
* `agent_scope`：子智能体能力子集（父 grant 子集 + 深度 + 用途），只能缩小，
  不能扩大；超出父级范围时由主智能体统一申请（单层）。
* `approval_origin`：审批来源，"user" | "background" | "subagent"。后台/子智能体
  产生的审批在界面上必须能说清来源，且不能冒充用户主动操作。
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Iterator

if TYPE_CHECKING:  # pragma: no cover - 仅用于类型标注，避免循环导入
    from agent.security.instructions import Grant, InstructionContext
    from agent.security.requirements import RequirementBinding


@dataclass(frozen=True)
class AgentScope:
    """子智能体能力子集：父 grant 的子集 + 深度 + 用途。

    子智能体的所有 effect 必须带 `agent_scope`；引擎评估时若 effect 超出父授权，
    不允许子智能体直接向用户弹窗，而是交给主智能体统一申请。
    """

    parent_instruction_id: str | None = None
    grants: tuple["Grant", ...] = ()
    depth: int = 0
    purpose: str = ""
    label: str = ""
    #: R9-G：子智能体继承父级的具体用户要求绑定（不得改绑/扩大）。
    requirement: object | None = None


#: 本轮用户指令上下文（授权依据）。
current_instruction: ContextVar["InstructionContext | None"] = ContextVar(
    "qio_current_instruction", default=None
)

#: 当前操作依据的具体用户要求绑定（R9-G 契约 §3）：requirement_id + version。
#: 由 turn 运行时在解析用户消息后设置；工具/引擎只使用这个绑定，**不再回退**
#: 到"数据库里最近一条 active 要求"。子智能体继承父绑定，不得改绑。
current_requirement: ContextVar["RequirementBinding | None"] = ContextVar(
    "qio_current_requirement", default=None
)

#: 本轮"用户要求关系解析"的结果状态（ok / unavailable）。解析不可用时不使用
#: 关键词兜底，但审查输入必须如实标注，避免把"没解析"当成"已确认"。
requirement_resolution_status: ContextVar[str] = ContextVar(
    "qio_requirement_resolution_status", default="ok"
)

#: 要求解析模型理解出的**结构化限制**（本轮生效，不落纯文本解析）。跨重启的
#: 持久化受当前 schema 限制（W1 报告已记录），历史纯文本 limits 不参与判定。
requirement_limits: ContextVar[tuple[dict, ...]] = ContextVar(
    "qio_requirement_limits", default=()
)

#: 当前子智能体能力子集。
agent_scope: ContextVar[AgentScope | None] = ContextVar("qio_agent_scope", default=None)

#: 审批来源：user / background / subagent。
approval_origin: ContextVar[str] = ContextVar("qio_approval_origin", default="user")

APPROVAL_ORIGINS = frozenset({"user", "background", "subagent"})


def current_origin() -> str:
    origin = str(approval_origin.get() or "user")
    return origin if origin in APPROVAL_ORIGINS else "user"


@contextmanager
def instruction_context(instruction: "InstructionContext | None") -> Iterator[None]:
    """在 with 块内把当前指令设为 *instruction*，退出时恢复（不关闭 grant）。"""
    token: Token = current_instruction.set(instruction)
    try:
        yield
    finally:
        current_instruction.reset(token)


@contextmanager
def requirement_context(binding: "RequirementBinding | None") -> Iterator[None]:
    """在 with 块内把"当前操作依据的用户要求绑定"设为 *binding*，退出时恢复。

    绑定只描述"这条操作依据哪条用户要求及哪个版本"；它本身不是许可，仍需
    引擎按当前模式、固定规则、模型审查/审批规则判定。
    """
    token: Token = current_requirement.set(binding)
    try:
        yield
    finally:
        current_requirement.reset(token)


@contextmanager
def requirement_resolution_context(status: str) -> Iterator[None]:
    token: Token = requirement_resolution_status.set(str(status or "ok"))
    try:
        yield
    finally:
        requirement_resolution_status.reset(token)


@contextmanager
def requirement_limits_context(limits: tuple[dict, ...] | list[dict] | None) -> Iterator[None]:
    token: Token = requirement_limits.set(tuple(limits or ()))
    try:
        yield
    finally:
        requirement_limits.reset(token)


@contextmanager
def agent_scope_context(scope: AgentScope | None) -> Iterator[None]:
    token: Token = agent_scope.set(scope)
    try:
        yield
    finally:
        agent_scope.reset(token)


@contextmanager
def approval_origin_context(origin: str) -> Iterator[None]:
    """显式设置审批来源；非法值按 user 处理（不伪装成后台来源）。"""
    value = origin if origin in APPROVAL_ORIGINS else "user"
    token: Token = approval_origin.set(value)
    try:
        yield
    finally:
        approval_origin.reset(token)
