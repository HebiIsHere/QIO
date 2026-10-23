"""R9-G 统一"实际操作开始前"重核对（契约 §7）。

每个已接入的执行入口（文件工具、命令、自建工具、子智能体、批量子项、模型审查返回、
人工批准恢复、后台继续、重启恢复）在**真正开始**一项操作前，调用这里的
`RecheckService.recheck()`：

1. 绑定的具体用户要求仍存在、status=active 且版本一致；
2. 用**当前模式**与绑定要求重新走统一引擎（固定规则 / 固定查询 / 模型审查复用 /
   永久删除政策 / 审批政策），不是只比较"是否切到只读"；
3. 操作指纹（对象身份/程序/argv/cwd/脚本摘要/接收方/模式）与审查/批准时一致；
4. 绑定的内容/前置状态摘要仍一致；
5. 条件证据仍成立、委派范围仍有效；
6. 通过后才返回 ok，调用方必须立即开始该操作，不得再长期排队。

本模块不替代引擎判定，也不新增运行期沙箱；它只把"最新事实"重新核对一遍。
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class OperationBinding:
    """审查/批准/执行共用的同一份操作绑定。

    `fingerprint` 由 review.operation_fingerprint 计算（含对象身份/程序/argv/cwd/
    脚本摘要/接收方/模式）；`content_digests` 是 (对象路径, 内容摘要) 的有序集合；
    `conditions` 是结构化条件与证据；`authorization_id` 可为 authorization_record_id
    或 approval_id，由调用方在需要时核对。
    """

    requirement: Any  # RequirementBinding（requirement_id + version）
    mode: str = ""
    operation_kind: str = ""
    fingerprint: str = ""
    spec_digest: str | None = None
    content_digests: tuple[tuple[str, str], ...] = ()
    conditions: tuple[dict, ...] = ()
    authorization_id: str | None = None
    delegation: dict | None = None
    approvals: tuple[str, ...] = ()


@dataclass(frozen=True)
class RecheckResult:
    ok: bool
    code: str = ""
    reason: str = ""
    decision: Any = None  # 重新判定得到的 AuthDecision（ok=False 时可用于展示原因）
    refreshed: OperationBinding | None = None


def _sha256_text(text: str) -> str:
    return hashlib.sha256(str(text).encode("utf-8")).hexdigest()


def content_digest(path: str | Path) -> str | None:
    """内容摘要（不落明文；读取失败返回 None，调用方按"无法确认"处理）。"""
    try:
        from agent.tools import security_gate

        value = security_gate.content_sha256(path)
        return str(value) if value else None
    except Exception:  # noqa: BLE001 - 读不到摘要不能当作一致
        return None


class RecheckService:
    """把"要求仍有效 + 当前模式仍允许 + 指纹/内容/条件仍一致"统一核对一遍。"""

    def __init__(
        self,
        *,
        engine: Any,
        requirements: Any,
        approvals: Any = None,
        authorizations: Any = None,
        spec_digest_provider: Callable[[Any], str | None] | None = None,
    ) -> None:
        self.engine = engine
        self.requirements = requirements
        self.approvals = approvals
        self.authorizations = authorizations
        self.spec_digest_provider = spec_digest_provider

    # -- 公共入口 ---------------------------------------------------------

    async def recheck(
        self,
        binding: OperationBinding | None,
        effect: Any,
        *,
        context: Any = None,
        approval_id: str | None = None,
    ) -> RecheckResult:
        if binding is None:
            return self._fail("no_binding", "这次操作没有绑定具体用户要求，不能直接执行（需要重新判断）")
        requirement = self._bound_requirement(binding)
        if requirement is None:
            return self._fail(
                "requirement_invalid",
                "这条操作依据的用户要求已不存在、已失效、已被修正或版本不一致；本次没有执行，需要重新判断",
            )
        # 1) 当前模式 + 绑定的要求重新走统一引擎（固定规则/审查复用/审批规则）。
        try:
            from agent.security.gateway import AuthContext

            # 重建 AuthContext 时必须带上已持久化的限制/解析状态（R9-H §6）：
            # 不能因为重新构造上下文而丢失用户限制或把 legacy 当成“无限制”。
            requirement_state = str(
                getattr(requirement, "structured_limits_state", "structured")
                or "structured"
            )
            requirement_limits = tuple(
                item
                for item in (getattr(requirement, "structured_limits", None) or ())
                if isinstance(item, dict)
            )
            ctx = AuthContext(
                requirement=getattr(binding, "requirement", None),
                origin=str(getattr(context, "origin", "user") if context is not None else "user"),
                extra={
                    "resolution_limits": (
                        requirement_limits if requirement_state == "structured" else ()
                    ),
                    "requirement_resolution_status": (
                        "resolved" if requirement_state == "structured" else "unavailable"
                    ),
                },
            )
            decision = await self.engine.authorize(effect, ctx=ctx)
        except Exception as exc:  # noqa: BLE001 - 重核对失败不能默认允许
            logger.warning("执行前重核对判定失败：%s: %s", type(exc).__name__, exc)
            return self._fail("recheck_failed", "执行前无法重新确认授权（本次没有执行）")
        verdict = getattr(getattr(decision, "verdict", None), "value", None) or str(
            getattr(decision, "verdict", "")
        )
        if verdict == "deny":
            return RecheckResult(
                ok=False,
                code="denied",
                reason=str(getattr(decision, "reason", "") or "当前规则不允许这次操作"),
                decision=decision,
            )
        if verdict != "allow":
            # approve：需要用户重新决策/确认；不是"继续执行"。
            return RecheckResult(
                ok=False,
                code="needs_confirmation",
                reason=str(getattr(decision, "reason", "") or "这次操作需要重新确认"),
                decision=decision,
            )
        # 2) 操作指纹：对象/程序/参数/cwd/脚本内容/接收方/模式与审查/批准时一致。
        expected = str(getattr(binding, "fingerprint", "") or "")
        actual = self._effect_fingerprint(effect)
        if expected and actual and expected != actual:
            return self._fail(
                "operation_changed",
                "实际操作内容/对象/程序/参数与审查或批准时不一致；旧结论不能继续使用",
            )
        # 3) 内容/前置状态摘要仍一致。
        for path, digest in getattr(binding, "content_digests", ()) or ():
            if not digest:
                continue
            current = content_digest(path)
            if current is None or str(current) != str(digest):
                return self._fail(
                    "content_changed",
                    "对象内容或前置状态在审查/批准后发生变化；本次没有执行，需要重新判断",
                )
        # 4) 启动类 spec_digest（若调用方/执行层提供）。
        expected_spec = str(getattr(binding, "spec_digest", "") or "")
        if expected_spec:
            actual_spec = self._effect_spec_digest(effect)
            if actual_spec is not None and str(actual_spec) != expected_spec:
                return self._fail(
                    "spec_changed",
                    "程序/脚本内容或参数在审查/批准后发生变化；旧结论不能继续使用",
                )
        # 5) 条件证据：没有可靠证据不能声称条件已满足。
        for condition in getattr(binding, "conditions", ()) or ():
            if not isinstance(condition, dict):
                continue
            if condition.get("satisfied") is not True:
                return self._fail(
                    "condition_unmet",
                    "条件执行的前提尚未被可靠证据确认；本次没有执行",
                )
            if not str(condition.get("evidence") or "").strip():
                return self._fail(
                    "condition_evidence_missing",
                    "条件执行缺少可靠证据；本次没有执行",
                )
        # 6) 人工审批：若绑定了审批单，审批必须仍然有效且未过期/未拒绝。
        approval = str(getattr(binding, "authorization_id", "") or approval_id or "")
        if approval and self.approvals is not None:
            state = self._approval_state(approval)
            if state is not None and state not in ("approved", "executing", "completed"):
                return self._fail(
                    "approval_invalid",
                    "这条操作对应的人工审批已过期、被拒绝或已失效；本次没有执行",
                )
        return RecheckResult(ok=True, code="ok", reason="执行前重核对通过", decision=decision, refreshed=binding)

    # -- 内部 -------------------------------------------------------------

    def _bound_requirement(self, binding: OperationBinding) -> Any:
        reference = getattr(binding, "requirement", None)
        requirement_id = getattr(reference, "requirement_id", None)
        version = getattr(reference, "version", None)
        if not requirement_id and isinstance(reference, str):
            requirement_id = reference
        if not requirement_id or self.requirements is None:
            return None
        try:
            requirement = self.requirements.by_id(str(requirement_id))
        except Exception:  # noqa: BLE001
            return None
        if requirement is None or str(getattr(requirement, "status", "")) != "active":
            return None
        if version is not None and int(getattr(requirement, "version", 1) or 1) != int(version):
            return None
        return requirement

    def _effect_fingerprint(self, effect: Any) -> str:
        try:
            from agent.security.review import operation_fingerprint

            operation = self.engine._review_operation(effect)
            mode = str(
                getattr(self.engine.mode(), "value", self.engine.mode())
            )
            return str(operation_fingerprint(operation, mode=mode))
        except Exception:  # noqa: BLE001
            return ""

    def _effect_spec_digest(self, effect: Any) -> str | None:
        detail = getattr(effect, "detail", None) or {}
        for key in ("spec_digest", "launch_spec_digest"):
            value = detail.get(key)
            if value:
                return str(value)
        provider = self.spec_digest_provider
        if callable(provider):
            try:
                return provider(effect)
            except Exception:  # noqa: BLE001
                return None
        return None

    def _approval_state(self, approval_id: str) -> str | None:
        loader = getattr(self.approvals, "load", None) or getattr(
            self.approvals, "get", None
        )
        if not callable(loader):
            return None
        try:
            item = loader(approval_id)
        except Exception:  # noqa: BLE001
            return None
        if item is None:
            return None
        if isinstance(item, dict):
            return str(item.get("state") or item.get("status") or "") or None
        return str(getattr(item, "state", "") or "") or None

    @staticmethod
    def _fail(code: str, reason: str) -> RecheckResult:
        return RecheckResult(ok=False, code=code, reason=reason)
