"""Tool dev workflow: DevWorkspace, dev tools, lifecycle submit path."""

from __future__ import annotations

import asyncio
import json
import pytest

from agent.tools.base import ToolResult
from agent.tools.dev_workspace import DevWorkspace
from agent.tools.spec import ToolDefinition


# ---------- DevWorkspace ----------

def test_workspace_create_write_read_cleanup(tmp_path):
    ws = DevWorkspace(tmp_path / "ws")
    task = ws.create("计算两个数的和")
    assert task.id.startswith("ws_")
    assert (tmp_path / "ws" / task.id).exists()
    ws.write_file(task.id, "tool.py", "def run(**kwargs):\n    return 1")
    assert "def run" in ws.read_file(task.id, "tool.py")
    assert ws.read_file(task.id, "ghost.py") is None
    ws.cleanup(task.id)
    assert not (tmp_path / "ws" / task.id).exists()
    assert ws.task(task.id) is None


def test_workspace_path_validation(tmp_path):
    ws = DevWorkspace(tmp_path / "ws")
    task = ws.create("x")
    with pytest.raises(ValueError):
        ws.write_file(task.id, "../evil.py", "x")
    with pytest.raises(ValueError):
        ws.write_file(task.id, "a/b.py", "x")
    with pytest.raises(ValueError):
        ws.write_file(task.id, "x", "x" * 200_001)


def test_workspace_write_definition(tmp_path):
    ws = DevWorkspace(tmp_path / "ws")
    task = ws.create("x")
    ws.write_definition(task.id, ToolDefinition(
        name="add_numbers", description="求和", tool_type="function",
        code="def run(**kwargs):\n    return {'sum': kwargs['a'] + kwargs['b']}",
        tests=[{"name": "t1", "input": {"a": 1, "b": 2}, "expect": {"sum": 3}}],
    ))
    loaded = ws.read_definition(task.id)
    assert loaded is not None and loaded.name == "add_numbers"
    assert loaded.tests[0].expect == {"sum": 3}


# ---------- dev tools ----------

from agent.api.bus import EventBus
from agent.tools.dev_tools import (
    CreateToolTool,
    DevReadFileTool,
    DevRunTestsTool,
    DevSubmitTool,
    DevWriteFileTool,
)


async def test_create_tool_returns_guide():
    ws = DevWorkspace(Path_factory())
    tool = CreateToolTool(ws)
    result = await tool.run(request="帮我做一个计算工具")
    assert result.ok and "ws_" in result.content
    assert "需求规格" in result.content  # 指南含必填项


def Path_factory():
    import tempfile
    from pathlib import Path
    return Path(tempfile.mkdtemp()) / "ws"


async def test_dev_write_read_file():
    ws = DevWorkspace(Path_factory())
    task = ws.create("x")
    wt = DevWriteFileTool(ws)
    assert (await wt.run(workspace=task.id, name="tool.py", content="code")).ok
    assert (await wt.run(workspace="ghost", name="a", content="b")).ok is False
    rt = DevReadFileTool(ws)
    r = await rt.run(workspace=task.id, name="tool.py")
    assert r.ok and r.content == "code"


async def test_dev_run_tests_pass_and_fail():
    ws = DevWorkspace(Path_factory())
    task = ws.create("求和工具")
    ws.write_definition(task.id, ToolDefinition(
        name="add_numbers", description="求和", tool_type="function",
        code="def run(**kwargs):\n    return {'sum': kwargs['a'] + kwargs['b']}",
        tests=[
            {"name": "positive", "input": {"a": 1, "b": 2}, "expect": {"sum": 3}},
            {"name": "negative", "input": {"a": -1, "b": 1}, "expect": {"sum": 0}},
        ],
    ))
    tool = DevRunTestsTool(ws)
    r = await tool.run(workspace=task.id)
    assert r.ok and "2/2" in r.content
    # 失败场景
    task2 = ws.create("坏工具")
    ws.write_definition(task2.id, ToolDefinition(
        name="bad", description="x", tool_type="function",
        code="def run(**kwargs):\n    return {'sum': 999}",
        tests=[{"name": "t", "input": {}, "expect": {"sum": 1}}],
    ))
    r2 = await tool.run(workspace=task2.id)
    assert r2.ok is False and "assertion" in (r2.error or "")


