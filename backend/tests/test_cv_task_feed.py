"""对话页 v2 · 左侧任务栏权威快照的定向测试（后端）。

覆盖验收重点：
* 展示状态诚实映射：unknown 不默认 running、partial 不折叠成 completed、
  budget_exhausted / cancelled 独立；rawStatus 原样保留；
* 派生任务自动重试不算最终失败；
* 未完成开发任务需要用户处理并给出真实支持的动作；
* 工具执行失败置顶；「知道了」后进历史且结果保留；
* 子任务生命周期钩子（queued → running → done）与取消事实。
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import sqlite3

import pytest
from fastapi.testclient import TestClient

from agent.api.bus import EventBus
from agent.api.server import create_app
from agent.config import Settings
from agent.storage import tool_records
from agent.storage.db import connect
from agent.tools.base import ToolResult
from agent.tools.task_manager import TaskManager


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@pytest.fixture()
def client(db_conn: sqlite3.Connection, settings: Settings):
    from agent.credentials.store import MemoryKeyring

    app = create_app(settings, db_conn)
    app.state.ctx.credentials._kr = MemoryKeyring()
    with TestClient(app) as c:
        yield c


def _find(client, task_id: str, view: str = "current"):
    response = client.get(f"/api/tasks?view={view}&limit=100")
    assert response.status_code == 200, response.text
    for item in response.json()["items"]:
        if item["id"] == task_id:
            return item
    return None


def test_task_feed_unknown_status_is_not_running(client):
    ctx = client.app.state.ctx
    ctx.conn.execute(
        "INSERT INTO task_feed (id, source, source_id, title, status, raw_status, "
        "origin, requires_attention, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?)",
        ("subagent:weird", "subagent", "weird", "状态未知的任务", "unknown", "novel_state", "unknown", _now()),
    )
    ctx.conn.commit()
    item = _find(client, "subagent:weird")
    assert item is not None
    assert item["status"] == "unknown"
    assert item["rawStatus"] == "novel_state"


def test_task_feed_partial_and_budget_are_not_completed_or_running(client):
    ctx = client.app.state.ctx
    now = _now()
    for source_id, status, raw in (
        ("partial1", "partial", "partially_completed"),
        ("budget1", "budget_exhausted", "budget_exhausted"),
        ("cancel1", "cancelled", "cancelled"),
    ):
        ctx.conn.execute(
            "INSERT INTO task_feed (id, source, source_id, title, status, raw_status, "
            "origin, requires_attention, updated_at) VALUES (?, ?, ?, ?, ?, ?, 'topic', 0, ?)",
            (f"subagent:{source_id}", "subagent", source_id, source_id, status, raw, now),
        )
    ctx.conn.commit()
    assert _find(client, "subagent:partial1")["status"] == "partial"
    assert _find(client, "subagent:budget1")["status"] == "budget_exhausted"
    item = _find(client, "subagent:cancel1")
    assert item is not None and item["status"] == "cancelled"


def test_derived_task_auto_retry_is_queued_not_failed(client):
    ctx = client.app.state.ctx
    now = datetime.now(timezone.utc)
    ctx.conn.execute(
        "INSERT INTO nodes (id, type, name, created_at, updated_at) VALUES (?, 'topic', ?, ?, ?)",
        ("topic_cv", "话题", now.isoformat(), now.isoformat()),
    )
    ctx.conn.execute(
        "INSERT INTO fragments (id, topic_id, created_at) VALUES (?, ?, ?)",
        ("frag_cv", "topic_cv", now.isoformat()),
    )
    ctx.conn.execute(
        "INSERT INTO derived_tasks (id, kind, fragment_id, content_version, state, attempts, "
        "last_error, run_after, created_at, updated_at) VALUES (?, 'summary', ?, 1, 'failed', 2, ?, ?, ?, ?)",
        (
            "task_retry",
            "frag_cv",
            "模型调用暂时失败",
            (now + timedelta(minutes=5)).isoformat(),
            now.isoformat(),
            now.isoformat(),
        ),
    )
    ctx.conn.commit()
    item = _find(client, "derived:task_retry")
    assert item is not None
    assert item["status"] == "queued", "自动重试中的派生任务不能被当成最终失败"
    assert item["requiresAttention"] is False
    assert "自动重试" in item["rawStatus"]


def test_unfinished_dev_task_needs_user_and_exposes_supported_actions(client):
    ctx = client.app.state.ctx
    task = ctx.dev_workspaces.create("修复示例项目里的构建脚本")
    item = _find(client, f"dev_task:{task.id}")
    assert item is not None
    assert item["status"] == "waiting_user"
    assert item["requiresAttention"] is True
    kinds = {action["kind"] for action in item["actions"] if action["supported"]}
    assert {"continue_dev", "abandon_dev"} <= kinds
    # 没有真实授权时不得显示「撤销执行授权」
    revoke = next(action for action in item["actions"] if action["kind"] == "revoke_auth")
    assert revoke["supported"] is False


def test_failed_tool_execution_is_attention_then_ack_moves_to_history(client):
    ctx = client.app.state.ctx
    record_id = tool_records.record_tool_call(
        ctx.conn,
        turn_id="turn_cv",
        tool_name="run_shell",
        arguments={"cmd": "pytest"},
        output="命令行失败输出",
        status="failed",
        error="exit 1",
        duration_ms=1500,
    )
    assert record_id
    item = _find(client, f"tool:{record_id}")
    assert item is not None
    assert item["status"] == "failed"
    assert item["requiresAttention"] is True
    assert "仅显示部分" in item["output"]["hint"] or item["output"]["truncated"] is False
    detail = client.get(f"/api/tasks/tool:{record_id}")
    assert detail.status_code == 200, detail.text
    assert detail.json()["item"]["id"] == f"tool:{record_id}"
    assert detail.json()["parts"], "详情必须返回可读输出"
    ack = client.post(f"/api/tasks/tool:{record_id}/ack")
    assert ack.status_code == 200
    assert _find(client, f"tool:{record_id}", view="history") is not None
    assert _find(client, f"tool:{record_id}", view="current") is None


def test_unknown_task_detail_returns_404(client):
    response = client.get("/api/tasks/subagent:missing")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_task_manager_lifecycle_callback_reports_real_states():
    changes: list[str] = []
    manager = TaskManager(EventBus(), on_change=lambda record: changes.append(record.status))

    async def work() -> ToolResult:
        await asyncio.sleep(0)
        return ToolResult(ok=True, content="完成")

    task_id = manager.submit("research", work)
    _, result = await manager.await_result(task_id, timeout=5)
    assert result is not None and result.ok
    assert "queued" in changes, changes
    assert "running" in changes, changes
    assert changes[-1] == "done", changes


@pytest.mark.asyncio
async def test_task_manager_cancel_callback_reports_cancelled():
    changes: list[str] = []
    manager = TaskManager(EventBus(), on_change=lambda record: changes.append(record.status))
    release = asyncio.Event()

    async def work() -> ToolResult:
        await release.wait()
        return ToolResult(ok=True, content="迟到结果")

    task_id = manager.submit("research", work)
    await asyncio.sleep(0)
    assert manager.cancel(task_id, "用户取消") is True
    assert changes[-1] == "cancelled"
    release.set()
    await asyncio.sleep(0.05)
    # 取消之后迟到结果不得复活任务
    assert manager.task_facts(task_id)["outcome"] == "cancelled"
