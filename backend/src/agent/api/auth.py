"""Local API session authentication.

Threat model: any web page the user visits can issue requests to 127.0.0.1.
So "it came from localhost" proves nothing. The QIO desktop shell generates a
256-bit random session token for this process, hands it to the backend and to
its own WebView; every QIO API call must carry that token.

Rules implemented here:

* token from `QIO_SESSION_TOKEN` (or generated and written to
  `QIO_SESSION_TOKEN_FILE`, 0600) — never persisted in SQLite, never logged;
* only the QIO WebView origin may talk to the API (Tauri origin in production,
  local dev servers only when development mode is explicitly enabled);
* the Host header must be loopback (defense against DNS rebinding);
* SSE cannot send headers, so it may use a single-use, short-lived,
  scope-limited ticket obtained through an authenticated request.
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path

TICKET_TTL_SECONDS = 60.0
TICKET_SCOPE_EVENTS = "events"
AUTH_HEADER = "authorization"
SESSION_HEADER = "x-qio-session"
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
# 这些路径不要求令牌：只返回进程存活状态，不泄露任何用户数据。
PUBLIC_PATHS = {"/api/health"}


def host_without_port(host_header: str | None) -> str:
    raw = (host_header or "").strip()
    if not raw:
        return ""
    if raw.startswith("["):  # [::1]:8734
        return raw.split("]", 1)[0].lstrip("[")
    if raw.count(":") > 1:  # bare IPv6
        return raw
    return raw.split(":", 1)[0]


def is_loopback_host(host_header: str | None) -> bool:
    return host_without_port(host_header) in LOOPBACK_HOSTS


def is_loopback_origin(origin: str) -> bool:
    from urllib.parse import urlparse

    parsed = urlparse(origin)
    if parsed.scheme not in ("http", "https"):
        return False
    return (parsed.hostname or "") in LOOPBACK_HOSTS


@dataclass
class _Ticket:
    scope: str
    expires_at: float


@dataclass
class SessionAuth:
    """Session token + origin/Host policy + SSE tickets."""

    token: str = ""
    enforce: bool = True
    dev: bool = False
    allowed_origins: tuple[str, ...] = ()
    ticket_ttl: float = TICKET_TTL_SECONDS
    generated: bool = False
    token_file: Path | None = None
    _tickets: dict[str, _Ticket] = field(default_factory=dict, repr=False)

    # -- token ------------------------------------------------------------

    @classmethod
    def from_settings(cls, settings) -> "SessionAuth":
        token = (settings.session_token or "").strip()
        generated = False
        if not token and settings.auth_required:
            # fail-closed：没有配置令牌就自己造一个，绝不静默变成「无认证」。
            token = secrets.token_urlsafe(32)
            generated = True
            if settings.session_token_file is not None:
                _write_token_file(settings.session_token_file, token)
        return cls(
            token=token,
            enforce=settings.auth_required,
            dev=bool(settings.dev_insecure),
            allowed_origins=tuple(settings.allowed_origins),
            generated=generated,
            token_file=settings.session_token_file,
        )

    @property
    def enabled(self) -> bool:
        return self.enforce and bool(self.token)

    # -- policy -----------------------------------------------------------

    def is_allowed_origin(self, origin: str | None) -> bool:
        if not origin:
            return True  # 非浏览器请求（无 Origin）：由令牌负责
        if origin in self.allowed_origins:
            return True
        # 开发模式：允许本机 dev server 的任意端口（明确区分 development）。
        return bool(self.dev and is_loopback_origin(origin))

    def check_request(
        self, *, path: str, method: str, headers: dict[str, str], ticket: str | None = None
    ) -> tuple[bool, str]:
        """Return (allowed, reason). reason 是给调用方记录用的短标识，不含密钥。"""
        if not self.enabled:
            # 显式开发豁免：API 本来就开放，Host/Origin 检查没有额外价值。
            return True, "auth_disabled"
        if not is_loopback_host(headers.get("host")):
            # 防 DNS rebinding：只有回环 Host 才能访问本机后端。
            return False, "host_not_loopback"
        if not self.is_allowed_origin(headers.get("origin")):
            return False, "origin_rejected"
        if method == "OPTIONS":
            return True, "preflight"
        if path in PUBLIC_PATHS:
            return True, "public"
        if self.verify_token(headers):
            return True, "token"
        if ticket is not None and self.consume_ticket(ticket, TICKET_SCOPE_EVENTS):
            return True, "ticket"
        return False, "unauthorized"

    def verify_token(self, headers: dict[str, str]) -> bool:
        if not self.token:
            return False
        presented = ""
        raw = headers.get(AUTH_HEADER, "")
        if raw.lower().startswith("bearer "):
            presented = raw[7:].strip()
        if not presented:
            presented = headers.get(SESSION_HEADER, "").strip()
        if not presented:
            return False
        return secrets.compare_digest(presented, self.token)

    # -- SSE tickets ------------------------------------------------------

    def issue_ticket(self, scope: str = TICKET_SCOPE_EVENTS) -> str:
        ticket = secrets.token_urlsafe(24)
        self._prune_tickets()
        self._tickets[ticket] = _Ticket(scope=scope, expires_at=time.monotonic() + self.ticket_ttl)
        return ticket

    def consume_ticket(self, ticket: str, scope: str = TICKET_SCOPE_EVENTS) -> bool:
        """一次性：无论成功失败都从表里删掉，杜绝 replay。"""
        self._prune_tickets()
        entry = self._tickets.pop(ticket, None)
        if entry is None:
            return False
        if entry.scope != scope:
            return False
        return entry.expires_at >= time.monotonic()

    def _prune_tickets(self) -> None:
        now = time.monotonic()
        expired = [t for t, e in self._tickets.items() if e.expires_at < now]
        for t in expired:
            self._tickets.pop(t, None)


def _write_token_file(path: Path, token: str) -> None:
    """把令牌写到受限文件，供 Tauri 壳读取后交给 WebView（不进日志）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(token, encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass  # Windows 上 chmod 语义有限；文件位于用户私有目录
