"""ANCHOR 事件：agent 切换/创建话题后实时广播新锚点。"""
from __future__ import annotations

import asyncio
import json

import pytest

from agent.api.bus import EventBus
from agent.config import Settings
from agent.graph.anchors import AnchorService
from agent.services.app import AppContext
from agent.storage.db import connect
from agent.storage.migrate import apply_migrations
from agent.tools.base import ToolResult


@pytest.fixture()
def ctx(tmp_path) -> AppContext:
    conn = connect(tmp_path / "app.db")
    apply_migrations(conn)
    return AppContext(Settings(data_dir=tmp_path), conn, EventBus())


async def _collect_anchor(ctx: AppContext):
    """订阅总线，返回收集 ANCHOR 事件 data 的列表与收尾函数。"""
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


async def test_switch_topic_result_publishes_anchor(ctx: AppContext):
    node = ctx.topics.nodes.create_topic("养鹅")
    AnchorService(ctx.conn).set_active(node.id)

    collected, finish = await _collect_anchor(ctx)
    await ctx._on_tool_anchor_result(
        {"tool": "switch_topic", "result": ToolResult(ok=True, content="已切换到话题「养鹅」")}
    )
    await finish()

    assert len(collected) == 1
    assert collected[0]["topic_id"] == node.id
    assert collected[0]["topic_name"] == "养鹅"


async def test_create_topic_result_publishes_anchor(ctx: AppContext):
    node = ctx.topics.nodes.create_topic("五里关火锅")
    AnchorService(ctx.conn).set_active(node.id)

    collected, finish = await _collect_anchor(ctx)
    await ctx._on_tool_anchor_result(
        {"tool": "create_topic", "result": ToolResult(ok=True, content="已创建并切换")}
    )
    await finish()

    assert len(collected) == 1
    assert collected[0]["topic_name"] == "五里关火锅"


async def test_non_topic_tool_does_not_publish(ctx: AppContext):
    collected, finish = await _collect_anchor(ctx)
    await ctx._on_tool_anchor_result(
        {"tool": "now", "result": ToolResult(ok=True, content="time")}
    )
    await finish()
    assert collected == []


async def test_failed_switch_does_not_publish(ctx: AppContext):
    collected, finish = await _collect_anchor(ctx)
    await ctx._on_tool_anchor_result(
        {"tool": "switch_topic", "result": ToolResult(ok=False, error="话题不存在")}
    )
    await finish()
    assert collected == []
