"""Authorization policy (scheme C) and credential snapshots.

Resolution:
1. candidate keys: 状态 active、启用中、**通过过验证**、用途标签与 required_tags
   有交集、预算没用完；
2. ordering: 用户显式设的默认项优先 → `main-loop` 标签优先 → 预算余额降序 →
   key_id 稳定排序。

这三条都是安全语义，不是偏好：

- 主 Agent Loop 以前会优先选中「非 main-loop」标签的凭据（例如一个打 `vision`
  标签的 Key），于是专项凭据被主循环悄悄拿去用；现在主循环优先 `main-loop`，
  其它用途（chat/code/vision/research）只在没有 main-loop 可用时才回落；
- 显式默认项只影响**同一批合法候选之间**的先后：它不能绕过停用、撤销、预算、
  用途标签和验证状态 —— 「设为默认」永远只是排序，不是授权；
- 新建但没通过验证的凭据不进这里：用户可以重试验证，但不会在验证通过之前被
  任何任务自动拿去用。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from agent.credentials.store import USABLE_VERIFY_STATES, CredentialStore

PRESET_TAGS = [
    "main-loop",
    "subagent",
    "vision",
    "video",
    "audio",
    "research",
    "embedding",
]


class BudgetExhausted(RuntimeError):
    """这条凭据的累计用量上限已经用完：不再发新的模型请求。

    「没有上限」是另一回事：那种情况不抛，见 :func:`remaining_budget`。
    """

    def __init__(self, key_id: str, remaining: float = 0.0) -> None:
        super().__init__(f"凭据 {key_id} 的用量上限已耗尽（剩余 {remaining:.0f}）")
        self.key_id = key_id
        self.remaining = remaining


def remaining_budget(store: Any, key_id: str) -> float | None:
    """这条凭据还剩多少累计用量额度。

    * ``None`` = **没有设置上限**（无预算），不是「耗尽」；
    * ``<= 0`` = 已耗尽；
    * ``> 0`` = 还能发请求。

    拿不到账本（凭据不存在 / 读失败）同样返回 None —— 不把「查不到」当成耗尽。
    """
    reader = getattr(store, "budget_left", None)
    if not callable(reader):
        return None
    try:
        left = reader(key_id)
    except Exception:  # noqa: BLE001 - 账本读失败不得变成「请求失败」
        return None
    if left is None:
        return None
    try:
        return float(left)
    except (TypeError, ValueError):
        return None


def ensure_budget_available(store: Any, key_id: str) -> float | None:
    """实际请求前核对累计用量；已耗尽抛 :class:`BudgetExhausted`。

    返回剩余额度（``None`` 表示没有上限）。主循环、子任务、维护、后台提炼与
    内部重试共用这一处判断，不各自实现一套。
    """
    left = remaining_budget(store, key_id)
    if left is not None and left <= 0:
        raise BudgetExhausted(key_id, left)
    return left


@dataclass(frozen=True)
class CredentialRef:
    key_id: str
    version: int
    endpoint: str | None
    default_model: str | None
    tags: tuple[str, ...]
    budget_left: float | None
    is_default: bool = False
    verify_state: str = "legacy"


@dataclass
class Snapshot:
    """A frozen set of resolved credentials for one task/turn."""

    requestor: str
    required_tags: tuple[str, ...]
    refs: list[CredentialRef]
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def by_tag(self, tag: str) -> CredentialRef | None:
        for ref in self.refs:
            if tag in ref.tags:
                return ref
        return None

    def env(self, secrets: dict[str, str]) -> dict[str, str]:
        """Environment-variable injection map (never part of prompts)."""
        env: dict[str, str] = {}
        for ref in self.refs:
            secret = secrets.get(ref.key_id)
            if secret is not None:
                env[f"QIO_KEY_{ref.key_id.upper().replace('-', '_')}"] = secret
        return env


class CredentialPolicy:
    def __init__(self, store: CredentialStore) -> None:
        self.store = store
        # 进程内默认凭据库：真实 adapter 在**每次实际请求**上自动归因（见
        # credentials/usage.py）。弱引用，不会把应用/测试的临时库钉住不放。
        from agent.credentials.usage import set_default_accounting_store

        set_default_accounting_store(store)

    def resolve(
        self, requestor: str, required_tags: list[str], limit: int | None = None
    ) -> list[CredentialRef]:
        required = set(required_tags)
        candidates: list[CredentialRef] = []
        for meta in self.store.list_credentials():
            if meta["status"] != "active":
                continue
            if not meta.get("enabled", 1):
                continue
            if (meta.get("verify_state") or "unverified") not in USABLE_VERIFY_STATES:
                continue
            if not (set(meta["tags"]) & required):
                continue
            # 预算口径只有一处：remaining_budget（None = 没有上限，不是耗尽）。
            budget_left = remaining_budget(self.store, meta["id"])
            if budget_left is not None and budget_left <= 0:
                continue
            candidates.append(
                CredentialRef(
                    key_id=meta["id"],
                    version=meta["version"],
                    endpoint=meta.get("endpoint"),
                    default_model=meta.get("default_model"),
                    tags=tuple(meta["tags"]),
                    budget_left=budget_left,
                    is_default=bool(meta.get("is_default")),
                    verify_state=str(meta.get("verify_state") or "unverified"),
                )
            )
        # 显式默认项优先，其次 main-loop，其余按预算余额降序，最后按 key_id 保证稳定。
        candidates.sort(
            key=lambda r: (
                0 if r.is_default else 1,
                0 if "main-loop" in r.tags else 1,
                -(r.budget_left if r.budget_left is not None else float("inf")),
                r.key_id,
            )
        )
        return candidates if limit is None else candidates[:limit]

    def snapshot(self, requestor: str, required_tags: list[str]) -> Snapshot:
        refs = self.resolve(requestor, required_tags)
        return Snapshot(
            requestor=requestor,
            required_tags=tuple(required_tags),
            refs=refs,
        )
