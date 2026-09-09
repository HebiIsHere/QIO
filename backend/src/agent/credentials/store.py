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
_UNSET = object()


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
            "INSERT INTO credentials (id, version, tags, endpoint, default_model, "
            "budget, budget_used, status, created_at, updated_at, note) "
            "VALUES (?, 1, ?, ?, ?, ?, 0, 'active', ?, ?, ?)",
            (
                key_id,
                json.dumps(tags, ensure_ascii=False),
                endpoint,
                default_model,
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

    def update_metadata(
        self,
        key_id: str,
        *,
        tags: Any = _UNSET,
        endpoint: Any = _UNSET,
        default_model: Any = _UNSET,
        budget: Any = _UNSET,
        note: Any = _UNSET,
        triggered_by: str = "user",
    ) -> dict[str, Any]:
        """Update credential metadata only (never the secret/version)."""
        row = self._row(key_id)
        if row is None:
            raise KeyError(f"credential not found: {key_id}")
        sets: list[str] = []
        params: list[Any] = []
        if tags is not _UNSET:
            sets.append("tags = ?")
            params.append(json.dumps(tags or [], ensure_ascii=False))
        if endpoint is not _UNSET:
            sets.append("endpoint = ?")
            params.append(endpoint)
        if default_model is not _UNSET:
            sets.append("default_model = ?")
            params.append(default_model)
        if budget is not _UNSET:
            sets.append("budget = ?")
            params.append(budget)
            sets.append("budget_used = 0")
        if note is not _UNSET:
            sets.append("note = ?")
            params.append(note)
        if not sets:
            return self.get_metadata(key_id) or {}
        sets.append("updated_at = ?")
        params.append(_now())
        params.append(key_id)
        self.conn.execute(
            f"UPDATE credentials SET {', '.join(sets)} WHERE id = ?", params
        )
        self._audit(key_id, "update", row["version"], row["version"], triggered_by)
        return self.get_metadata(key_id) or {}

    def set_enabled(
        self, key_id: str, enabled: bool, triggered_by: str = "user"
    ) -> dict[str, Any]:
        """Soft pause/resume. A disabled credential is not resolvable and its
        secret is not returned, but it is not revoked/removed."""
        row = self._row(key_id)
        if row is None:
            raise KeyError(f"credential not found: {key_id}")
        self.conn.execute(
            "UPDATE credentials SET enabled = ?, updated_at = ? WHERE id = ?",
            (1 if enabled else 0, _now(), key_id),
        )
        self._audit(key_id, "update", row["version"], row["version"], triggered_by)
        return self.get_metadata(key_id) or {}

    def delete(self, key_id: str) -> None:
        """Permanently remove a credential (secret, metadata, and audit history).

        Unlike revoke (which only marks status='revoked' and keeps history),
        this is a hard delete so the key can never reappear in listings.
        """
        row = self._row(key_id)
        if row is None:
            raise KeyError(f"credential not found: {key_id}")
        try:
            self._kr.delete_password(self.service, key_id)
        except Exception:
            pass
        self.conn.execute(
            "DELETE FROM credential_audit WHERE credential_id = ?", (key_id,)
        )
        self.conn.execute("DELETE FROM credentials WHERE id = ?", (key_id,))

    # -- reading ----------------------------------------------------------

    def get_secret(self, key_id: str) -> str | None:
        """Return the secret only when the credential is active and enabled."""
        row = self._row(key_id)
        if row is None or row["status"] != "active" or not row["enabled"]:
            return None
        return self._kr.get_password(self.service, key_id)

    def get_default_secret(self) -> str | None:
        """Fallback secret: the enabled/active `main-loop` credential with budget."""
        rows = self.conn.execute(
            "SELECT * FROM credentials WHERE status = 'active' AND enabled = 1 ORDER BY created_at"
        ).fetchall()
        best: sqlite3.Row | None = None
        best_left: float | None = None
        for row in rows:
            if "main-loop" not in json.loads(row["tags"] or "[]"):
                continue
            budget = row["budget"]
            used = float(row["budget_used"] or 0)
            left = None if budget is None else float(budget) - used
            if left is not None and left <= 0:
                continue
            if best is None or (left is None and best_left is not None) or (left is not None and (best_left is None or left > best_left)):
                best = row
                best_left = left
        if best is None:
            return None
        return self._kr.get_password(self.service, best["id"])

    def list_tagged(self, tag: str) -> list[dict[str, Any]]:
        """Active/enabled credentials carrying `tag`, with budget available,
        sorted by budget remaining descending (then key_id for stability)."""
        rows = self.conn.execute(
            "SELECT * FROM credentials WHERE status = 'active' AND enabled = 1"
        ).fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            if tag not in json.loads(row["tags"] or "[]"):
                continue
            meta = self._serialize(row)
            budget = meta["budget"]
            used = float(meta["budget_used"] or 0)
            left = None if budget is None else float(budget) - used
            if left is not None and left <= 0:
                continue
            meta["_budget_left"] = left
            out.append(meta)
        out.sort(
            key=lambda m: (
                -(m["_budget_left"] if m["_budget_left"] is not None else float("inf")),
                m["id"],
            )
        )
        return out

    def get_default_meta(self) -> dict[str, Any] | None:
        """Best active/enabled `main-loop` credential metadata (fallback target)."""
        tagged = self.list_tagged("main-loop")
        return tagged[0] if tagged else None

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
        data["enabled"] = bool(data.get("enabled", 1))
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
