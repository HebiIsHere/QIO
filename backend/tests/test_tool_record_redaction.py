"""工具错误的打码：error 通道曾经是唯一的例外（密钥原文落库）。

真实缺陷（渗透实验复现，2026-10-02）：工具把凭据写进异常再抛出时，
tool_records.error 是**密钥原文**，而同一行的 output 已经是打码后的 —— 对照非常明显；
审计表 tool_calls.error 也一样。这正面违反 AGENTS.md「任何日志、事件、Trace、错误
信息、测试输出都不得出现密钥原文」。

修复：error 与 output 完全同构（redact_text + _clip + truncated 标记），
审计行（tool_calls）的参数 / 结果 / 错误也走同一个打码入口。
"""

from __future__ import annotations

import json

import pytest

from agent.adapters.base import ChatMessage, Completion, ToolCall
from agent.api.bus import EventBus
from agent.config import Settings
from agent.credentials.store import MemoryKeyring
from agent.services.app import AppContext
from agent.storage.db import connect
from agent.storage.migrate import apply_migrations
from agent.storage.tool_records import record_tool_call
from agent.tools.base import Tool, ToolResult

SECRET = "sk-LEAKCANARY0123456789abcdef"


class _LeakyTool(Tool):
    name = "leaky_tool"
    description = "把凭据写进异常再抛出来（渗透实验用）"
    parameters = {"type": "object", "properties": {}}

    async def run(self, **kwargs):
        raise RuntimeError(f"connect failed with api_key={SECRET}")


class _FailingTool(Tool):
    name = "failing_tool"
    description = "失败但返回（不走异常）"
    parameters = {"type": "object", "properties": {}}

    async def run(self, **kwargs):
        return ToolResult(ok=False, error=f"bad key {SECRET}", content=f"trace: {SECRET}")


class _OneToolCall:
    mode = "native"
    model = "m"

    def __init__(self, tool_name: str) -> None:
        self.tool_name = tool_name
        self.n = 0

    async def complete(self, messages, tools, **kwargs):
        self.n += 1
        if self.n == 1:
            return Completion(
                message=ChatMessage(
                    role="assistant",
                    content=None,
                    tool_calls=[ToolCall(id="c1", name=self.tool_name, arguments={})],
                )
            )
        return Completion(message=ChatMessage(role="assistant", content="结束"))


def _ctx(tmp_path, tool) -> AppContext:
    conn = connect(tmp_path / "app.db")
    apply_migrations(conn)
    ctx = AppContext(Settings(data_dir=tmp_path), conn, EventBus())
    ctx.credentials._kr = MemoryKeyring()
    ctx.registry.register(tool)
    return ctx


async def _run_turn(ctx: AppContext, tool_name: str) -> None:
    adapter = _OneToolCall(tool_name)

    async def _build():
        return adapter

    ctx.build_adapter = _build  # type: ignore[assignment]
    topic = ctx.topics.nodes.create_topic("打码").id
    await ctx.run_turn("调用工具", topic_id=topic)


def _blob(ctx: AppContext, sql: str) -> str:
    rows = ctx.conn.execute(sql).fetchall()
    return " | ".join(str(v) for row in rows for v in tuple(row))


async def test_tool_error_is_redacted_wherever_it_is_stored(tmp_path):
    ctx = _ctx(tmp_path, _LeakyTool())
    await _run_turn(ctx, "leaky_tool")

    history_error = _blob(ctx, "SELECT error FROM tool_records")
    history_output = _blob(ctx, "SELECT output FROM tool_records")
    audit_error = _blob(ctx, "SELECT error FROM tool_calls")
    audit_result = _blob(ctx, "SELECT result FROM tool_calls")
    audit_args = _blob(ctx, "SELECT arguments FROM tool_calls")
    trace_row = _blob(ctx, "SELECT * FROM turn_traces")

    # 打码后的文本里必须还能看出「这里原本是凭据」——不是把错误吞掉
    assert "***redacted***" in history_error, history_error
    for name, blob in (
        ("tool_records.error", history_error),
        ("tool_records.output", history_output),
        ("tool_calls.error", audit_error),
        ("tool_calls.result", audit_result),
        ("tool_calls.arguments", audit_args),
        ("turn_traces", trace_row),
    ):
        assert SECRET not in blob, f"{name} 出现了密钥原文"


async def test_failed_result_and_error_are_redacted(tmp_path):
    ctx = _ctx(tmp_path, _FailingTool())
    await _run_turn(ctx, "failing_tool")

    rows = ctx.conn.execute("SELECT output, error, status FROM tool_records").fetchall()
    assert rows, "没有落任何工具历史"
    for row in rows:
        assert SECRET not in (row["output"] or "")
        assert SECRET not in (row["error"] or "")
        assert row["status"] == "failed"


async def test_audit_record_redacts_args_result_error(db_conn, tmp_path):
    """审计表（tool_calls）的参数 / 结果 / 错误也走同一个打码入口。"""
    from agent.services.app import AppContext as _App

    conn = db_conn
    ctx = _App(Settings(data_dir=tmp_path), conn, EventBus())
    ctx._record_tool_call(
        {
            "tool_name": "web_fetch",
            "arguments": {"api_key": SECRET, "url": "https://example.com"},
            "ok": False,
            "result": f"body echoed {SECRET}",
            "error": f"401 with api_key={SECRET}",
        }
    )
    row = conn.execute("SELECT arguments, result, error FROM tool_calls").fetchone()
    assert SECRET not in row["arguments"]
    assert SECRET not in row["result"]
    assert SECRET not in row["error"]
    assert "example.com" in row["arguments"]  # 非敏感内容照旧保留


def test_error_is_clipped_like_output(db_conn):
    """error 超长与 output 同构：截断 + 标记 truncated（本轮新增的一致性）。"""
    short = record_tool_call(
        db_conn, turn_id="t_short", call_id="c_short", tool_name="x", output="ok", error="boom"
    )
    long_error = "E" * 50_000
    long = record_tool_call(
        db_conn, turn_id="t_long", call_id="c_long", tool_name="x", output="ok", error=long_error
    )

    short_row = db_conn.execute(
        "SELECT error, truncated FROM tool_records WHERE id = ?", (short,)
    ).fetchone()
    long_row = db_conn.execute(
        "SELECT error, truncated FROM tool_records WHERE id = ?", (long,)
    ).fetchone()
    assert short_row["error"] == "boom"
    assert short_row["truncated"] == 0
    assert len(long_row["error"]) < 50_000
    assert "已截断" in long_row["error"]
    assert long_row["truncated"] == 1


def test_error_still_redacted_when_output_saving_is_off(db_conn):
    rid = record_tool_call(
        db_conn,
        turn_id="t_off",
        call_id="c_off",
        tool_name="x",
        output=f"secret {SECRET}",
        error=f"secret {SECRET}",
        save_output=False,
    )
    row = db_conn.execute(
        "SELECT output, error, output_missing, missing_reason FROM tool_records WHERE id = ?",
        (rid,),
    ).fetchone()
    assert row["output"] == "" and row["output_missing"] == 1
    assert row["missing_reason"] == "setting"
    assert SECRET not in row["error"]  # 关掉输出保存不等于错误可以不脱敏
    assert "***redacted***" in row["error"]