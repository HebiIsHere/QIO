"""Agent 的片段级历史选择能力：memory_search（只读）+ continue_from_fragment（显式改变位置）。"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timezone

import pytest

from agent.api.bus import EventBus
from agent.config import Settings
from agent.credentials.store import MemoryKeyring
from agent.graph.anchors import AnchorService
from agent.services.app import AppContext
from agent.storage.db import connect
from agent.storage.migrate import apply_migrations
from agent.tools.base import ToolResult
from agent.tools.memory_search import MemorySearchTool


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@pytest.fixture()
def ctx(tmp_path) -> AppContext:
    conn = connect(tmp_path / "app.db")
    apply_migrations(conn)
    app_ctx = AppContext(Settings(data_dir=tmp_path), conn, EventBus())
    app_ctx.credentials._kr = MemoryKeyring()
    return app_ctx


def _topic(ctx: AppContext, name: str) -> str:
    return ctx.topics.nodes.create_topic(name).id


def _seed_fragment(
    ctx: AppContext,
    topic_id: str,
    *,
    title: str = "Anchor 生命周期",
    summary: str = "SUMMARY-ANCHOR-XYZ：anchor 是用户明确选择的历史位置，不等于最新片段",
    message: str = "MESSAGE-ANCHOR-XYZ 我们讨论 anchor 语义：不应该等于 latest fragment",
) -> str:
    from agent.memory.fragment import new_id

    frag_id = new_id("frag")
    now = _now()
    ctx.conn.execute(
        "INSERT INTO fragments (id, topic_id, summary, summary_version, created_at, closed_at) "
        "VALUES (?, ?, ?, 1, ?, ?)",
        (frag_id, topic_id, summary, now, now),
    )
    ctx.conn.execute(
        "INSERT INTO messages (id, fragment_id, role, content, content_type, created_at) "
        "VALUES (?, ?, 'user', ?, 'text', ?)",
        (f"{frag_id}_m0", frag_id, message, now),
    )
    ctx.conn.execute(
        "UPDATE fragments SET start_message_id = ?, end_message_id = ? WHERE id = ?",
        (f"{frag_id}_m0", f"{frag_id}_m0", frag_id),
    )
    ctx.conn.execute(
        "INSERT INTO memory_index (id, fragment_id, topic_id, entity_ids, keywords, "
        "title, token_estimate, created_at) VALUES (?, ?, ?, '[]', '[]', ?, 10, ?)",
        (new_id("idx"), frag_id, topic_id, title, now),
    )
    ctx.conn.commit()
    ctx._refresh_selector()
    return frag_id


def _cursor_snapshot(ctx: AppContext) -> list[tuple]:
    return [
        tuple(r)
        for r in ctx.conn.execute(
            "SELECT anchor_type, topic_id, fragment_id FROM cursor ORDER BY anchor_type, updated_at"
        ).fetchall()
    ]


# -- memory_search：结果必须能定位到具体 Fragment -------------------------


def test_memory_search_output_identifies_fragment(ctx: AppContext):
    topic = _topic(ctx, "Anchor 讨论")
    frag = _seed_fragment(ctx, topic)
    tool = MemorySearchTool(ctx.retriever)

    result = asyncio.run(tool.run(query="anchor 生命周期 为什么"))

    assert result.ok is True
    assert frag in result.content, "必须给出 Fragment ID（Agent 才能 continue 到它）"
    assert topic in result.content, "必须给出 Topic ID"
    assert "Anchor 生命周期" in result.content, "必须给出标题"
    assert "Score:" in result.content, "必须给出相关性分数"
    assert "SUMMARY-ANCHOR-XYZ" in result.content or "MESSAGE-ANCHOR-XYZ" in result.content


def test_memory_search_without_hits_is_explicit(ctx: AppContext):
    topic = _topic(ctx, "空话题")
    tool = MemorySearchTool(ctx.retriever)
    result = asyncio.run(tool.run(query="完全不存在的主题 zzz"))
    assert result.ok is True
    assert result.content.strip() != ""


def test_memory_search_does_not_touch_anchor_or_history(ctx: AppContext):
    topic = _topic(ctx, "只读验证")
    frag = _seed_fragment(ctx, topic)
    anchors = AnchorService(ctx.conn)
    anchors.set_active(topic, frag)
    before = _cursor_snapshot(ctx)

    tool = MemorySearchTool(ctx.retriever)
    for _ in range(10):
        asyncio.run(tool.run(query="anchor"))

    assert _cursor_snapshot(ctx) == before


# -- continue_from_fragment：显式改变历史位置 ------------------------------


def _continue_tool(ctx: AppContext):
    from agent.tools.continue_tool import ContinueFromFragmentTool

    return ContinueFromFragmentTool(ctx.conn)


def test_continue_from_fragment_sets_topic_and_position(ctx: AppContext):
    a = _topic(ctx, "话题A")
    b = _topic(ctx, "话题B")
    frag = _seed_fragment(ctx, b)
    AnchorService(ctx.conn).set_active(a)

    result = _continue_tool(ctx).run_sync(fragment_id=frag, reason="用户要求从那段继续")

    assert result.ok is True
    active = AnchorService(ctx.conn).get_active()
    assert active.topic_id == b, "跨话题 continue 必须同时切换话题"
    assert active.fragment_id == frag
    assert "继续" in result.content or "已" in result.content
    # 与 switch_topic 一致：建立相关关系，便于后续话题亲和
    row = ctx.conn.execute(
        "SELECT weight FROM edges WHERE type='related' AND src=? AND dst=?",
        tuple(sorted([a, b])),
    ).fetchone()
    assert row is not None and row["weight"] >= 1.0


def test_continue_from_fragment_same_topic_position(ctx: AppContext):
    a = _topic(ctx, "话题A")
    frag = _seed_fragment(ctx, a)
    AnchorService(ctx.conn).set_active(a, None)

    result = _continue_tool(ctx).run_sync(fragment_id=frag)

    assert result.ok is True
    assert AnchorService(ctx.conn).get_active().fragment_id == frag


def test_continue_from_fragment_rejects_unknown_without_state_change(ctx: AppContext):
    a = _topic(ctx, "话题A")
    AnchorService(ctx.conn).set_active(a)
    before = _cursor_snapshot(ctx)

    result = _continue_tool(ctx).run_sync(fragment_id="frag_ghost")

    assert result.ok is False
    assert "frag_ghost" in (result.error or "") or "不存在" in (result.error or "")
    assert _cursor_snapshot(ctx) == before, "失败不得留下半更新状态"


def test_continue_from_fragment_rejects_missing_topic(ctx: AppContext):
    a = _topic(ctx, "话题A")
    frag = _seed_fragment(ctx, a)
    ctx.conn.execute("PRAGMA foreign_keys = OFF")
    ctx.conn.execute("DELETE FROM nodes WHERE id = ?", (a,))
    ctx.conn.execute("PRAGMA foreign_keys = ON")
    ctx.conn.commit()
    before = _cursor_snapshot(ctx)

    result = _continue_tool(ctx).run_sync(fragment_id=frag)

    assert result.ok is False
    assert _cursor_snapshot(ctx) == before


def test_continue_from_fragment_requires_fragment_id(ctx: AppContext):
    result = _continue_tool(ctx).run_sync()
    assert result.ok is False
    assert "fragment_id" in (result.error or "")


# -- Agent 完整流程：search → continue -------------------------------------


def test_agent_search_then_continue_flow(ctx: AppContext):
    a = _topic(ctx, "当前话题")
    b = _topic(ctx, "Anchor 讨论")
    frag = _seed_fragment(ctx, b)
    AnchorService(ctx.conn).set_active(a)

    search = MemorySearchTool(ctx.retriever)
    hits = asyncio.run(search.run(query="anchor 生命周期"))
    assert frag in hits.content
    # 搜索本身不改位置
    assert AnchorService(ctx.conn).get_active().topic_id == a

    result = _continue_tool(ctx).run_sync(fragment_id=frag, reason="从那次讨论继续")
    assert result.ok is True
    active = AnchorService(ctx.conn).get_active()
    assert (active.topic_id, active.fragment_id) == (b, frag)


# -- 工具 schema 与事件广播 ------------------------------------------------


def test_tool_description_and_schema_are_explicit(ctx: AppContext):
    from agent.tools.continue_tool import ContinueFromFragmentTool

    tool = ContinueFromFragmentTool(ctx.conn)
    assert tool.name == "continue_from_fragment"
    props = tool.parameters["properties"]
    assert "fragment_id" in props
    assert tool.parameters["required"] == ["fragment_id"]
    desc = tool.description
    # 明确「什么时候该调用 / 什么时候只要 memory_search」
    assert "从那里继续" in desc or "继续" in desc
    assert "memory_search" in desc
    assert "不要" in desc and "查询" in desc


def test_continue_tool_publishes_anchor_event(ctx: AppContext):
    import json

    async def collect():
        a = _topic(ctx, "话题A")
        b = _topic(ctx, "话题B")
        frag = _seed_fragment(ctx, b)
        AnchorService(ctx.conn).set_active(a)
        _continue_tool(ctx).run_sync(fragment_id=frag)

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
        await ctx._on_tool_anchor_result(
            {"tool": "continue_from_fragment", "result": ToolResult(ok=True, content="ok")}
        )
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return collected

    collected = asyncio.run(collect())
    assert collected, "continue_from_fragment 成功后必须广播 ANCHOR"
    assert collected[0]["fragment_id"]
    assert collected[0]["historic"] is True, "历史位置要显式标记，前端才会显示提示"
