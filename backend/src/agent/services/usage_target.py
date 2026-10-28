"""用途 → (凭据, 模型) 的运行时解析（设置页改版 §8）。

为什么单独一个模块
------------------
基线只有一条「按 tags 解析」的路径（`CredentialPolicy.resolve`）：一把 Key 只能整体
被某个用途占用，同一个 Key 下的不同模型无法分给不同用途。本轮的数据模型是
「用途 → (credential_id, model_id|null)」，`model_id` 为空 = 跟随默认模型。

解析优先级（顺序本身就是语义，不是排序偏好）：

1. `model_usages` 里用户**显式**指定（`mode='explicit'` 且 `model_id` 非空）；
2. 默认模型 `settings['model.default']`（用户选的「日常用哪一家的哪个模型」）；
3. 基线 `CredentialPolicy.resolve` 回退（停用 / 撤销 / 验证状态 / 预算照样拦）。

数据来自哪里
------------
「用途分配 / 默认模型 / 按模型的可用性」由数据层 `agent.credentials.models`
（`ModelCatalogStore`，B 提供）负责读。本模块**不重复实现**那套判断：

* 数据层在（迁移 73 已跑）→ 直接用 `get_usage()` / `resolve_usage()`；
* 数据层不在（本分支独立运行 / 老库还没迁移）→ 退回本模块的**兼容读法**
  （看 `model_usages` 表在不在、`model.default` 键在不在），行为等价：
  「没有显式分配」→ 默认模型 → policy 回退。

四条必须守住的边界
------------------
* **未迁移的老库不能炸**：表 / 键不存在按「没有显式指定」处理，直接落到 policy 回退。
* **显式指定绝不静默换 Key**：显式指定的目标不可用（停用 / 撤销 / 过期 / 预算耗尽 /
  验证失败 / 密钥缺失）时抛 :class:`UsageUnavailable` 并带 reason_code。
* **默认层允许回退但必须留痕**：默认模型不可用时回退到 policy 候选，并把
  `degraded=True` + `degrade_reason` 写进解析结果（界面显示「默认模型不可用，已回退到 X」）。
  基线的「默认项只是排序、不是授权」语义保持不变。
* **不是所有用途都继承默认模型**：`video` / `audio` / `embedding` 的
  `inherits_default=False`，没有显式指定时如实报「沿用现有处理方式」；历史自定义
  标签 `custom:<tag>` 不产生任何执行能力。

这里只做**解析**（纯读），不建 adapter、不发请求、不写任何数据 —— 建 adapter 仍然走
`services/app.py::build_adapter_for_credential`（用量归因与预算闸门只有那一处）。
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from typing import Any

from agent.credentials.policy import (
    MAIN_LOOP_USAGE_TAGS,
    CredentialPolicy,
    remaining_budget,
)
from agent.credentials.store import USABLE_VERIFY_STATES
from agent.credentials.usages import (
    find_usage,
    is_custom_usage,
    legacy_tags_for_usage,
)

logger = logging.getLogger(__name__)

__all__ = [
    "UsageTarget",
    "UsageUnavailable",
    "SOURCE_EXPLICIT",
    "SOURCE_DEFAULT",
    "SOURCE_POLICY",
    "REASON_EXPLICIT_UNAVAILABLE",
    "REASON_NO_CREDENTIAL",
    "REASON_CREDENTIAL_MISSING",
    "REASON_CREDENTIAL_REVOKED",
    "REASON_CREDENTIAL_EXPIRED",
    "REASON_CREDENTIAL_DISABLED",
    "REASON_CREDENTIAL_UNVERIFIED",
    "REASON_BUDGET_EXHAUSTED",
    "REASON_SECRET_MISSING",
    "REASON_MODEL_MISSING",
    "REASON_MODEL_UNUSABLE",
    "REASON_NOT_INHERITED",
    "REASON_NO_CALL_PATH",
    "REASON_ADAPTER_UNAVAILABLE",
    "credential_problem",
    "default_policy_tags",
    "describe_embedding_impact",
    "is_explicit_assignment",
    "read_default_model",
    "read_usage_assignments",
    "resolve_shared_target",
    "resolve_usage_target",
    "usage_table_ready",
]

# 解析结果的来源（界面要能说清「这个用途现在凭什么在用它」）。
SOURCE_EXPLICIT = "explicit"  # 用户显式指定（model_usages.mode='explicit'）
SOURCE_DEFAULT = "default"  # 跟随默认模型（settings['model.default']）
SOURCE_POLICY = "policy"  # 基线凭据政策回退

# 不可用原因码：界面按码给文案，日志/测试按码断言，不靠解析中文。
REASON_EXPLICIT_UNAVAILABLE = "explicit_unavailable"
REASON_NO_CREDENTIAL = "no_credential"
REASON_CREDENTIAL_MISSING = "credential_missing"
REASON_CREDENTIAL_REVOKED = "credential_revoked"
REASON_CREDENTIAL_EXPIRED = "credential_expired"
REASON_CREDENTIAL_DISABLED = "credential_disabled"
REASON_CREDENTIAL_UNVERIFIED = "credential_unverified"
REASON_BUDGET_EXHAUSTED = "budget_exhausted"
REASON_SECRET_MISSING = "secret_missing"
REASON_MODEL_MISSING = "model_missing"
#: 数据层按模型给出的不可用（模型行不存在 / 该模型验证失败 / 缺少用途需要的能力）。
REASON_MODEL_UNUSABLE = "model_unusable"
REASON_NOT_INHERITED = "usage_not_inherited"
REASON_NO_CALL_PATH = "no_call_path"
#: 解析出了目标，但建 adapter 失败（端点 / 模型 / 协议缺失）：后台任务据此如实失败。
REASON_ADAPTER_UNAVAILABLE = "adapter_unavailable"

#: 哪些用途会「跟随默认模型」但仍然没有自己的历史标签：它们的 policy 回退必须与
#: 主对话同一批候选（否则迁移前后行为不一致：老库没有 model.default 时，
#: 后台三理会因为「没有标签」而突然没有可用凭据）。
_INHERIT_FROM_MAIN_LOOP = ("main-chat", "general-chat", "code")


class UsageUnavailable(RuntimeError):
    """这条用途当前没有可用的目标：如实带原因，不静默换用别的 Key。

    `reason_code` 供界面/测试判定，`message` 是给用户看的中文。
    文案里只出现凭据 id 与用途，**不出现**密钥、endpoint 或请求内容。
    """

    def __init__(
        self,
        usage_key: str,
        reason_code: str,
        message: str,
        *,
        credential_id: str | None = None,
        model_id: str | None = None,
        detail_code: str = "",
    ) -> None:
        super().__init__(message)
        self.usage_key = str(usage_key or "")
        self.reason_code = str(reason_code or REASON_NO_CREDENTIAL)
        self.message = str(message or "")
        self.credential_id = credential_id
        self.model_id = model_id
        # 更具体的原因码（例如「显式指定不可用」里究竟是停用还是预算耗尽）。
        # 界面既可以按 reason_code 走大类，也可以按 detail_code 给精确文案。
        self.detail_code = str(detail_code or "")

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": False,
            "usage_key": self.usage_key,
            "reason_code": self.reason_code,
            "message": self.message,
            "credential_id": self.credential_id,
            "model_id": self.model_id,
            "detail_code": self.detail_code,
        }


@dataclass(frozen=True)
class UsageTarget:
    """一次解析结果：**冻结**之后整轮/整个任务都用它。

    `model_id` 为空表示「用这条凭据自己的默认模型」（与基线
    `credentials.default_model` 的读法一致），由建 adapter 的那一处解析。
    """

    usage_key: str
    credential_id: str
    model_id: str | None = None
    source: str = SOURCE_POLICY
    #: 显式指定可能来自共用同一调用路径的兄弟用途（general-chat 优先于 main-chat）。
    matched_usage_key: str = ""
    #: 默认层不可用 → 回退到政策候选时置 True，并把原因写进下面两个字段。
    degraded: bool = False
    degrade_reason: str = ""
    degrade_message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": True,
            "usage_key": self.usage_key,
            "credential_id": self.credential_id,
            "model_id": self.model_id,
            "source": self.source,
            "matched_usage_key": self.matched_usage_key or self.usage_key,
            "degraded": self.degraded,
            "degrade_reason": self.degrade_reason,
            "degrade_message": self.degrade_message,
        }


# -- 数据层（B 的 ModelCatalogStore；不在时退回兼容读法） ----------------------


def _conn_of(store: Any) -> sqlite3.Connection | None:
    conn = getattr(store, "conn", None)
    return conn if isinstance(conn, sqlite3.Connection) else None


def usage_table_ready(conn: sqlite3.Connection | None) -> bool:
    """`model_usages` 表在不在（迁移前的老库返回 False，不抛异常）。"""
    if conn is None:
        return False
    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'model_usages'"
        ).fetchone()
    except sqlite3.Error:  # 连接已关 / 只读库等：按没有表处理
        return False
    return row is not None


def _catalog_store(store: Any, conn: sqlite3.Connection | None = None):
    """数据层的模型目录存储；拿不到（未合并 / 老库未迁移 / 构造失败）返回 None。

    为什么用「能力探测 + 构造」而不是模块顶部 import：本分支必须能独立跑
    （数据层没合入时用兼容读法），合并后自动切到唯一那份实现。
    """
    connection = conn if conn is not None else _conn_of(store)
    if connection is None or not usage_table_ready(connection):
        return None
    try:
        from agent.credentials.models import ModelCatalogStore
    except Exception:  # noqa: BLE001 - 数据层未合并：走兼容读法
        return None
    try:
        return ModelCatalogStore(connection, store)
    except Exception:  # noqa: BLE001 - 构造失败同样退化到兼容读法
        logger.warning("模型目录存储不可用，按兼容读法解析用途", exc_info=True)
        return None


def read_usage_assignments(
    store: Any, *, conn: sqlite3.Connection | None = None
) -> dict[str, dict[str, Any]]:
    """读出用途分配表（`usage_key -> {credential_id, model_id, mode}`）。

    表不存在、读失败、列被裁剪（老库 / 精简表）都不抛：返回空 dict 即「没有显式分配」。
    """
    connection = conn if conn is not None else _conn_of(store)
    if connection is None or not usage_table_ready(connection):
        return {}
    try:
        info = connection.execute("PRAGMA table_info(model_usages)").fetchall()
    except sqlite3.Error:
        return {}
    columns = {str(row[1]) for row in info}
    wanted = [name for name in ("credential_id", "model_id", "mode") if name in columns]
    select = ", ".join(["usage_key", *wanted])
    try:
        rows = connection.execute(f"SELECT {select} FROM model_usages").fetchall()
    except sqlite3.Error:  # 表在但读不了：不得变成「启动失败」
        logger.warning("读取 model_usages 失败，按没有用途分配处理", exc_info=True)
        return {}
    assignments: dict[str, dict[str, Any]] = {}
    for row in rows:
        try:
            data = dict(row)
        except (TypeError, ValueError):
            continue
        key = str(data.get("usage_key") or "").strip()
        if key:
            assignments[key] = data
    return assignments


def is_explicit_assignment(row: dict[str, Any] | None) -> bool:
    """这一行是不是「用户显式指定」。

    **只有 `mode == 'explicit'` 才算**（与数据层 `ModelCatalogStore._is_explicit` 同口径，
    两边不能各有一套判断）：

    * `model_id` 为空 = 跟随默认（契约 §6.1），不算显式；
    * 迁移 73 回填历史 `credentials.tags` 时写的是 `mode='legacy'` —— 历史标签只说明
      「这把 Key 以前服务过这个用途」，并不锁定模型，所以它**不**算显式指定，仍然走
      「默认模型 → 基线 policy」那条语义（否则老用户换过默认模型后会被旧标签钉住）；
    * `mode` 列缺失（极简测试表）时以「model_id 非空」为准 —— 只有显式写下的模型
      才可能非空。
    """
    if not isinstance(row, dict):
        return False
    if not str(row.get("model_id") or "").strip():
        return False
    mode = row.get("mode")
    if mode is None:
        return True
    return str(mode).strip().lower() == "explicit"


def read_default_model(settings_store: Any) -> dict[str, Any] | None:
    """读默认模型 `settings['model.default']`（JSON），脏值按「未设置」处理。"""
    getter = getattr(settings_store, "get", None)
    if not callable(getter):
        return None
    try:
        raw = getter("model.default", None)
    except Exception:  # noqa: BLE001 - 设置读失败 = 没有默认模型，不是崩溃理由
        logger.warning("读取 model.default 失败，按未设置处理", exc_info=True)
        return None
    if not raw:
        return None
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        logger.warning("model.default 不是合法 JSON，按未设置处理")
        return None
    if not isinstance(data, dict):
        return None
    credential_id = str(data.get("credential_id") or "").strip()
    if not credential_id:
        return None
    return {
        "credential_id": credential_id,
        "model_id": str(data.get("model_id") or "").strip() or None,
    }


# 数据层的中文原因 → 本模块的原因码（界面按码走，不解析中文）。
_REASON_CODE_BY_TEXT = {
    "连接不存在（可能已删除）": REASON_CREDENTIAL_MISSING,
    "连接已撤销：本机已移除密钥": REASON_CREDENTIAL_REVOKED,
    "连接已过期": REASON_CREDENTIAL_EXPIRED,
    "连接已停用": REASON_CREDENTIAL_DISABLED,
    "这条连接的累计用量上限已用完": REASON_BUDGET_EXHAUSTED,
    "这个模型在当前配置上验证没通过": REASON_MODEL_UNUSABLE,
    "该模型没有声明这个用途需要的能力": REASON_MODEL_UNUSABLE,
    "这个连接下没有记录这个模型": REASON_MODEL_UNUSABLE,
    # 连接级（数据层 credentials/models.py 与这里的 credential_problem 同一条事实）：
    # 未通过验证 / 密钥在本机不可读 —— 目录与运行时必须给出同一个 reason_code。
    "连接还没有通过验证": REASON_CREDENTIAL_UNVERIFIED,
    "这条连接的密钥在本机不可读": REASON_SECRET_MISSING,
}


def _reason_code_for(reason: str) -> str:
    text = str(reason or "").strip()
    if not text:
        return REASON_MODEL_UNUSABLE
    mapped = _REASON_CODE_BY_TEXT.get(text)
    if mapped:
        return mapped
    if text.startswith("该模型没有声明"):
        return REASON_MODEL_UNUSABLE
    if text.startswith("连接还没有通过验证"):
        # 连接级未验证（含失败态后缀「（上次验证没通过）」）。
        return REASON_CREDENTIAL_UNVERIFIED
    if text.startswith("这条连接的密钥"):
        return REASON_SECRET_MISSING
    if text.startswith("连接状态为"):
        return REASON_CREDENTIAL_REVOKED
    return REASON_MODEL_UNUSABLE


# -- 可用性：一处判断，三处共用 ------------------------------------------------


def credential_problem(store: Any, credential_id: str) -> tuple[str, str] | None:
    """这条凭据现在能不能真的发请求：不能就返回 (reason_code, 可读原因)。

    检查项与基线 policy 一致（状态 / 启用 / 验证 / 预算），外加「密钥还在不在」——
    撤销会删密钥，于是「元数据说 active 但密钥没了」必须在这里如实失败，
    而不是等建 adapter 时静默返回 None。

    顺序即语义（状态 → 启用 → 验证 → 预算 → 密钥）。目录侧
    （agent.credentials.models.ModelCatalogStore.connection_unusable_reason）按同一
    顺序、同一批事实判断：两处结论必须一致，否则界面会展示「可选但一发就失败」的目标。
    """
    key_id = str(credential_id or "").strip()
    if not key_id:
        return REASON_CREDENTIAL_MISSING, "用途指向的凭据为空"
    getter = getattr(store, "get_metadata", None)
    meta = getter(key_id) if callable(getter) else None
    if not isinstance(meta, dict):
        return REASON_CREDENTIAL_MISSING, f"凭据 {key_id} 已不存在（可能被删除）"
    status = str(meta.get("status") or "").strip()
    if status and status != "active":
        code = (
            REASON_CREDENTIAL_EXPIRED
            if status == "expired"
            else REASON_CREDENTIAL_REVOKED
        )
        return code, f"凭据 {key_id} 当前状态是 {status}（已撤销 / 过期 / 未激活）"
    if "enabled" in meta and not bool(meta.get("enabled", True)):
        return REASON_CREDENTIAL_DISABLED, f"凭据 {key_id} 已停用"
    verify_state = str(meta.get("verify_state") or "unverified")
    if verify_state not in USABLE_VERIFY_STATES:
        return (
            REASON_CREDENTIAL_UNVERIFIED,
            f"凭据 {key_id} 还没有通过验证（{verify_state}）",
        )
    left = remaining_budget(store, key_id)
    if left is not None and left <= 0:
        return REASON_BUDGET_EXHAUSTED, f"凭据 {key_id} 的累计用量上限已耗尽"
    secret_getter = getattr(store, "get_secret", None)
    if callable(secret_getter):
        try:
            secret = secret_getter(key_id)
        except Exception:  # noqa: BLE001 - 读不到密钥 = 这条凭据发不出请求
            secret = None
        if secret is None:
            return REASON_SECRET_MISSING, f"凭据 {key_id} 的密钥已不可用（可能已被撤销）"
    return None


def _has_model(store: Any, credential_id: str, model_id: str | None) -> bool:
    """这条目标至少有一个模型名可用（显式给的，或凭据自己的默认模型）。"""
    if str(model_id or "").strip():
        return True
    getter = getattr(store, "get_metadata", None)
    meta = getter(credential_id) if callable(getter) else None
    if not isinstance(meta, dict):
        return False
    return bool(str(meta.get("default_model") or "").strip())


def _target_problem(
    store: Any,
    catalog: Any,
    credential_id: str,
    model_id: str | None,
    *,
    catalog_usable: bool | None = None,
    catalog_reason: str = "",
) -> tuple[str, str] | None:
    """解析出的目标现在能不能用：不能就返回 (reason_code, 可读原因)。

    顺序有讲究：先看**连接级**事实（状态 / 启用 / 验证 / 预算 / 密钥），再看数据层的
    **按模型**结论（模型行是否存在、该模型是否验证失败、用途需要的能力是否声明）。
    前者是本模块的强判断 —— 数据层拿不到密钥，判断不了「密钥是否还在」。
    """
    problem = credential_problem(store, credential_id)
    if problem is not None:
        return problem
    if catalog_usable is False:
        text = str(catalog_reason or "").strip() or "该模型在当前配置上不可用"
        return _reason_code_for(text), text
    if catalog is None and not _has_model(store, credential_id, model_id):
        return (
            REASON_MODEL_MISSING,
            f"凭据 {credential_id} 没有可用模型，请先在设置里选择模型",
        )
    return None


def default_policy_tags(usage_key: str) -> list[str]:
    """这条用途落到 policy 回退时该用哪些 tags。

    * 有历史标签 → 就用它（`vision` → vision、`subagent` → subagent）；
    * 继承默认模型但**没有**历史标签的用途（后台三类 / 通用对话 / 写代码）→ 与主对话
      同一批候选：迁移前这些路径本来就用主对话的 adapter，不能因为新增用途分配就
      突然「没有可用凭据」。
    """
    key = str(usage_key or "").strip()
    tags = list(legacy_tags_for_usage(key))
    spec = find_usage(key)
    if key in _INHERIT_FROM_MAIN_LOOP:
        return list(MAIN_LOOP_USAGE_TAGS)
    if tags:
        return tags
    if spec is not None and spec.inherits_default:
        return list(MAIN_LOOP_USAGE_TAGS)
    return tags


# -- 读「用户给了什么」（数据层优先，兼容读法兜底） ---------------------------


def _explicit_ownership(store: Any, catalog: Any, usage_key: str) -> dict[str, Any] | None:
    """这条用途有没有**显式**指定：有就返回目标 +（能拿到时）数据层的可用性结论。"""
    if catalog is not None:
        try:
            usage = catalog.get_usage(usage_key)
            resolved = catalog.resolve_usage(usage_key)
        except Exception:  # noqa: BLE001 - 数据层读失败按「没有显式分配」处理
            logger.warning("读取用途分配失败（%s）", usage_key, exc_info=True)
            return None
        if not isinstance(usage, dict) or not str(usage.get("model_id") or "").strip():
            return None
        if str(usage.get("mode") or "").strip().lower() != "explicit":
            return None
        if not isinstance(resolved, dict) or resolved.get("source") != SOURCE_EXPLICIT:
            # 数据层认为这条不是显式（例如连接被删后只剩孤儿行）：按没有处理。
            return None
        return {
            "credential_id": str(resolved.get("credential_id") or "").strip(),
            "model_id": str(resolved.get("model_id") or "").strip() or None,
            "usable": bool(resolved.get("usable")),
            "reason": str(resolved.get("unavailable_reason") or ""),
        }
    row = read_usage_assignments(store).get(usage_key)
    if not is_explicit_assignment(row):
        return None
    return {
        "credential_id": str((row or {}).get("credential_id") or "").strip(),
        "model_id": str((row or {}).get("model_id") or "").strip() or None,
        "usable": None,
        "reason": "",
    }


def _default_ownership(catalog: Any, usage_key: str) -> dict[str, Any] | None:
    """默认模型指向哪里（含数据层给的可用性结论）。

    按**目标用途自己**去问：数据层的 `resolve_usage(usage_key)` 在「这条用途没有显式
    分配」时返回默认模型层；拿主对话去问会漏掉「主对话显式指定、其它用途跟随默认」这种
    完全合法的组合。
    """
    try:
        resolved = catalog.resolve_usage(usage_key)
    except Exception:  # noqa: BLE001 - 读失败按「没有默认模型」
        logger.warning("读取默认模型失败", exc_info=True)
        return None
    if isinstance(resolved, dict) and resolved.get("source") == SOURCE_DEFAULT:
        return {
            "credential_id": str(resolved.get("credential_id") or "").strip(),
            "model_id": str(resolved.get("model_id") or "").strip() or None,
            "usable": bool(resolved.get("usable")),
            "reason": str(resolved.get("unavailable_reason") or ""),
        }
    return None


# -- 解析 ----------------------------------------------------------------------


def resolve_usage_target(
    store: Any,
    settings_store: Any,
    usage_key: str,
    *,
    policy: CredentialPolicy | None = None,
    conn: sqlite3.Connection | None = None,
    policy_tags: list[str] | None = None,
) -> UsageTarget:
    """单用途解析：显式 → 默认模型 → 基线 policy。"""
    return resolve_shared_target(
        store,
        settings_store,
        [usage_key],
        policy=policy,
        conn=conn,
        policy_tags=policy_tags,
    )


def resolve_shared_target(
    store: Any,
    settings_store: Any,
    usage_keys: list[str] | tuple[str, ...],
    *,
    policy: CredentialPolicy | None = None,
    conn: sqlite3.Connection | None = None,
    policy_tags: list[str] | None = None,
    primary_key: str | None = None,
) -> UsageTarget:
    """共用同一条调用路径的多个用途一起解析（main-chat + general-chat）。

    `usage_keys` 的顺序即显式指定的优先级（通用对话显式指定优先于主对话）；
    `primary_key` 是真正被调用的那条路径（默认取 `usage_keys[0]`），
    也就是解析结果与错误文案里说的用途。

    不可用时抛 :class:`UsageUnavailable`：显式指定损坏**不降级**，
    默认模型损坏**降级为 policy** 但把 `degraded` 记进结果。
    """
    keys = [str(key or "").strip() for key in usage_keys]
    keys = [key for key in keys if key]
    if not keys:
        raise ValueError("usage_keys 不能为空")
    primary = str(primary_key or "").strip() or keys[0]
    if primary not in keys:
        keys = [primary, *keys]
    if is_custom_usage(primary):
        # 历史自定义标签只承载可见性与映射，没有执行能力（usages.py 的硬语义 3）。
        raise UsageUnavailable(
            primary,
            REASON_NO_CALL_PATH,
            "历史自定义标签没有对应的模型调用路径：它不会改变任何真实请求使用哪把 Key",
        )

    spec = find_usage(primary)
    label = spec.label if spec is not None else primary
    catalog = _catalog_store(store, conn)

    # 1. 显式指定（绝不静默替换）
    for key in keys:
        owned = _explicit_ownership(store, catalog, key)
        if owned is None:
            continue
        matched = find_usage(key)
        matched_label = matched.label if matched is not None else label
        credential_id = owned["credential_id"]
        model_id = owned["model_id"]
        problem = _target_problem(
            store,
            catalog,
            credential_id,
            model_id,
            catalog_usable=owned["usable"],
            catalog_reason=owned["reason"],
        )
        if problem is not None:
            detail_code, message = problem
            raise UsageUnavailable(
                primary,
                REASON_EXPLICIT_UNAVAILABLE,
                f"{matched_label}已显式指定 {credential_id or '(空)'}，但它现在不可用：{message}",
                credential_id=credential_id or None,
                model_id=model_id,
                detail_code=detail_code,
            )
        return UsageTarget(
            usage_key=primary,
            credential_id=credential_id,
            model_id=model_id,
            source=SOURCE_EXPLICIT,
            matched_usage_key=key,
        )

    inherits = True if spec is None else bool(spec.inherits_default)
    if not inherits:
        # video / audio / embedding：不继承默认对话模型（错维度、错能力）。
        raise UsageUnavailable(
            primary,
            REASON_NOT_INHERITED,
            f"{label}沿用现有处理方式，不继承默认对话模型；需要时请在设置里显式指定",
        )

    # 2. 默认模型（不可用 → 降级到 policy，但必须留痕）
    if catalog is not None:
        default = _default_ownership(catalog, primary)
    else:
        default = read_default_model(settings_store)
    degrade_reason = ""
    degrade_message = ""
    if default is not None:
        credential_id = str(default.get("credential_id") or "").strip()
        model_id = str(default.get("model_id") or "").strip() or None
        problem = _target_problem(
            store,
            catalog,
            credential_id,
            model_id,
            catalog_usable=default.get("usable"),
            catalog_reason=str(default.get("reason") or ""),
        )
        if problem is None:
            return UsageTarget(
                usage_key=primary,
                credential_id=credential_id,
                model_id=model_id,
                source=SOURCE_DEFAULT,
            )
        degrade_reason, degrade_message = problem
        logger.warning(
            "默认模型不可用，已回退到凭据政策候选：%s（%s）",
            degrade_reason,
            degrade_message,
        )

    # 3. 基线 policy 回退
    tags = list(policy_tags) if policy_tags else default_policy_tags(primary)
    active_policy = policy or CredentialPolicy(store, register_default_accounting=False)
    refs = active_policy.resolve(primary, tags)
    if not refs:
        if degrade_reason:
            raise UsageUnavailable(
                primary,
                degrade_reason,
                f"{label}没有可用目标：默认模型不可用（{degrade_message}），"
                "且没有其它满足条件的凭据",
                detail_code=degrade_reason,
            )
        raise UsageUnavailable(
            primary,
            REASON_NO_CREDENTIAL,
            f"没有可用于「{label}」的模型凭据：请先在设置里添加并验证一条可用凭据",
        )
    ref = refs[0]
    return UsageTarget(
        usage_key=primary,
        credential_id=ref.key_id,
        model_id=ref.default_model,
        source=SOURCE_POLICY,
        degraded=bool(degrade_reason),
        degrade_reason=degrade_reason,
        degrade_message=degrade_message,
    )


def describe_embedding_impact(
    store: Any,
    settings_store: Any,
    *,
    conn: sqlite3.Connection | None = None,
    backend_name: str | None = None,
    backend_dimensions: int | None = None,
) -> dict[str, Any]:
    """嵌入用途的事实接口：**只陈述事实**，不提供「看起来生效」的选择器。

    嵌入换模型会改变向量维度，既有索引必须重建才谈得上可用 —— 这件事由界面说明，
    但事实必须由后端给（前端不猜）。返回：

    * `assignment`：用户是否给 embedding 显式指定了外部模型（没有就是沿用本地方案）；
    * `backend` / `dimensions`：当前真实在用的嵌入后端与维度（拿不到就是 None，不编造）；
    * `reindex_required`：换外部模型后是否必须重建索引；
    * `note`：给用户看的一句话。
    """
    assignments = read_usage_assignments(store, conn=conn)
    row = assignments.get("embedding")
    explicit = is_explicit_assignment(row)
    assignment = None
    if explicit:
        assignment = {
            "credential_id": str((row or {}).get("credential_id") or "").strip() or None,
            "model_id": str((row or {}).get("model_id") or "").strip() or None,
        }
    note = (
        "嵌入当前沿用本地 ONNX / 现有检索链，不会调用对话模型"
        if not explicit
        else "已为嵌入显式指定外部模型：既有索引维度与之不一致时必须重建索引才能检索"
    )
    return {
        "usage_key": "embedding",
        "assignment": assignment,
        "follows_existing": assignment is None,
        "backend": backend_name,
        "dimensions": backend_dimensions,
        "reindex_required": bool(assignment is not None),
        "note": note,
    }
