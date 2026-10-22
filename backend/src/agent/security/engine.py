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
        )

    async def authorize(self, effect: Effect, *, ctx: AuthContext | None = None) -> AuthDecision:
        context = self._context(ctx)
        mode = self.mode()
        self._enrich_origin(effect)
        verdict, reason, scope_id = self._verdict(effect, context, mode)
        needs_review = False
        if (
            mode == PermissionMode.RESTRICTED
            and verdict == Verdict.APPROVE
            and self._review_candidate(effect)
        ):
            verdict, reason, scope_id, needs_review = await self._apply_review(
                effect, context, mode, reason, scope_id
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
            detail={"origin": context.origin, "needs_review": needs_review},
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
            if (
                mode == PermissionMode.RESTRICTED
                and verdict == Verdict.APPROVE
                and self._review_candidate(effect)
            ):
                verdict, reason, hit_scope, _ = await self._apply_review(
                    effect, context, mode, reason, hit_scope
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

    def _current_requirement(self) -> Any:
        if self.requirements is None:
            return None
        try:
            return self.requirements.current()
        except Exception:  # noqa: BLE001 - 台账不可用按"没有可对照要求"
            return None

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
        )

    async def _apply_review(
        self,
        effect: Effect,
        context: AuthContext,
        mode: PermissionMode,
        reason: str,
        scope_id: str | None,
    ) -> tuple[Verdict, str, str | None, bool]:
        """受限模式下把"需要自然语言理解"的操作交给模型审查（后端验证后生效）。

        模型不可用/超时/输出非法/对象无法核实 → 不默认允许，退回普通审批并写明原因；
        用户仍可对这条具体操作手动审批（审批单会列明对象与操作）。
        """
        unavailable_note = "（暂时无法确认授权范围；本次没有执行，你可以对这条具体操作手动审批）"
        if self.reviewer is None or self.requirements is None or self.authorizations is None:
            return Verdict.APPROVE, reason + unavailable_note, scope_id, True
        requirement = self._current_requirement()
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
        try:
            from agent.security.review import operation_fingerprint as _op_fingerprint

            fingerprint = _op_fingerprint(operation, mode=mode.value)
        except Exception:  # noqa: BLE001 - 模块异常时用本地实现（仍含同样字段）
            fingerprint = self._operation_fingerprint(
                requirement.requirement_id, mode.value, operation
            )
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
        request = ReviewRequest(
            requirement=requirement,
            corrections=tuple(getattr(requirement, "corrections", ()) or ()),
            limits=user_limits,
            mode=mode.value,
            operation=operation,
            related_grants=related_grants,
            prior_decisions=(),
            approvals=(),
            user_constraints=user_limits,
        )
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

                record = AuthorizationRecord(
                    # record_id 只是行身份；复用键是 (requirement_id, fingerprint)，
                    # 不同要求下相同操作也不能撞主键。
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

    @staticmethod
    def _is_app_managed(effect: Effect) -> bool:
        """应用受控行为（审批落库/日志/解析临时文件）——不是模型的任意写能力。"""
        return bool(effect.detail.get("app_managed"))

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
        protected = self._internal_paths()
        # 1) 内部运行数据：所有模式都要求三次确认（写入/删除/脚本维护）。
        if internal_data.effect_targets_internal_data(effect, protected):
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
        # 2) 凭据 / 版本库内部数据：读取红线；直接改写按内部数据规则。
        sensitive = any(
            internal_data.is_sensitive_credential_path(target.value)
            for target in effect.targets
            if target.kind == "path"
        )
        if sensitive:
            if effect.kind == EffectKind.READ:
                if mode == PermissionMode.UNRESTRICTED:
                    return Verdict.ALLOW, "完全无限制模式下允许读取；仍受系统账户权限约束", None
                return Verdict.DENY, "凭据与版本库内部数据不能通过文件工具读取", None
            if effect.kind in MUTATING_EFFECTS:
                return (
                    Verdict.APPROVE,
                    "这是凭据/版本库内部数据，直接改写需要你连续三次确认（后端校验确认状态）",
                    None,
                )
        # 3) 子智能体范围（只能缩小，不能扩大）。
        limited, limit_reason = self._scope_limited(effect, context, mode)
        if limited:
            return Verdict.APPROVE, limit_reason, None
        # 4) 用户明确禁止（R9-F 契约 §5.3）：三种模式都必须遵守。
        # 这是**限制**检测：只把"不要修改/只分析/禁止删除/不要外发"等用户原话
        # 转成拒绝，永远不会产生许可；误判方向是更保守（多拒绝一次），不是放行。
        prohibited = self._user_prohibition(effect)
        if prohibited:
            return Verdict.DENY, prohibited, None
        # 5) 三种模式。
        if mode == PermissionMode.READ_ONLY:
            return self._read_only(effect)
        if mode == PermissionMode.UNRESTRICTED:
            return (
                Verdict.ALLOW,
                "完全无限制模式下不再加常规审批（仍受操作系统账户权限约束）",
                None,
            )
        return self._restricted(effect, context)

    #: 用户"只分析/不许改"类明确禁止 → 拒绝所有会改状态的效果。
    _MUTATION_PROHIBITIONS = (
        "只分析",
        "只读分析",
        "不要修改",
        "别修改",
        "不要改",
        "别改",
        "禁止修改",
        "不许修改",
        "不要动",
        "不要写",
        "别写",
    )
    _DELETE_PROHIBITIONS = ("不要删除", "别删除", "禁止删除", "不许删除", "不要删")
    _EGRESS_PROHIBITIONS = (
        "不要发送",
        "别发送",
        "不要外发",
        "禁止外发",
        "不要上传",
        "禁止上传",
        "不要发给",
    )

    def _user_prohibition(self, effect: Effect) -> str | None:
        """从当前用户要求原话里识别**禁止**（只用于拒绝，不产生任何许可）。

        完全无限制模式也遵守用户明确禁止（契约 §5.3）；受限模式同理，
        且禁止优先于模型审查（不让模型把用户刚说出的限制推翻）。
        """
        requirement = self._current_requirement()
        if requirement is None:
            return None
        parts = [str(getattr(requirement, "text", "") or "")]
        limits = getattr(requirement, "limits", ()) or ()
        parts.extend(str(item) for item in limits)
        text = "\n".join(item for item in parts if item)
        if not text.strip():
            return None
        compact = text.replace(" ", "").lower()
        kind = effect.kind
        if kind in MUTATING_EFFECTS and any(
            token in compact for token in self._MUTATION_PROHIBITIONS
        ):
            return "你明确要求只分析/不要修改；这次操作会改变状态，本次没有执行。"
        if kind == EffectKind.DELETE and any(
            token in compact for token in self._DELETE_PROHIBITIONS
        ):
            return "你明确要求不要删除；本次没有执行。"
        if kind == EffectKind.NETWORK_EGRESS and any(
            token in compact for token in self._EGRESS_PROHIBITIONS
        ):
            return "你明确要求不要外发/上传；本次没有执行。"
        return None

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