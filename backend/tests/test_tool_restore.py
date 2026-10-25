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


@pytest.mark.requires_docker
@pytest.mark.asyncio
async def test_restore_agent_tool_after_restart(tmp_path):
    import asyncio
    import json

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
    # 新语义（契约 §2.3/§5.3）：自建工具每次调用要经审批。第一次调用必须返回
    # pending（不是直接执行）；批准后由审批执行器重跑管线拿到真实结果。
    from agent.security.context import requirement_context

    requirement = ctx2.requirements.record_user_message(
        text="试用重启后恢复的工具", message_id="msg_restore", turn_id="turn_restore"
    )
    binding = ctx2.requirements.binding_for(requirement.requirement_id)
    with requirement_context(binding):
        pending = await tool.run(x=41)
    assert pending.ok is True and pending.code == "pending_approval", pending
    approval_id = pending.data["approval_id"]
    await ctx2.approvals.decide(approval_id, "approve")
    for _ in range(300):
        row = ctx2.approvals.store.get(approval_id) or {}
        if row.get("state") in ("completed", "failed"):
            break
        await asyncio.sleep(0.02)
    row = ctx2.approvals.store.get(approval_id) or {}
    assert row.get("state") == "completed", row
    stored = row.get("result") or {}
    if isinstance(stored, str):
        stored = json.loads(stored or "{}")
    assert stored.get("content") == '{"v": 42}', stored
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


def test_restore_skipped_when_policy_changed_since_approval(tmp_path):
    """能力指纹不一致 → 不恢复（需重新批准）。"""
    db_path = tmp_path / "app.db"
    ctx1, conn1 = _make_ctx(db_path, tmp_path / "data")
    definition = ToolDefinition(
        name="widened_fn",
        description="曾批准过、但现在能力已变化",
        tool_type="function",
        code="def run(**kwargs):\n    return {}",
        # 伪造一个“旧的批准指纹”（与当前推导不一致）
        approved_policy_fingerprint="deadbeefdeadbeef",
    )
    ctx1.tool_store.save(definition)
    conn1.close()

    ctx2, conn2 = _make_ctx(db_path, tmp_path / "data")
    assert ctx2.registry.get("widened_fn") is None  # 未恢复
    conn2.close()