async def test_dev_submit_maps_outcome_and_cleans():
    ws = DevWorkspace(Path_factory())
    task = ws.create("x")
    calls = {"n": 0}

    from agent.tools.lifecycle import ToolOutcome

    async def fake_submit(definition, explanation):
        calls["n"] += 1
        assert definition.name == "add_numbers"
        return ToolOutcome(True, "add_numbers", "registered", "ok")

    class FakeLifecycle:
        async def submit_definition(self, definition, explanation):
            return await fake_submit(definition, explanation)

    async def builder():
        return FakeLifecycle()

    tool = DevSubmitTool(ws, lifecycle_builder=builder)
    definition = {
        "name": "add_numbers", "description": "求和", "tool_type": "function",
        "code": "def run(**kwargs):\n    return {'sum': 1}",
        "tests": [{"name": "t", "input": {}, "expect": {"sum": 1}}],
    }
    r = await tool.run(workspace=task.id, definition=definition, explanation="这个工具计算两个数之和")
    assert r.ok
    assert calls["n"] == 1
    # 成功后清理
    assert ws.task(task.id) is None
    # 无效工作区
    r2 = await tool.run(workspace="ghost", definition=definition, explanation="x")
    assert r2.ok is False


# ---------- lifecycle.submit_definition ----------

async def test_lifecycle_submit_definition_full_path(db_conn: sqlite3.Connection):
    from agent.api.server import EventBus as _EB
    from agent.credentials.store import MemoryKeyring
    from agent.tools.approval import ApprovalService
    from agent.tools.lifecycle import ToolLifecycle
    from agent.tools.registry import ToolRegistry
    from agent.tools.sandbox import SandboxExecutor

    class ScriptedAdapter:
        mode = "native"

        async def complete(self, messages, tools, **kwargs):
            from agent.adapters.base import ChatMessage, Completion
            return Completion(message=ChatMessage(role="assistant", content="ok"))

    bus = EventBus()
    approvals = ApprovalService(bus, timeout_seconds=5)
    registry = ToolRegistry()

    class FakeCreds:
        def get_metadata(self, ref):
            return None  # 无凭据引用，不触发段 2

    lifecycle = ToolLifecycle(
        adapter=ScriptedAdapter(),
        approvals=approvals,
        sandbox=SandboxExecutor(executor="subprocess"),
        registry=registry,
        credentials=FakeCreds(),
    )

    async def auto_approve():
        async for chunk in bus.stream():
            for line in chunk.splitlines():
                if line.startswith("data: "):
                    event = json.loads(line[6:])
                    if event["type"] == "APPROVAL_REQUIRED":
                        await approvals.respond(event["data"]["approval"]["approval_id"], "approved")

    task = asyncio.create_task(auto_approve())
    await asyncio.sleep(0.05)
    definition = ToolDefinition(
        name="add_numbers", description="求和", tool_type="function",
        code="def run(**kwargs):\n    return {'sum': kwargs['a'] + kwargs['b']}",
        tests=[{"name": "t", "input": {"a": 1, "b": 2}, "expect": {"sum": 3}}],
    )
    outcome = await lifecycle.submit_definition(definition, "计算两个数之和")
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert outcome.ok and outcome.step == "registered"
    tool = registry.get("add_numbers")
    assert tool is not None


async def test_lifecycle_submit_definition_test_gate(db_conn: sqlite3.Connection):
    from agent.tools.approval import ApprovalService
    from agent.tools.lifecycle import ToolLifecycle
    from agent.tools.registry import ToolRegistry
    from agent.tools.sandbox import SandboxExecutor

    class ScriptedAdapter:
        mode = "native"

        async def complete(self, messages, tools, **kwargs):
            from agent.adapters.base import ChatMessage, Completion
            return Completion(message=ChatMessage(role="assistant", content="ok"))

    bus = EventBus()
    lifecycle = ToolLifecycle(
        adapter=ScriptedAdapter(),
        approvals=ApprovalService(bus, timeout_seconds=1),
        sandbox=SandboxExecutor(executor="subprocess"),
        registry=ToolRegistry(),
    )
    bad = ToolDefinition(
        name="bad_tool", description="x", tool_type="function",
        code="def run(**kwargs):\n    return {'sum': 999}",
        tests=[{"name": "t", "input": {}, "expect": {"sum": 1}}],
    )
    outcome = await lifecycle.submit_definition(bad, "x")
    assert not outcome.ok and outcome.step == "test"
