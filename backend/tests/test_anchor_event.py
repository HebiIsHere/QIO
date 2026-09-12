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


async def test_anchor_api_publishes_authoritative_anchor(ctx: AppContext):
    """用户「从这里继续」（POST /api/anchor）也要广播 ANCHOR：

    前端不应该自己用摘要拼一个「标题」，权威标题与 historic 标记只能有一个来源（后端）。
    """
    from datetime import datetime, timezone

    import httpx

    from agent.api.server import create_app
    from agent.memory.fragment import new_id

    node = ctx.topics.nodes.create_topic("从这里继续")
    now = datetime.now(timezone.utc).isoformat()
    frag = new_id("frag")
    ctx.conn.execute(
        "INSERT INTO fragments (id, topic_id, summary, summary_version, created_at, closed_at) "
        "VALUES (?, ?, '历史片段摘要', 1, ?, ?)",
        (frag, node.id, now, now),
    )
    ctx.conn.execute(
        "INSERT INTO memory_index (id, fragment_id, topic_id, entity_ids, keywords, title, "
        "token_estimate, created_at) VALUES (?, ?, ?, '[]', '[]', '权威标题', 10, ?)",
        (new_id("idx"), frag, node.id, now),
    )
    ctx.conn.commit()

    # /api/anchor 走真实 HTTP 路由（ASGITransport：与订阅者在同一个事件循环里）
    app = create_app(ctx.settings, ctx.conn)
    app_ctx = app.state.ctx
    collected, finish = await _collect_anchor(app_ctx)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/anchor", json={"topic_id": node.id, "fragment_id": frag}
        )
    assert resp.status_code == 200
    # 响应直接给出权威标题与 historic：前端不需要用摘要自己拼标题，也不受事件到达顺序影响
    body = resp.json()
    assert body["fragment_title"] == "权威标题"
    assert body["historic"] is True

    await finish()
    assert collected, "POST /api/anchor 必须广播 ANCHOR"
    assert collected[-1]["fragment_id"] == frag
    assert collected[-1]["fragment_title"] == "权威标题"
    assert collected[-1]["historic"] is True
