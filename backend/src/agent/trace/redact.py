"""Unified redaction for trace payloads.

Traces must be safe-to-inspect by default: no API keys, tokens, cookies or
credential material may ever be persisted, streamed, or returned by the API.
This module is the single choke point for that guarantee.

打码分三层，**不靠正则猜密钥长什么样**这一条路径：

1. **字段名**（结构化）：键名命中 (`password` / `secret` / `api_key` /
   `authorization` / `cookie` …) 的整个值直接替换，不管值长什么样 ——
   也包含工具 schema 自己声明的 `secret_fields`；
2. **已知密钥登记表**（exact match）：真正在用的凭据值登记在这里，出现即替换。
   这是唯一能挡住「形状不像密钥」的密钥（随机串、密码、自定义 token）的办法；
   登记表只活在进程内存里：不落库、不进日志、不发事件、**不送给模型**；
   对外只能问到条数（`registered_secret_count`），拿不到值；
3. **形状正则**（兜底）：Bearer / sk-·pk-·rk- / JWT / URL 里的 user:pass /
   PEM 私钥块 / `key=value` 形式。

字符串里嵌的 JSON 文档会先按结构打码再序列化回去，避免「正则把 JSON 里的
引号一起吃掉」这种既难看又可能漏掉的情况。
"""

from __future__ import annotations

import json
import re
import threading
from typing import Any, Iterable

REDACTED = "***redacted***"

# 太短的值不当已知密钥：一个字符的「密钥」会把整段文本打烂，而且登记它没有意义。
_MIN_SECRET_LENGTH = 6

# 字段名命中即整体替换（大小写不敏感）。
# 注意：token 用 (?!s) 排除 total_tokens / output_tokens 这类计数字段。
_SECRET_FIELD = re.compile(
    r"(?i)("
    r"pass(word|wd|phrase)?|secret|token(?!s)|api[_-]?key|apikey|"
    r"authorization|cookie|credential|private[_-]?key|qio_key|"
    r"(^|_)key$|^key$"
    r")"
)

# 文本内联模式（兜底）
_INLINE_PATTERNS = [
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]+"), "Bearer " + REDACTED),
    (re.compile(r"(?i)\b(sk|pk|rk)-[A-Za-z0-9_\-]{6,}"), REDACTED),
    # 单独出现的 QIO_KEY_WEATHER_KEY 之类环境变量名不是值，但也可能是值的一部分，
    # 于是把它后面的 `=值` 交给下面的 kv 规则处理。
    # URL 里内嵌的 user:password（http://user:pass@host）
    (
        re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*://)[^/\s:@]+:[^/\s@]+@"),
        lambda m: m.group(1) + REDACTED + "@",
    ),
    # JWT（三段式）：不打码的话它整条都是可用凭据
    (
        re.compile(r"\beyJ[A-Za-z0-9_\-]{6,}\.[A-Za-z0-9_\-]{6,}\.[A-Za-z0-9_\-]{2,}"),
        REDACTED,
    ),
    # PEM 私钥块（含多行内容）
    (
        re.compile(
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"
        ),
        REDACTED,
    ),
    # key: value / key=value 形式
    (
        re.compile(
            r"(?i)\b(api[_-]?key|authorization|cookie|token|secret|password|"
            r"qio_key_[A-Z0-9_]+)\s*[:=]\s*\S+"
        ),
        lambda m: m.group(0).split(":", 1)[0].split("=", 1)[0].strip() + "=" + REDACTED,
    ),
]

# 已知密钥登记表：进程内、只用于匹配。
_secrets_lock = threading.Lock()
_known_secrets: list[str] = []  # 长的在前（先替换长的，避免被短的前缀先啃掉）
_known_secret_set: set[str] = set()

# 字符串里嵌的 JSON 最多再往下一层解析这么多次，避免构造出来的深嵌套文本
# 把打码变成指数级工作。
_MAX_JSON_DEPTH = 3


