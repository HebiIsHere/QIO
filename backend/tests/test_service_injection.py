"""轻量服务注入：ServiceRegistry + Tool.inject attach（AppContext 装配）。"""
from __future__ import annotations

import pytest

from agent.api.bus import EventBus
from agent.config import Settings
from agent.storage.db import connect
from agent.storage.migrate import apply_migrations
from agent.tools.base import Tool, ToolResult
from agent.tools.registry import ToolRegistry
from agent.tools.services import ServiceRegistry


class _Retriever:
    def search(self, q):
        return f"retrieved:{q}"


class InjectTool(Tool):
    name = "inject_tool"
    description = "inject"
    parameters = {"type": "object", "properties": {}}
    inject = ["retriever", "predictor", "missing_service"]

    async def run(self, **kwargs):
        return ToolResult(ok=True, content="ok")


class PlainTool(Tool):
    name = "plain_tool"
    description = "plain"
    parameters = {"type": "object", "properties": {}}

    async def run(self, **kwargs):
        return ToolResult(ok=True, content="ok")


def test_service_registry_register_resolve_attach():
    svc = ServiceRegistry()
    retriever = _Retriever()
    svc.register("retriever", retriever)
    assert svc.resolve("retriever") is retriever
    assert svc.resolve("nope") is None

    tool = InjectTool()
    svc.attach(tool)
    assert tool.retriever is retriever
    assert "predictor" not in tool.__dict__  # 未注册的声明不 setattr
    assert "missing_service" not in tool.__dict__


def test_registry_attach_on_register():
    svc = ServiceRegistry()
    retriever = _Retriever()
    svc.register("retriever", retriever)
    reg = ToolRegistry(services=svc)
    tool = InjectTool()
    reg.register(tool)
    assert tool.retriever is retriever


def test_registry_without_services_is_noop():
    reg = ToolRegistry()
    tool = InjectTool()
    reg.register(tool)
    assert not hasattr(tool, "retriever")


def test_registry_plain_tool_unaffected():
    svc = ServiceRegistry()
    svc.register("retriever", _Retriever())
    reg = ToolRegistry(services=svc)
    tool = PlainTool()
    reg.register(tool)
    assert not hasattr(tool, "retriever")


def test_app_context_registers_services(tmp_path):
    conn = connect(tmp_path / "app.db")
    apply_migrations(conn)
    ctx = __import__("agent.services.app", fromlist=["AppContext"]).AppContext(
        Settings(data_dir=tmp_path), conn, EventBus()
    )
    services = ctx.services
    assert services.resolve("retriever") is ctx.retriever
    assert services.resolve("predictor") is ctx.predictor
    assert services.resolve("approvals") is ctx.approvals
    assert services.resolve("embedding") is ctx.embedding
    assert services.resolve("task_manager") is ctx.task_manager
    assert services.resolve("tool_store") is ctx.tool_store
    conn.close()
