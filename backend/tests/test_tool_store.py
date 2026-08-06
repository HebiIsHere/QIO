"""Tool persistence: store + startup restore + lifecycle auto-save."""

from __future__ import annotations

import sqlite3

from agent.storage.tool_store import ToolStore
from agent.tools.spec import ToolDefinition


def _def(name: str = "persist_tool") -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description="持久化测试工具",
        tool_type="function",
        code="def run(**kwargs):\n    return {'ok': True}",
        tests=[{"name": "t", "input": {}, "expect": {"ok": True}}],
    )


def test_tool_store_roundtrip(db_conn: sqlite3.Connection):
    store = ToolStore(db_conn)
    assert store.load_all() == []
    store.save(_def())
    loaded = store.load_all()
    assert len(loaded) == 1 and loaded[0].name == "persist_tool"
    assert loaded[0].tests[0].expect == {"ok": True}
    # 重名覆盖
    store.save(_def(name="persist_tool"))
    assert len(store.load_all()) == 1
    store.remove("persist_tool")
    assert store.load_all() == []


def test_tool_store_subagent_definition(db_conn: sqlite3.Connection):
    store = ToolStore(db_conn)
    d = ToolDefinition(
        name="sub_p", description="x", tool_type="subagent",
        credential_ref="k1", model="m1",
        subagent_budget={"max_iterations": 3, "max_tokens": 50000, "output_limit_chars": 1000},
    )
    store.save(d)
    loaded = store.load_all()[0]
    assert loaded.tool_type == "subagent"
    assert loaded.subagent_budget.max_iterations == 3
