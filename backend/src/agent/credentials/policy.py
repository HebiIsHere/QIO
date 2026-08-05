"""Authorization policy (scheme C) and credential snapshots.

Scheme C: category-default authorization narrowed by per-key scope, taking
the strictest intersection. Resolution order:
1. candidate keys: status active AND tags intersect required_tags;
2. scope filter: key scope is null (category default) OR contains requestor;
3. ordering: budget remaining desc, then created_at asc (stable tie-break);
4. a key whose scope is a non-empty list is treated as "pinned" to those
   requestors only.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from agent.credentials.store import CredentialStore

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
    scope: tuple[str, ...] | None
    budget_left: float | None

    @property
    def pinned(self) -> bool:
        return self.scope is not None


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
            if not (set(meta["tags"]) & required):
                continue
            scope = tuple(meta["scope"]) if meta["scope"] else None
            if scope is not None and requestor not in scope:
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
                    scope=scope,
                    budget_left=budget_left,
                )
            )
        # budget remaining desc, then created_at asc for stability
        candidates.sort(
            key=lambda r: (
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