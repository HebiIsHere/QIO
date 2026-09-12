"""Tool execution policy: what a tool is *allowed to do*.

Key principle: a restricted subprocess is **not** a security sandbox. It lowers
accidental-damage risk but cannot safely run arbitrary untrusted AI code. The
policy below therefore states capabilities explicitly, instead of relying on
"which credential it uses".

Capability levels:
    PURE (0)       — default for AI-generated tools: no credential, no network,
                     no arbitrary shell, only a controlled scratch/temp dir,
                     no access to user files, strict timeout + resource limits.
    RESTRICTED (1) — the tool explicitly requests capability (network / a
                     filesystem path / a credential category / an endpoint);
                     must be shown to the user at approval time.
    TRUSTED (2)    — needs strong local access; requires explicit user approval
                     and must never be granted automatically.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class CapabilityLevel(int, Enum):
    PURE = 0
    RESTRICTED = 1
    TRUSTED = 2


class IsolationLevel(str, Enum):
    NONE = "none"           # in-process (not used for AI code)
    SUBPROCESS = "subprocess"
    CONTAINER = "container"


class SideEffect(str, Enum):
    PURE = "pure"
    READ = "read"
    WRITE = "write"
    DESTRUCTIVE = "destructive"


class Concurrency(str, Enum):
    PARALLEL = "parallel"
    SERIALIZED = "serialized"
    RESOURCE_SCOPED = "resource_scoped"


@dataclass(frozen=True)
class ToolExecutionPolicy:
    level: CapabilityLevel = CapabilityLevel.PURE
    isolation: IsolationLevel = IsolationLevel.CONTAINER
    network: bool = False
    network_allow: tuple[str, ...] = ()
    filesystem: tuple[str, ...] = ()   # allowed absolute paths (empty = scratch only)
    shell: bool = False
    process: bool = False
    credentials: tuple[str, ...] = ()  # credential categories (tags) explicitly granted
    side_effect: SideEffect = SideEffect.PURE
    concurrency: Concurrency = Concurrency.PARALLEL
    resource_scope: str | None = None
    timeout_ms: int = 10_000
    output_limit_chars: int = 4_000

    def is_high_risk(self) -> bool:
        """Capabilities that cannot be safely contained by a plain subprocess."""
        return bool(
            self.network
            or self.filesystem
            or self.process
            or self.shell
            or self.credentials
            or self.side_effect in (SideEffect.WRITE, SideEffect.DESTRUCTIVE)
        )

    def describe(self) -> list[str]:
        """Human-readable capability bullets for the approval dialog."""
        if self.network:
            net = "联网：是" + (
                f"（限 {', '.join(self.network_allow)}）" if self.network_allow else ""
            )
        else:
            net = "联网：否"
        out: list[str] = [net]
        out.append(
            "读取文件：" + ("是（" + ", ".join(self.filesystem) + "）" if self.filesystem else "否")
        )
        out.append(
            "写入文件："
            + ("是" if self.side_effect in (SideEffect.WRITE, SideEffect.DESTRUCTIVE) else "否")
        )
        out.append("启动进程：" + ("是" if self.process or self.shell else "否"))
        out.append(
            "使用凭据：" + ("、".join(self.credentials) if self.credentials else "无")
        )
        out.append("副作用：" + self.side_effect.value)
        return out


def default_policy_for(definition: Any) -> ToolExecutionPolicy:
    """Derive a policy for an agent-created tool definition.

    AI-generated function tools are PURE unless they explicitly reference a
    credential, which promotes them to RESTRICTED with that category only.
    """
    cred = getattr(definition, "credential_ref", None)
    if cred:
        return ToolExecutionPolicy(
            level=CapabilityLevel.RESTRICTED,
            credentials=(str(cred),),
            side_effect=SideEffect.READ,
        )
    return ToolExecutionPolicy()


# 声明式覆盖：工具可携带 `execution_policy` 属性（dict 或实例）
def resolve_policy(tool: Any) -> ToolExecutionPolicy:
    """Resolve a tool's effective policy.

    A tool that carries credentials is treated as TRUSTED here: the credential
    grant is approved explicitly (credential_grant approval) before such a tool
    can be registered, so execution-time is "user explicitly approved" mode.
    """
    raw = getattr(tool, "execution_policy", None)
    if isinstance(raw, ToolExecutionPolicy):
        policy = raw
        if policy.credentials and policy.level != CapabilityLevel.TRUSTED:
            from dataclasses import replace

            return replace(policy, level=CapabilityLevel.TRUSTED)
        return policy
    if isinstance(raw, dict):
        raw = ToolExecutionPolicy(**raw)
        if raw.credentials and raw.level != CapabilityLevel.TRUSTED:
            from dataclasses import replace

            return replace(raw, level=CapabilityLevel.TRUSTED)
        return raw
    definition = getattr(tool, "definition", None)
    if definition is not None:
        policy = default_policy_for(definition)
        if policy.credentials:
            from dataclasses import replace

            return replace(policy, level=CapabilityLevel.TRUSTED)
        return policy
    return ToolExecutionPolicy()
