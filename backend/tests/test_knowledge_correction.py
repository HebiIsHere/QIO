"""Knowledge correction: correct_knowledge tool + API."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest

from fastapi.testclient import TestClient

from agent.api.server import create_app
from agent.config import Settings
from agent.credentials.store import MemoryKeyring
from agent.knowledge.lifecycle import KnowledgeService
from agent.storage.db import connect
from agent.storage.migrate import apply_migrations
from agent.tools.knowledge_correction import CorrectKnowledgeTool


@pytest.fixture()
def client(db_conn: sqlite3.Connection, settings: Settings):
    app = create_app(settings, db_conn)
    app.state.ctx.credentials._kr = MemoryKeyring()
    with TestClient(app) as c:
        yield c


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _seed_knowledge(db_conn: sqlite3.Connection) -> dict[str, str]:
    ks = KnowledgeService(db_conn)
    now = _now()
    ids: dict[str, str] = {}
    for key, category, content in (
        ("diet", "user_profile", "用户偏好清淡饮食，完全不吃辣"),
        ("milk", "user_profile", "用户讨厌喝牛奶"),
        ("sql", "general_fact", "SQLite 支持 WAL 模式"),
    ):
        item = ks.create(category=category, content=content)
        ks.submit(item.id)
        ks.verify(item.id, verified_by="user")
        ks.activate(item.id)
        ids[key] = item.id
    return ids


def _tool(db_conn: sqlite3.Connection, snapshot=None) -> CorrectKnowledgeTool:
    return CorrectKnowledgeTool(
        db_conn,
        snapshot_provider=lambda: snapshot or [],
    )


async def test_correct_snapshot_hit_creates_supersede(db_conn: sqlite3.Connection):
    ids = _seed_knowledge(db_conn)
    ks = KnowledgeService(db_conn)
    tool = _tool(db_conn, snapshot=[{"item_id": ids["diet"], "content": "用户偏好清淡饮食，完全不吃辣"}])
    r = await tool.run(
        content="用户偏好清淡饮食，完全不吃辣",
        new_content="用户其实开始吃辣了",
    )
    assert r.ok
    old = ks.get(ids["diet"])
    assert old.state.value == "revoked"  # 旧条目自动 revoked
    rows = db_conn.execute(
        "SELECT id, state, content, supersedes_id FROM knowledge "
        "WHERE content LIKE '%吃辣了%' AND state = 'active'"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["supersedes_id"] == ids["diet"]


async def test_correct_delete_revokes(db_conn: sqlite3.Connection):
    ids = _seed_knowledge(db_conn)
    ks = KnowledgeService(db_conn)
    tool = _tool(db_conn, snapshot=[{"item_id": ids["milk"], "content": "用户讨厌喝牛奶"}])
    r = await tool.run(content="用户讨厌喝牛奶", delete=True)
    assert r.ok
    assert ks.get(ids["milk"]).state.value == "revoked"


async def test_correct_fallback_full_library_and_candidates(db_conn: sqlite3.Connection):
    ids = _seed_knowledge(db_conn)
    ks = KnowledgeService(db_conn)
    # 快照为空 → 全库匹配唯一
    tool = _tool(db_conn, snapshot=[])
    r = await tool.run(content="SQLite 支持 WAL 模式", new_content="SQLite 默认 journal 模式是 delete")
    assert r.ok
    assert ks.get(ids["sql"]).state.value == "revoked"
    # 多候选 → 返回列表，candidate_index 选择
    db_conn.execute(
        "INSERT INTO knowledge (id, category, state, content, created_at, updated_at) "
        "VALUES ('kn_dup1', 'user_profile', 'active', '用户偏好清淡饮食', ?, ?)",
        (_now(), _now()),
    )
    db_conn.commit()
    tool2 = _tool(db_conn, snapshot=[])
    r2 = await tool2.run(content="用户偏好清淡饮食", new_content="新版本")
    assert r2.ok is False  # 多候选未指定 index → 返回候选提示
    assert "candidate_index" in (r2.error or "")
    r3 = await tool2.run(content="用户偏好清淡饮食", new_content="新版本", candidate_index=1)
    assert r3.ok


async def test_correct_missing_content_fails(db_conn: sqlite3.Connection):
    tool = _tool(db_conn, snapshot=[])
    r = await tool.run(content="")
    assert r.ok is False
    r2 = await tool.run(content="不存在的内容", new_content="x")
    assert r2.ok is False
    r3 = await tool.run(content="不存在的内容", delete=True)
    assert r3.ok is False


# ---------- API ----------

def test_knowledge_revise_api(client):
    ctx = client.app.state.ctx
    ks = KnowledgeService(ctx.conn)
    item = ks.create(category="general_fact", content="旧事实")
    ks.submit(item.id)
    ks.verify(item.id, verified_by="system")
    ks.activate(item.id)
    r = client.post(f"/api/knowledge/{item.id}/revise", json={"content": "新事实"})
    assert r.status_code == 200
    assert ks.get(item.id).state.value == "revoked"
    r2 = client.post(f"/api/knowledge/{item.id}/revoke")
    assert r2.status_code == 200
    assert client.post("/api/knowledge/ghost/revoke").status_code == 404
    assert client.post(f"/api/knowledge/{item.id}/revise", json={"content": ""}).status_code == 400
