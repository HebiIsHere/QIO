"""Dev workflow integration: main loop drives create -> write -> test -> submit -> register."""

from __future__ import annotations

import asyncio
import json

import pytest

from agent.adapters.base import ChatMessage, Completion, ToolCall
from agent.api.bus import EventBus
from agent.config import Settings
from agent.services.app import AppContext
from agent.storage.db import connect
from agent.storage.migrate import apply_migrations


@pytest.fixture()
def ctx(tmp_path) -> AppContext:
    conn = connect(tmp_path / "app.db")
    apply_migrations(conn)
    return AppContext(Settings(data_dir=tmp_path), conn, EventBus())


class DevScriptAdapter:
    """Main-loop adapter driven by a fixed script of tool calls."""

    mode = "native"
    model = "main"

    def __init__(self, script: list) -> None:
        self.script = list(script)
        self.seen: list[str] = []

    async def complete(self, messages, tools, **kwargs):
        self.seen.append(messages[-1].content or "")
        step = self.script.pop(0)
        if step is None:
            return Completion(message=ChatMessage(role="assistant", content="工具已开发完成"))
        name, args = step
        return Completion(
            message=ChatMessage(
                role="assistant",
                content=None,
                tool_calls=[ToolCall(id=f"c{len(self.seen)}", name=name, arguments=args)],
            )
        )


TOOL_JSON = json.dumps({
    "name": "dev_add",
    "description": "两个数求和",
    "tool_type": "function",
    "sync": True,
    "code": "def run(**kwargs):\n    return {'sum': kwargs['a'] + kwargs['b']}",
    "tests": [
        {"name": "positive", "input": {"a": 1, "b": 2}, "expect": {"sum": 3}},
        {"name": "zero", "input": {"a": 0, "b": 0}, "expect": {"sum": 0}},
    ],
}, ensure_ascii=False)


async def test_full_dev_workflow_registers_tool(ctx: AppContext, monkeypatch):
    from unittest.mock import AsyncMock

    from agent.core.loop import AgentLoop

    # lifecycle 构造需要 adapter（submit_definition 不使用提案，fake 即可）
    class FakeAdapter:
        mode = "native"
        model = "main"

        async def complete(self, messages, tools, **kwargs):
            return Completion(message=ChatMessage(role="assistant", content="ok"))

    monkeypatch.setattr(ctx, "build_adapter", AsyncMock(return_value=FakeAdapter()))

    # 自动审批消费者
    async def auto_approve():
        async for chunk in ctx.bus.stream():
            for line in chunk.splitlines():
                if line.startswith("data: "):
                    event = json.loads(line[6:])
                    if event["type"] == "APPROVAL_REQUIRED":
                        await ctx.approvals.respond(
                            event["data"]["approval"]["approval_id"], "approved"
                        )

    consumer = asyncio.create_task(auto_approve())
    await asyncio.sleep(0.05)

    adapter = DevScriptAdapter([
        ("create_tool", {"request": "用户需要一个两个数求和的计算工具"}),
        ("dev_write_file", {"workspace": "WS", "name": "tool.json", "content": TOOL_JSON}),
        ("dev_run_tests", {"workspace": "WS"}),
        ("dev_submit_tool", {
            "workspace": "WS",
            "definition": json.loads(TOOL_JSON),
            "explanation": "这个工具可以把两个数加起来，返回它们的和",
        }),
        None,
    ])
    # 工作区 id 动态：先手动创建一个工作区，把 id 注入脚本
    ws = ctx.dev_workspaces.create("两个数求和")
    for i, step in enumerate(adapter.script):
        if isinstance(step, tuple) and step[0] in ("dev_write_file", "dev_run_tests", "dev_submit_tool"):
            args = dict(step[1])
            args["workspace"] = ws.id
            adapter.script[i] = (step[0], args)

    loop = AgentLoop(adapter, ctx.registry, ctx.bus)
    result = await loop.run("帮我做一个求和工具")
    consumer.cancel()
    try:
        await consumer
    except asyncio.CancelledError:
        pass

    assert "工具已开发完成" in (result.final_content or "")
    tool = ctx.registry.get("dev_add")
    assert tool is not None, "tool should be registered"
    # 工作区已清理
    assert ctx.dev_workspaces.task(ws.id) is None
    # 新工具可调用
    r = await tool.run(a=2, b=3)
    assert r.ok and r.content == '{"sum": 5}'
