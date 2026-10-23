"""三模式统一授权判定引擎（契约 §2.3）。

判定以**实际操作效果**为准，不用工具名/工作目录代替：

* read_only  —— 不能修改业务文件；读取默认允许（凭据红线除外）；
* restricted —— 范围外/未获指令授权的操作需要用户确认；
* unrestricted —— 普通操作不再加常规审批，但内部运行数据仍需三次确认。

所有分支返回中文原因（可直接展示）。判定结果同时写入轻量审计（audit.py）。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable, Sequence

from agent.security import internal_data
from agent.security.audit import AuthorizationAudit
from agent.security.context import (
    AgentScope,
    agent_scope as agent_scope_var,
    approval_origin,
    current_instruction,
    current_requirement,
    requirement_limits,
    requirement_resolution_status,
)
from agent.security.effects import (
    AuthDecision,
    Effect,
    EffectKind,
    MUTATING_EFFECTS,
    Target,
    Verdict,
)
from agent.security.gateway import AuthContext, AuthorizationGateway
from agent.security.instructions import (
    InstructionContext,
    OPERATION_EXECUTE,
    instruction_allows_path,
    operations_for,
)
from agent.security.modes import (
    DEFAULT_MODE,
    MODE_SETTING_KEY,
    PermissionMode,
    is_new_mode_value,
    normalize_mode,
)

logger = logging.getLogger(__name__)

_READ_ONLY_DENY = "只读模式下 QIO 不能修改业务文件"
_NEEDS_APPROVAL = "受限模式下这类操作需要你确认后才会执行"


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


class AuthorizationEngine(AuthorizationGateway):
    def __init__(
        self,
        *,
        conn: Any = None,
        approvals: Any = None,
        settings: Any = None,
        instance_id: str | None = None,
        workspace_root: Callable[[], Path] | None = None,
        internal_data_paths: Callable[[], list[Path]] | None = None,
        audit: AuthorizationAudit | None = None,
        # R9-F：受限模式的模型授权审查与后端文件来源（均可缺省，缺省时行为与 R9 一致）。
        requirements: Any = None,
        authorizations: Any = None,
        reviewer: Any = None,
        origin_provider: Callable[[Path], str | None] | None = None,
        prompt_version: str = "",
        # R9-H2 §5：内部数据真实对象身份登记（C 提供；缺省按路径兜底）。
        internal_data_identity_keys: Callable[[], object] | None = None,
        internal_data_known_paths: Callable[[], object] | None = None,
    ) -> None:
        self.conn = conn
        self.approvals = approvals
        self.settings = settings
        self.instance_id = instance_id
        self._workspace_root = workspace_root or (lambda: Path.cwd())
        self._internal_data_paths = internal_data_paths or (lambda: [])
        self.audit = audit or AuthorizationAudit()
        self.requirements = requirements
        self.authorizations = authorizations
        self.reviewer = reviewer
        self.origin_provider = origin_provider
        self.prompt_version = prompt_version
        self._internal_data_identity_keys = internal_data_identity_keys
        self._internal_data_known_paths = internal_data_known_paths

    # -- 模式：读取归一；写入只接受用户发起 ---------------------------------

    def mode(self) -> PermissionMode:
        raw = DEFAULT_MODE.value
        if self.settings is not None:
            try:
                raw = self.settings.get(MODE_SETTING_KEY, DEFAULT_MODE.value)
            except Exception:  # noqa: BLE001 - 读设置失败按默认（不提高权限）
                raw = DEFAULT_MODE.value
        return normalize_mode(raw)

    def set_mode(self, value: object, *, user_initiated: bool = False) -> PermissionMode:
        """切换权限模式。**只允许 API 层以 user_initiated=True 调用。**

        模型/工具/子智能体没有任何调用路径；非用户发起时抛 PermissionError。
        只接受新值（旧值只在读入时映射）。响应里的展示字段由 API 层组装。
        """
        if not user_initiated:
            raise PermissionError("只有用户自己可以切换权限模式")
        if not is_new_mode_value(value):
            raise ValueError("invalid_mode")
        mode = normalize_mode(value)
        if self.settings is None:
            raise RuntimeError("没有设置存储，无法持久化权限模式")
        from agent.storage.db import transaction

        if self.conn is not None:
            with transaction(self.conn):
                self.settings.set(MODE_SETTING_KEY, mode.value)
        else:
            self.settings.set(MODE_SETTING_KEY, mode.value)
        return mode

    # -- 供工具/SandboxExecutor 复用的小接口 ---------------------------------

    def internal_data_paths(self) -> list[Path]:
        """受保护路径清单（AppContext 提供）；工具/执行器用它识别内部运行数据。"""
        return list(self._internal_paths())

    def child_scope(
        self,
        *,
        purpose: str = "",
        grants: Sequence[Any] | None = None,
    ) -> AgentScope:
        """生成子智能体能力子集（父 grant 子集 + 深度 + 用途），只能缩小不能扩大。"""
        parent_instruction = current_instruction.get()
        parent_scope = agent_scope_var.get()
        if grants is not None:
            base = tuple(grants)
        elif parent_scope is not None:
            base = tuple(parent_scope.grants)
        elif parent_instruction is not None:
            base = tuple(parent_instruction.active_grants())
        else:
            base = ()
        depth = (parent_scope.depth + 1) if parent_scope is not None else 1
        return AgentScope(
            parent_instruction_id=(
                parent_scope.parent_instruction_id
                if parent_scope is not None
                else (parent_instruction.instruction_id if parent_instruction is not None else None)
            ),
            grants=base,
            depth=depth,
            purpose=str(purpose or ""),
            requirement=(
                parent_scope.requirement
                if parent_scope is not None
                else current_requirement.get()
            ),
        )

    # -- 判定 ---------------------------------------------------------------

    def instruction(self) -> InstructionContext | None:
        return current_instruction.get()

    def _context(self, ctx: AuthContext | None) -> AuthContext:
        if ctx is not None:
            return ctx
        instruction = current_instruction.get()
        scope = agent_scope_var.get()
        return AuthContext(
            instruction=instruction,
            agent_scope=scope,
            origin=str(approval_origin.get() or "user"),
            requirement=current_requirement.get(),
            extra={
                "requirement_resolution_status": str(
                    requirement_resolution_status.get() or "ok"
                ),
                "resolution_limits": tuple(requirement_limits.get() or ()),
            },
        )

    async def authorize(self, effect: Effect, *, ctx: AuthContext | None = None) -> AuthDecision:
        context = self._context(ctx)
        mode = self.mode()
        self._enrich_origin(effect)
        verdict, reason, scope_id = self._verdict(effect, context, mode)
        needs_review = False
        # R9-H3 §3：普通独立授权审查只在 restricted 模式。unrestricted 的
        # legacy/unresolved 未恢复部分由 _verdict 返回具体确认（APPROVE），
        # 不再调用 ModelAuthorizationReviewer（不重新引入独立授权审查）。
        if (
            mode == PermissionMode.RESTRICTED
            and verdict == Verdict.APPROVE
            and self._review_candidate(effect)
            and not self._permanent_delete_pending(effect)
        ):
            verdict, reason, scope_id, needs_review = await self._apply_review(
                effect,
                context,
                mode,
                reason,
                scope_id,
            )
        decision = AuthDecision(
            verdict=verdict,
            reason=reason,
            approval_id=None,
            effects=(effect,),
            scope_id=scope_id,
            needs_review=needs_review,
        )
        instruction = context.instruction or current_instruction.get()
        self.audit.record(
            effect=effect,
            decision=decision,
            mode=mode.value,
            instruction_id=instruction.instruction_id if instruction is not None else None,
            detail={
                "origin": context.origin,
                "needs_review": needs_review,
                "requirement_id": getattr(
                    getattr(context, "requirement", None), "requirement_id", None
                ),
                "requirement_version": getattr(
                    getattr(context, "requirement", None), "version", None
                ),
            },
        )
        return decision

    async def authorize_many(
        self, effects: Sequence[Effect], *, ctx: AuthContext | None = None
    ) -> AuthDecision:
        context = self._context(ctx)
        tuples = list(effects)
        if not tuples:
            return AuthDecision(verdict=Verdict.ALLOW, reason="没有需要执行的效果", approval_id=None)
        mode = self.mode()
        denies: list[str] = []
        approves: list[str] = []
        scope_id: str | None = None
        for effect in tuples:
            self._enrich_origin(effect)
            verdict, reason, hit_scope = self._verdict(effect, context, mode)
            # 与 authorize 同一顺序：unrestricted 不调用普通独立授权审查。
            if (
                mode == PermissionMode.RESTRICTED
                and verdict == Verdict.APPROVE
                and self._review_candidate(effect)
            ):
                verdict, reason, hit_scope, _ = await self._apply_review(
                    effect,
                    context,
                    mode,
                    reason,
                    hit_scope,
                )
            scope_id = scope_id or hit_scope
            if verdict == Verdict.DENY:
                denies.append(reason)
            elif verdict == Verdict.APPROVE:
                approves.append(reason)
        if denies:
            verdict = Verdict.DENY
            reason = denies[0]
        elif approves:
            verdict = Verdict.APPROVE
            reason = approves[0]
        else:
            verdict = Verdict.ALLOW
            reason = "在当前权限模式与你的指令范围内，可以执行"
        decision = AuthDecision(
            verdict=verdict,
            reason=reason,
            approval_id=None,
            effects=tuple(tuples),
            scope_id=scope_id,
        )
        instruction = context.instruction or current_instruction.get()
        self.audit.record(
            effect=tuples[0],
            decision=decision,
            mode=mode.value,
            instruction_id=instruction.instruction_id if instruction is not None else None,
            detail={"origin": context.origin, "count": len(tuples)},
        )
        return decision

    async def authorize_batch(
        self, effects: Sequence[Effect], *, ctx: AuthContext | None = None
    ) -> list[AuthDecision]:
        """批量操作逐项判定（R9-G 契约 §6）：每项独立得到结论，绝不用一项覆盖整批。

        调用方（文件/附件等批量入口）负责把一批操作展开为逐项 effect，按项执行
        allowed、按需把 waiting 项合并成一个审批操作；本方法不聚合。
        """
        decisions: list[AuthDecision] = []
        for effect in effects:
            decisions.append(await self.authorize(effect, ctx=ctx))
        return decisions

    async def authorize_tool_invocation(
        self,
        *,
        tool_name: str,
        arguments: dict | None = None,
        declared_effects: Sequence[Effect] | None = None,
        ctx: AuthContext | None = None,
    ) -> AuthDecision:
        """工具审批策略用：优先采用工具声明的效果；未声明则保守地要求确认。

        未声明 effect 的工具不允许被"猜成允许"——保守走审批（APPROVE），由
        submit() 持久化后延迟执行。
        """
        if declared_effects:
            return await self.authorize_many(declared_effects, ctx=ctx)
        effect = Effect(
            kind=EffectKind.EXECUTE,
            targets=(),
            purpose="调用工具 " + str(tool_name or ""),
            tool=str(tool_name or ""),
            detail={"arguments": dict(arguments or {}), "undeclared_effects": True},
        )
        context = self._context(ctx)
        mode = self.mode()
        if mode == PermissionMode.UNRESTRICTED:
            return AuthDecision(
                verdict=Verdict.ALLOW,
                reason="完全无限制模式下不再额外审批这次工具调用",
                effects=(effect,),
            )
        if mode == PermissionMode.READ_ONLY:
            return AuthDecision(
                verdict=Verdict.DENY,
                reason="只读模式下不能调用需要确认的工具（它可能改变系统状态）",
                effects=(effect,),
            )
        return AuthDecision(
            verdict=Verdict.APPROVE,
            reason="这个工具需要你确认后才能执行",
            effects=(effect,),
        )

    async def submit(self, operation: Any, *, ctx: AuthContext | None = None) -> Any:
        """需要审批时提交待执行操作：持久化 + 立即返回 waiting 票据（不阻塞）。"""
        if self.approvals is None:
            raise RuntimeError("没有审批通道，无法提交需要确认的操作")
        context = self._context(ctx)
        instruction = context.instruction or current_instruction.get()
        if not operation.mode_at_request:
            operation.mode_at_request = self.mode().value
        if instruction is not None:
            operation.instruction_id = operation.instruction_id or instruction.instruction_id
            operation.instruction_text = operation.instruction_text or instruction.text
        if not operation.purpose and instruction is not None:
            operation.purpose = str(instruction.text or "")[:200]
        # R9-G：审批单绑定操作实际依据的具体用户要求及版本（执行前核对；
        # 没有绑定就是没有绑定，不能用"最近一条 active"补）。
        binding = getattr(context, "requirement", None) or current_requirement.get()
        if binding is not None:
            requirement_id = getattr(binding, "requirement_id", None)
            version = getattr(binding, "version", None)
            if requirement_id:
                operation.requirement_id = operation.requirement_id or str(requirement_id)
            if version is not None and operation.requirement_version is None:
                try:
                    operation.requirement_version = int(version)
                except (TypeError, ValueError):
                    pass
        return await self.approvals.submit(
            operation, mode=operation.mode_at_request, origin=context.origin
        )

    # -- R9-F：受限模式模型授权审查 + 后端文件来源 -------------------------

    #: 需要理解"用户具体要求是否覆盖这次操作"的效果类别。读取/QIO 受控数据/
    #: 专用环境装依赖等固定规则不在其中（不为每次读取调用模型）。
    _REVIEW_KINDS = frozenset(
        {
            EffectKind.WRITE,
            EffectKind.CREATE,
            EffectKind.DELETE,
            EffectKind.EXECUTE,
            EffectKind.INSTALL_DEPS,
            EffectKind.SYSTEM_CHANGE,
            EffectKind.NETWORK_EGRESS,
        }
    )

    def _review_candidate(self, effect: Effect) -> bool:
        if effect.kind not in self._REVIEW_KINDS:
            return False
        if self._is_app_managed(effect):
            return False
        # 未声明效果的工具调用没有具体对象可核对：交人工审批，不让模型猜。
        if effect.detail.get("undeclared_effects"):
            return False
        return True

    def _enrich_origin(self, effect: Effect) -> None:
        """用后端持久化来源覆盖"来源归属"（不信任模型/参数传入的 origin）。

        来源查询失败不阻断判定：缺省 origin 会退回"用户原始文件"语义（需要确认），
        而不是被当成 QIO 生成物放行。
        """
        provider = self.origin_provider
        if provider is None or effect.origin():
            return
        for target in effect.path_targets():
            try:
                origin = provider(Path(target.value))
            except Exception:  # noqa: BLE001 - 查不到来源按未记录处理（保守）
                origin = None
            if origin:
                effect.detail["origin"] = str(origin)
                return

    def _requirement_for(self, context: AuthContext) -> Any:
        """取出这次操作依据的**绑定要求**（R9-G）；没有绑定不回退"最近一条"。

        绑定必须指向仍 active、且版本一致的具体用户要求；否则视为没有可对照要求
        （需要人工确认），绝不用另一条更有利的要求替代。
        """
        binding = getattr(context, "requirement", None) or current_requirement.get()
        if binding is None:
            return None
        requirement_id = getattr(binding, "requirement_id", None)
        version = getattr(binding, "version", None)
        if not requirement_id and isinstance(binding, str):
            requirement_id = binding
        if not requirement_id:
            return None
        if self.requirements is None:
            return None
        try:
            requirement = self.requirements.by_id(str(requirement_id))
        except Exception:  # noqa: BLE001 - 台账不可用按无绑定处理
            return None
        if requirement is None or str(getattr(requirement, "status", "")) != "active":
            return None
        if version is not None and int(getattr(requirement, "version", 1) or 1) != int(version):
            return None
        return requirement

    @staticmethod
    def _operation_fingerprint(requirement_id: str, mode: str, operation: Any) -> str:
        import hashlib
        import json

        payload = {
            "requirement_id": requirement_id,
            "mode": mode,
            "kind": operation.kind,
            "objects": [
                {
                    "kind": obj.kind,
                    "value": obj.value,
                    "identity": getattr(obj, "identity_key", None),
                    "origin": getattr(obj, "file_origin", None),
                }
                for obj in operation.objects
            ],
            "program": operation.program,
            "argv": list(operation.argv),
            "cwd": operation.cwd,
            "script_sha256": operation.script_sha256,
            "network_hosts": list(operation.network_hosts),
        }
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _review_operation(self, effect: Effect) -> Any:
        from agent.security.review import ReviewObject, ReviewOperation

        objects = []
        for target in effect.targets:
            if target.kind == "path":
                objects.append(
                    ReviewObject(
                        kind="path",
                        value=str(target.value),
                        identity_key=target.identity,
                        file_origin=effect.origin() or None,
                    )
                )
            elif target.kind == "program":
                objects.append(ReviewObject(kind="program", value=str(target.value)))
            elif target.kind == "url":
                objects.append(ReviewObject(kind="service", value=str(target.value)))
        kind_map = {
            "write": "write",
            "create": "create",
            "delete": "delete",
            "execute": "execute",
            "install_deps": "install",
            "system_change": "system_change",
            "network_egress": "egress",
        }
        detail = effect.detail or {}
        execution = dict(detail.get("execution") or {})
        # 内容/前置状态/启动规格/删除方式/条件必须来自真实工具与执行器写入的
        # effect.detail；不能只靠数据类字段（R9-H §4）。字段映射见 docs/r9h-contracts.md。
        content_digest = (
            detail.get("content_digest")
            or detail.get("content_sha256")
            or execution.get("content_digest")
        )
        precondition_digest = (
            detail.get("precondition_digest")
            or detail.get("base_content_sha256")
            or execution.get("precondition_digest")
        )
        spec_digest = (
            detail.get("spec_digest")
            or execution.get("spec_digest")
            or execution.get("launch_spec_digest")
        )
        delete_mode = detail.get("delete_mode")
        if not delete_mode and effect.kind == EffectKind.DELETE:
            delete_mode = (
                "permanent"
                if detail.get("permanent") is True or detail.get("permanent_delete") is True
                else "recycle_bin"
            )
        return ReviewOperation(
            kind=kind_map.get(effect.kind.value, effect.kind.value),
            objects=tuple(objects),
            program=str(detail.get("program") or "") or None,
            argv=tuple(str(item) for item in (detail.get("argv") or [])),
            cwd=str(detail.get("cwd") or "") or None,
            script_sha256=(
                str(detail.get("script_sha256") or "") or None
            ),
            network_hosts=tuple(str(item) for item in (detail.get("network_hosts") or [])),
            summary=str(effect.purpose or "")[:300],
            spec_digest=(str(spec_digest) if spec_digest else None),
            content_digest=(str(content_digest) if content_digest else None),
            precondition_digest=(
                str(precondition_digest) if precondition_digest else None
            ),
            delete_mode=(str(delete_mode) if delete_mode else None),
            conditions=tuple(
                item for item in (detail.get("conditions") or ()) if isinstance(item, dict)
            ),
        )

    def _permanent_delete_pending(self, effect: Effect) -> bool:
        """永久删除必须走"明确人工确认"，模型审查 ALLOW 不能替代（契约 §5）。"""
        try:
            intent = self._delete_intent(effect)
        except Exception:  # noqa: BLE001
            return False
        return bool(
            intent is not None
            and getattr(intent, "permanent", False)
            and getattr(intent, "verified", False)
        )

    async def _apply_review(
        self,
        effect: Effect,
        context: AuthContext,
        mode: PermissionMode,
        reason: str,
        scope_id: str | None,
    ) -> tuple[Verdict, str, str | None, bool]:
        """受限模式把需要语义理解的操作交给模型审查（unrestricted 不再调用）。

        模型不可用/超时/输出非法/对象无法核实 → 不默认允许，退回普通审批并写明原因；
        用户仍可对这条具体操作手动审批（审批单会列明对象与操作）。
        """
        unavailable_note = "（暂时无法确认授权范围；本次没有执行，你可以对这条具体操作手动审批）"
        if self.reviewer is None or self.requirements is None or self.authorizations is None:
            return Verdict.APPROVE, reason + unavailable_note, scope_id, True
        requirement = self._requirement_for(context)
        if requirement is None:
            return (
                Verdict.APPROVE,
                reason + "（没有可对照的具体用户要求；你可以对这条具体操作手动审批）",
                scope_id,
                True,
            )
        try:
            from agent.security.review import (
                ReviewDecision,
                ReviewRequest,
                ReviewUnavailable,
                validate_outcome,
            )
            from agent.security.requirements import AuthorizationRecord
        except Exception:  # noqa: BLE001 - 审查模块未就绪时不能默认放行
            return Verdict.APPROVE, reason + unavailable_note, scope_id, True

        operation = self._review_operation(effect)
        # fingerprint 用审查模块的单一实现（含操作类型/对象身份/程序/argv/cwd/
        # 脚本摘要/接收方/模式）；后端自算版本只作为模块缺失时的兜底。
        requirement_version = int(getattr(requirement, "version", 1) or 1)
        try:
            from agent.security.review import operation_fingerprint as _op_fingerprint

            fingerprint = _op_fingerprint(operation, mode=mode.value)
        except Exception:  # noqa: BLE001 - 模块异常时用本地实现（仍含同样字段）
            fingerprint = self._operation_fingerprint(
                requirement.requirement_id, mode.value, operation
            )
        # 复用键 = (requirement_id, operation fingerprint)：修正产生新的要求行/版本，
        # requirement_id 变化后旧记录自然不命中；同一个要求的版本差由 Ledger 的
        # active/版本校验兜底。R9-H2 §4.3：限制状态 legacy/unresolved 时禁止复用
        # 旧授权记录（即使 fingerprint 命中），必须走当前审查/审批。
        record = None
        if not self._limits_unresolved(requirement):
            try:
                record = self.authorizations.reuse(
                    requirement_id=requirement.requirement_id,
                    operation_kind=operation.kind,
                    fingerprint=fingerprint,
                )
            except Exception:  # noqa: BLE001 - 复用查询失败按未命中（会重新审查）
                record = None
        if record is not None:
            return (
                Verdict.ALLOW,
                "这条操作在已经确认过的授权范围内（复用审查记录，没有再次调用模型）",
                str(getattr(record, "record_id", "") or "") or scope_id,
                False,
            )

        instruction = context.instruction or current_instruction.get()
        related_grants = ()
        if instruction is not None:
            related_grants = tuple(
                {
                    "grant_id": grant.grant_id,
                    "operations": sorted(grant.operations),
                    "objects": [
                        {"kind": scope.kind, "value": scope.value}
                        for scope in grant.objects
                    ],
                    "purpose": grant.purpose,
                    "source": grant.source,
                }
                for grant in instruction.active_grants()
            )
        user_limits = tuple(getattr(requirement, "limits", ()) or ())
        corrections = ()
        try:
            loader = getattr(self.requirements, "corrections_for", None)
            if callable(loader):
                corrections = tuple(loader(requirement.requirement_id) or ())
        except Exception:  # noqa: BLE001 - 读取修正失败不阻断（审查输入会缺上下文）
            corrections = ()
        prior_decisions = ()
        try:
            rows = self.authorizations.records_for_requirement(
                requirement.requirement_id
            )
            prior_decisions = tuple(
                {
                    "kind": getattr(row, "operation_kind", ""),
                    "decision": getattr(row, "decision", ""),
                    "reason": getattr(row, "reason", ""),
                    "revoked_at": getattr(row, "revoked_at", None),
                }
                for row in rows
            )
        except Exception:  # noqa: BLE001
            prior_decisions = ()
        request_kwargs: dict[str, Any] = dict(
            requirement=requirement,
            corrections=corrections,
            limits=user_limits,
            mode=mode.value,
            operation=operation,
            related_grants=related_grants,
            prior_decisions=prior_decisions,
            approvals=(),
            user_constraints=user_limits,
        )
        # R9-G 给 ReviewRequest 增加字段（版本/相关要求/条件/解析状态/材料）；
        # 用 dataclass 字段过滤，保证 W1 分支合入前后都能构造。
        try:
            import dataclasses as _dc

            known = {item.name for item in _dc.fields(ReviewRequest)}
        except Exception:  # noqa: BLE001
            known = set(request_kwargs)
        requirement_state = str(
            getattr(requirement, "structured_limits_state", "structured") or "structured"
        )
        resolution_raw = str(
            (context.extra or {}).get("requirement_resolution_status")
            or (
                "resolved"
                if requirement_state == "structured"
                else "unavailable"
            )
        )
        resolution_status = "resolved" if resolution_raw in ("ok", "resolved") else (
            resolution_raw if resolution_raw in ("resolved", "unavailable", "not_attempted") else "unavailable"
        )
        context_limits = tuple(
            (context.extra or {}).get("resolution_limits") or ()
        )
        requirement_limits = tuple(
            item
            for item in (getattr(requirement, "structured_limits", None) or ())
            if isinstance(item, dict)
        )
        optional_kwargs = {
            "requirement_version": requirement_version,
            "resolution_status": resolution_status,
            "requirement_resolution_status": resolution_status,
            "resolution_limits": context_limits or requirement_limits,
            "related_requirements": (),
            "condition_evidence": tuple(
                (context.extra or {}).get("condition_evidence") or ()
            ),
            "untrusted_material": tuple(
                (context.extra or {}).get("untrusted_material") or ()
            ),
        }
        for key, value in optional_kwargs.items():
            if key in known:
                request_kwargs[key] = value
        request = ReviewRequest(**request_kwargs)
        try:
            outcome = await self.reviewer.review(request)
        except ReviewUnavailable as exc:
            return (
                Verdict.APPROVE,
                reason + "（暂时无法确认授权范围：%s；本次没有执行，你可以手动审批）" % (exc,),
                scope_id,
                True,
            )
        except Exception as exc:  # noqa: BLE001 - 审查崩溃不能默认允许
            logger.warning("授权审查失败：%s: %s", type(exc).__name__, exc)
            return (
                Verdict.APPROVE,
                reason + "（授权审查失败，本次没有执行；你可以手动审批）",
                scope_id,
                True,
            )
        try:
            outcome = validate_outcome(outcome, request)
        except Exception as exc:  # noqa: BLE001 - 非法输出一律不放行
            logger.warning("授权审查输出无效：%s", exc)
            return (
                Verdict.APPROVE,
                reason + "（授权审查结果无效，本次没有执行；你可以手动审批）",
                scope_id,
                True,
            )
        if outcome.decision == ReviewDecision.ALLOW:
            try:
                from dataclasses import asdict

                import uuid

                record_kwargs = dict(
                    # record_id 只是行身份；复用键是 (requirement_id, fingerprint)。
                    record_id="authz_" + uuid.uuid4().hex[:16],
                    requirement_id=requirement.requirement_id,
                    operation_kind=operation.kind,
                    fingerprint=fingerprint,
                    decision="allow",
                    reason=str(outcome.reason or "")[:500],
                    allowed_operations=tuple(outcome.allowed_operations),
                    objects=tuple(asdict(obj) for obj in outcome.objects),
                    limits=tuple(outcome.limits),
                    source="model_review",
                    mode=mode.value,
                    created_at=_now_iso(),
                    expires_at=None,
                    revoked_at=None,
                    review_id=None,
                )
                try:
                    import dataclasses as _dc

                    known = {item.name for item in _dc.fields(AuthorizationRecord)}
                except Exception:  # noqa: BLE001
                    known = set(record_kwargs)
                if "requirement_version" in known:
                    record_kwargs["requirement_version"] = requirement_version
                record = AuthorizationRecord(**record_kwargs)
                self.authorizations.record(record)
            except Exception as exc:  # noqa: BLE001 - 记录不了授权就不放行
                logger.warning("授权记录保存失败：%s", exc)
                return (
                    Verdict.APPROVE,
                    reason + "（授权记录保存失败，本次没有执行；你可以手动审批）",
                    scope_id,
                    True,
                )
            return (
                Verdict.ALLOW,
                "模型审查确认这属于你当前的要求：" + str(outcome.reason or "")
                + "（授权范围：%s）" % "、".join(outcome.allowed_operations),
                record.record_id,
                False,
            )
        if outcome.decision == ReviewDecision.CONFIRM:
            return Verdict.APPROVE, "模型审查认为需要你确认：" + str(outcome.reason or ""), None, True
        return (
            Verdict.DENY,
            "模型审查认为这与你的要求冲突，不能执行（不会转成普通审批）："
            + str(outcome.reason or ""),
            None,
            False,
        )

    # -- 规则实现 -----------------------------------------------------------

    def _workspace_root_path(self) -> Path | None:
        try:
            return Path(self._workspace_root()).resolve()
        except Exception:  # noqa: BLE001 - 工作区不可判定时按"根外"处理
            return None

    def _internal_paths(self) -> list[Path]:
        try:
            return internal_data.protected_list(self._internal_data_paths())
        except Exception:  # noqa: BLE001 - 路径清单失败不能放行内部数据
            return []

    def _internal_identity_kwargs(self) -> dict[str, object]:
        """C 提供对象身份登记时传给 internal_data；未合入/读失败不传（路径兜底）。"""
        kwargs: dict[str, object] = {}
        for key, provider in (
            ("identity_keys", self._internal_data_identity_keys),
            ("known_paths", self._internal_data_known_paths),
        ):
            if not callable(provider):
                continue
            try:
                value = provider()
            except Exception:  # noqa: BLE001 - 身份登记读失败不能让它变成放行依据
                continue
            if value is not None:
                kwargs[key] = value
        return kwargs

    def _effect_is_internal(self, effect: Effect, protected: list[Path]) -> bool:
        """内部运行数据判定：路径 + 真实对象身份（C 接口存在时）。"""
        kwargs = self._internal_identity_kwargs()
        if kwargs:
            try:
                return bool(
                    internal_data.effect_targets_internal_data(
                        effect, protected, **kwargs
                    )
                )
            except TypeError:  # C 分支未合入：退回路径判定（不静默降级为“非内部”之外的放行）
                pass
        return bool(internal_data.effect_targets_internal_data(effect, protected))

    def _inside_workspace(self, path: object) -> bool:
        root = self._workspace_root_path()
        candidate = internal_data.normalize_path(path)
        if root is None or candidate is None:
            return False
        try:
            return candidate == root or candidate.is_relative_to(root)
        except (OSError, ValueError):  # pragma: no cover
            return False

    @staticmethod
    def _is_qio_generated(effect: Effect) -> bool:
        return effect.origin() == "qio_generated" or bool(
            effect.detail.get("qio_generated")
        )

    #: 应用自身受控运行（设置/记忆/日志/审批落库）只能由代码内部受控路径产生，
    #: 不能由模型可控的工具参数自报标记。约定：这类 effect 的 tool 以 "internal."
    #: 开头（由服务代码设置），引擎才认作应用自身运行；普通工具自报 app_managed
    #: 一律无效（不能靠标记豁免只读/审批）。
    _TRUSTED_INTERNAL_TOOL_PREFIX = "internal."

    @classmethod
    def _is_app_managed(cls, effect: Effect) -> bool:
        """应用受控行为（审批落库/日志/解析临时文件）——不是模型的任意写能力。"""
        if not effect.detail.get("app_managed"):
            return False
        return str(effect.tool or "").startswith(cls._TRUSTED_INTERNAL_TOOL_PREFIX)

    def _grant_scope_id(
        self, effect: Effect, context: AuthContext
    ) -> tuple[bool, str | None]:
        """effect 是否被当前指令的 grant 完整覆盖；返回 (覆盖, 命中的 scope/grant id)。"""
        instruction = context.instruction or current_instruction.get()
        if instruction is None:
            return False, None
        path_targets = effect.path_targets()
        program_targets = [t.value for t in effect.targets if t.kind == "program"]
        url_targets = [t.value for t in effect.targets if t.kind == "url"]
        if not path_targets and not program_targets and not url_targets:
            return False, None
        hit_scope: str | None = None
        if path_targets:
            for target in path_targets:
                hit = None
                for grant in instruction.active_grants():
                    if instruction_allows_path(effect, target.value, grant):
                        hit = grant
                        break
                if hit is None:
                    return False, None
                hit_scope = hit.grant_id
        if program_targets:
            matched = False
            for grant in instruction.active_grants():
                for program in program_targets:
                    if grant.allows(OPERATION_EXECUTE, program=program):
                        matched = True
                        hit_scope = grant.grant_id
                        break
                if matched:
                    break
            if not matched:
                return False, None
        if url_targets:
            host = str(url_targets[0]).split("/")[0]
            matched = False
            for grant in instruction.active_grants():
                if grant.allows("network_egress", url_host=host):
                    matched = True
                    hit_scope = grant.grant_id
                    break
            if not matched:
                return False, None
        return True, hit_scope

    def _scope_limited(self, effect: Effect, context: AuthContext, mode: PermissionMode) -> tuple[bool, str]:
        """子智能体能力子集：超出父级授权时必须由主智能体统一申请。"""
        scope: AgentScope | None = context.agent_scope or agent_scope_var.get()
        if scope is None or mode == PermissionMode.UNRESTRICTED:
            return False, ""
        if effect.kind not in MUTATING_EFFECTS and effect.kind != EffectKind.NETWORK_EGRESS:
            return False, ""
        path_targets = effect.path_targets()
        if not path_targets:
            return False, ""
        for target in path_targets:
            covered = False
            for grant in scope.grants:
                if instruction_allows_path(effect, target.value, grant):
                    covered = True
                    break
            if not covered:
                return True, "这个操作超出子智能体获得的授权范围，需要主智能体统一向你申请"
        return False, ""

    def _verdict(
        self, effect: Effect, context: AuthContext, mode: PermissionMode
    ) -> tuple[Verdict, str, str | None]:
        """统一判定顺序（R9-H3 §2.1，单项/批量/审批恢复共用）。

        1) 当前绑定要求与当前有效限制（structured；legacy/unresolved 也使用已保存
           的 known limits；context.extra 的 resolution_limits 同）；
        2) 先做限制冲突核对，命中即 DENY：不调用模型审查、不复用旧 allow、不进入
           内部数据确认/普通审批；
        3) 内部数据特殊规则、子智能体范围、永久删除、固定查询、模式；
        4) 最后处理未恢复部分：unrestricted 对相关操作返回具体确认（APPROVE），
           不调用普通独立授权审查；restricted 仍走原有审查。
        """
        # 0) 先取要求与有效限制（legacy/unresolved 的已知限制也会被 _limit_conflict 使用）。
        requirement = self._requirement_for(context)
        extra_limits = tuple((context.extra or {}).get("resolution_limits") or ())
        # 1) 有效限制优先：命中即 DENY，先于内部数据/审批/模式/审查。
        conflict = self._limit_conflict(
            effect, requirement, extra_limits=extra_limits
        )
        if conflict:
            return Verdict.DENY, conflict, None

        protected = self._internal_paths()
        # 2) 内部运行数据：所有模式都要求三次确认（写入/删除/脚本维护）。
        #    注意：有效禁止已在上一步 DENY，内部数据确认不能绕过它。
        if self._effect_is_internal(effect, protected):
            if effect.kind == EffectKind.READ:
                if mode == PermissionMode.UNRESTRICTED:
                    return Verdict.ALLOW, "完全无限制模式下允许读取；仍受系统账户权限约束", None
                return (
                    Verdict.DENY,
                    "QIO 内部运行数据不能通过文件工具直接读取（请使用应用内受控入口）",
                    None,
                )
            return (
                Verdict.APPROVE,
                internal_data.INTERNAL_DATA_WARNING
                + "（写入/删除/脚本维护需要你连续三次确认，确认状态由后端校验）",
                None,
            )
        # 3) 名称/后缀/目录名不再直接决定权限（R9-H §5）：只按 AppContext 真实注册
        #    的内部运行数据路径与对象身份/路径规范化判断。
        # 4) 子智能体范围（只能缩小，不能扩大）。
        limited, limit_reason = self._scope_limited(effect, context, mode)
        if limited:
            return Verdict.APPROVE, limit_reason, None
        # 5) 永久删除政策（结构化删除意图，按契约 §5；模型 ALLOW/一般授权不能替代
        #    受限模式下的明确人工确认；已知限制已在上一步优先 DENY）。
        permanent = self._permanent_delete_decision(effect, mode, requirement)
        if permanent is not None:
            return permanent
        # 6) 已验证的固定查询：三种模式直接允许；判定来自真实程序身份/argv 结构，
        #    不按字符串搜索，也不整程序放行。
        fixed = self._fixed_query_decision(effect)
        if fixed is not None:
            return fixed
        # 7) 尚未可靠恢复的限制：unrestricted 对相关操作给出具体确认，不自动放行，
        #    也不调用普通独立授权审查；restricted 继续走原审查/审批。
        if (
            requirement is not None
            and mode == PermissionMode.UNRESTRICTED
            and self._limits_unresolved(requirement)
            and effect.kind in self._LIMIT_OPERATION_NAMES
        ):
            return (
                Verdict.APPROVE,
                "这条用户要求的历史结构化限制尚未可靠恢复；本次不自动执行，"
                "请确认或等待重新理解后再继续（只影响相关操作）",
                None,
            )
        # 8) 三种模式。
        if mode == PermissionMode.READ_ONLY:
            return self._read_only(effect)
        if mode == PermissionMode.UNRESTRICTED:
            return (
                Verdict.ALLOW,
                "完全无限制模式下不再加常规审批（仍受操作系统账户权限约束）",
                None,
            )
        return self._restricted(effect, context)

    # -- R9-G：结构化限制 / 固定查询 / 永久删除 -----------------------------

    _LIMIT_OPERATION_NAMES = {
        EffectKind.WRITE: "write",
        EffectKind.CREATE: "create",
        EffectKind.DELETE: "delete",
        EffectKind.EXECUTE: "execute",
        EffectKind.INSTALL_DEPS: "install",
        EffectKind.SYSTEM_CHANGE: "system_change",
        EffectKind.NETWORK_EGRESS: "egress",
        # R9-H3 §2.1：内部数据写入/维护 effect 在引擎里是独立 kind，但语义上是
        # "写/改内部对象"；有效限制必须能命中它，不能因为 kind 不在表里而绕过
        # 内部数据三次确认分支（对象仍按 path target 结构化核对）。
        EffectKind.INTERNAL_DATA: "write",
    }

    @staticmethod
    def _limit_path_matches(path: str, item: dict) -> bool:
        """限制对象与 effect 对象做**结构化**路径核对（不解析文字）。"""
        kind = str(item.get("kind") or "path")
        value = str(item.get("value") or "")
        if not value:
            return False
        candidate = internal_data.normalize_path(path)
        target = internal_data.normalize_path(value)
        if candidate is None or target is None:
            return False
        try:
            if kind == "path_prefix" or kind == "directory":
                return candidate == target or candidate.is_relative_to(target)
            return candidate == target
        except (OSError, ValueError):
            return False

    @staticmethod
    def _limits_unresolved(requirement: Any) -> bool:
        """历史/无法可靠恢复的结构化限制不得当作“明确无限制”（R9-H §6）。"""
        if requirement is None:
            return False
        state = str(
            getattr(requirement, "structured_limits_state", "structured") or "structured"
        )
        structured = getattr(requirement, "structured_limits", ())
        if state in {"legacy", "unresolved"}:
            return True
        return structured is None

    def _limit_conflict(
        self,
        effect: Effect,
        requirement: Any,
        *,
        extra_limits: Sequence[dict] = (),
    ) -> str | None:
        """检查结构化用户限制是否覆盖这次操作（只做拒绝，不产生许可）。

        R9-H：限制来自 requirement.structured_limits（持久化 JSON）；旧版纯文本
        limits 不解析、不参与判定。历史 NULL/legacy 由 _limits_unresolved 单独处理。
        """
        structured = (
            getattr(requirement, "structured_limits", None)
            if requirement is not None
            else None
        )
        limits = [dict(item) for item in (structured or ()) if isinstance(item, dict)]
        limits.extend(item for item in (extra_limits or ()) if isinstance(item, dict))
        if not limits:
            return None
        operation = self._LIMIT_OPERATION_NAMES.get(effect.kind)
        if operation is None:
            return None
        path_targets = [target.value for target in effect.path_targets()]
        for raw in limits:
            if not isinstance(raw, dict):
                continue
            operations = raw.get("operations") or ()
            if operations and "*" not in operations:
                if operation not in operations and not any(
                    str(item).strip().lower() == operation for item in operations
                ):
                    continue
            objects = raw.get("objects") or ()
            if not path_targets:
                # 没有对象的目标（如 shell）只按操作限制处理：模型明确限制该操作即拒绝。
                if not objects:
                    return str(raw.get("reason") or "你已明确限制这类操作；本次没有执行。")
                continue
            if not objects:
                return str(raw.get("reason") or "你已明确限制这类操作；本次没有执行。")
            matched = any(
                self._limit_path_matches(path, item)
                for path in path_targets
                for item in objects
                if isinstance(item, dict)
            )
            if matched:
                return str(raw.get("reason") or "这个对象在用户明确限制的范围内；本次没有执行。")
        return None

    def _fixed_query_decision(self, effect: Effect) -> tuple[Verdict, str, None] | None:
        """已验证的固定查询在三种模式直接允许；判定由分类器按真实结构完成。"""
        if effect.kind != EffectKind.EXECUTE:
            return None
        fixed = self._classify_fixed_query(effect)
        if fixed is None:
            return None
        reason = str(getattr(fixed, "reason", "") or "").strip()
        return Verdict.ALLOW, "已验证属于固定查询（只读、无修改行为）：" + reason[:160], None

    def _classify_fixed_query(self, effect: Effect) -> Any:
        detail = effect.detail or {}
        try:
            from agent.security.fixed_queries import (
                FixedQueryFacts,
                classify_fixed_query,
                fixed_query_facts_from_detail,
            )

            # 优先用 W2 的单一事实提取（读 effect.detail["execution"]，由真实
            # LaunchSpec 生成），不采信工具自报的 fixed_query 结论。
            facts_obj = fixed_query_facts_from_detail(detail)
            if facts_obj is not None:
                return classify_fixed_query(facts_obj)
        except Exception:  # noqa: BLE001 - 模块未就绪：不把执行当固定查询
            FixedQueryFacts = None  # type: ignore[assignment]
            classify_fixed_query = None  # type: ignore[assignment]
        if FixedQueryFacts is None or classify_fixed_query is None:
            return None
        raw_facts = detail.get("launch_facts")
        facts: dict[str, Any] = dict(raw_facts) if isinstance(raw_facts, dict) else {}
        program_targets = [str(t.value) for t in effect.targets if t.kind == "program"]
        program_value = program_targets[0] if program_targets else str(detail.get("program") or "")
        facts.setdefault("kind", "shell" if detail.get("shell") else "program")
        facts.setdefault("program_name", Path(program_value).name.lower() if program_value else "")
        facts.setdefault("program_path", program_value or None)
        facts.setdefault("argv", tuple(str(item) for item in (detail.get("argv") or [])))
        facts.setdefault("cwd", detail.get("cwd"))
        facts.setdefault("shell", bool(detail.get("shell")))
        try:
            import dataclasses as _dc

            known = {item.name for item in _dc.fields(FixedQueryFacts)}
            filtered = {key: value for key, value in facts.items() if key in known}
            return classify_fixed_query(FixedQueryFacts(**filtered))
        except Exception:  # noqa: BLE001 - 结构不完整 / 分类失败：退出固定查询
            return None

    def _delete_intent(self, effect: Effect) -> Any:
        try:
            from agent.security.deletion import detect_delete_intent
        except Exception:  # noqa: BLE001
            return None
        detail = dict(effect.detail or {})
        # 文件工具明确声明的永久删除（结构化字段，不是文字搜索）：优先于命令级
        # 默认回收站意图；执行前仍会核对对象身份与人工确认。
        if effect.kind == EffectKind.DELETE and (
            detail.get("permanent") is True or detail.get("permanent_delete") is True
        ):
            from types import SimpleNamespace

            return SimpleNamespace(
                permanent=True,
                verified=True,
                objects=tuple(
                    SimpleNamespace(
                        path=str(target.value),
                        identity=getattr(target, "identity", None),
                        is_directory=False,
                    )
                    for target in effect.path_targets()
                ),
                method="fs.permanent_delete",
            )
        facts = detail.get("launch_facts")
        facts_dict = dict(facts) if isinstance(facts, dict) else {}
        try:
            return detect_delete_intent(
                effect_kind=effect.kind.value, detail=detail, facts=facts_dict
            )
        except Exception:  # noqa: BLE001
            return None

    def _permanent_delete_decision(
        self, effect: Effect, mode: PermissionMode, requirement: Any
    ) -> tuple[Verdict, str, str | None] | None:
        intent = self._delete_intent(effect)
        # 文件工具显式声明永久删除（结构字段，不是文字搜索）：即使 deletion 模块
        # 没有给出命令级意图，也按永久删除政策处理。
        if intent is None and effect.kind == EffectKind.DELETE:
            detail = effect.detail or {}
            if detail.get("permanent") is True or detail.get("permanent_delete") is True:
                from types import SimpleNamespace

                intent = SimpleNamespace(
                    permanent=True,
                    verified=True,
                    objects=tuple(
                        SimpleNamespace(
                            path=str(target.value),
                            identity=getattr(target, "identity", None),
                            is_directory=False,
                        )
                        for target in effect.path_targets()
                    ),
                    method="fs.permanent_delete",
                )
        if intent is None or not bool(getattr(intent, "permanent", False)):
            return None
        if not bool(getattr(intent, "verified", False)):
            # 无法从真实程序/参数完整解析：不进入永久删除规则，按普通流程处理。
            return None
        objects = []
        for item in getattr(intent, "objects", ()) or ():
            path = getattr(item, "path", None) or (item.get("path") if isinstance(item, dict) else None)
            if path:
                objects.append(str(path))
        object_text = "、".join(objects[:8]) + ("等" if len(objects) > 8 else "")
        if mode == PermissionMode.READ_ONLY:
            return (
                Verdict.DENY,
                "只读模式下不能永久删除文件（只读规则不允许删除业务文件）",
                None,
            )
        if mode == PermissionMode.UNRESTRICTED:
            return (
                Verdict.ALLOW,
                "完全无限制模式下允许永久删除（不会进入回收站，删除后不可恢复）",
                None,
            )
        # 受限：只有用户对相同对象/行为的明确人工确认可以覆盖；模型审查/一般授权
        # 记录不能替代。已有仍有效且明确覆盖的确认可复用。
        fingerprint = self._permanent_delete_fingerprint(effect, intent)
        # R9-H2 §4.3：历史限制未可靠恢复时，旧 allow 记录不能绕过 unresolved。
        if (
            self.authorizations is not None
            and requirement is not None
            and not self._limits_unresolved(requirement)
        ):
            try:
                record = self.authorizations.reuse(
                    requirement_id=getattr(requirement, "requirement_id", ""),
                    operation_kind="delete_permanent",
                    fingerprint=fingerprint,
                )
            except Exception:  # noqa: BLE001
                record = None
            if record is not None:
                return (
                    Verdict.ALLOW,
                    "已有明确覆盖相同对象的永久删除确认（不会进入回收站）",
                    str(getattr(record, "record_id", "") or ""),
                )
        return (
            Verdict.APPROVE,
            "这是永久删除（目标：" + (object_text or "未列出对象") + "）：不会进入系统回收站，删除后不可恢复；"
            "需要你明确确认后才会执行",
            None,
        )

    @staticmethod
    def _permanent_delete_fingerprint(effect: Effect, intent: Any) -> str:
        import hashlib
        import json

        payload = {
            "action": "permanent_delete",
            "objects": [
                {
                    "path": str(getattr(item, "path", "") or ""),
                    "identity": getattr(item, "identity", None),
                    "is_directory": bool(getattr(item, "is_directory", False)),
                }
                for item in getattr(intent, "objects", ()) or ()
            ],
            "method": getattr(intent, "method", ""),
        }
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def recheck_approved(
        self,
        effect: Effect,
        *,
        context: AuthContext | None = None,
        approval_kind: str = "",
        confirmations_required: int = 1,
        confirmed_stage: int = 0,
    ) -> AuthDecision:
        """已获用户批准/确认的操作，在执行前按**当前**状态核对模式与限制。

        这不是再走一次"是否需要授权审查"（那样会把已批准操作重新变成待审批），
        而是核对：当前模式仍允许这类操作、用户限制仍不冲突、要求绑定仍有效、
        永久删除仍有明确确认、内部数据确认次数已完成。对象/内容/审批有效期由
        调用方（RecheckService/执行器）另行核对。
        """
        context = self._context(context)
        mode = self.mode()
        self._enrich_origin(effect)
        requirement = self._requirement_for(context)
        # R9-H3 §2.2：先按**最新**有效限制核对（legacy/unresolved 的 known limits 也生效）；
        # 命中即 DENY，旧审批/旧 allow/内部数据确认/模式都不能覆盖有效禁止。
        conflict = self._limit_conflict(
            effect,
            requirement,
            extra_limits=tuple((context.extra or {}).get("resolution_limits") or ()),
        )
        if conflict:
            return AuthDecision(verdict=Verdict.DENY, reason=conflict, effects=(effect,))
        # 未恢复部分：unrestricted 相关操作在执行前核对也只给具体确认，不自动放行。
        if (
            requirement is not None
            and mode == PermissionMode.UNRESTRICTED
            and self._limits_unresolved(requirement)
            and effect.kind in self._LIMIT_OPERATION_NAMES
        ):
            return AuthDecision(
                verdict=Verdict.APPROVE,
                reason="这条用户要求的历史结构化限制尚未可靠恢复；执行前核对不自动放行",
                effects=(effect,),
            )
        limited, limit_reason = self._scope_limited(effect, context, mode)
        if limited:
            return AuthDecision(verdict=Verdict.DENY, reason=limit_reason, effects=(effect,))
        complete = int(confirmations_required or 1) <= 1 or int(confirmed_stage or 0) >= int(
            confirmations_required or 1
        )
        internal = effect.kind == EffectKind.INTERNAL_DATA or self._effect_is_internal(
            effect, self._internal_paths()
        )
        if internal:
            if not complete:
                return AuthDecision(
                    verdict=Verdict.APPROVE,
                    reason="内部运行数据的确认次数尚未完成，本次没有执行",
                    effects=(effect,),
                )
            if effect.kind == EffectKind.READ or effect.kind == EffectKind.NETWORK_EGRESS:
                pass
            else:
                return AuthDecision(
                    verdict=Verdict.ALLOW,
                    reason="内部运行数据按既定确认次数完成，执行前核对通过",
                    effects=(effect,),
                )
        permanent = self._permanent_delete_decision(effect, mode, requirement)
        if permanent is not None:
            verdict, reason, scope_id = permanent
            explicit_kinds = {
                "delete_permanent",
                "fs.permanent_delete",
                "proc.delete",
                "proc.permanent_delete",
            }
            if verdict == Verdict.APPROVE and approval_kind in explicit_kinds:
                return AuthDecision(
                    verdict=Verdict.ALLOW,
                    reason="已有用户对相同永久删除对象的明确确认，执行前核对通过",
                    effects=(effect,),
                    scope_id=scope_id,
                )
            return AuthDecision(
                verdict=verdict, reason=reason, effects=(effect,), scope_id=scope_id
            )
        fixed = self._fixed_query_decision(effect)
        if fixed is not None:
            return AuthDecision(verdict=fixed[0], reason=fixed[1], effects=(effect,))
        if mode == PermissionMode.READ_ONLY:
            verdict, reason, scope_id = self._read_only(effect)
            return AuthDecision(
                verdict=verdict, reason=reason, effects=(effect,), scope_id=scope_id
            )
        if mode == PermissionMode.UNRESTRICTED:
            return AuthDecision(
                verdict=Verdict.ALLOW,
                reason="完全无限制模式下按最新状态核对通过（仍受操作系统账户权限约束）",
                effects=(effect,),
            )
        if not complete:
            return AuthDecision(
                verdict=Verdict.APPROVE,
                reason="确认次数尚未完成，本次没有执行",
                effects=(effect,),
            )
        return AuthDecision(
            verdict=Verdict.ALLOW,
            reason="已有有效人工审批，按当前模式重新核对通过",
            effects=(effect,),
        )

    def _read_only(self, effect: Effect) -> tuple[Verdict, str, str | None]:
        kind = effect.kind
        if kind == EffectKind.READ:
            return Verdict.ALLOW, "只读模式下允许读取", None
        if self._is_app_managed(effect):
            return Verdict.ALLOW, "这是应用受控的运行数据/临时文件，不构成模型写文件能力", None
        if kind == EffectKind.NETWORK_EGRESS:
            if effect.detail.get("scope") == "configured_cloud_model":
                return Verdict.ALLOW, "发给用户已配置的云端模型，只读模式下允许", None
            return Verdict.APPROVE, "发给第三方服务需要你确认（审批单会列出材料、接收服务与用途）", None
        if kind == EffectKind.SUBAGENT:
            return Verdict.ALLOW, "子智能体只获得父级授权的子集", None
        if kind in MUTATING_EFFECTS:
            return Verdict.DENY, _READ_ONLY_DENY, None
        return Verdict.DENY, _READ_ONLY_DENY, None

    def _restricted(
        self, effect: Effect, context: AuthContext
    ) -> tuple[Verdict, str, str | None]:
        kind = effect.kind
        granted, scope_id = self._grant_scope_id(effect, context)
        if kind == EffectKind.READ:
            if self._is_app_managed(effect):
                return Verdict.ALLOW, "应用受控读取（不是模型任意读取）", None
            return Verdict.ALLOW, "受限模式下允许读取当前系统账户可读的内容", None
        if self._is_app_managed(effect):
            return Verdict.ALLOW, "这是应用受控的运行数据/临时文件", None
        if kind in (EffectKind.WRITE, EffectKind.CREATE):
            if self._is_qio_generated(effect):
                return Verdict.ALLOW, "QIO 生成的工作文件，受限模式下允许修改", None
            if granted:
                return Verdict.ALLOW, "你的指令已明确授权修改这个对象（目的：" + effect.purpose[:60] + "）", scope_id
            return Verdict.APPROVE, "修改你的原有文件/工作区材料需要你确认（批准后自动执行）", None
        if kind == EffectKind.DELETE:
            if granted:
                return Verdict.ALLOW, "你的指令已明确要求删除这个对象（会进系统回收站）", scope_id
            if self._is_qio_generated(effect):
                return Verdict.ALLOW, "删除 QIO 生成的工作文件（会进系统回收站）", None
            return Verdict.APPROVE, "删除你的文件需要你确认（批准后进系统回收站执行）", None
        if kind == EffectKind.EXECUTE:
            if effect.detail.get("shell"):
                return Verdict.APPROVE, "自由 shell/任意脚本每次都需要你确认", None
            if granted:
                return Verdict.ALLOW, "在指令授权的项目范围内运行命令（shell=False 优先）", scope_id
            return Verdict.APPROVE, "执行命令/脚本需要你确认（每次重新判定）", None
        if kind == EffectKind.INSTALL_DEPS:
            if effect.detail.get("env_scope") == "tool_env":
                return Verdict.ALLOW, "在 QIO 专用环境内安装依赖，受限模式下允许", None
            return Verdict.APPROVE, "系统级安装/系统配置需要你确认", None
        if kind == EffectKind.SYSTEM_CHANGE:
            return Verdict.APPROVE, "系统级改动需要你确认", None
        if kind == EffectKind.NETWORK_EGRESS:
            if effect.detail.get("scope") == "configured_cloud_model":
                return Verdict.ALLOW, "发给用户已配置的云端模型，受限模式下允许", None
            if granted:
                return Verdict.ALLOW, "你的指令已明确指定这个接收服务", scope_id
            service = ""
            if effect.targets:
                service = effect.targets[0].value
            material = str(effect.detail.get("material_summary") or effect.purpose or "")
            return (
                Verdict.APPROVE,
                "把本地内容发给未指定的第三方服务需要你确认；审批单会列明材料、接收服务与用途"
                + ("（接收：" + service + "）" if service else ""),
                None,
            )
        if kind == EffectKind.TOOL_REGISTER:
            return Verdict.ALLOW, "工具注册本身不产生永久文件/网络授权，每次调用重新判定", None
        if kind == EffectKind.SUBAGENT:
            return Verdict.ALLOW, "子智能体只获得父级授权的子集，逐调用判定", None
        if kind == EffectKind.INTERNAL_DATA:  # pragma: no cover - 前面已处理
            return Verdict.APPROVE, internal_data.INTERNAL_DATA_WARNING, None
        return Verdict.APPROVE, _NEEDS_APPROVAL, None