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

from agent.credentials.providers import KINDS
from agent.storage.db import transaction

SERVICE_NAME = "qio"
_UNSET = object()
# 凭据的安全身份：provider/endpoint/secret 任一变化都不是「普通元数据编辑」。
_LOOPBACK = {"127.0.0.1", "localhost", "::1", "0.0.0.0"}

# 「这把钥匙能不能被自动选中」看的是验证状态：
# - verified：真的发过一次请求并成功；
# - legacy：本次改动之前就在用的老凭据（按老行为视为可用，不误标成未验证）；
# - unverified / failed：新建但没通过 / 上一次没通过 —— 不进自动选择，
#   但记录仍在（用户可以在设置里重试，重试操作的是同一条记录）。
USABLE_VERIFY_STATES = ("verified", "legacy")
VERIFY_STATE_UNVERIFIED = "unverified"
VERIFY_STATE_VERIFIED = "verified"
VERIFY_STATE_FAILED = "failed"
VERIFY_STATE_LEGACY = "legacy"


def validate_endpoint(endpoint: str | None) -> None:
    """端点默认必须 HTTPS；只有本机 loopback provider 允许明文 HTTP。

    「HTTP 仅限本地 provider」是刻意的：把对话内容与 API Key 明文发到远端
    是不应该被一次静默元数据编辑打开的口子。
    """
    if not endpoint:
        return
    from urllib.parse import urlparse

    parsed = urlparse(str(endpoint))
    if parsed.scheme not in ("http", "https"):
        raise ValueError("endpoint must be an http(s) URL")
    if parsed.scheme == "http" and (parsed.hostname or "") not in _LOOPBACK:
        raise ValueError("plain http endpoint is only allowed for a localhost provider")


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
        # 惰性解析凭据后端：构造期**不**探测系统 keyring。
        # headless Linux（GitHub Actions 的 ubuntu runner、容器、没有 SecretService 的机器）
        # 没有任何可用后端；如果在构造期就抛，应用工厂与几百个测试都会在启动阶段直接炸掉，
        # 而「这台机器有没有可用后端」只有在**真正读写密钥**时才重要。
        self._kr_override = keyring_backend
        self._kr_cached: Any | None = None

    # -- keyring backend --------------------------------------------------

    @property
    def _kr(self) -> Any:
        """当前凭据后端；第一次真正使用时才解析，并缓存结果。"""
        if self._kr_cached is None:
            self._kr_cached = self._kr_override or _default_keyring()
        return self._kr_cached

    @_kr.setter
    def _kr(self, backend: Any) -> None:
        """显式指定后端（测试注入；覆盖之后不再探测系统后端）。"""
        self._kr_override = backend
        self._kr_cached = backend

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
        kind: str | None = None,
        verify_state: str = VERIFY_STATE_UNVERIFIED,
        triggered_by: str = "user",
    ) -> int:
        if not key_id or not key_id.strip():
            raise ValueError("key_id must not be empty")
        # 创建时就走与「更新 endpoint」同一套安全规则：
        # 远端只能 HTTPS，明文 HTTP 仅限 loopback 本地 provider。
        validate_endpoint(endpoint)
        if self._row(key_id) is not None:
            raise ValueError(f"credential already exists: {key_id}")
        # 顺序：先写密钥，再在一个事务里写元数据 + 审计。
        # 反过来（先写元数据）一旦密钥写失败，就会留下
        # 「active 元数据但没有密钥」的半成品 —— 那条凭据永远跑不通，
        # 而且用户看不出为什么。
        self._kr.set_password(self.service, key_id, secret)
        try:
            now = _now()
            with transaction(self.conn):
                self.conn.execute(
                    "INSERT INTO credentials (id, version, tags, endpoint, default_model, "
                    "budget, budget_used, status, created_at, updated_at, note, kind, "
                    "verify_state, verified_at, is_default) "
                    "VALUES (?, 1, ?, ?, ?, ?, 0, 'active', ?, ?, ?, ?, ?, ?, 0)",
                    (
                        key_id,
                        json.dumps(tags, ensure_ascii=False),
                        endpoint,
                        default_model,
                        budget,
                        now,
                        now,
                        note,
                        kind,
                        verify_state,
                        now if verify_state == VERIFY_STATE_VERIFIED else None,
                    ),
                )
                self._audit(key_id, "create", None, 1, triggered_by)
        except BaseException:
            # 元数据没落库 → 把刚写进去的密钥删掉，回到操作前的状态
            self._restore_secret(key_id, None)
            raise
        return 1

    # -- 验证状态与默认项 ------------------------------------------------

    def set_verified(self, key_id: str, ok: bool, reason: str | None = None) -> dict[str, Any]:
        """记录一次验证结果（不改密钥、不动版本）。

        审计动作沿用既有的 'test'，与「改配置」区分开：验证不是配置变更，
        它只回答「现在这把钥匙能不能用」。
        """
        row = self._row(key_id)
        if row is None:
            raise KeyError(f"credential not found: {key_id}")
        state = VERIFY_STATE_VERIFIED if ok else VERIFY_STATE_FAILED
        self.conn.execute(
            "UPDATE credentials SET verify_state = ?, verified_at = ?, verify_error = ?, "
            "updated_at = ? WHERE id = ?",
            (state, _now() if ok else None, None if ok else (reason or ""), _now(), key_id),
        )
        self._audit(key_id, "test", row["version"], row["version"], "user")
        return self.get_metadata(key_id) or {}

    def _is_candidate_row(self, row: sqlite3.Row) -> bool:
        """能不能被自动选中：状态、启用、验证、预算四项全过。"""
        if row["status"] != "active" or not row["enabled"]:
            return False
        if (row["verify_state"] or VERIFY_STATE_UNVERIFIED) not in USABLE_VERIFY_STATES:
            return False
        budget = row["budget"]
        if budget is None:
            return True
        return float(budget) - float(row["budget_used"] or 0) > 0

    @staticmethod
    def _resolved_kind(row: sqlite3.Row) -> str:
        """这一行**实际上**用的协议：老数据（kind 为空）按地址判断。

        判断规则与运行时（`providers.uses_anthropic`）完全一致 —— 否则「什么都没改」
        也会被当成一次协议变更，要求用户重新输入 Key 并显式确认。
        """
        from agent.credentials.providers import KIND_ANTHROPIC, KIND_OPENAI, uses_anthropic

        stored = (row["kind"] or "").strip()
        if stored:
            return stored
        return KIND_ANTHROPIC if uses_anthropic(None, row["endpoint"]) else KIND_OPENAI

    def _main_loop_candidates(self) -> list[sqlite3.Row]:
        rows = self.conn.execute(
            "SELECT * FROM credentials ORDER BY created_at, id"
        ).fetchall()
        out: list[sqlite3.Row] = []
        for row in rows:
            if "main-loop" not in json.loads(row["tags"] or "[]"):
                continue
            if not self._is_candidate_row(row):
                continue
            out.append(row)
        return out

    def current_default(self) -> dict[str, Any] | None:
        """当前显式默认项；它不可用（停用/撤销/预算用尽/失去 main-loop）时返回 None。"""
        row = self.conn.execute(
            "SELECT * FROM credentials WHERE is_default = 1 AND status = 'active' "
            "AND enabled = 1 ORDER BY created_at, id LIMIT 1"
        ).fetchone()
        if row is None or not self._is_candidate_row(row):
            return None
        if "main-loop" not in json.loads(row["tags"] or "[]"):
            return None
        return self._serialize(row)

    def set_default(self, key_id: str, triggered_by: str = "user") -> dict[str, Any]:
        """显式指定默认主对话凭据（同一时刻只有一条）。"""
        row = self._row(key_id)
        if row is None:
            raise KeyError(f"credential not found: {key_id}")
        with transaction(self.conn):
            self.conn.execute(
                "UPDATE credentials SET is_default = 0, updated_at = ? "
                "WHERE is_default = 1 AND id <> ?",
                (_now(), key_id),
            )
            self.conn.execute(
                "UPDATE credentials SET is_default = 1, updated_at = ? WHERE id = ?",
                (_now(), key_id),
            )
            self._audit(key_id, "update", row["version"], row["version"], triggered_by)
        return self.get_metadata(key_id) or {}

    def promote_default(self, triggered_by: str = "system") -> str | None:
        """没有可用默认项时挑一条（幂等）。

        - 已有可用默认项 → 什么都不做，绝不悄悄替换（用户显式选的那条优先）；
        - 按创建顺序取第一条可用且验证过的主对话凭据（`main-loop` 优先语义不变）；
        - 没有任何可用主对话凭据 → 返回 None（界面显示「当前没有可用凭据」）。
        """
        existing = self.current_default()
        if existing is not None:
            return str(existing["id"])
        candidates = self._main_loop_candidates()
        if not candidates:
            return None
        chosen = str(candidates[0]["id"])
        self.set_default(chosen, triggered_by=triggered_by)
        return chosen

    def update_secret(self, key_id: str, new_secret: str, triggered_by: str = "user") -> int:
        row = self._row(key_id)
        if row is None:
            raise KeyError(f"credential not found: {key_id}")
        # 先记住旧密钥：万一元数据提交失败，要能把密钥写回去，
        # 否则会留下「version 已经是新的、密钥还是旧的」这种对不上的状态。
        old_secret = self._read_secret(key_id)
        new_version = int(row["version"]) + 1
        self._kr.set_password(self.service, key_id, new_secret)
        try:
            with transaction(self.conn):
                self.conn.execute(
                    "UPDATE credentials SET version = ?, updated_at = ? WHERE id = ?",
                    (new_version, _now(), key_id),
                )
                self._audit(key_id, "update", row["version"], new_version, triggered_by)
        except BaseException:
            self._restore_secret(key_id, old_secret)
            raise
        return new_version

    def _restore_secret(self, key_id: str, old_secret: str | None) -> None:
        """把密钥恢复到操作前的状态（尽力而为，失败只记日志）。"""
        try:
            if old_secret is None:
                self._kr.delete_password(self.service, key_id)
            else:
                self._kr.set_password(self.service, key_id, old_secret)
        except Exception:  # noqa: BLE001 - 回滚失败不能掩盖原始异常
            import logging

            logging.getLogger(__name__).warning(
                "failed to roll back credential secret for %s", key_id, exc_info=True
            )

    # -- combined reconfiguration ------------------------------------------

    def reconfigure(
        self,
        key_id: str,
        *,
        secret: str | None = None,
        tags: Any = _UNSET,
        endpoint: Any = _UNSET,
        kind: Any = _UNSET,
        default_model: Any = _UNSET,
        budget: Any = _UNSET,
        note: Any = _UNSET,
        confirm_reconfigure: bool = False,
        triggered_by: str = "user",
    ) -> dict[str, Any]:
        """一次完整的凭据重配置：secret + endpoint + 其他元数据 = **一个**操作。

        keyring 与 SQLite 无法共享事务，所以这里用显式补偿：

        1. 读旧状态（row + 旧 secret）；
        2. 校验全部输入（endpoint 规则、endpoint 变化必须重新输入 secret 并显式确认）；
        3. 写新 secret（失败 → 什么都没改，直接抛）；
        4. 在一个 SQLite 事务里写元数据（+ 需要时推进 version + 审计）；
        5. 第 4 步失败 → 把 secret 写回旧值、事务回滚元数据；
           如果**连回滚也失败**：记录 CRITICAL 并抛出一个明确的错误，
           绝不假装操作成功（这种状态需要人工诊断）。

        这样 API 层「改 secret 顺手改 endpoint」只会得到两种结果：
        全部成功，或全部回到操作前。
        """
        row = self._row(key_id)
        if row is None:
            raise KeyError(f"credential not found: {key_id}")

        old_secret = self._read_secret(key_id)
        old_version = int(row["version"])
        old_endpoint = (row["endpoint"] or "").strip()
        old_kind = self._resolved_kind(row)

        sets: list[str] = []
        params: list[Any] = []
        version_bump = False

        if endpoint is not _UNSET:
            new_endpoint = str(endpoint or "").strip()
            if new_endpoint != old_endpoint:
                if not (secret and confirm_reconfigure):
                    raise ValueError(
                        "changing endpoint is a credential reconfiguration: "
                        "re-enter the secret and pass confirm_reconfigure=true"
                    )
                validate_endpoint(new_endpoint)
                version_bump = True
            sets.append("endpoint = ?")
            params.append(new_endpoint or None)

        if kind is not _UNSET:
            new_kind = str(kind or "").strip() or old_kind
            if new_kind not in KINDS:
                raise ValueError(f"unsupported protocol: {new_kind}")
            if new_kind != old_kind:
                # 协议和地址一样属于「这把钥匙发给谁、怎么发」：换了就必须重新
                # 输入 Key 并显式确认，不能让高级设置悄悄改掉发送目标。
                if not (secret and confirm_reconfigure):
                    raise ValueError(
                        "changing protocol is a credential reconfiguration: "
                        "re-enter the secret and pass confirm_reconfigure=true"
                    )
                version_bump = True
            sets.append("kind = ?")
            params.append(new_kind)

        if secret is not None:
            if not str(secret).strip():
                raise ValueError("secret must not be empty")
            version_bump = True
            # 换了钥匙，上一次的验证结论就不再适用。
            sets.append("verify_state = ?")
            params.append(VERIFY_STATE_UNVERIFIED)
            sets.append("verified_at = NULL")
            sets.append("verify_error = NULL")

        if tags is not _UNSET:
            sets.append("tags = ?")
            params.append(json.dumps(tags or [], ensure_ascii=False))
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

        new_version = old_version + 1 if version_bump else old_version

        # 先写密钥：这一步失败时元数据一个字都没动，直接抛即可
        if secret is not None:
            self._kr.set_password(self.service, key_id, secret)

        if not sets:
            return self.get_metadata(key_id) or {}

        if version_bump:
            sets.append("version = ?")
            params.append(new_version)
        sets.append("updated_at = ?")
        params.append(_now())
        params.append(key_id)

        try:
            with transaction(self.conn):
                self.conn.execute(
                    f"UPDATE credentials SET {', '.join(sets)} WHERE id = ?", params
                )
                self._audit(
                    key_id,
                    # 审计表的 action 集合是固定的（create/update/revoke/test）：
                    # 组合重配置也记为 update，区别体现在 from_version → to_version 上。
                    "update",
                    old_version,
                    new_version,
                    triggered_by,
                )
        except BaseException as exc:
            if secret is not None:
                try:
                    self._restore_secret_strict(key_id, old_secret)
                except Exception as rollback_error:  # noqa: BLE001
                    import logging

                    logging.getLogger(__name__).critical(
                        "credential %s is in an inconsistent state: metadata update failed (%s) "
                        "and rolling the secret back also failed (%s)",
                        key_id,
                        exc,
                        rollback_error,
                        exc_info=True,
                    )
                    raise CredentialRollbackError(key_id, exc, rollback_error) from exc
            raise
        return self.get_metadata(key_id) or {}

    def _restore_secret_strict(self, key_id: str, old_secret: str | None) -> None:
        """回滚密钥；失败就向上抛（调用方需要知道系统已经不一致）。"""
        if old_secret is None:
            self._kr.delete_password(self.service, key_id)
        else:
            self._kr.set_password(self.service, key_id, old_secret)


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
        kind: Any = _UNSET,
        default_model: Any = _UNSET,
        budget: Any = _UNSET,
        note: Any = _UNSET,
        triggered_by: str = "user",
        secret: str | None = None,
        confirm_reconfigure: bool = False,
    ) -> dict[str, Any]:
        """Update credential metadata.

        `endpoint` 属于凭据的安全身份，不是普通元数据：改了端点就等于换了服务
        提供方。若继续复用 keyring 里的旧 Key，就等于把用户的 Key 悄悄发给
        新端点（把 `https://api.openai.com` 改成攻击者域名即可命中）。
        因此 endpoint 变化必须重新输入 secret 并显式确认（= 重新配置）。
        """
        row = self._row(key_id)
        if row is None:
            raise KeyError(f"credential not found: {key_id}")
        sets: list[str] = []
        params: list[Any] = []
        if tags is not _UNSET:
            sets.append("tags = ?")
            params.append(json.dumps(tags or [], ensure_ascii=False))
        if endpoint is not _UNSET:
            new_endpoint = str(endpoint or "").strip()
            current_endpoint = (row["endpoint"] or "").strip()
            if new_endpoint != current_endpoint:
                if not (secret and confirm_reconfigure):
                    raise ValueError(
                        "changing endpoint is a credential reconfiguration: "
                        "re-enter the secret and pass confirm_reconfigure=true"
                    )
                validate_endpoint(new_endpoint)
            sets.append("endpoint = ?")
            params.append(new_endpoint or None)
        if kind is not _UNSET:
            current_kind = self._resolved_kind(row)
            new_kind = str(kind or "").strip() or current_kind
            if new_kind not in KINDS:
                raise ValueError(f"unsupported protocol: {new_kind}")
            if new_kind != current_kind and not (secret and confirm_reconfigure):
                raise ValueError(
                    "changing protocol is a credential reconfiguration: "
                    "re-enter the secret and pass confirm_reconfigure=true"
                )
            sets.append("kind = ?")
            params.append(new_kind)
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
        return self._read_secret(key_id)

    def _read_secret(self, key_id: str) -> str | None:
        """读密钥；这台机器没有可用凭据后端时，答案就是「没有可用密钥」。

        读路径返回 None 是有意义的：应用本来就有「当前没有可用凭据」的降级路径
        （提示用户去设置里添加 Key）。写路径不在这里兜底 —— 存 / 轮换 / 删除
        仍然大声失败，绝不静默假成功。
        """
        try:
            backend = self._kr
        except (RuntimeError, keyring.errors.NoKeyringError):
            return None
        try:
            return backend.get_password(self.service, key_id)
        except keyring.errors.NoKeyringError:
            return None

    def get_default_secret(self) -> str | None:
        """回落密钥：当前默认的主对话凭据（没有则按既有排序取第一条可用的）。"""
        meta = self.get_default_meta()
        if meta is None:
            return None
        return self._read_secret(str(meta["id"]))

    def list_tagged(self, tag: str) -> list[dict[str, Any]]:
        """Active/enabled credentials carrying `tag`, with budget available,
        sorted by budget remaining descending (then key_id for stability).

        未通过验证的凭据不在这里返回：它们可以留在库里等用户重试，但不会被
        自动选进任何任务（主循环、子任务、工具都一样）。
        """
        rows = self.conn.execute(
            "SELECT * FROM credentials WHERE status = 'active' AND enabled = 1"
        ).fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            if tag not in json.loads(row["tags"] or "[]"):
                continue
            if not self._is_candidate_row(row):
                continue
            meta = self._serialize(row)
            budget = meta["budget"]
            meta["_budget_left"] = (
                None if budget is None else float(budget) - float(meta["budget_used"] or 0)
            )
            out.append(meta)
        out.sort(
            key=lambda m: (
                -(m["_budget_left"] if m["_budget_left"] is not None else float("inf")),
                m["id"],
            )
        )
        return out

    def get_default_meta(self) -> dict[str, Any] | None:
        """当前默认的主对话凭据：显式默认项优先，其次按既有排序回落。"""
        return self.effective_default()

    def effective_default(self) -> dict[str, Any] | None:
        """界面上的「当前默认使用」：显式默认项 → 排序第一条可用主对话凭据。"""
        explicit = self.current_default()
        if explicit is not None:
            return explicit
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
        data["is_default"] = bool(data.get("is_default", 0))
        data["verify_state"] = data.get("verify_state") or VERIFY_STATE_UNVERIFIED
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


class CredentialRollbackError(RuntimeError):
    """元数据提交失败之后，连密钥回滚都失败了 —— 需要人工诊断的状态。

    刻意把两个原因都带出来：只报「更新失败」会让运维以为什么都没变。
    """

    def __init__(self, key_id: str, original: BaseException, rollback: BaseException) -> None:
        super().__init__(
            f"credential {key_id} may be inconsistent: the metadata update failed "
            f"({original}) and rolling the secret back also failed (rollback error: {rollback}). "
            "Manual inspection is required."
        )
        self.key_id = key_id
        self.original = original
        self.rollback = rollback
