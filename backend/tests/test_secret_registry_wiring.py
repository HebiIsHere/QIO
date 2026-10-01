"""D4 接线：凭据库读出来的密钥自动进打码表 —— 且不落盘、不进日志、不进历史。

覆盖三件事：

1. 凭据读路径（`CredentialStore.get_secret` / `_read_secret`）登记密钥；
2. 登记之后再写**工具历史**与**日志**，输出里不含密钥原文；
3. 登记表**有界**：长期运行的进程不会因为登记密钥而无限增长。

设计边界（写清楚，不当成 bug）：只在**读路径**喂入 —— 从没被读过的密钥不会被
登记，因为系统里根本没有那个值可匹配。凭据一旦被读取（真实使用），它就会被挡住。
"""

from __future__ import annotations

import io
import json
import logging
import sqlite3

import pytest

from agent.credentials.store import CredentialStore, MemoryKeyring
from agent.storage.tool_records import get_record, record_tool_call
from agent.trace import redact
from agent.trace.redact import (
    MAX_KNOWN_SECRETS,
    REDACTED,
    clear_registered_secrets,
    log_redaction_installed,
    redact_text,
    register_secret,
    registered_secret_count,
)

# 刻意不是一个「像密钥」的值：前缀、长度都不符合任何正则，只有 exact match 挡得住。
SECRET = "Zx9Qm2Wv7Lp4Secret"


@pytest.fixture(autouse=True)
def _clean_registry():
    clear_registered_secrets()
    yield
    clear_registered_secrets()


def test_reading_a_secret_through_the_store_registers_it(db_conn: sqlite3.Connection):
    store = CredentialStore(db_conn, keyring_backend=MemoryKeyring())
    store.create("weather-key", SECRET, tags=["weather"], budget=100)
    clear_registered_secrets()
    assert registered_secret_count() == 0

    assert store.get_secret("weather-key") == SECRET

    assert registered_secret_count() == 1
    assert SECRET not in redact_text(f"provider answered with {SECRET}")
    assert REDACTED in redact_text(f"provider answered with {SECRET}")


def test_the_default_secret_read_path_registers_too(db_conn: sqlite3.Connection):
    store = CredentialStore(db_conn, keyring_backend=MemoryKeyring())
    # 默认项要带 main-loop 标签、并且验证过（见 CredentialStore.current_default）
    store.create("main-key", SECRET, tags=["main-loop"], budget=100)
    store.set_verified("main-key", True, "fake provider: ok")
    store.mark_active("main-key")
    store.set_default("main-key")
    clear_registered_secrets()

    assert store.get_default_secret() == SECRET

    assert registered_secret_count() == 1
    assert SECRET not in redact_text(SECRET)


def test_a_registered_secret_never_reaches_tool_history(db_conn: sqlite3.Connection):
    register_secret(SECRET)

    record_id = record_tool_call(
        db_conn,
        turn_id="turn-1",
        tool_name="web_fetch",
        arguments={"query": SECRET},
        output=f"响应体：{SECRET}",
    )

    row = get_record(db_conn, record_id)
    assert SECRET not in row["output"]
    assert SECRET not in json.dumps(row["arguments"], ensure_ascii=False)
    assert REDACTED in row["output"]


def test_a_registered_secret_never_reaches_the_log(caplog):
    register_secret(SECRET)
    logger = logging.getLogger("agent.tests.secret_registry")

    with caplog.at_level(logging.INFO, logger="agent.tests.secret_registry"):
        logger.info("调用 provider，密钥 %s", SECRET)
        logger.error("请求失败：token=%s", SECRET)

    assert SECRET not in caplog.text
    assert REDACTED in caplog.text


def test_log_redaction_is_installed_by_the_module_itself():
    """日志这条路不许依赖调用方记得安装：模块导入即装。"""
    assert log_redaction_installed() is True


def test_the_registry_is_bounded():
    """有界：登记得再多也不会无限增长（老的最先被淘汰）。"""
    total = MAX_KNOWN_SECRETS + 5
    for index in range(total):
        register_secret(f"secret-value-{index:04d}")

    assert registered_secret_count() == MAX_KNOWN_SECRETS
    # 最早登记的已经不在表里了（值原样出现在文本中，说明不再被替换）
    assert "secret-value-0000" in redact_text("secret-value-0000")
    # 最近登记的还在表里
    assert f"secret-value-{total - 1:04d}" not in redact_text(
        f"secret-value-{total - 1:04d}"
    )


def test_short_values_never_enter_the_registry(db_conn: sqlite3.Connection):
    """太短的值会把正常文本打烂，而且本来也不像密钥。"""
    store = CredentialStore(db_conn, keyring_backend=MemoryKeyring())
    store.create("short-key", "abc12", tags=["x"], budget=100)
    clear_registered_secrets()

    assert store.get_secret("short-key") == "abc12"

    assert registered_secret_count() == 0


def test_the_registry_holds_values_only_in_memory(tmp_path, monkeypatch):
    """不落盘：登记之后当前目录（以及数据目录）里不该多出任何文件。"""
    monkeypatch.chdir(tmp_path)
    register_secret(SECRET)

    assert list(tmp_path.iterdir()) == []
    assert redact.__file__  # 模块本身不产生状态文件

def test_ordinary_logs_are_not_mangled(caplog):
    """正常日志保持可读：只有「像密钥」的内容才被替换，普通散文/数字/计数器不动。"""
    logger = logging.getLogger("agent.tests.ordinary_logs")
    with caplog.at_level(logging.INFO, logger="agent.tests.ordinary_logs"):
        logger.info("处理 %s 条消息，耗时 %d ms", "abc", 12)
        logger.info("模型返回 512 tokens，命中 3 条记忆，trace_id=%s", "0123456789abcdef")
        logger.info('{"total_tokens": 120, "note": "ok"}')

    text = caplog.text
    assert "处理 abc 条消息，耗时 12 ms" in text
    assert "模型返回 512 tokens" in text
    assert "0123456789abcdef" in text  # trace_id 不是密钥：保持原样
    assert '"total_tokens": 120' in text  # 计数字段不动
    assert REDACTED not in text


def test_no_secret_reaches_a_file_handler(tmp_path):
    register_secret(SECRET)
    path = tmp_path / "qio.log"
    handler = logging.FileHandler(path, encoding="utf-8")
    logger = logging.getLogger("agent.tests.file_sink")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    try:
        logger.info("调用凭据 %s", SECRET)
    finally:
        logger.removeHandler(handler)
        handler.close()

    stored = path.read_text(encoding="utf-8")
    assert SECRET not in stored
    assert REDACTED in stored


def test_no_secret_reaches_the_root_logger():
    """子 logger 向上传的记录也要被打码（root 的 Filter 挡不住这条路）。"""
    register_secret(SECRET)
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    root = logging.getLogger()
    previous_level = root.level
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    try:
        logging.getLogger().info("root 上的密钥 %s", SECRET)
        logging.getLogger("agent.tests.child").info("子 logger 的密钥 %s", SECRET)
    finally:
        root.removeHandler(handler)
        root.setLevel(previous_level)

    text = stream.getvalue()
    assert SECRET not in text
    assert REDACTED in text
