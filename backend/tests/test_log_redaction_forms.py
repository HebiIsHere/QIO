"""日志打码的**形态覆盖**：msg / args / exc_info / stack_info，以及已知边界。

为什么单列一个文件：Agent B 报了真实泄漏 —— 派生链里 logger.warning(..., exc) 的异常消息
可能夹带请求原文；全仓还有 40+ 处同类站点。全局 LogRecord 工厂是唯一现实的全覆盖手段，
但「只改 msg / args」的过滤器有一个经典陷阱：exc_info=True 产生的 traceback 由 Formatter
渲染（record.exc_text / formatException），不经过 msg。这里用真实 Formatter 输出做证据。
"""

from __future__ import annotations

import io
import logging

import pytest

from agent.trace.redact import REDACTED, clear_registered_secrets, register_secret

SECRET = "Zx9Qm2Wv7Lp4Secret"
SHAPED = "sk-live-abcdef1234567890"


@pytest.fixture(autouse=True)
def _clean_registry():
    clear_registered_secrets()
    yield
    clear_registered_secrets()


def _logger_with_formatter() -> tuple[logging.Logger, io.StringIO]:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    logger = logging.getLogger(f"agent.tests.forms.{id(stream)}")
    logger.handlers = [handler]
    logger.setLevel(logging.INFO)
    logger.propagate = False
    return logger, stream


def test_a_traceback_with_a_registered_secret_is_redacted(caplog):
    register_secret(SECRET)
    logger = logging.getLogger("agent.tests.forms.caplog")

    try:
        raise RuntimeError(f"upstream 400: 我的密钥是 {SECRET}")
    except Exception:
        with caplog.at_level(logging.WARNING, logger="agent.tests.forms.caplog"):
            logger.warning("call failed", exc_info=True)

    assert SECRET not in caplog.text
    # 栈没有被整段吞掉：异常类型与调用点还在（可诊断）
    assert "RuntimeError" in caplog.text
    assert "call failed" in caplog.text


def test_a_traceback_with_a_key_shaped_secret_is_redacted_by_a_real_formatter():
    logger, stream = _logger_with_formatter()

    try:
        raise RuntimeError(f"upstream 400: token {SHAPED} / {SECRET}")
    except Exception:
        logger.warning("call failed", exc_info=True)

    out = stream.getvalue()
    # 形状兜底：没登记过、但明显是密钥形状的 → 正则打掉
    assert SHAPED not in out
    assert REDACTED in out
    assert "RuntimeError" in out
    assert "upstream 400" in out  # 失败原因还看得懂
    # 已知边界：既没登记、又不像密钥的值，正则认不出来 —— 这正是「登记表是主防线」
    # 的原因（这里刻意用一个非密钥形状的假值来钉住这条边界）。
    assert SECRET in out


def test_an_exception_passed_through_args_is_redacted(caplog):
    register_secret(SECRET)
    logger = logging.getLogger("agent.tests.forms.args")
    exc = RuntimeError(f"provider rejected key {SECRET}")

    with caplog.at_level(logging.WARNING, logger="agent.tests.forms.args"):
        logger.warning("call failed: %s", exc)
        logger.warning("two args: %s / %s", exc, f"token {SHAPED}")

    assert SECRET not in caplog.text
    assert SHAPED not in caplog.text
    assert "provider rejected key" in caplog.text  # 说明还看得懂失败原因


def test_a_secret_in_a_dict_argument_is_redacted(caplog):
    register_secret(SECRET)
    logger = logging.getLogger("agent.tests.forms.dictargs")

    with caplog.at_level(logging.WARNING, logger="agent.tests.forms.dictargs"):
        logger.warning("payload %(body)s", {"body": f"secret={SECRET}"})

    assert SECRET not in caplog.text


def test_stack_info_is_kept_but_still_redacted(caplog):
    """stack_info 也会渲染栈文本：保留栈（可诊断），但同样过一遍打码。"""
    logger = logging.getLogger("agent.tests.forms.stack")

    with caplog.at_level(logging.WARNING, logger="agent.tests.forms.stack"):
        logger.warning("call failed %s", SHAPED, stack_info=True)

    assert SHAPED not in caplog.text
    assert "Stack (most recent call last)" in caplog.text


def test_ordinary_tracebacks_are_not_mangled(caplog):
    """不误伤：没有密钥的堆栈保持完整可读（文件名 / 行号 / 异常类型都在）。"""
    logger = logging.getLogger("agent.tests.forms.plain")

    try:
        raise ValueError("plain failure without any secret")
    except Exception:
        with caplog.at_level(logging.WARNING, logger="agent.tests.forms.plain"):
            logger.warning("plain call failed", exc_info=True)

    assert "ValueError: plain failure without any secret" in caplog.text
    assert "test_log_redaction_forms.py" in caplog.text  # 文件行还在
    assert REDACTED not in caplog.text


def test_the_stack_info_field_itself_is_redacted():
    """直接对记录做单元级验证：stack_info 文本本身就是被替换过的（不是靠 msg 顺带过）。"""
    from agent.trace.redact import _redact_log_record

    record = logging.LogRecord(
        name="x", level=logging.WARNING, pathname=__file__, lineno=1,
        msg="msg", args=None, exc_info=None,
    )
    record.stack_info = f"Stack (most recent call last):\n  File \"{SHAPED}\", line 1"

    _redact_log_record(record)

    assert SHAPED not in (record.stack_info or "")
    assert "Stack (most recent call last)" in (record.stack_info or "")


def test_a_directly_constructed_log_record_is_not_covered():
    """已知边界（文档化）：直接构造 LogRecord 不经过工厂 → 不受保护。

    这不是「应该修」的漏洞，而是这套机制的适用面：它拦的是 logging 正常路径
    （logger.* → 工厂 → handler），拦不住有人绕过 logging 自己拼字符串写文件。
    """
    register_secret(SECRET)
    record = logging.LogRecord(
        name="manual",
        level=logging.WARNING,
        pathname=__file__,
        lineno=1,
        msg=f"manual record with {SECRET}",
        args=None,
        exc_info=None,
    )
    assert SECRET in record.getMessage()
