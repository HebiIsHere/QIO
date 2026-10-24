"""可恢复操作与审批票据（契约 §5.3）。

工具/执行器在需要审批时构造 PendingOperation（真正要执行什么 + 对象指纹 +
授权依据），审批服务把它持久化并立即返回 ApprovalTicket —— 工具返回 pending 结果，
本轮正常结束；用户批准后由后台执行器按 kind 从注册表取出执行函数真正执行。

执行函数签名：async fn(payload: dict, ctx: ExecutionContext) -> ExecResult。
每个操作在真实执行处必须再次走引擎的模式快照与对象重核对（ctx 提供）。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable


def operation_digest(operation: "PendingOperation") -> str:
    """审批单操作内容的独立摘要（防"批准后 operation_detail 被改动"）。

    只覆盖会改变实际执行的字段（动作/参数/效果/要求绑定/确认次数/来源），不含
    result/error 等执行后字段。提交时写入 fingerprint 列（独立于 operation 列），
    批准/领取/执行前用同一函数重算比对。
    """
    fingerprint_without_digest = {
        str(key): value
        for key, value in dict(operation.fingerprint or {}).items()
        if key != "operation_digest"
    }
    payload = {
        "kind": operation.kind,
        "label": operation.label,
        "payload": operation.payload,
        "effect": operation.effect,
        "fingerprint": fingerprint_without_digest,
        "instruction_id": operation.instruction_id,
        "instruction_text": operation.instruction_text,
        "purpose": operation.purpose,
        "mode_at_request": operation.mode_at_request,
        "confirmations_required": int(operation.confirmations_required or 1),
        "source": operation.source,
        "turn_id": operation.turn_id,
        "session_id": operation.session_id,
        "requirement_id": operation.requirement_id,
        "requirement_version": operation.requirement_version,
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


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
    #: 审批原始截止时间（重启/排队后仍按它核对，不续期）。
    expires_at: str | None = None
    #: 已完成的连续确认次数（内部运行数据为 3）；执行前重核对必须看到它。
    confirmed_stage: int = 0
    #: R9-H4 §10：本次执行前重核对的依据（模式/要求/内容/范围/条件是否一致），
    #: 由 authorize_started 写入，执行器/审批结果/审计共同引用（不是重新生成授权）。
    authorization_basis: dict = field(default_factory=dict)

    def replay_basis(self) -> dict:
        """本次重放要完整传递的「原审批快照 + 操作依据」。

        结束进程/程序/脚本等审批执行器必须使用它核对「本次执行 == 被批准的同一操作」；
        不得因为拿不到快照就一律报「没有审批记录」。
        """
        op = self.operation
        return {
            "approval_id": str(self.approval_id or ""),
            "kind": str(op.kind or ""),
            "label": str(op.label or ""),
            "payload": dict(op.payload or {}),
            "effect": dict(op.effect or {}),
            "fingerprint": dict(op.fingerprint or {}),
            "instruction_id": op.instruction_id,
            "instruction_text": op.instruction_text,
            "purpose": str(op.purpose or ""),
            "mode_at_request": str(self.mode_at_request or op.mode_at_request or ""),
            "mode_at_approval": str(self.mode_at_approval or ""),
            "requirement_id": op.requirement_id,
            "requirement_version": op.requirement_version,
            "confirmations_required": int(op.confirmations_required or 1),
            "confirmed_stage": int(self.confirmed_stage or 0),
            "expires_at": self.expires_at,
            "source": str(op.source or "tool"),
            "turn_id": op.turn_id,
            "session_id": op.session_id,
        }

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

    async def authorize_started(self, effect=None, *, common_only: bool = False) -> tuple[bool, str]:
        """真实执行前重核对：当前模式 + 绑定要求 + 审批/确认是否仍允许这次操作。

        返回 (是否允许, 中文原因)。批准后若模式变化、要求被修正/撤回、授权/审批失效，
        不得执行。没有绑定具体用户要求的审批单不能直接执行（需要重新判断）。
        """
        if self.engine is None:
            return True, ""
        if self.expires_at:
            try:
                from datetime import datetime, timezone

                deadline = datetime.fromisoformat(str(self.expires_at))
                if deadline.tzinfo is None:
                    deadline = deadline.replace(tzinfo=timezone.utc)
                if datetime.now(timezone.utc) > deadline:
                    return False, "这条审批已超过原始有效期（不会重新获得 10 分钟），本次没有执行"
            except (TypeError, ValueError):
                # 截止时间读不了：不能当作无限期有效。
                return False, "审批截止时间无法核对，本次没有执行（需要重新提出）"
        binding = self.requirement_binding()
        if binding is None:
            return (
                False,
                "这项操作没有绑定具体用户要求，不能直接执行（需要重新判断，不是继续执行）",
            )
        # 模式是整批共同条件：批量 common_only 先核对；单次审批仍由引擎
        # recheck_approved 按模式给出精确拒绝原因（既有契约）。
        if common_only and self.mode_at_approval:
            try:
                current_mode = str(self.current_mode() or "")
            except Exception:  # noqa: BLE001 - 读不到模式按不能执行
                return False, "执行前无法确认当前权限模式，本次没有执行"
            if current_mode and current_mode != str(self.mode_at_approval):
                return (
                    False,
                    "权限模式已从审批时的 "
                    + str(self.mode_at_approval)
                    + " 变为 "
                    + current_mode
                    + "；旧审批不能继续执行",
                )
        # R9-H4 §5/§10.1：绑定要求在该时刻必须仍 active 且版本一致。若已被撤回/修正/
        # 替换，不能因为引擎「没有可对照要求」而落到更宽松的分支（尤其 unrestricted）。
        try:
            from agent.security.gateway import AuthContext as _AuthContext

            resolver = getattr(self.engine, "_requirement_for", None)
            if callable(resolver):
                resolved_requirement = resolver(
                    _AuthContext(requirement=binding, origin="background")
                )
                if resolved_requirement is None:
                    return (
                        False,
                        "审批单绑定的用户要求已失效（被撤回/修正或版本不一致），"
                        "本次没有执行（需要重新判断，不是继续执行）",
                    )
        except Exception:  # noqa: BLE001 - 核对失败按不能执行（保守）
            return False, "执行前无法核对审批绑定的用户要求，本次没有执行"
        try:
            from agent.security.gateway import AuthContext
            from agent.tools import security_gate

            effective_effect = effect
            if effective_effect is None:
                effective_effect = security_gate.effect_from_dict(self.operation.effect)
            if effective_effect is None:
                return False, "审批单缺少可核对的执行效果，本次没有执行"
            if common_only:
                # 批量共同条件已在上方核对（期限/绑定要求 active/版本）；逐项限制/
                # 内容由每个子项 effect 在执行前单独 recheck，不能整批一起失效。
                self.authorization_basis = {
                    "approval_id": str(self.approval_id or ""),
                    "operation_kind": str(self.operation.kind or ""),
                    "requirement_id": self.operation.requirement_id,
                    "requirement_version": self.operation.requirement_version,
                    "mode": self.current_mode(),
                    "confirmed_stage": int(self.confirmed_stage or 0),
                    "confirmations_required": int(self.operation.confirmations_required or 1),
                    "kind": "batch_common_precheck",
                }
                return True, ""
            recheck = getattr(self.engine, "recheck_approved", None)
            if callable(recheck):
                decision = recheck(
                    effective_effect,
                    context=AuthContext(requirement=binding, origin="background"),
                    approval_kind=str(self.operation.kind or ""),
                    confirmations_required=int(
                        self.operation.confirmations_required or 1
                    ),
                    confirmed_stage=int(self.confirmed_stage or 0),
                )
            else:  # 兼容旧引擎：只做模式判定
                decision = await self.engine.authorize(
                    effective_effect, ctx=AuthContext(requirement=binding, origin="background")
                )
        except Exception as exc:  # noqa: BLE001 - 重核对失败不能默认允许
            return False, "执行前无法重新确认授权（本次没有执行）：" + type(exc).__name__
        verdict = getattr(getattr(decision, "verdict", None), "value", None) or str(
            getattr(decision, "verdict", "")
        )
        reason = str(getattr(decision, "reason", "") or "当前状态不再允许这次操作")
        decision_code = str(getattr(decision, "decision_code", "") or "")
        basis_common = {
            "approval_id": str(self.approval_id or ""),
            "operation_kind": str(self.operation.kind or ""),
            "requirement_id": self.operation.requirement_id,
            "requirement_version": self.operation.requirement_version,
            "mode": self.current_mode(),
            "confirmed_stage": int(self.confirmed_stage or 0),
            "confirmations_required": int(self.operation.confirmations_required or 1),
            "reason": reason,
            "decision_code": decision_code or None,
        }
        if verdict == "allow":
            self.authorization_basis = {
                **basis_common,
                "kind": "recheck_allow",
                "does_not_grant_long_term": True,
            }
            return True, ""
        # R9-H4 §10.1：unresolved 下用户对**这个具体操作**的有效批准可以完成本次操作。
        # recheck_approved 已先核对最新已知限制（命中即 DENY，走不到这里）；这里的
        # APPROVE 仅表示「历史结构化限制未可靠恢复」这一状态本身，审批单已绑定同一条
        # 要求/版本/内容/范围/条件。因此只放行这一次批准，不改写要求、不清除未知限制、
        # 不解除已知禁止、不形成长期授权；下一次操作仍需重新判断。
        if verdict == "approve" and self._approve_completes_unresolved(decision_code):
            self.authorization_basis = {
                **basis_common,
                "kind": "specific_approval_completed_unresolved",
                "known_limits_checked": True,
                "unresolved_requirement_kept": True,
                "does_not_clear_unknown_limits": True,
                "does_not_lift_known_prohibitions": True,
                "does_not_grant_long_term": True,
            }
            return True, ""
        return False, reason

    def _approve_completes_unresolved(self, decision_code: str) -> bool:
        """只有结构化 code 表示「unresolved 限制需确认本次操作」时才允许完成本次批准。

        绝不解析中文 reason、语言或关键词；旧审批没有 code 一律不能据此放行。
        还必须绑定 active/版本一致且当前确实 legacy/unresolved（结构化事实核对）。
        """
        from agent.security.effects import DECISION_UNRESOLVED_LIMITS

        if str(decision_code or "") != DECISION_UNRESOLVED_LIMITS:
            return False
        binding = self.requirement_binding()
        if binding is None:
            return False
        requirement = None
        try:
            from agent.security.gateway import AuthContext

            getter = getattr(self.engine, "_requirement_for", None)
            if callable(getter):
                requirement = getter(
                    AuthContext(requirement=binding, origin="background")
                )
        except Exception:  # noqa: BLE001 - 读不到按不能放行（保守）
            requirement = None
        if requirement is None:
            return False
        if str(getattr(requirement, "status", "")) != "active":
            return False
        try:
            if int(getattr(requirement, "version", 1) or 1) != int(binding.version):
                return False
        except (TypeError, ValueError):
            return False
        state = str(
            getattr(requirement, "structured_limits_state", "unresolved")
            or "unresolved"
        )
        return state in {"legacy", "unresolved"}


def replay_gateway_kwargs(ctx: ExecutionContext) -> dict:
    """Lead 接线帮助：把「原审批快照 + 操作依据」原样交给审批重放网关。

    approved_operations.execute_proc_kill 等执行器应使用：

        _ApprovedReplayGateway(security, **replay_gateway_kwargs(ctx))

    而不是只传 security（那会因为没有 operation 快照一律报「没有审批记录」）。
    """
    return {
        "operation": ctx.operation,
        "approval_id": str(getattr(ctx, "approval_id", "") or ""),
    }


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
