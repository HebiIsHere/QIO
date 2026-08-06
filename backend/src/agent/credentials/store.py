"""Secret storage in the OS credential backend (keyring) + metadata in SQLite.

Rules (agreed in design review):
- secrets never touch SQLite, logs, events, or API responses;
- keyring holds only the current version; history is audit-only;
- revoke deletes the secret so any reference fails loudly;
- metadata (tags/scope/budget/status) lives in the credentials table.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any

import keyring
from keyring.backends.fail import Keyring as FailKeyring

SERVICE_NAME = "qio"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class MemoryKeyring:
    """In-memory keyring for tests; mirrors the keyring.Keyring interface."""

    def __init__(self) -> None:
        self._data: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self._data.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self._data[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        self._data.pop((service, username), None)


class CredentialStore:
    """Coordinates keyring secrets with SQLite metadata and audit log."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        keyring_backend: Any | None = None,
        service: str = SERVICE_NAME,
    ) -> None:
        self.conn = conn
        self.service = service
        self._kr = keyring_backend or _default_keyring()

    # -- metadata helpers -------------------------------------------------

    def _row(self, key_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM credentials WHERE id = ?", (key_id,)
        ).fetchone()

    def _audit(
        self,
        credential_id: str,
        action: str,
        from_version: int | None,
        to_version: int | None,
        triggered_by: str,
    ) -> None:
        self.conn.execute(
            "INSERT INTO credential_audit (id, credential_id, action, from_version, "
            "to_version, triggered_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (_new_id("aud"), credential_id, action, from_version, to_version, triggered_by, _now()),
        )

    # -- lifecycle --------------------------------------------------------

    def create(
        self,
        key_id: str,
        secret: str,
        tags: list[str],
        endpoint: str | None = None,
        default_model: str | None = None,
        scope: list[str] | None = None,
        budget: float | None = None,
        note: str | None = None,
        triggered_by: str = "user",
    ) -> int:
        if not key_id or not key_id.strip():
            raise ValueError("key_id must not be empty")
        if self._row(key_id) is not None:
            raise ValueError(f"credential already exists: {key_id}")
        now = _now()
        self.conn.execute(
            "INSERT INTO credentials (id, version, tags, endpoint, default_model, scope, "
            "budget, budget_used, status, created_at, updated_at, note) "
            "VALUES (?, 1, ?, ?, ?, ?, ?, 0, 'active', ?, ?, ?)",
            (
                key_id,
                json.dumps(tags, ensure_ascii=False),
                endpoint,
                default_model,
                "null" if scope is None else json.dumps(scope, ensure_ascii=False),
                budget,
                now,
                now,
                note,
            ),
        )
        self._kr.set_password(self.service, key_id, secret)
        self._audit(key_id, "create", None, 1, triggered_by)
        return 1

    def update_secret(self, key_id: str, new_secret: str, triggered_by: str = "user") -> int:
        row = self._row(key_id)
        if row is None:
            raise KeyError(f"credential not found: {key_id}")
        new_version = int(row["version"]) + 1
        self.conn.execute(
            "UPDATE credentials SET version = ?, updated_at = ? WHERE id = ?",
            (new_version, _now(), key_id),
        )
        self._kr.set_password(self.service, key_id, new_secret)
        self._audit(key_id, "update", row["version"], new_version, triggered_by)
        return new_version

    def revoke(self, key_id: str, triggered_by: str = "user") -> None:
        row = self._row(key_id)
        if row is None:
            raise KeyError(f"credential not found: {key_id}")
        self.conn.execute(
            "UPDATE credentials SET status = 'revoked', updated_at = ? WHERE id = ?",
            (_now(), key_id),
        )
        try:
            self._kr.delete_password(self.service, key_id)
        except Exception:
            pass
        self._audit(key_id, "revoke", row["version"], row["version"], triggered_by)

    # -- reading ----------------------------------------------------------

    def get_secret(self, key_id: str) -> str | None:
        row = self._row(key_id)
        if row is None or row["status"] != "active":
            return None
        return self._kr.get_password(self.service, key_id)

    def get_metadata(self, key_id: str) -> dict[str, Any] | None:
        row = self._row(key_id)
        if row is None:
            return None
        return self._serialize(row)

    def list_credentials(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM credentials ORDER BY created_at"
        ).fetchall()
        return [self._serialize(r) for r in rows]

    def audit_log(self, key_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM credential_audit WHERE credential_id = ? ORDER BY created_at",
            (key_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def grant_tool_scope(self, key_id: str, tool_id: str) -> None:
        """Narrow a key scope to a specific tool (scheme C, strictest intersection).

        scope null (category default) becomes [tool_id]; otherwise tool_id is
        appended. Other requestors lose access once narrowed.
        """
        row = self._row(key_id)
        if row is None:
            raise KeyError(f"credential not found: {key_id}")
        scope = json.loads(row["scope"] or "null")
        if scope is None:
            scope = [tool_id]
        elif tool_id not in scope:
            scope.append(tool_id)
        self.conn.execute(
            "UPDATE credentials SET scope = ?, updated_at = ? WHERE id = ?",
            (json.dumps(scope, ensure_ascii=False), _now(), key_id),
        )

    def record_usage(self, key_id: str, tokens: int, usd: float | None = None) -> None:
        row = self._row(key_id)
        if row is None:
            return
        self.conn.execute(
            "UPDATE credentials SET budget_used = budget_used + ?, last_used_at = ? WHERE id = ?",
            (tokens, _now(), key_id),
        )

    def budget_left(self, key_id: str) -> float | None:
        row = self._row(key_id)
        if row is None:
            return None
        if row["budget"] is None:
            return None
        return float(row["budget"]) - float(row["budget_used"])

    def mark_active(self, key_id: str) -> None:
        self.conn.execute(
            "UPDATE credentials SET last_used_at = ? WHERE id = ?", (_now(), key_id)
        )

    # -- internals --------------------------------------------------------

    def _serialize(self, row: sqlite3.Row) -> dict[str, Any]:
        """Metadata only; the secret is never serialized."""
        data = dict(row)
        data["tags"] = json.loads(data.get("tags") or "[]")
        data["scope"] = json.loads(data.get("scope") or "null")
        return data


def _default_keyring() -> Any:
    kr = keyring.get_keyring()
    # Never silently fall back to a no-op backend.
    if isinstance(kr, FailKeyring):
        raise RuntimeError(
            "no usable OS credential backend; on Windows the Credential Manager "
            "must be available"
        )
    return kr