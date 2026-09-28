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
            budget_left = self.store.budget_left(meta["id"])
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
