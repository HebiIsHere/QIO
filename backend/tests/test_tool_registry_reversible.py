"""可逆注册：register 返回 disposer + tool/unregistered 事件 + 生命周期撤销。"""
from __future__ import annotations

import asyncio

from agent.adapters.base import ToolCall
from agent.storage.tool_store import ToolStore
from agent.tools.base import Tool, ToolResult
from agent.tools.lifecycle import ToolLifecycle
from agent.tools.registry import ToolRegistry
from agent.tools.spec import ToolDefinition


class EchoTool(Tool):
    name = "echo"
    description = "echo"
    parameters = {"type": "object", "properties": {}}

    async def run(self, **kwargs):
        return ToolResult(ok=True, content="pong")


def make_call(name: str = "echo") -> ToolCall:
    return ToolCall(id="c1", name=name, arguments={})


async def test_register_disposer_removes_tool():
    reg = ToolRegistry()
    dispose = reg.register(EchoTool())
    assert [s.name for s in reg.specs()] == ["echo"]
    dispose()
    assert [s.name for s in reg.specs()] == []
    # 幂等：重复调用不报错
    dispose()
    assert [s.name for s in reg.specs()] == []


async def test_duplicate_register_raises():
    reg = ToolRegistry()
    reg.register(EchoTool())
    try:
        reg.register(EchoTool())
        raised = False
    except ValueError:
        raised = True
    assert raised


async def test_disposer_restores_ability_to_register():
    reg = ToolRegistry()
    dispose = reg.register(EchoTool())
    dispose()
    reg.register(EchoTool())  # 可再次注册
    assert reg.get("echo") is not None


async def test_unregister_emits_unregistered_event():
    reg = ToolRegistry()
    reg.register(EchoTool())
    seen: list[dict] = []

    def on_unregistered(data):
        seen.append(data)

    reg.register_policy("tool/unregistered", on_unregistered)
    reg.unregister("echo")
    await asyncio.sleep(0)  # 让异步广播任务跑完
    assert seen == [{"tool": "echo"}]


async def test_disposer_emits_unregistered_event():
    reg = ToolRegistry()
    dispose = reg.register(EchoTool())
    seen: list[dict] = []

    def on_unregistered(data):
        seen.append(data)

    reg.register_policy("tool/unregistered", on_unregistered)
    dispose()
    await asyncio.sleep(0)
    assert seen == [{"tool": "echo"}]


def _definition(name: str = "adder") -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description="adds",
        parameters={"type": "object", "properties": {}},
        code="def run(**kwargs):\n    return {'ok': True}",
        tool_type="function",
        sync=True,
        tests=[{"name": "t", "input": {}, "expect": {"ok": True}}],
    )


async def test_lifecycle_revoke_removes_from_registry_and_store(db_conn):
    store = ToolStore(db_conn)
    registry = ToolRegistry()
    lifecycle = ToolLifecycle(
        adapter=None,
        approvals=None,
        registry=registry,
        tool_store=store,
    )
    definition = _definition("adder")
    store.save(definition)  # 真实链路：注册后持久化
    lifecycle._register(definition)
    assert registry.get("adder") is not None
    assert lifecycle.revoke_tool("adder")
    assert registry.get("adder") is None
    rows = db_conn.execute(
        "SELECT status FROM tools WHERE name = ?", ("adder",)
    ).fetchall()
    assert rows and rows[0]["status"] == "removed"


async def test_lifecycle_revoke_unknown_returns_false():
    registry = ToolRegistry()
    lifecycle = ToolLifecycle(adapter=None, approvals=None, registry=registry)
    assert lifecycle.revoke_tool("missing") is False
