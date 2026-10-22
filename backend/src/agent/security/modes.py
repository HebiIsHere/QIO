"""权限模式：三模式 + 旧值映射（契约 docs/r9-contracts.md §1）。

产品事实：QIO 只有三种权限模式，且**只有用户**能提高模式。

* `read_only`（只读）：不能修改业务文件；
* `restricted`（受限，首次安装默认）：范围外/未获指令授权的操作需要用户确认；
* `unrestricted`（完全无限制）：普通操作不再加常规审批，但内部运行数据仍要
  连续三次确认（见 internal_data.py）。

旧版本的 `default / plan / accept-edits / bypass` 只作为**读入兼容**：
读取时映射到新值；新写入一律写新值。未知值回落到 restricted —— 不静默
提高权限（fail-closed）。
"""

from __future__ import annotations

from enum import Enum


class PermissionMode(str, Enum):
    READ_ONLY = "read_only"
    RESTRICTED = "restricted"  # 首次安装默认
    UNRESTRICTED = "unrestricted"


MODE_LABELS: dict[str, str] = {
    PermissionMode.READ_ONLY.value: "只读模式",
    PermissionMode.RESTRICTED.value: "受限模式",
    PermissionMode.UNRESTRICTED.value: "完全无限制模式",
}

LEGACY_MODE_MAP = {
    "default": "restricted",
    "plan": "read_only",
    "accept-edits": "restricted",
    "bypass": "unrestricted",
}

#: 新写入只写这个值；读取时按 normalize_mode 归一。
DEFAULT_MODE = PermissionMode.RESTRICTED

#: 持久化设置键（兼容既有数据）。
MODE_SETTING_KEY = "computer.permission_mode"

NEW_MODE_VALUES: frozenset[str] = frozenset(mode.value for mode in PermissionMode)
LEGACY_MODE_VALUES: frozenset[str] = frozenset(LEGACY_MODE_MAP)

#: 旧客户端可用的等价旧值（GET /api/security/mode 的 legacy_value）。
_MODE_TO_LEGACY = {
    PermissionMode.READ_ONLY.value: "plan",
    PermissionMode.RESTRICTED.value: "default",
    PermissionMode.UNRESTRICTED.value: "bypass",
}


def normalize_mode(value: object) -> PermissionMode:
    """把任意存储值/旧值归一为新模式；未知值 → restricted（不静默提高）。"""
    if isinstance(value, PermissionMode):
        return value
    text = str(value or "").strip().lower()
    if text in NEW_MODE_VALUES:
        return PermissionMode(text)
    if text in LEGACY_MODE_MAP:
        return PermissionMode(LEGACY_MODE_MAP[text])
    return DEFAULT_MODE


def is_new_mode_value(value: object) -> bool:
    """是否新值（只读/受限/完全无限制）。用于"只接受新值"的写入口校验。"""
    if isinstance(value, PermissionMode):
        return True
    return str(value or "").strip().lower() in NEW_MODE_VALUES


def is_valid_mode_value(value: object) -> bool:
    """新值或旧值都认可（旧设置接口的读入兼容）。"""
    if isinstance(value, PermissionMode):
        return True
    text = str(value or "").strip().lower()
    return text in NEW_MODE_VALUES or text in LEGACY_MODE_MAP


def label_for(value: object) -> str:
    return MODE_LABELS[normalize_mode(value).value]


def legacy_value_for(value: object) -> str:
    """模式对应的等价旧值（只用于兼容展示/旧客户端）。"""
    return _MODE_TO_LEGACY[normalize_mode(value).value]


def is_stricter(candidate: object, reference: object) -> bool:
    """candidate 是否比 reference 更严格（只读 > 受限 > 完全无限制）。"""
    order = {
        PermissionMode.UNRESTRICTED.value: 0,
        PermissionMode.RESTRICTED.value: 1,
        PermissionMode.READ_ONLY.value: 2,
    }
    return order[normalize_mode(candidate).value] > order[normalize_mode(reference).value]
