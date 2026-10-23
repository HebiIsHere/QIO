"""可恢复操作与审批票据（契约 §5.3）。

工具/执行器在需要审批时构造 PendingOperation（真正要执行什么 + 对象指纹 +
授权依据），审批服务把它持久化并立即返回 ApprovalTicket —— 工具返回 pending 结果，
本轮正常结束；用户批准后由后台执行器按 kind 从注册表取出执行函数真正执行。

执行函数签名：async fn(payload: dict, ctx: ExecutionContext) -> ExecResult。
每个操作在真实执行处必须再次走引擎的模式快照与对象重核对（ctx 提供）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable


@dataclass
class PendingOperation:
    kind: str  # "fs.write" | "fs.patch" | "fs.delete" | "proc.run_program" | "proc.shell"
    #           | "dep.install" | "net.egress" | "internal.data_write" | "tool.invoke"
    label: str
    payload: dict = field(default_factory=dict)  # 可序列化执行参数
    effect: dict = field(default_factory=dict)  # 效果摘要（kind/targets/purpose）
    fingerprint: dict = field(default_factory=dict)  # 对象身份/内容摘要，执行前重核对
    instruction_id: str | None = None
    instruction_text: str | None = None
    purpose: str = ""
    mode_at_request: str = ""
    confirmations_required: int = 1  # INTERNAL_DATA 为 3
    source: str = "tool"  # tool | subagent | background | user
    turn_id: str | None = None
    session_id: str | None = None
    #: R9-G：这条待执行操作依据的具体用户要求及版本（执行前必须核对）。
    requirement_id: str | None = None
    requirement_version: int | None = None

    def as_dict(self) -> dict:
        return {
            "kind": self.kind,
            "label": self.label,
            "payload": self.payload,
            "effect": self.effect,
            "fingerprint": self.fingerprint,
            "instruction_id": self.instruction_id,
            "instruction_text": self.instruction_text,
            "purpose": self.purpose,
            "mode_at_request": self.mode_at_request,
            "confirmations_required": int(self.confirmations_required or 1),
            "source": self.source,
            "turn_id": self.turn_id,
            "session_id": self.session_id,
            "requirement_id": self.requirement_id,
            "requirement_version": self.requirement_version,
        }

    @classmethod
    def from_dict(cls, data: dict | None) -> "PendingOperation":
        raw = dict(data or {})
        return cls(
            kind=str(raw.get("kind") or ""),
            label=str(raw.get("label") or ""),
            payload=dict(raw.get("payload") or {}),
            effect=dict(raw.get("effect") or {}),
            fingerprint=dict(raw.get("fingerprint") or {}),
            instruction_id=raw.get("instruction_id"),
            instruction_text=raw.get("instruction_text"),
            purpose=str(raw.get("purpose") or ""),
            mode_at_request=str(raw.get("mode_at_request") or ""),
            confirmations_required=int(raw.get("confirmations_required") or 1),
            source=str(raw.get("source") or "tool"),
            turn_id=raw.get("turn_id"),
            session_id=raw.get("session_id"),
            requirement_id=raw.get("requirement_id"),
            requirement_version=(
                int(raw["requirement_version"])
                if raw.get("requirement_version") not in (None, "")
                else None
            ),
        )

    def summary(self) -> dict:
        """给界面看的摘要（不含执行参数细节）。"""
        return {
            "kind": self.kind,
            "label": self.label,
            "purpose": self.purpose,
            "source": self.source,
            "confirmations_required": int(self.confirmations_required or 1),
            "requirement_id": self.requirement_id,
            "requirement_version": self.requirement_version,
        }


@dataclass
class ApprovalTicket:
    approval_id: str
    state: str = "waiting"
    expires_at: str = ""
    confirmations_required: int = 1
    revisions: int = 0

    def as_dict(self) -> dict:
        return {
            "approval_id": self.approval_id,
            "state": self.state,
            "expires_at": self.expires_at,
            "confirmations_required": self.confirmations_required,
            "revisions": self.revisions,
        }


@dataclass
class ExecResult:
    ok: bool
    summary: str = ""
    detail: dict = field(default_factory=dict)
    state: str = "completed"  # completed | failed | interrupted_unverified
    error: str | None = None

    def as_dict(self) -> dict:
        return {
            "ok": bool(self.ok),
            "summary": self.summary,
            "detail": self.detail,
            "state": self.state,
            "error": self.error,
        }


@dataclass
class ExecutionContext:
    """执行器拿到的上下文：审批身份 + 模式快照 + 引擎入口 + 对象指纹。"""

    approval_id: str
    operation: PendingOperation
    mode_at_request: str = ""
    mode_at_approval: str = ""
    attempt: int = 1
    engine: Any = None
    mode_getter: Callable[[], str] | None = None
    instruction: Any = None

    def current_mode(self) -> str:
        if self.mode_getter is None:
            return self.mode_at_approval or self.mode_at_request
        try:
            return str(self.mode_getter())
        except Exception:  # noqa: BLE001 - 模式读取失败不能伪造成功
            return self.mode_at_approval or self.mode_at_request

    def requirement_binding(self) -> Any:
        """审批单绑定的具体用户要求（id + version）；缺失时返回 None。"""
        if not self.operation.requirement_id:
            return None
        try:
            from agent.security.requirements import RequirementBinding

            return RequirementBinding(
                requirement_id=str(self.operation.requirement_id),
                version=int(self.operation.requirement_version or 1),
            )
        except Exception:  # noqa: BLE001 - 兼容 W1 分支尚未提供 dataclass 时
            from types import SimpleNamespace

            return SimpleNamespace(
                requirement_id=str(self.operation.requirement_id),
                version=int(self.operation.requirement_version or 1),
            )

    async def authorize_started(self) -> tuple[bool, str]:
        """真实执行前重核对：当前模式 + 绑定要求 + 审批/确认是否仍允许这次操作。

        返回 (是否允许, 中文原因)。批准后若模式变化、要求被修正/撤回、授权/审批失效，
        不得执行。没有绑定具体用户要求的审批单不能直接执行（需要重新判断）。
        """
        if self.engine is None:
            return True, ""
        binding = self.requirement_binding()
        if binding is None:
            return (
                False,
                "这项操作没有绑定具体用户要求，不能直接执行（需要重新判断，不是继续执行）",
            )
        try:
            from agent.security.gateway import AuthContext
            from agent.tools import security_gate

            effect = security_gate.effect_from_dict(self.operation.effect)
            if effect is None:
                return False, "审批单缺少可核对的执行效果，本次没有执行"
            decision = await self.engine.authorize(
                effect, ctx=AuthContext(requirement=binding, origin="background")
            )
        except Exception as exc:  # noqa: BLE001 - 重核对失败不能默认允许
            return False, "执行前无法重新确认授权（本次没有执行）：" + type(exc).__name__
        verdict = getattr(getattr(decision, "verdict", None), "value", None) or str(
            getattr(decision, "verdict", "")
        )
        if verdict == "allow":
            return True, ""
        return False, str(getattr(decision, "reason", "") or "当前状态不再允许这次操作")


OperationHandler = Callable[[dict, ExecutionContext], Awaitable[ExecResult]]


class OperationRegistry:
    """操作执行器注册表：register_operation(kind, fn) / get(kind)。"""

    def __init__(self) -> None:
        self._handlers: dict[str, OperationHandler] = {}

    def register(
        self, kind: str, handler: OperationHandler, *, replace: bool = False
    ) -> None:
        key = str(kind or "").strip()
        if not key:
            raise ValueError("operation kind 不能为空")
        if key in self._handlers and not replace:
            raise ValueError("operation 已注册: " + key)
        self._handlers[key] = handler

    def get(self, kind: str) -> OperationHandler | None:
        return self._handlers.get(str(kind or "").strip())

    def registered(self) -> list[str]:
        return sorted(self._handlers)

    async def execute(
        self, kind: str, payload: dict, ctx: ExecutionContext
    ) -> ExecResult:
        handler = self.get(kind)
        if handler is None:
            # 没有执行器就如实失败：绝不把「未执行」说成「已执行」。
            return ExecResult(
                ok=False,
                state="failed",
                summary="没有为这类操作注册执行器，本次未执行",
                error="executor_not_registered: " + str(kind or ""),
            )
        try:
            result = await handler(dict(payload or {}), ctx)
        except Exception as exc:  # noqa: BLE001 - 执行失败是审批结果的一种
            return ExecResult(
                ok=False,
                state="failed",
                summary="执行时出错，本次没有完成",
                error=f"{type(exc).__name__}: {str(exc)[:300]}",
            )
        if not isinstance(result, ExecResult):  # pragma: no cover - 防御
            return ExecResult(
                ok=False,
                state="failed",
                summary="执行器返回了非预期结果，本次按未完成处理",
                error=f"invalid_executor_result: {type(result).__name__}",
            )
        return result
