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
        extra_env: dict[str, str] = {}
        if self.definition.credential_ref:
            if self.credentials is None:
                return ToolResult(
                    ok=False, error="tool references a credential but none is wired"
                )
            secret = self.credentials.get_secret(self.definition.credential_ref)
            if secret is None:
                return ToolResult(ok=False, error="referenced credential unavailable")
            key = self.definition.credential_ref.upper().replace("-", "_")
            extra_env[f"SMART_AGENT_KEY_{key}"] = secret
        result = await self.sandbox.execute(
            self.definition.code, kwargs, extra_env=extra_env
        )
        if not result.ok:
            return ToolResult(ok=False, error=result.error or "sandbox failure")
        return ToolResult(
            ok=True, content=json.dumps(result.value, ensure_ascii=False)
        )


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