def register_secret(value: str | None, *, source: str | None = None) -> bool:
    """把一个**真的在用**的密钥登记进打码表（返回值：是否登记成功）。

    只存进进程内存。调用方（凭据库读取密钥的地方）不需要、也不应该把值写进
    任何日志：这里同样不记。
    """
    text = str(value or "")
    if len(text) < _MIN_SECRET_LENGTH:
        return False
    with _secrets_lock:
        if text in _known_secret_set:
            return False
        _known_secret_set.add(text)
        _known_secrets.append(text)
        _known_secrets.sort(key=len, reverse=True)
    return True


def register_secrets(values: Iterable[str | None]) -> int:
    """批量登记；返回新登记成功的条数（不是总条数）。"""
    added = 0
    for value in values:
        if register_secret(value):
            added += 1
    return added


def registered_secret_count() -> int:
    """登记表里有多少条（**只给条数**：值本身不对外暴露）。"""
    with _secrets_lock:
        return len(_known_secrets)


def clear_registered_secrets() -> None:
    """清空登记表（测试用；也用于凭据被删除后主动失效）。"""
    with _secrets_lock:
        _known_secrets.clear()
        _known_secret_set.clear()


def _replace_known_secrets(text: str) -> str:
    if not text:
        return text
    with _secrets_lock:
        secrets = tuple(_known_secrets)
    for secret in secrets:
        if secret in text:
            text = text.replace(secret, REDACTED)
    return text


def redact_text(text: str | None, *, secret_fields: set[str] | None = None) -> str:
    return _redact_text(text, secret_fields=secret_fields, depth=0)


def _redact_text(
    text: str | None, *, secret_fields: set[str] | None, depth: int
) -> str:
    if not text:
        return ""
    out = _replace_known_secrets(text)
    if depth < _MAX_JSON_DEPTH:
        out = _redact_embedded_json(out, secret_fields=secret_fields, depth=depth)
    for pattern, repl in _INLINE_PATTERNS:
        out = pattern.sub(repl, out)
    return out


def _redact_embedded_json(text: str, *, secret_fields: set[str] | None, depth: int) -> str:
    """字符串里嵌着完整 JSON 文档时按**结构**打码，而不是靠正则猜。

    只处理「整段就是一份 JSON 对象/数组」的情况：散文里的一段 JSON 片段不动，
    免得把无关文本重排。字段名规则与 `redact_any` 完全同一套。
    """
    stripped = text.strip()
    if not stripped or stripped[0] not in "[{":
        return text
    try:
        parsed = json.loads(stripped)
    except (ValueError, TypeError):
        return text
    if not isinstance(parsed, (dict, list)):
        return text
    cleaned = _redact_any(parsed, secret_fields=secret_fields, depth=depth + 1)
    try:
        return json.dumps(cleaned, ensure_ascii=False)
    except (TypeError, ValueError):
        return text


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
    return _redact_any(value, secret_fields=secret_fields, depth=0)


def _redact_any(value: Any, *, secret_fields: set[str] | None, depth: int) -> Any:
    if isinstance(value, dict):
        return {
            str(k): redact_value(str(k), v, secret_fields=secret_fields)
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact_any(v, secret_fields=secret_fields) for v in value]
    if isinstance(value, str):
        return _redact_text(value, secret_fields=secret_fields, depth=depth)
    return value


def preview(value: Any, limit: int = 400, *, secret_fields: set[str] | None = None) -> str:
    """Redacted, truncated string preview for trace storage."""
    if value is None:
        return ""
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(redact_any(value, secret_fields=secret_fields), ensure_ascii=False)
        except (TypeError, ValueError):
            text = str(value)
    text = redact_text(text, secret_fields=secret_fields)
    return text if len(text) <= limit else text[:limit] + "…"


def assert_clean(payload: Any, secrets: list[str]) -> None:
    """Test/guard helper: raise if any raw secret leaks into the payload."""
    blob = payload if isinstance(payload, str) else str(payload)
    for s in secrets:
        if s and s in blob:
            raise AssertionError(f"secret leaked into trace payload: {s[:4]}…")
