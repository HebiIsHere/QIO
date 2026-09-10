"""End-to-end: a turn produces a trace; secrets never land in the DB."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from agent.adapters.base import ChatMessage, Completion, ToolCall
from agent.api.bus import EventBus
from agent.config import Settings
from agent.credentials.store import MemoryKeyring
from agent.services.app import AppContext
from agent.storage.db import connect
from agent.storage.migrate import apply_migrations
from agent.tools.base import Tool, ToolResult


@pytest.fixture()
def ctx(tmp_path) -> AppContext:
    conn = connect(tmp_path / "app.db")
    apply_migrations(conn)
    app_ctx = AppContext(Settings(data_dir=tmp_path), conn, EventBus())
    app_ctx.credentials._kr = MemoryKeyring()
    return app_ctx


class _EchoTool(Tool):
    name = "echo_pii"
    description = "echo"
    parameters = {"type": "object", "properties": {"api_key": {"type": "string"}}}

    async def run(self, **kwargs):
        return ToolResult(ok=True, content=f"echo:{kwargs.get('api_key', '')}")


class _ToolThenAnswer:
    mode = "native"
    model = "deepseek-v4-flash"

    def __init__(self, secret: str) -> None:
        self.secret = secret
        self.n = 0

    async def complete(self, messages, tools, **kwargs):
        self.n += 1
        if self.n == 1:
            tc = ToolCall(id="c1", name="echo_pii", arguments={"api_key": self.secret})
            return Completion(
                message=ChatMessage(role="assistant", content=None, tool_calls=[tc]),
                usage={"prompt_tokens": 10, "completion_tokens": 5},
            )
        return Completion(
            message=ChatMessage(role="assistant", content="done"),
            usage={"prompt_tokens": 20, "completion_tokens": 7},
        )


async def test_turn_records_trace_without_secret(ctx: AppContext):
    node = ctx.topics.nodes.create_topic("t")
    ctx.registry.register(_EchoTool())
    secret = "sk-FAKEsecret1234567890"
    adapter = _ToolThenAnswer(secret)
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(ctx, "build_adapter", AsyncMock(return_value=adapter))

    await ctx.run_turn("请用工具", topic_id=node.id)
    monkeypatch.undo()

    traces = ctx.trace_store.list(limit=10)
    assert len(traces) == 1
    trace = ctx.trace_store.get(traces[0]["turn_id"])
    assert trace["status"] == "done"
    assert len(trace["model_calls"]) == 2
    assert trace["model_calls"][0]["output_tokens"] == 5
    assert len(trace["tool_runs"]) == 1
    assert trace["tool_runs"][0]["tool"] == "echo_pii"
    assert len(trace["writes"]["messages"]) >= 2  # user + assistant
    assert trace["topic"]["initial"] == node.id

    # 敏感值不得落库（args_preview / result_preview / 整行）
    import json

    row = ctx.conn.execute("SELECT * FROM turn_traces").fetchone()
    blob = json.dumps(dict(row), ensure_ascii=False, default=str)
    assert secret not in blob


class _AnswerAdapter:
    mode = "native"
    model = "deepseek-v4-flash"

    def __init__(self, text: str) -> None:
        self.text = text

    async def complete(self, messages, tools, **kwargs):
        return Completion(
            message=ChatMessage(role="assistant", content=self.text),
            usage={"prompt_tokens": 12, "completion_tokens": 4},
        )


async def test_subagent_loop_records_trace(ctx: AppContext):
    from agent.tools.subagent_tool import SubagentTool
    from agent.tools.spec import ToolDefinition

    definition = ToolDefinition(
        name="sub1",
        description="d",
        tool_type="subagent",
        subagent_budget={"max_iterations": 2, "max_tokens": 1000, "output_limit_chars": 500},
    )

    class _TM:
        def record_info(self, tid):
            return None

    tool = SubagentTool(
        definition,
        credentials=None,
        task_manager=_TM(),
        trace_store=ctx.trace_store,
    )
    tool.bus = EventBus()
    res = await tool._execute("task_x", _AnswerAdapter("子任务结果"), {"x": 1})
    assert res.ok
    trace = ctx.trace_store.get("subagent:task_x")
    assert trace is not None
    assert trace["status"] == "done"
    assert len(trace["model_calls"]) >= 1


async def test_notify_turn_records_trace(ctx: AppContext):
    from agent.core.turn import TurnContext

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(ctx, "build_adapter", AsyncMock(return_value=_AnswerAdapter("收到")))
    tctx = TurnContext(turn_id="turn_notify", message="子任务完成：结果 X", notify=True)
    await ctx._execute_notify_turn(tctx)
    monkeypatch.undo()

    trace = ctx.trace_store.get("turn_notify")
    assert trace is not None
    assert trace["status"] == "done"
    assert len(trace["model_calls"]) >= 1
    assert trace["writes"].get("messages")
