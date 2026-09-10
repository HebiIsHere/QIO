"""Unified redaction for trace payloads.

Traces must be safe-to-inspect by default: no API keys, tokens, cookies or
credential material may ever be persisted, streamed, or returned by the API.
This module is the single choke point for that guarantee.
"""

from __future__ import annotations

import re
from typing import Any

REDACTED = "***redacted***"

# 字段名命中即整体替换（大小写不敏感）。
# 注意：token 用 (?!s) 排除 total_tokens / output_tokens 这类计数字段。
_SECRET_FIELD = re.compile(
    r"(?i)("
    r"pass(word|wd|phrase)?|secret|token(?!s)|api[_-]?key|apikey|"
    r"authorization|cookie|credential|private[_-]?key|qio_key|"
    r"(^|_)key$|^key$"
    r")"
)

# 文本内联模式
_INLINE_PATTERNS = [
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]+"), "Bearer " + REDACTED),
    (re.compile(r"(?i)\b(sk|pk|rk)-[A-Za-z0-9_\-]{6,}"), REDACTED),
    # key: value / key=value 形式
    (
        re.compile(
            r"(?i)\b(api[_-]?key|authorization|cookie|token|secret|password|"
            r"qio_key_[A-Z0-9_]+)\s*[:=]\s*\S+"
        ),
        lambda m: m.group(0).split(":", 1)[0].split("=", 1)[0].strip() + "=" + REDACTED,
    ),
]


def redact_text(text: str | None) -> str:
    if not text:
        return ""
    out = text
    for pattern, repl in _INLINE_PATTERNS:
        out = pattern.sub(repl, out)
    return out


def is_secret_field(name: str) -> bool:
    return bool(_SECRET_FIELD.search(name or ""))


def redact_value(key: str, value: Any, *, secret_fields: set[str] | None = None) -> Any:
    """Redact one key/value pair. `secret_fields` = tool-schema declared secrets."""
    if secret_fields and key in secret_fields:
        return REDACTED
    if is_secret_field(key):
        return REDACTED
    return redact_any(value, secret_fields=secret_fields)


def redact_any(value: Any, *, secret_fields: set[str] | None = None) -> Any:
    if isinstance(value, dict):
        return {k: redact_value(str(k), v, secret_fields=secret_fields) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_any(v, secret_fields=secret_fields) for v in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


def preview(value: Any, limit: int = 400, *, secret_fields: set[str] | None = None) -> str:
    """Redacted, truncated string preview for trace storage."""
    if value is None:
        return ""
    if isinstance(value, str):
        text = value
    else:
        import json

        try:
            text = json.dumps(redact_any(value, secret_fields=secret_fields), ensure_ascii=False)
        except (TypeError, ValueError):
            text = str(value)
    text = redact_text(text)
    return text if len(text) <= limit else text[:limit] + "…"


def assert_clean(payload: Any, secrets: list[str]) -> None:
    """Test/guard helper: raise if any raw secret leaks into the payload."""
    blob = payload if isinstance(payload, str) else str(payload)
    for s in secrets:
        if s and s in blob:
            raise AssertionError(f"secret leaked into trace payload: {s[:4]}…")
