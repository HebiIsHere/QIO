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


#: 本轮用户指令上下文（授权依据）。
current_instruction: ContextVar["InstructionContext | None"] = ContextVar(
    "qio_current_instruction", default=None
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
