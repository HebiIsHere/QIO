"""Tool persistence integration: restore on AppContext startup."""

from __future__ import annotations

import pytest

from agent.api.bus import EventBus
from agent.config import Settings
from agent.services.app import AppContext
from agent.storage.db import connect
from agent.storage.migrate import apply_migrations
from agent.tools.spec import ToolDefinition


def _make_ctx(db_path, data_dir):
    conn = connect(db_path)
    apply_migrations(conn)
    return AppContext(Settings(data_dir=data_dir), conn, EventBus()), conn


def test_restore_agent_tool_after_restart(tmp_path):
    db_path = tmp_path / "app.db"
    ctx1, conn1 = _make_ctx(db_path, tmp_path / "data")
    definition = ToolDefinition(
        name="persisted_fn",
        description="持久化函数工具",
        tool_type="function",
        code="def run(**kwargs):\n    return {'v': kwargs.get('x', 0) + 1}",
        tests=[{"name": "t", "input": {"x": 1}, "expect": {"v": 2}}],
    )
    ctx1.tool_store.save(definition)
    conn1.close()

    ctx2, conn2 = _make_ctx(db_path, tmp_path / "data")
    tool = ctx2.registry.get("persisted_fn")
    assert tool is not None, "agent tool should be restored after restart"
    import asyncio
    r = asyncio.run(tool.run(x=41))
    assert r.ok and r.content == '{"v": 42}'
    conn2.close()


def test_restore_subagent_tool_after_restart(tmp_path):
    db_path = tmp_path / "app.db"
    ctx1, conn1 = _make_ctx(db_path, tmp_path / "data")
    definition = ToolDefinition(
        name="persisted_sub",
        description="持久化子agent工具",
        tool_type="subagent",
        credential_ref="k1",
        model="m1",
        subagent_budget={"max_iterations": 3, "max_tokens": 50000, "output_limit_chars": 1000},
    )
    ctx1.tool_store.save(definition)
    conn1.close()

    ctx2, conn2 = _make_ctx(db_path, tmp_path / "data")
    from agent.tools.subagent_tool import SubagentTool
    tool = ctx2.registry.get("persisted_sub")
    assert isinstance(tool, SubagentTool)
    assert tool.definition.subagent_budget.max_iterations == 3
    conn2.close()
