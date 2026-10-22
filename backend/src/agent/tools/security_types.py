"""契约类型桥接（W2 执行边界使用）。

R9 契约 §2 把统一授权模型定义在 agent/security/ 包（W1 负责）。本 worktree
（r9/w2-exec）从契约提交拉出时该包尚未合入，因此这里做**只读的**类型桥接：

* 包已经存在（W1 合入后）→ 直接导入契约里的权威类型，不做任何复制；
* 包还不存在（本分支单独运行时）→ 提供与契约字段完全一致的临时数据类，
  仅用于让工具层按契约写调用、让测试用 fake gateway 跑起来。

这里**没有**任何权限判定逻辑：判定永远在 AuthorizationEngine（W1）里。合入
W1 分支后本模块的所有名字都会解析到 agent.security.*，临时定义不会被使用。

引入本模块的意义：工具层可以无条件 from agent.tools.security_types import
Effect, EffectKind, Target, ... 按契约构造 effect，而不必等到 W1 合入才写代码。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

BRIDGED_FROM_SECURITY = True


def _import(module: str, *names: str):
    """逐个导入契约符号；整包不存在时返回 None（由调用方用本地定义兜底）。"""
    try:
        imported = __import__(module, fromlist=list(names))
    except Exception:  # noqa: BLE001 - 包可能只存在一部分（W1 正在开发）
        return None
    return tuple(getattr(imported, name, None) for name in names)


# -- agent/security/effects.py ---------------------------------------------

_effects = _import("agent.security.effects", "EffectKind", "Target", "Effect", "Verdict", "AuthDecision")
if _effects is not None and all(item is not None for item in _effects):
    EffectKind, Target, Effect, Verdict, AuthDecision = _effects  # type: ignore[assignment]
    BRIDGED_FROM_SECURITY = True
else:
    BRIDGED_FROM_SECURITY = False

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


# -- agent/security/operations.py（契约 §5.3） ------------------------------

_operations = _import(
    "agent.security.operations", "PendingOperation", "ApprovalTicket", "ExecutionContext", "ExecResult"
)
if _operations is not None and _operations[0] is not None and _operations[1] is not None:
    PendingOperation, ApprovalTicket = _operations[0], _operations[1]  # type: ignore[assignment]
else:

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


# -- agent/security/context.py / instructions.py ---------------------------

_context = _import("agent.security.context", "AgentScope")
if _context is not None and _context[0] is not None:
    AgentScope = _context[0]  # type: ignore[assignment]
else:

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

    _context = None

_instructions = _import("agent.security.instructions", "InstructionContext", "Grant", "Scope")
if _instructions is not None and _instructions[0] is not None:
    InstructionContext, Grant, Scope = _instructions  # type: ignore[assignment]
else:

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


# -- agent/security/modes.py（契约 §1） ------------------------------------

_modes = _import("agent.security.modes", "PermissionMode", "normalize_mode", "MODE_LABELS", "LEGACY_MODE_MAP", "DEFAULT_MODE")
if _modes is not None and _modes[0] is not None:
    PermissionMode, normalize_mode = _modes[0], _modes[1]  # type: ignore[assignment]
    MODE_LABELS = _modes[2] if _modes[2] is not None else {
        "read_only": "只读模式",
        "restricted": "受限模式",
        "unrestricted": "完全无限制模式",
    }
else:

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
