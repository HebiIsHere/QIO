"""Runtime tools for agent-created definitions."""

from __future__ import annotations

import json
from typing import Any

from agent.credentials.store import CredentialStore
from agent.tools.base import Tool, ToolResult
from agent.tools.sandbox import SandboxExecutor
from agent.tools.spec import ToolDefinition


class CodeTool(Tool):
    """Function-type tool: executes the approved code in the sandbox."""

    def __init__(
        self,
        definition: ToolDefinition,
        sandbox: SandboxExecutor,
        credentials: CredentialStore | None = None,
    ) -> None:
        self.definition = definition
        self.name = definition.name
        self.description = definition.description
        self.parameters = definition.parameters
        self.sandbox = sandbox
        self.credentials = credentials

    async def run(self, **kwargs: Any) -> ToolResult:
        from agent.tools.policy import resolve_policy

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
        result = await self.sandbox.execute(
            self.definition.code, kwargs, extra_env=extra_env, policy=policy
        )
        if not result.ok:
            return ToolResult(ok=False, error=result.error or "沙箱执行失败")
        content = json.dumps(result.value, ensure_ascii=False)
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
