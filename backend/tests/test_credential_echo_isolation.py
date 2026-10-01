"""D4 正面缺口（真实实验固化）：注入给工具的凭据被工具原样打印时，不许顺着
工具结果流进模型上下文 / 工具历史 / trace。

实验（2026-10-02，真实受限子进程沙箱）：写一个 `print(os.environ['QIO_KEY_X'])`
并把它返回/抛出的工具，逐条检查五个出口。修复前：

* ❌ `ToolResult.content`（交模型）带着密钥原文；
* ❌ `ToolResult.error`（失败兜底文案）带着密钥原文；
* ✅ 工具历史 / trace / SSE 预览 / 后端日志（打码表 + 日志工厂已挡住）。

修复点：`tools/runtime_tools.py`（CodeTool 成功与失败两条出口都过 `redact_text`）。
这里是固化的回归用例 —— 断言里有「凭据确实注入过」的证明，避免用例变成空洞断言。
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from agent.credentials.store import CredentialStore, MemoryKeyring
from agent.storage.tool_records import get_record, record_tool_call
from agent.tools.runtime_tools import CodeTool
from agent.tools.sandbox import SandboxExecutor
from agent.tools.spec import ToolDefinition
from agent.trace.redact import (
    REDACTED,
    clear_registered_secrets,
    preview,
    register_secret,
)
from agent.trace.store import TraceStore

# 形状不像密钥：只有「已知密钥登记表」挡得住它
SECRET = "Zx9Qm2Wv7Lp4Secret"

RETURNS_ENV = (
    "import os\n\n"
    "def run(**kwargs):\n"
    "    return {'value': os.environ.get('QIO_KEY_WEATHER_KEY')}\n"
)
PRINTS_ENV_THEN_FAILS = (
    "import os\n\n"
    "def run(**kwargs):\n"
    "    print(os.environ.get('QIO_KEY_WEATHER_KEY'))\n"
    "    raise ValueError(os.environ.get('QIO_KEY_WEATHER_KEY'))\n"
)


@pytest.fixture(autouse=True)
def _clean_registry():
    clear_registered_secrets()
    yield
    clear_registered_secrets()


def _store(db_conn: sqlite3.Connection) -> CredentialStore:
    store = CredentialStore(db_conn, keyring_backend=MemoryKeyring())
    store.create("weather-key", SECRET, tags=["research"], budget=100)
    return store


def _tool(store: CredentialStore, code: str, *, name: str) -> CodeTool:
    definition = ToolDefinition(
        name=name,
        description="把注入的凭据环境变量原样返回/打印（真实调试写法）",
        code=code,
        tests=[{"name": "t", "input": {}, "expect": {}}],
        credential_ref="weather-key",
    )
    return CodeTool(definition, SandboxExecutor(executor="subprocess"), credentials=store)


async def test_a_tool_that_returns_its_credential_is_masked_before_the_model(db_conn):
    store = _store(db_conn)
    result = await _tool(store, RETURNS_ENV, name="echo_env").run()

    assert result.ok is True
    assert SECRET not in (result.content or "")
    # 非空洞：工具确实拿到了凭据（返回值就是它），只是交出去之前被打码了
    assert json.loads(result.content)["value"] == REDACTED


async def test_a_tool_that_prints_its_credential_then_fails_is_masked(db_conn):
    store = _store(db_conn)
    result = await _tool(store, PRINTS_ENV_THEN_FAILS, name="print_env_then_fail").run()

    assert result.ok is False
    assert SECRET not in (result.error or "")
    assert SECRET not in (result.content or "")
    # 诊断里确实提到了这个异常（不是把整段吞掉），只是密钥被替换了
    assert "ValueError" in (result.error or "") or "ValueError" in (result.content or "")


async def test_the_tool_exit_paths_reach_history_and_trace_clean(db_conn):
    """按调用方真实的写法把结果写进工具历史与 trace，两处都不许有原文。"""
    store = _store(db_conn)
    result = await _tool(store, PRINTS_ENV_THEN_FAILS, name="print_env_then_fail").run()

    record_id = record_tool_call(
        db_conn,
        turn_id="turn-1",
        tool_name="print_env_then_fail",
        arguments={"env": SECRET},  # 参数通道也塞一次
        output=result.content or "",
        error=result.error or "",
        status="failed",
    )
    row = get_record(db_conn, record_id)
    assert SECRET not in row["output"]
    assert SECRET not in row["error"]
    assert SECRET not in json.dumps(row["arguments"], ensure_ascii=False)

    trace = TraceStore(db_conn)
    trace.begin("turn-1")
    trace.record_tool_run(
        "turn-1",
        {"tool": "print_env_then_fail", "output": result.content, "error": result.error},
    )
    assert SECRET not in json.dumps(trace.get("turn-1"), ensure_ascii=False)


def test_the_registry_is_what_blocks_the_delivery(db_conn):
    """机制证明：同一个值，登记前后在打码路径上的表现不同。"""
    raw = f"工具原样返回：{SECRET}"
    assert SECRET in preview(raw)  # 没登记过：打码表不认识它

    register_secret(SECRET)

    assert SECRET not in preview(raw)
    assert SECRET not in record_output(db_conn, raw)


def record_output(db_conn: sqlite3.Connection, raw: str) -> str:
    record_id = record_tool_call(
        db_conn, turn_id="turn-2", tool_name="x", arguments={}, output=raw
    )
    return get_record(db_conn, record_id)["output"]
