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
    ) -> None:
        self.conn = conn
        self.approvals = approvals
        self.settings = settings
        self.instance_id = instance_id
        self._workspace_root = workspace_root or (lambda: Path.cwd())
        self._internal_data_paths = internal_data_paths or (lambda: [])
        self.audit = audit or AuthorizationAudit()

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
        verdict, reason, scope_id = self._verdict(effect, context, mode)
        decision = AuthDecision(
            verdict=verdict,
            reason=reason,
            approval_id=None,
            effects=(effect,),
            scope_id=scope_id,
        )
        instruction = context.instruction or current_instruction.get()
        self.audit.record(
            effect=effect,
            decision=decision,
            mode=mode.value,
            instruction_id=instruction.instruction_id if instruction is not None else None,
            detail={"origin": context.origin},
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
            verdict, reason, hit_scope = self._verdict(effect, context, mode)
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
        # 4) 三种模式。
        if mode == PermissionMode.READ_ONLY:
            return self._read_only(effect)
        if mode == PermissionMode.UNRESTRICTED:
            return (
                Verdict.ALLOW,
                "完全无限制模式下不再加常规审批（仍受操作系统账户权限约束）",
                None,
            )
        return self._restricted(effect, context)

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