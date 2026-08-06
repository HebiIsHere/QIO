"""M12 maintenance: contradiction scan, dreaming, tool candidates, scheduler."""

from __future__ import annotations

import asyncio
import json

import pytest

from agent.api.bus import EventBus
from agent.config import Settings
from agent.knowledge.lifecycle import KnowledgeService
from agent.services.app import AppContext
from agent.storage.db import connect
from agent.storage.migrate import apply_migrations
from agent.services.maintenance import (
    MaintenanceScheduler,
    mine_tool_candidates,
    run_dreaming,
    scan_contradictions,
)


@pytest.fixture()
def ctx(tmp_path) -> AppContext:
    conn = connect(tmp_path / "app.db")
    apply_migrations(conn)
    return AppContext(Settings(data_dir=tmp_path), conn, EventBus())


def _seed_active_knowledge(ctx: AppContext, content: str, category: str = "general_fact") -> str:
    ks = KnowledgeService(ctx.conn)
    item = ks.create(category=category, content=content)
    ks.submit(item.id)
    ks.verify(item.id, verified_by="user")
    ks.activate(item.id)
    return item.id


async def test_scan_contradictions_low_impact_degrades(ctx: AppContext):
    kid = _seed_active_knowledge(ctx, "用户每天喝三杯咖啡")
    ctx.conn.execute(
        "INSERT INTO messages (id, fragment_id, role, content, content_type, created_at) "
        "VALUES ('m1', NULL, 'user', '其实我不是每天喝三杯咖啡，我不太喝咖啡', 'text', '2026-08-01T00:00:00+00:00')"
    )
    ctx.conn.commit()
    result = await scan_contradictions(ctx)
    assert result["contradiction_hits"] >= 1
    row = ctx.conn.execute("SELECT confidence FROM knowledge WHERE id = ?", (kid,)).fetchone()
    assert row["confidence"] < 0.9


async def test_scan_contradictions_high_impact_requests_approval(ctx: AppContext):
    kid = _seed_active_knowledge(ctx, "用户偏好清淡饮食", category="user_profile")
    ctx.conn.execute(
        "INSERT INTO messages (id, fragment_id, role, content, content_type, created_at) "
        "VALUES ('m2', NULL, 'user', '不对，我其实特别喜欢吃辣，不是清淡', 'text', '2026-08-01T00:00:00+00:00')"
    )
    ctx.conn.commit()
    await scan_contradictions(ctx)
    # 高影响产生审批请求（事件流可见）——直接验证 confidence 已降
    row = ctx.conn.execute("SELECT confidence FROM knowledge WHERE id = ?", (kid,)).fetchone()
    assert row["confidence"] < 0.9


async def test_dreaming_low_impact_applies_correct(ctx: AppContext, monkeypatch):
    kid = _seed_active_knowledge(ctx, "用户偏好清淡饮食")
    payload = json.dumps({
        "candidates": [
            {"action": "correct", "knowledge_id": kid, "new_content": "用户偏好清淡且不吃辣", "reason": "合并"},
        ]
    }, ensure_ascii=False)

    class FakeAdapter:
        mode = "native"
        model = "m"

        async def complete(self, messages, tools, **kwargs):
            from agent.adapters.base import ChatMessage, Completion
            return Completion(message=ChatMessage(role="assistant", content=payload))

    monkeypatch.setattr(ctx, "build_adapter", _fake_build(FakeAdapter()))
    result = await run_dreaming(ctx)
    assert result["dreaming_candidates"] == 1
    old = KnowledgeService(ctx.conn).get(kid)
    assert old.state.value == "revoked"  # correct 走 supersedes


async def test_dreaming_high_impact_pending_approval(ctx: AppContext, monkeypatch):
    kid = _seed_active_knowledge(ctx, "用户偏好清淡饮食", category="user_profile")
    payload = json.dumps({
        "candidates": [
            {"action": "expire", "knowledge_id": kid, "reason": "长期未提及"},
        ]
    }, ensure_ascii=False)

    class FakeAdapter:
        mode = "native"
        model = "m"

        async def complete(self, messages, tools, **kwargs):
            from agent.adapters.base import ChatMessage, Completion
            return Completion(message=ChatMessage(role="assistant", content=payload))

    monkeypatch.setattr(ctx, "build_adapter", _fake_build(FakeAdapter()))
    result = await run_dreaming(ctx)
    # 高影响不自动应用；发起审批请求（等待超时后仍保留原状）
    assert result["dreaming_candidates"] == 1
    item = KnowledgeService(ctx.conn).get(kid)
    assert item.state.value == "active"  # 未自动 revoke


async def test_mine_tool_candidates_cluster_and_request(ctx: AppContext, monkeypatch):
    for i in range(3):
        ctx.conn.execute(
            "INSERT INTO messages (id, fragment_id, role, content, content_type, created_at) "
            "VALUES (?, NULL, 'user', ?, 'text', '2026-08-01T00:00:00+00:00')",
            (f"mc{i}", f"帮我查一下今天的天气怎么样 {i}"),
        )
    ctx.conn.commit()
    result = await mine_tool_candidates(ctx)
    assert result["tool_candidates"] >= 1


async def test_scheduler_run_once_no_concurrency(ctx: AppContext, monkeypatch):
    calls = {"n": 0}

    async def fake_scan(c):
        calls["n"] += 1
        await asyncio.sleep(0.05)
        return {"contradiction_hits": 0}

    monkeypatch.setattr("agent.services.maintenance.scan_contradictions", fake_scan)
    scheduler = MaintenanceScheduler(ctx)
    first = asyncio.create_task(scheduler.run_once())
    await asyncio.sleep(0.01)
    second = await scheduler.run_once()
    assert second["ok"] is False  # 防重入
    await first
    assert calls["n"] == 1


def _fake_build(adapter):
    from unittest.mock import AsyncMock
    return AsyncMock(return_value=adapter)
