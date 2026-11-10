"""F12：ANCHOR 事件的权威操作关联字段（Lead 共享接线）。

验证：
* 普通/内部导航（无操作绑定）→ origin=manual，operation_id 为 null（前端不得据此迁移草稿）；
* 话题工具真实成功且绑定了操作身份 → origin=topic_operation，携带 operation_id /
  param_version / turn_id，以及真实 nav_version 与单调 seq；
* 事件仍携带既有权威字段（topic/fragment/historic），不改变原有行为。
"""
from __future__ import annotations

import asyncio
import json

import pytest

from agent.api.bus import EventBus
from agent.config import Settings
from agent.graph.anchors import AnchorService
from agent.services.app import AppContext
from agent.services.topic_operation import (
    IntentSource,
    TopicOperation,
    TopicOperationRegistry,
    operation_scope,
)
from agent.storage.db import connect
from agent.storage.migrate import apply_migrations
from agent.tools.base import ToolResult


@pytest.fixture()
def ctx(tmp_path) -> AppContext:
    conn = connect(tmp_path / "app.db")
    apply_migrations(conn)
    return AppContext(Settings(data_dir=tmp_path), conn, EventBus())


async def _collect_anchor(ctx: AppContext):
    collected: list[dict] = []

    async def consumer():
        async for chunk in ctx.bus.stream():
            for line in chunk.splitlines():
                if line.startswith("data: "):
                    evt = json.loads(line[6:])
                    if evt["type"] == "ANCHOR":
                        collected.append(evt["data"])

    task = asyncio.create_task(consumer())
    await asyncio.sleep(0.02)

    async def finish():
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    return collected, finish


async def test_manual_anchor_has_no_operation_identity(ctx: AppContext):
    node = ctx.topics.nodes.create_topic("手动导航")
    AnchorService(ctx.conn).set_active(node.id)
    collected, finish = await _collect_anchor(ctx)
    await ctx._publish_anchor_event()
    await finish()
    assert len(collected) == 1
    event = collected[0]
    assert event["origin"] == "manual"
    assert event["operation_id"] is None
    assert event["param_version"] is None
    assert event["turn_id"] is None
    assert isinstance(event["nav_version"], int)
    assert isinstance(event["seq"], int)


async def test_topic_tool_success_carries_operation_identity(ctx: AppContext):
    node = ctx.topics.nodes.create_topic("操作关联")
    AnchorService(ctx.conn).set_active(node.id)
    operation = TopicOperation(
        operation_id="topop-test-1",
        turn_id="turn-test-1",
        kind="topic_switch",
        intent_source=IntentSource.USER_REQUEST,
        param_version=3,
    )
    collected, finish = await _collect_anchor(ctx)
    with operation_scope(operation):
        await ctx._on_tool_anchor_result(
            {"tool": "switch_topic", "result": ToolResult(ok=True, content="已切换")}
        )
    await finish()
    assert len(collected) == 1
    event = collected[0]
    assert event["origin"] == "topic_operation"
    assert event["operation_id"] == "topop-test-1"
    assert event["param_version"] == 3
    assert event["turn_id"] == "turn-test-1"
    assert event["topic_id"] == node.id
    assert isinstance(event["nav_version"], int)
    assert isinstance(event["seq"], int)


async def test_manual_anchor_after_operation_is_manual_again(ctx: AppContext):
    node = ctx.topics.nodes.create_topic("操作后手动")
    AnchorService(ctx.conn).set_active(node.id)
    operation = TopicOperation(
        operation_id="topop-test-2", turn_id="turn-test-2", kind="topic_create"
    )
    collected, finish = await _collect_anchor(ctx)
    with operation_scope(operation):
        await ctx._on_tool_anchor_result(
            {"tool": "create_topic", "result": ToolResult(ok=True, content="已创建")}
        )
    await ctx._publish_anchor_event()  # 之后一次普通广播：不得沿用旧身份
    await finish()
    assert [event["origin"] for event in collected] == ["topic_operation", "manual"]
    assert collected[1]["operation_id"] is None


async def test_non_topic_tool_does_not_publish(ctx: AppContext):
    collected, finish = await _collect_anchor(ctx)
    await ctx._on_tool_anchor_result(
        {"tool": "now", "result": ToolResult(ok=True, content="time")}
    )
    await finish()
    assert collected == []
