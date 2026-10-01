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
3. **形状正则**（只是兜底，**不是主防线**）：Bearer / sk-·pk-·rk- / JWT /
   裸的 `qio_key_` 令牌 / URL 里的 user:pass / PEM 私钥块 / `key=value` 形式。

分工会被误解，所以写在这里：正则永远只能覆盖「长得像密钥」的东西 —— 随机串、
密码、自定义 token 它一概认不出。**主防线是第 2 层的精确登记**：真正在用的
凭据值一律从凭据读路径登记进来，出现即替换。正则只负责「这个值还没被登记过，
但它明显是密钥形状」这类兜底；加了新正则不等于可以少登记。

字符串里嵌的 JSON 文档会先按结构打码再序列化回去，避免「正则把 JSON 里的
引号一起吃掉」这种既难看又可能漏掉的情况。
"""

from __future__ import annotations

import json
import logging
import re
import threading
import traceback
from collections import OrderedDict
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
    # 裸的 qio_key_ 令牌（后面没有 : 或 =）。**必须排在 kv 那条之后**：
    # 先让 kv 规则把 `QIO_KEY_X=<值>` 的**值**一起打掉，再由这条把剩下的名字打掉；
    # 反过来先吃掉名字，kv 规则就再也看不到 `名字=值`，值会漏出去（实测过）。
    # qio_key 后面必须跟下划线：`qio_keyword_test` 这种词不会被误伤。
    (re.compile(r"(?i)\bqio_key_[A-Za-z0-9_\-]{6,}"), REDACTED),
]

# 已知密钥登记表：进程内、只用于匹配。**有界**：超过上限先淘汰最早登记的，
# 免得长期运行的进程把内存吃成一条新的可靠性问题。
MAX_KNOWN_SECRETS = 64

_secrets_lock = threading.Lock()
_known_secrets: "OrderedDict[str, None]" = OrderedDict()
_ordered_cache: tuple[str, ...] | None = None  # 长的在前，避免短前缀先啃掉长值

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
    global _ordered_cache
    with _secrets_lock:
        if text in _known_secrets:
            return False
        _known_secrets[text] = None
        while len(_known_secrets) > MAX_KNOWN_SECRETS:
            _known_secrets.popitem(last=False)
        _ordered_cache = None
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
    global _ordered_cache
    with _secrets_lock:
        _known_secrets.clear()
        _ordered_cache = None


def _ordered_secrets() -> tuple[str, ...]:
    """长的在前：短的前缀先替换会把长值啃成半截，剩下的仍然泄露。"""
    global _ordered_cache
    with _secrets_lock:
        cached = _ordered_cache
        if cached is None:
            cached = tuple(sorted(_known_secrets, key=len, reverse=True))
            _ordered_cache = cached
        return cached


def _replace_known_secrets(text: str) -> str:
    if not text:
        return text
    for secret in _ordered_secrets():
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
    免得把无关文本重排。

    与 `redact_any` 的关键差别：这里**只动字符串叶子**。字符串才是要交给模型 /
    落库的内容；把非字符串叶子也按字段名替换会改掉数据形状 —— 全量回归抓过一次：
    工具返回 {"has_key": true} 会变成 {"has_key": "***redacted***"}，类型都变了。
    结构化路径（`redact_any`）保留「整个子树替换」的强行为。
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
    cleaned = _redact_json_strings(parsed, secret_fields=secret_fields, depth=depth + 1)
    try:
        return json.dumps(cleaned, ensure_ascii=False)
    except (TypeError, ValueError):
        return text


def _is_secret_name(key: str, secret_fields: set[str] | None) -> bool:
    if secret_fields and key in secret_fields:
        return True
    return is_secret_field(key)


def _redact_json_strings(
    value: Any, *, secret_fields: set[str] | None, depth: int
) -> Any:
    """嵌在文本里的 JSON：只替换字符串，非字符串（bool/数字/null）原样保留。"""
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            name = str(key)
            if isinstance(item, str) and _is_secret_name(name, secret_fields):
                out[name] = REDACTED
            else:
                out[name] = _redact_json_strings(
                    item, secret_fields=secret_fields, depth=depth
                )
        return out
    if isinstance(value, (list, tuple)):
        return [
            _redact_json_strings(item, secret_fields=secret_fields, depth=depth)
            for item in value
        ]
    if isinstance(value, str):
        return _redact_text(value, secret_fields=secret_fields, depth=depth)
    return value


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

# ---------- 日志路径：密钥同样不许出现 ----------
#
# trace / 工具历史走的是 redact_any / redact_text；日志是另一条独立出口，
# 以前谁都没管它。这里用 LogRecord 工厂做全局兜底：任何 logger 打出来的
# 记录，在**进入任何 handler 之前**先过一遍打码。
#
# 为什么不是给 root logger 加 Filter：Filter 只作用于「直接打在它身上」的记录，
# 子 logger 向上传的记录不会经过 root 的 filter（只会经过 handler 的）。

_factory_lock = threading.Lock()
_log_redaction_installed = False
_previous_factory = None


def _redact_log_args(args: Any) -> Any:
    if isinstance(args, dict):
        return {key: _redact_log_args(value) for key, value in args.items()}
    if isinstance(args, tuple):
        return tuple(_redact_log_args(value) for value in args)
    if isinstance(args, str):
        return redact_text(args)
    return args


# 日志量远大于 trace：普通日志不该为了一条「没有任何可疑痕迹」的消息跑五条正则。
# 先做一次廉价的「有没有可能藏着密钥」预筛，命中才走完整打码。
_LOG_HINT = re.compile(
    r"(?i)sk-|pk-|rk-|bearer|://|-----begin|eyj|token|key|secret|password|cookie|authorization"
)


def _redact_log_message(text: str) -> str:
    """日志消息打码：已知密钥 exact match（便宜）+ 命中线索才跑完整规则。"""
    out = _replace_known_secrets(text)
    stripped = out.lstrip()
    if stripped[:1] in ("{", "[") or _LOG_HINT.search(out):
        return _redact_text(out, secret_fields=None, depth=0)
    return out


def _redact_log_record(record: logging.LogRecord) -> logging.LogRecord:
    """把一条日志记录打码（只做本地替换，值不出进程）。

    顺序很重要：**先把 % 参数合成完整消息，再打码**。反过来做会在消息模板上
    误伤格式串（例如模板里的 `token=%s` 会被 kv 规则吃掉 %s），结果是
    「not all arguments converted」这种日志系统自己的异常 —— 测试抓过一次。
    合成之后 msg 已是最终文本，args 清空（getMessage 不再二次格式化）。
    """
    try:
        message = record.getMessage()
    except Exception:  # noqa: BLE001 - 调用方格式串写错：至少把参数里的密钥挡掉
        if record.args:
            record.args = _redact_log_args(record.args)
        return record
    record.msg = _redact_log_message(message)
    record.args = None
    if record.exc_info and not record.exc_text:
        try:
            text = "".join(traceback.format_exception(*record.exc_info))
        except Exception:  # noqa: BLE001 - 拿不到 traceback 不算打码失败
            text = ""
        if text:
            record.exc_text = redact_text(text)
    return record


def install_log_redaction() -> bool:
    """装上日志打码（幂等）。返回本次是否真的装上了。"""
    global _log_redaction_installed, _previous_factory
    with _factory_lock:
        if _log_redaction_installed:
            return False
        _previous_factory = logging.getLogRecordFactory()

        def _factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
            record = _previous_factory(*args, **kwargs)
            return _redact_log_record(record)

        logging.setLogRecordFactory(_factory)
        _log_redaction_installed = True
        return True


def log_redaction_installed() -> bool:
    return _log_redaction_installed


# 导入即装：redact 模块本身就是「密钥不出现在持久化/输出路径」的唯一收口，
# 让日志这条路依赖各调用方记得安装，等于默认留了一条漏的路径。
install_log_redaction()
