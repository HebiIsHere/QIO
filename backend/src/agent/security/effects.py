"""统一授权公共类型：Effect / Target / Verdict / AuthDecision（契约 §2.1）。

判定以**实际操作效果**为准，不用工具名或工作目录代替。写入/删除类目标必须
能给出文件身份键（可延迟填，见 §7）；这里只承载数据，不实现判定。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class EffectKind(str, Enum):
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


#: 会改变磁盘/系统状态的 effect（只读模式一律拒绝）。
MUTATING_EFFECTS: frozenset[EffectKind] = frozenset(
    {
        EffectKind.WRITE,
        EffectKind.CREATE,
        EffectKind.DELETE,
        EffectKind.EXECUTE,
        EffectKind.INSTALL_DEPS,
        EffectKind.SYSTEM_CHANGE,
        EffectKind.INTERNAL_DATA,
        EffectKind.TOOL_REGISTER,
    }
)


@dataclass(frozen=True)
class Target:
    kind: str  # "path" | "url" | "program" | "record"
    value: str
    identity: str | None = None  # 文件身份键（§7），写入/删除类必填（可延迟填）
    exists: bool | None = None


@dataclass(frozen=True)
class Effect:
    kind: EffectKind
    targets: tuple[Target, ...] = ()
    purpose: str = ""  # 人类可读用途
    tool: str | None = None
    detail: dict = field(default_factory=dict)  # 命令 argv、内容摘要、origin 等
    reversible: bool = False

    def path_targets(self) -> tuple[Target, ...]:
        return tuple(t for t in self.targets if t.kind == "path")

    def origin(self) -> str:
        """效果归属：qio_generated / user_saved_material / user_original（可缺省）。"""
        return str(self.detail.get("origin") or "")


class Verdict(str, Enum):
    ALLOW = "allow"
    APPROVE = "approve"
    DENY = "deny"


#: 结构化决策码（授权行为只认它，不解析中文 reason）。
DECISION_UNRESOLVED_LIMITS = "unresolved_limits_requires_confirmation"
DECISION_PROGRAM_NAME_UNRESOLVED = "program_name_requires_object_binding"


@dataclass(frozen=True)
class AuthDecision:
    verdict: Verdict
    reason: str  # 中文、可直接展示
    approval_id: str | None = None
    effects: tuple[Effect, ...] = ()
    scope_id: str | None = None
    #: 受限模式下"需要模型授权审查"的候选（R9-F）。审查不可用时仍回退为普通审批，
    #: 但调用方/审计能看出这次判定没有经过自然语言授权理解。
    needs_review: bool = False
    #: R9-H5 §5：结构化决策码；调用方只能据此判断“本次具体批准可否完成”，
    #: 不得用 reason 文本、语言或关键词推断权限。
    decision_code: str | None = None
