"""declare_completion：把「完成 / 可用」交给后端事实核对。"""

from __future__ import annotations

from agent.tools.declare_completion import DeclareCompletionTool
from agent.tools.dev_workspace import DevWorkspace
from agent.tools.registry import ToolRegistry
from agent.tools.spec import ToolDefinition


class _DummyTool:
    def __init__(self, name: str) -> None:
        self.name = name
        self.description = "dummy"
        self.parameters = {"type": "object", "properties": {}}

    async def run(self, **kwargs):
        from agent.tools.base import ToolResult

        return ToolResult(ok=True, content="{}")


def _definition(tool_type: str = "function") -> ToolDefinition:
    return ToolDefinition(
        name="add_numbers",
        description="求和",
        tool_type=tool_type,
        code="def run(**kwargs):\n    return {'sum': 1}",
        tests=[{"name": "t", "input": {}, "expect": {"sum": 1}}] if tool_type == "function" else [],
    )


def _ready_workspace(tmp_path, *, submitted: bool = True, tested: bool = True):
    ws = DevWorkspace(tmp_path / "ws")
    registry = ToolRegistry()
    task = ws.create("求和工具")
    ws.write_definition(task.id, _definition())
    if tested:
        ws.record_test(task.id, True, "1/1 tests passed")
    registry.register(_DummyTool("add_numbers"))
    if submitted:
        ws.mark_submitted(task.id)
    return ws, registry, task


async def test_declare_accepts_when_everything_matches(tmp_path):
    ws, registry, task = _ready_workspace(tmp_path)
    tool = DeclareCompletionTool(ws, registry)
    r = await tool.run(
        task_id=task.id,
        version=ws.content_digest(task.id),
        claims=["test_passed", "registered", "usable"],
    )
    assert r.ok is True
    assert "1/1 tests passed" in r.content
    declaration = r.facts["declaration"]
    assert declaration["accepted"] is True
    assert declaration["missing"] == []


async def test_declare_rejects_stale_evidence(tmp_path):
    ws, registry, task = _ready_workspace(tmp_path, submitted=False)
    ws.write_file(task.id, "tool.py", "broken")  # 通过之后又改内容
    tool = DeclareCompletionTool(ws, registry)
    r = await tool.run(
        task_id=task.id, version=ws.content_digest(task.id), claims=["test_passed"]
    )
    assert r.ok is False
    assert "失效" in r.error and "重跑" in r.error
    assert r.facts["declaration"]["accepted"] is False
    assert r.facts["declaration"]["missing"]


async def test_declare_rejects_wrong_version(tmp_path):
    ws, registry, task = _ready_workspace(tmp_path)
    tool = DeclareCompletionTool(ws, registry)
    r = await tool.run(task_id=task.id, version="0" * 64, claims=["test_passed"])
    assert r.ok is False
    assert "版本" in r.error


async def test_declare_rejects_when_not_submitted(tmp_path):
    ws, registry, task = _ready_workspace(tmp_path, submitted=False)
    tool = DeclareCompletionTool(ws, registry)
    r = await tool.run(
        task_id=task.id, version=ws.content_digest(task.id), claims=["registered"]
    )
    assert r.ok is False
    assert "还没有提交" in r.error


async def test_declare_rejects_when_registry_lost_the_tool(tmp_path):
    ws, registry, task = _ready_workspace(tmp_path)
    registry.unregister("add_numbers")
    tool = DeclareCompletionTool(ws, registry)
    r = await tool.run(
        task_id=task.id, version=ws.content_digest(task.id), claims=["registered"]
    )
    assert r.ok is False
    assert "注册表" in r.error


async def test_declare_rejects_unknown_task(tmp_path):
    ws = DevWorkspace(tmp_path / "ws")
    tool = DeclareCompletionTool(ws, ToolRegistry())
    r = await tool.run(task_id="ws_ffffffffffff", version="0" * 64, claims=["usable"])
    assert r.ok is False
    assert "找不到开发任务" in r.error


async def test_declare_rejects_unknown_claim(tmp_path):
    ws, registry, task = _ready_workspace(tmp_path)
    tool = DeclareCompletionTool(ws, registry)
    r = await tool.run(
        task_id=task.id, version=ws.content_digest(task.id), claims=["全部搞定"]
    )
    assert r.ok is False
    assert "test_passed" in r.error  # 列出合法取值


async def test_declare_rejects_empty_claims(tmp_path):
    ws, registry, task = _ready_workspace(tmp_path)
    tool = DeclareCompletionTool(ws, registry)
    r = await tool.run(task_id=task.id, version=ws.content_digest(task.id), claims=[])
    assert r.ok is False
    assert "claims" in r.error


async def test_subagent_tool_does_not_need_test_evidence(tmp_path):
    ws = DevWorkspace(tmp_path / "ws")
    registry = ToolRegistry()
    task = ws.create("调研工具")
    ws.write_definition(task.id, _definition(tool_type="subagent"))
    registry.register(_DummyTool("add_numbers"))
    ws.mark_submitted(task.id)
    tool = DeclareCompletionTool(ws, registry)
    r = await tool.run(
        task_id=task.id,
        version=ws.content_digest(task.id),
        claims=["registered", "usable"],
    )
    assert r.ok is True, r.error
    assert "不需要确定性测试" in r.content


async def test_cross_check_uses_the_persisted_store_too(tmp_path):
    """注册表里在、但持久层里没有：不能算「已注册」。"""

    class _EmptyStore:
        def load(self, name):
            return None

    ws, registry, task = _ready_workspace(tmp_path)
    tool = DeclareCompletionTool(ws, registry, tool_store=_EmptyStore())
    r = await tool.run(
        task_id=task.id, version=ws.content_digest(task.id), claims=["registered"]
    )
    assert r.ok is False
