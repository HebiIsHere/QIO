"""统一授权入口协议（契约 §2.1）。

所有真实操作入口（文件工具、命令、脚本、自建工具、依赖安装、子智能体、子进程）
只能通过 AuthorizationGateway 取得判定与审批通道，不允许各自实现权限逻辑。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Sequence

from agent.security.effects import AuthDecision, Effect
from agent.security.modes import PermissionMode

if TYPE_CHECKING:  # pragma: no cover - 仅类型标注
    from agent.security.context import AgentScope
    from agent.security.instructions import InstructionContext
    from agent.security.operations import ApprovalTicket, PendingOperation


@dataclass(frozen=True)
class AuthContext:
    """一次判定的来源上下文（缺省从 ContextVar 取）。"""

    instruction: "InstructionContext | None" = None
    agent_scope: "AgentScope | None" = None
    origin: str = "user"  # user | background | subagent
    turn_id: str | None = None
    session_id: str | None = None
    extra: dict = field(default_factory=dict)


class AuthorizationGateway(ABC):
    """统一授权入口。工具只能走它。"""

    @abstractmethod
    def mode(self) -> PermissionMode:
        raise NotImplementedError

    @abstractmethod
    async def authorize(self, effect: Effect, *, ctx: AuthContext | None = None) -> AuthDecision:
        """判定一个操作效果。

        返回 ALLOW 后可执行；APPROVE 时调用方必须把操作交给 submit()（持久化 +
        立即返回 ticket），禁止在工具调用里同步等待用户点按钮占住后续回应能力；
        DENY 必须给出中文原因。
        """
        raise NotImplementedError

    @abstractmethod
    async def authorize_many(
        self, effects: Sequence[Effect], *, ctx: AuthContext | None = None
    ) -> AuthDecision:
        """多个 effect 合成一次判定：任一 DENY → DENY；否则任一 APPROVE → APPROVE。"""
        raise NotImplementedError

    @abstractmethod
    async def submit(
        self, operation: "PendingOperation", *, ctx: AuthContext | None = None
    ) -> "ApprovalTicket":
        """需要审批时提交待执行操作；立即返回 waiting 票据（不阻塞本轮）。"""
        raise NotImplementedError

    @abstractmethod
    def instruction(self) -> "InstructionContext | None":
        """当前指令上下文（授权依据）；没有则为 None。"""
        raise NotImplementedError

    # 便于测试与日志的只读描述。
    def describe(self) -> dict[str, Any]:  # pragma: no cover - 可选实现
        return {"mode": self.mode().value}
