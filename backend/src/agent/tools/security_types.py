"""契约类型桥接（W2 执行边界使用）。

R9 契约 §2 把统一授权模型定义在 agent/security/ 包（W1 负责）。本 worktree
（r9/w2-exec）从契约提交拉出时该包尚未合入，因此这里做**只读的**类型桥接：

* agent.security 包存在（W1 合入后）→ **无条件 re-export** 契约里的权威类型；
  不保留同名字段的独立 dataclass、不做第二套定义；
* 包还不存在（本分支单独运行时）→ 提供与契约字段完全一致的临时数据类，
  仅用于让工具层按契约写调用、让测试用 fake gateway 跑起来。

这里**没有任何权限判定逻辑**：判定永远在 AuthorizationEngine（W1）里。

集成约定（Lead 已确认）：agent.security 合入后本模块所有名字都解析到
agent.security.*；若契约符号不在预期模块里，re-export 会尝试 engine/gateway
等备选模块，**不会**退回本地定义。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

# 先判断包是否存在：只要存在就必须 re-export 权威类型（Lead 集成检查项）。
try:
    import agent.security as _security_package  # noqa: F401

    _HAS_SECURITY = True
except Exception:  # noqa: BLE001 - 包尚未合入（W1 分支未合）
    _HAS_SECURITY = False

BRIDGED_FROM_SECURITY = _HAS_SECURITY


def _require(*names: str, modules: tuple[str, ...]):
    """在候选模块里找契约符号；找不到就明确报 ImportError（不退回本地定义）。"""
    last_error: Exception | None = None
    for module in modules:
        try:
            imported = __import__(module, fromlist=list(names))
        except Exception as exc:  # noqa: BLE001 - 模块可能还没写/正在开发
            last_error = exc
            continue
        missing = [name for name in names if not hasattr(imported, name)]
        if missing:
            last_error = ImportError(f"{module} 缺少契约符号：{', '.join(missing)}")
            continue
        return tuple(getattr(imported, name) for name in names)
    raise ImportError(
        "agent.security 已存在，但找不到契约符号 "
        + "、".join(names)
        + f"（候选模块：{'、'.join(modules)}）：{last_error}"
    )


if _HAS_SECURITY:
    # -- 契约 §2.1 公共类型（effects 模块；engine/gateway 为契约未写死位置的备选） --
    EffectKind, Target, Effect, Verdict, AuthDecision = _require(
        "EffectKind",
        "Target",
        "Effect",
        "Verdict",
        "AuthDecision",
        modules=(
            "agent.security.effects",
            "agent.security.engine",
            "agent.security",
        ),
    )
    # -- 契约 §5.3 待审批操作 --
    PendingOperation, ApprovalTicket = _require(
        "PendingOperation",
        "ApprovalTicket",
        modules=("agent.security.operations", "agent.security"),
    )
    # -- 契约 §2.4 子智能体能力子集 --
    (AgentScope,) = _require(
        "AgentScope",
        modules=("agent.security.context", "agent.security.instructions", "agent.security"),
    )
    # -- 契约 §2.2 指令与授权 --
    InstructionContext, Grant, Scope = _require(
        "InstructionContext",
        "Grant",
        "Scope",
        modules=("agent.security.instructions", "agent.security"),
    )
    # -- 契约 §1 权限模式 --
    PermissionMode, normalize_mode = _require(
        "PermissionMode",
        "normalize_mode",
        modules=("agent.security.modes", "agent.security"),
    )
    try:
        from agent.security.modes import MODE_LABELS  # type: ignore
    except Exception:  # noqa: BLE001 - 契约没写死常量位置；按契约 §1 的值给出
        MODE_LABELS = {
            "read_only": "只读模式",
            "restricted": "受限模式",
            "unrestricted": "完全无限制模式",
        }
else:

    class EffectKind(str, Enum):  # type: ignore[no-redef]
        READ = "read"
        WRITE = "write"
        CREATE = "create"
        DELETE = "delete"
        EXECUTE = "execute"
        INSTALL_DEPS = "install_deps"
        SYSTEM_CHANGE = "system_change"
        NETWORK_EGRESS = "network_egress"
        INTERNAL_DATA = "internal_data"
        TOOL_REGISTER = "tool_register"
        SUBAGENT = "subagent"

    @dataclass(frozen=True)
    class Target:  # type: ignore[no-redef]
        kind: str  # "path" | "url" | "program" | "record"
        value: str
        identity: str | None = None
        exists: bool | None = None

    @dataclass(frozen=True)
    class Effect:  # type: ignore[no-redef]
        kind: EffectKind
        targets: tuple = ()
        purpose: str = ""
        tool: str | None = None
        detail: dict = field(default_factory=dict)
        reversible: bool = False

    class Verdict(str, Enum):  # type: ignore[no-redef]
        ALLOW = "allow"
        APPROVE = "approve"
        DENY = "deny"

    @dataclass(frozen=True)
    class AuthDecision:  # type: ignore[no-redef]
        verdict: Any
        reason: str = ""
        approval_id: str | None = None
        effects: tuple = ()
        scope_id: str | None = None

    @dataclass
    class PendingOperation:  # type: ignore[no-redef]
        kind: str
        label: str = ""
        payload: dict = field(default_factory=dict)
        effect: dict = field(default_factory=dict)
        fingerprint: dict = field(default_factory=dict)
        instruction_id: str | None = None
        instruction_text: str | None = None
        purpose: str = ""
        mode_at_request: str = ""
        confirmations_required: int = 1

    @dataclass
    class ApprovalTicket:  # type: ignore[no-redef]
        approval_id: str
        state: str = "waiting"
        expires_at: str = ""
        confirmations_required: int = 1

    @dataclass
    class AgentScope:  # type: ignore[no-redef]
        """子智能体能力子集（父 grant 子集 + 深度 + 用途）。

        字段是按契约 §2.4 的说明给出的最小形状；W1 的权威定义合入后以它为准，
        security_gate.make_agent_scope 会按真实数据类的字段名适配构造。
        """

        scope_id: str = ""
        parent_grant_ids: tuple = ()
        depth: int = 1
        max_depth: int = 1
        purpose: str = ""

    @dataclass
    class Scope:  # type: ignore[no-redef]
        kind: str = "path"
        value: str = ""

    @dataclass
    class Grant:  # type: ignore[no-redef]
        grant_id: str = ""
        instruction_id: str = ""
        operations: frozenset = frozenset()
        objects: list = field(default_factory=list)
        purpose: str = ""
        source: str = "user_explicit"
        created_at: str = ""
        revoked: bool = False

    @dataclass
    class InstructionContext:  # type: ignore[no-redef]
        instruction_id: str = ""
        text: str = ""
        turn_id: str = ""
        topic_id: str | None = None
        created_at: str = ""
        grants: list = field(default_factory=list)

    class PermissionMode(str, Enum):  # type: ignore[no-redef]
        READ_ONLY = "read_only"
        RESTRICTED = "restricted"
        UNRESTRICTED = "unrestricted"

    MODE_LABELS = {
        "read_only": "只读模式",
        "restricted": "受限模式",
        "unrestricted": "完全无限制模式",
    }
    LEGACY_MODE_MAP = {
        "default": "restricted",
        "plan": "read_only",
        "accept-edits": "restricted",
        "bypass": "unrestricted",
    }

    def normalize_mode(value: str | None) -> "PermissionMode":
        """旧值映射 + 未知值→restricted（不静默提高）；与契约 §1 同义。"""
        text = str(value or "").strip()
        mapped = LEGACY_MODE_MAP.get(text, text)
        try:
            return PermissionMode(mapped)
        except ValueError:
            return PermissionMode.RESTRICTED


#: 契约未冻结 AuthContext 的字段（W1 可能在 gateway/engine 里定义）；工具层
#: 只用 None，不自行构造。
AuthContext = Any

__all__ = [
    "EffectKind", "Target", "Effect", "Verdict", "AuthDecision",
    "PendingOperation", "ApprovalTicket",
    "AgentScope", "InstructionContext", "Grant", "Scope",
    "PermissionMode", "normalize_mode", "MODE_LABELS",
    "AuthContext", "BRIDGED_FROM_SECURITY",
]
