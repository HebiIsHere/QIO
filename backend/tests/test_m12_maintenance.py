"""M12 maintenance：工具候选挖掘 + 调度器生命周期（阶段二起知识维护独立）。

阶段二（契约 §12.6）退休了旧的否定词矛盾扫描与 dreaming（会按置信度下调 / revoke）：
知识整理改由独立的 agent/maintenance 调度器负责（合并 / 格式整理 / 独立问答）。
本文件保留仍然有效的工具候选挖掘与调度器防重入验证，并明确断言旧路径不再被调度。
"""

from __future__ import annotations

import asyncio
import json

import pytest

from agent.api.bus import EventBus
from agent.config import Settings
from agent.knowledge.lifecycle import KnowledgeService
from agent.services.app import AppContext
from agent.services.maintenance import MaintenanceScheduler, mine_tool_candidates
from agent.storage.db import connect
from agent.storage.migrate import apply_migrations


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


async def test_run_once_no_longer_scans_or_dreams(ctx: AppContext, monkeypatch):
    """旧矛盾扫描 / dreaming 不再被 run_once 调度。

    「不得因长期未出现、否定词或词语重合直接撤销或降低有效认识」：即使库里有一条
    带否定词的用户消息，维护也不得改这条有效知识的 confidence / state。
    """
    kid = _seed_active_knowledge(ctx, "用户每天喝三杯咖啡")
    ctx.conn.execute(
        "INSERT INTO messages (id, fragment_id, role, content, content_type, created_at) "
        "VALUES ('m1', NULL, 'user', '其实我不是每天喝三杯咖啡，我不太喝咖啡', 'text', '2026-08-01T00:00:00+00:00')"
    )
    ctx.conn.commit()

    async def _forbidden(*args, **kwargs):  # pragma: no cover - 被调用即失败
        raise AssertionError("旧知识维护路径不得再被 run_once 调度")

    monkeypatch.setattr("agent.services.maintenance.scan_contradictions", _forbidden)
    monkeypatch.setattr("agent.services.maintenance.run_dreaming", _forbidden)

    scheduler = MaintenanceScheduler(ctx)
    result = await scheduler.run_once()

    assert result["ok"] is True
    assert "contradiction_hits" not in result, "旧矛盾扫描不得再出现在维护结果里"
    assert "dreaming_candidates" not in result, "旧 dreaming 不得再出现在维护结果里"
    row = ctx.conn.execute("SELECT confidence, state FROM knowledge WHERE id = ?", (kid,)).fetchone()
    assert row["confidence"] is None or row["confidence"] >= 0.9, "有效认识不因否定词被降低置信度"
    assert row["state"] == "active", "有效认识不因长期未出现 / 否定词被撤销"


async def test_run_once_still_prunes_and_mines_tool_candidates(ctx: AppContext):
    """工具输出清理 / 工具记录保留 / 自动化建议（工具候选）仍由维护巡检负责。"""
    for i in range(3):
        ctx.conn.execute(
            "INSERT INTO messages (id, fragment_id, role, content, content_type, created_at) "
            "VALUES (?, NULL, 'user', ?, 'text', '2026-08-01T00:00:00+00:00')",
            (f"mc{i}", f"帮我查一下今天的天气怎么样 {i}"),
        )
    ctx.conn.commit()
    scheduler = MaintenanceScheduler(ctx)
    result = await scheduler.run_once()
    assert result["ok"] is True
    assert "tool_outputs_purged" in result
    assert "tool_records_purged" in result
    assert result.get("tool_candidates", 0) >= 1


async def test_mine_tool_candidates_cluster_and_request(ctx: AppContext):
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

    async def fake_mine(c):
        calls["n"] += 1
        await asyncio.sleep(0.05)
        return {"tool_candidates": 0}

    monkeypatch.setattr("agent.services.maintenance.mine_tool_candidates", fake_mine)
    scheduler = MaintenanceScheduler(ctx)
    first = asyncio.create_task(scheduler.run_once())
    await asyncio.sleep(0.01)
    second = await scheduler.run_once()
    assert second["ok"] is False  # 防重入
    await first
    assert calls["n"] == 1
