"""Tool execution policy: what a tool is *allowed to do*.

Key principle: a restricted subprocess is **not** a security sandbox. It lowers
accidental-damage risk but cannot safely run arbitrary untrusted AI code. The
policy below therefore states capabilities explicitly, instead of relying on
"which credential it uses".

Capability levels:
    PURE (0)       — default for AI-generated tools: no credential, no network,
                     no arbitrary shell, only a controlled scratch/temp dir,
                     strict timeout + output limit.
                     ⚠️ 这是**策略声明**，不是 OS 级隔离：没有 Docker 时生成代码跑在
                     同权限子进程里，谎报能力的工具仍能碰用户文件（见
                     tools/sandbox.py 的边界声明）。因此执行生成工具本身
                     必须由用户批准，能力指纹变化后必须重新批准。
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


# 受限子进程**没有**保护的东西：把它写成一份可被测试与文案共用的清单，
# 而不是散落在各处的形容词。用户文件 / 网络 / 任意进程 / 环境 都在同一用户
# 权限下，凭据不注入但用户自己的凭据文件仍可读，QIO 数据目录同样可读写。
# 真实强制隔离（容器 / AppContainer / 受限令牌 / 专用用户）见
# docs/security/tool-execution-isolation.md 的分阶段设计。
SUBPROCESS_UNPROTECTED_SURFACES = (
    "用户文件（家目录、桌面、文档等能读就能读，能写就能写）",
    "网络（不受限；没有按域名/端口拦截）",
    "任意进程（可以再拉起别的程序）",
    "环境（子进程环境已裁剪，但同用户下的其它信息仍可读）",
    "QIO 数据目录（同用户权限下可读写）",
    "凭据（不注入，但用户自己的凭据文件仍可读）",
)


def isolation_for_executor(executor: str | None) -> IsolationLevel:
    """真实执行器 → 它**实际**提供的隔离等级。

    只有探测到 docker 才算容器隔离；拿不到执行器、或它是别的值时按受限子进程
    记 —— 宁可少报隔离，也不在策略里写一个不存在的容器。
    """
    return (
        IsolationLevel.CONTAINER
        if str(executor or "") == "docker"
        else IsolationLevel.SUBPROCESS
    )


def isolation_label(executor: str | None) -> str:
    """用户可见的隔离说法**唯一来源**：受限子进程不许被叫成安全沙箱。"""
    if str(executor or "") == "docker":
        return "Docker 容器（隔离执行）"
    return "受限子进程（同一用户权限，不是安全沙箱）"


def unprotected_surfaces(executor: str | None) -> tuple[str, ...]:
    """这个执行器下**没有**被保护的面。容器分支只声明容器本身挡住的那些。"""
    if str(executor or "") == "docker":
        return ()
    return SUBPROCESS_UNPROTECTED_SURFACES


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


def default_policy_for(
    definition: Any, *, executor: str | None = None
) -> ToolExecutionPolicy:
    """Derive a policy for an agent-created tool definition.

    AI-generated function tools are PURE unless they explicitly reference a
    credential, which promotes them to RESTRICTED with that category only.

    `executor` = 实际会用的执行器（`SandboxExecutor.effective_executor()`）。
    给了它就按**真实隔离等级**记（subprocess / docker），指纹也跟着变 —— 换
    执行环境必须重新批准，而不是沿用旧授权。

    不给执行器时保持历史默认值不变：已批准工具的
    `approved_policy_fingerprint` 是按这个默认值算的，在这里改默认值会让所有
    已注册工具在升级后一次性「指纹不匹配」而不被恢复。要写清真实隔离的那条
    路径（执行授权、审批说明）一律显式传 executor。
    """
    cred = getattr(definition, "credential_ref", None)
    isolation = (
        IsolationLevel.CONTAINER if executor is None else isolation_for_executor(executor)
    )
    if cred:
        return ToolExecutionPolicy(
            level=CapabilityLevel.RESTRICTED,
            credentials=(str(cred),),
            side_effect=SideEffect.READ,
            isolation=isolation,
        )
    return ToolExecutionPolicy(isolation=isolation)


def policy_fingerprint(policy: ToolExecutionPolicy) -> str:
    """Stable hash of the capability-relevant fields.

    Used to detect "policy changed" so a widened policy forces re-approval
    instead of silently inheriting an old grant.
    """
    import hashlib
    import json

    payload = {
        "level": int(policy.level),
        "isolation": policy.isolation.value,
        "network": policy.network,
        "network_allow": list(policy.network_allow),
        "filesystem": list(policy.filesystem),
        "shell": policy.shell,
        "process": policy.process,
        "credentials": list(policy.credentials),
        "side_effect": policy.side_effect.value,
        "concurrency": policy.concurrency.value,
        "resource_scope": policy.resource_scope,
    }
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def effective_concurrency(tool: Any) -> Concurrency:
    """Adapter: rich policy if declared, else legacy `is_concurrency_safe` bool."""
    raw = getattr(tool, "execution_policy", None)
    if isinstance(raw, ToolExecutionPolicy):
        return raw.concurrency
    if isinstance(raw, dict) and raw.get("concurrency"):
        return Concurrency(raw["concurrency"])
    return (
        Concurrency.PARALLEL
        if getattr(tool, "is_concurrency_safe", False)
        else Concurrency.SERIALIZED
    )


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
