"""M12: tool trace persistence + maintenance scheduler."""

from __future__ import annotations

import asyncio
import sqlite3

import pytest

from agent.adapters.base import ChatMessage, Completion, ToolCall
from agent.api.bus import EventBus
from agent.config import Settings
from agent.core.loop import AgentLoop
from agent.services.app import AppContext
from agent.storage.db import connect
from agent.storage.migrate import apply_migrations
from agent.credentials.store import MemoryKeyring
from agent.tools.base import Tool, ToolResult
from agent.tools.registry import ToolRegistry


@pytest.fixture()
def ctx(tmp_path) -> AppContext:
    conn = connect(tmp_path / "app.db")
    apply_migrations(conn)
    app_ctx = AppContext(Settings(data_dir=tmp_path), conn, EventBus())
    app_ctx.credentials._kr = MemoryKeyring()
    return app_ctx


class EchoTool2(Tool):
    name = "echo2"
    description = "echo"
    parameters = {"type": "object", "properties": {}}

    async def run(self, **kwargs):
        return ToolResult(ok=True, content="pong")


class ScriptedAdapter:
    mode = "native"
    model = "m"

    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, messages, tools, **kwargs):
        self.calls += 1
        if self.calls == 1:
            return Completion(
                message=ChatMessage(
                    role="assistant",
                    content=None,
                    tool_calls=[ToolCall(id="c1", name="echo2", arguments={})],
                )
            )
        return Completion(message=ChatMessage(role="assistant", content="done"))


def test_migration_v6_tool_calls_table(db_conn: sqlite3.Connection):
    rows = db_conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='tool_calls'"
    ).fetchall()
    assert rows


def test_agent_loop_records_tool_trace(db_conn: sqlite3.Connection):
    traces: list[dict] = []
    bus = EventBus()
    registry = ToolRegistry()
    registry.register(EchoTool2())

    loop = AgentLoop(
        ScriptedAdapter(), registry, bus,
        tool_trace=lambda t: traces.append(t),
    )
    asyncio.run(loop.run("hi"))
    assert len(traces) == 1
    assert traces[0]["tool_name"] == "echo2"
    assert traces[0]["ok"] is True
    assert traces[0]["result"]


def test_app_context_records_tool_calls(ctx: AppContext):
    from agent.adapters.base import ChatMessage as CM, Completion as Comp, ToolCall as TC

    class A:
        mode = "native"
        model = "m"

        def __init__(self):
            self.calls = 0

        async def complete(self, messages, tools, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return Comp(message=CM(role="assistant", content=None,
                                       tool_calls=[TC(id="c1", name="echo", arguments={"text": "hi"})]))
            return Comp(message=CM(role="assistant", content="done"))

    # 直接验证 _record_tool_call 写库
    ctx._record_tool_call({"tool_name": "echo", "arguments": {"text": "hi"}, "ok": True, "result": "hi"})
    rows = ctx.conn.execute("SELECT * FROM tool_calls").fetchall()
    assert len(rows) == 1
    assert rows[0]["tool_name"] == "echo"
