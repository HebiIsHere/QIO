"""Runtime tools for agent-created definitions."""

from __future__ import annotations

import json
from typing import Any

from agent.credentials.store import CredentialStore
from agent.tools import security_gate
from agent.tools.base import Tool, ToolResult
from agent.trace.redact import redact_text
from agent.tools.sandbox import SandboxExecutor
from agent.tools.spec import ToolDefinition


class CodeTool(Tool):
    """Function-type tool: executes the approved code in the sandbox."""

    # security = 统一授权网关（契约 §2.3 TOOL_REGISTER/EXECUTE）：注册**不产生**
    # 任何长期权限，每次调用都重新判定。
    inject = ["security"]

    def __init__(
        self,
        definition: ToolDefinition,
        sandbox: SandboxExecutor,
        credentials: CredentialStore | None = None,
        envs=None,
        security=None,
    ) -> None:
        self.definition = definition
        self.name = definition.name
        self.description = definition.description
        self.parameters = definition.parameters
        self.sandbox = sandbox
        self.credentials = credentials
        # 项目级专用环境：声明了第三方依赖的工具必须用它跑（没有就明确失败）。
        self.envs = envs
        self.security = security

    async def _authorize_call(self, arguments: dict[str, Any]) -> ToolResult | None:
        """自建工具每次调用重新经统一入口判定（注册不产生长期授权）。

        返回 None = 可以执行；否则返回应直接交给模型的失败/待审批结果。
        """
        if self.security is None:
            return None
        mode = security_gate.mode_of(self.security)
        if mode == "read_only":
            return ToolResult(
                ok=False,
                error=(
                    "只读模式下 QIO 不能执行自建工具：无法证明它不会修改业务文件，"
                    "本次已拒绝（没有任何代码被执行）。"
                ),
                category="permission",
                recoverable=False,
                code="readonly_execution_denied",
            )
        # 审批执行器批准后重跑本工具（registry 的 approved 重跑标记）：审批已完成，
        # 不再重复提交；只读检查已在上方生效（批准后切只读 → 这里仍不执行）。
        from agent.tools.registry import current_approved_execution

        if current_approved_execution() is not None:
            return None
        effect = security_gate.script_effect(
            self.name,
            purpose=f"执行自建工具 {self.name}",
            code_digest=security_gate.text_sha256(self.definition.code),
            detail={"definition": self.name, "tool_type": self.definition.tool_type},
        )
        decision = await security_gate.authorize(self.security, effect)
        verdict = security_gate.verdict_of(decision)
        if verdict == "allow":
            return None
        if verdict == "deny":
            return security_gate.denied_result(decision, "当前模式不允许执行这个自建工具")
        operation = security_gate.build_pending_operation(
            kind="tool.invoke",
            label=f"执行自建工具 {self.name}",
            payload={"tool": self.name, "arguments": dict(arguments or {})},
            effect=effect,
            fingerprint={"tool": self.name, "code_sha256": security_gate.text_sha256(self.definition.code)},
            purpose=effect.purpose,
            mode_at_request=mode,
            instruction=security_gate.instruction_snapshot(self.security),
        )
        try:
            ticket = await security_gate.submit_pending(self.security, operation)
        except Exception as exc:  # noqa: BLE001 - 提交失败如实报，不假装已提交
            return ToolResult(
                ok=False,
                error=f"提交审批失败：{type(exc).__name__}: {exc}（本次没有执行）",
                category="permission",
                recoverable=False,
            )
        if ticket is not None:
            return security_gate.pending_result(
                ticket, operation_label=f"执行自建工具 {self.name}"
            )
        return ToolResult(
            ok=False,
            error=(
                "执行自建工具需要你的批准，但当前没有可用的审批执行器："
                "本次没有执行（执行边界暂不可用）。"
            ),
            category="permission",
            recoverable=False,
        )

    async def execute_after_approval(self, arguments: dict[str, Any]) -> ToolResult:
        """审批执行器入口：批准后真正运行（执行前由审批执行器重核对授权）。"""
        return await self._run(dict(arguments or {}))

    async def run(self, **kwargs: Any) -> ToolResult:
        blocked = await self._authorize_call(kwargs)
        if blocked is not None:
            return blocked
        return await self._run(kwargs)

    async def _run(self, kwargs: dict[str, Any]) -> ToolResult:
        from agent.tools.policy import resolve_policy

        interpreter: str | None = None
        container_image: str | None = None
        if self.definition.requirements:
            # 执行器决定用哪个环境：受限子进程用宿主专用环境，容器用按锁定清单构建的
            # 依赖镜像。这里没有审批通道（注册后的调用不该临时装东西）：两种情况都
            # 只复用已经准备好的，没准备好就明确失败，绝不换成没有依赖的解释器跑一遍。
            from agent.tools.tool_envs import resolve_execution_environment

            plan = await resolve_execution_environment(
                self.definition,
                self.envs,
                executor=await self.sandbox.effective_executor(),
                prepare=False,
                security=self.security,
            )
            if not plan.ok:
                return ToolResult(
                    ok=False,
                    error=plan.reason or "专用环境没准备好，已拒绝执行",
                    category="missing_dependency",
                )
            interpreter = plan.interpreter
            container_image = plan.container_image

        policy = resolve_policy(self)
        extra_env: dict[str, str] = {}
        # 只有在 policy 明确授权了凭据类别时才注入；否则工具环境里没有凭据。
        if policy.credentials:
            if self.credentials is None:
                return ToolResult(
                    ok=False, error="该工具引用了凭据，但当前没有可用凭据"
                )
            ref = self.definition.credential_ref
            secret = self.credentials.get_secret(ref) if ref else None
            if secret is None:
                secret = self.credentials.get_default_secret()
            if secret is None:
                return ToolResult(ok=False, error="引用的凭据不可用")
            key = (ref or "default").upper().replace("-", "_")
            extra_env[f"QIO_KEY_{key}"] = secret
        execution: dict[str, Any] = {"interpreter": interpreter}
        if container_image:
            # 只在真给了依赖镜像时才传：sandbox 的替身（测试里）不必认识这个参数；
            # 给了镜像却用受限子进程时 sandbox 会明确报错，不会静默忽略。
            execution["container_image"] = container_image
        result = await self.sandbox.execute(
            self.definition.code,
            kwargs,
            extra_env=extra_env,
            policy=policy,
            files=self.definition.files,
            entry=self.definition.entry,
            **execution,
        )
        if not result.ok:
            # 失败要把 stderr / 退出码作为诊断一起交给模型，而不是只回一句
            # 「工具执行失败」；类别也带上，供统一反馈层与界面使用。
            detail = result.diagnostic()
            hint = self.definition.dependency_hint(result.error or detail)
            if hint:
                detail = f"{hint}\n{detail}" if detail else hint
            return ToolResult(
                ok=False,
                # 不说「沙箱」：真实执行器可能是容器，也可能是受限子进程
                # （受限子进程不是安全沙箱），这里只是没有更具体错误时的兜底文案。
                error=redact_text(result.error or "工具执行失败"),
                content=redact_text(detail),
                category=result.category,
            )
        # 工具输出要过打码再交给模型与历史：注入给工具的凭据（QIO_KEY_*）如果被
        # 原样打印/返回（很常见的调试写法），不能顺着工具结果流进模型上下文。
        content = redact_text(json.dumps(result.value, ensure_ascii=False))
        # policy.output_limit_chars 之前只是声明，没有真正生效；这里显式截断并
        # 标注，避免超大输出直接灌进模型上下文（截断是可见的，不静默）。
        limit = getattr(policy, "output_limit_chars", 0) or 0
        if limit and len(content) > limit:
            content = (
                content[:limit]
                + f"\n...[输出已截断：{len(content)} 字符超过上限 {limit}]"
            )
        return ToolResult(ok=True, content=content)


class SubagentStubTool(Tool):
    """Subagent-type tool: registered with its model/key reference.

    The standalone subagent runtime (own loop, own model, own BYOK key)
    lands in v1.5; the stub keeps the registration contract complete.
    """

    def __init__(
        self,
        definition: ToolDefinition,
        credentials: CredentialStore | None = None,
    ) -> None:
        self.definition = definition
        self.name = definition.name
        self.description = definition.description
        self.parameters = definition.parameters
        self.credentials = credentials

    async def run(self, **kwargs: Any) -> ToolResult:
        return ToolResult(
            ok=False,
            error=(
                f"subagent tool '{self.name}' is registered but its standalone "
                "runtime lands in v1.5"
            ),
        )
