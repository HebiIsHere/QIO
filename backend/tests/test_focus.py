"""片段偏置（从这里开始）：锚点 API、聚焦块生成、注入组装、session context。"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from agent.api.bus import EventBus
from agent.api.server import create_app
from agent.config import Settings
from agent.credentials.store import MemoryKeyring
from agent.graph.anchors import AnchorService
from agent.knowledge.inject import InjectionSource
from agent.services.app import AppContext
from agent.storage.db import connect
from agent.storage.migrate import apply_migrations


@pytest.fixture()
def ctx(tmp_path) -> AppContext:
    conn = connect(tmp_path / "app.db")
    apply_migrations(conn)
    app_ctx = AppContext(Settings(data_dir=tmp_path), conn, EventBus())
    app_ctx.credentials._kr = MemoryKeyring()
    return app_ctx


@pytest.fixture()
def client(db_conn: sqlite3.Connection, settings: Settings):
    app = create_app(settings, db_conn)
    app.state.ctx.credentials._kr = MemoryKeyring()
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def topic(ctx: AppContext) -> str:
    node = ctx.topics.nodes.create_topic("测试话题")
    return node.id
from agent.services.injection import InjectionAssembler, InjectionBudget, BudgetConfig
from agent.services.retrieval import Retriever
from agent.selector.selector import Selector


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _seed_fragment(db_conn: sqlite3.Connection, topic_id: str, *, closed: bool = True) -> str:
    now = _now()
    frag_id = f"frag_{'closed' if closed else 'open'}_{topic_id[-8:]}"
    db_conn.execute(
        "INSERT OR IGNORE INTO fragments (id, topic_id, summary, created_at, closed_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (frag_id, topic_id, "该片段摘要：用户偏好清淡饮食", now, now if closed else None),
    )
    for i, role, content in (("m1", "user", "我最近开始吃清淡饮食"), ("m2", "assistant", "好的"), ("m3", "user", "我不吃辣")):
        db_conn.execute(
            "INSERT OR IGNORE INTO messages (id, fragment_id, role, content, content_type, created_at) "
            "VALUES (?, ?, ?, ?, 'text', ?)",
            (f"{frag_id}_{i}", frag_id, role, content, now),
        )
    if closed:
        db_conn.execute(
            "INSERT OR IGNORE INTO memory_index (id, fragment_id, topic_id, entity_ids, keywords, "
            "title, token_estimate, created_at) VALUES (?, ?, ?, '[]', '[\"饮食\"]', ?, 10, ?)",
            (f"idx_{frag_id}", frag_id, topic_id, "饮食偏好片段", now),
        )
    db_conn.commit()
    return frag_id


def test_focus_block_closed_fragment(ctx: AppContext):
    t = ctx.topics.nodes.create_topic("聚焦话题").id
    frag_id = _seed_fragment(ctx.conn, t, closed=True)
    block = ctx._focus_block(t, frag_id)
    assert "聚焦片段" in block and "饮食偏好片段" in block
    assert "该片段摘要" in block
    assert "我最近开始吃清淡饮食" in block  # 起始消息
    assert "不吃辣" in block  # 前 3 条内


def test_focus_block_open_fragment(ctx: AppContext):
    t = ctx.topics.nodes.create_topic("开放话题").id
    frag_id = _seed_fragment(ctx.conn, t, closed=False)
    block = ctx._focus_block(t, frag_id)
    assert "当前片段" in block
    assert "我最近开始吃清淡饮食" in block


def test_focus_block_invalid_and_cross_topic(ctx: AppContext):
    t1 = ctx.topics.nodes.create_topic("话题一").id
    t2 = ctx.topics.nodes.create_topic("话题二").id
    frag_id = _seed_fragment(ctx.conn, t1, closed=True)
    assert ctx._focus_block(t1, "not_exist") == ""
    assert ctx._focus_block(t2, frag_id) == ""  # 跨话题


def test_anchor_api_set_and_clear(client):
    # 建话题
    ctx = client.app.state.ctx
    node = ctx.topics.nodes.create_topic("锚点话题")
    frag_id = _seed_fragment(ctx.conn, node.id, closed=True)
    resp = client.post("/api/anchor", json={"topic_id": node.id, "fragment_id": frag_id})
    assert resp.status_code == 200
    active = AnchorService(ctx.conn).get_active()
    assert active.topic_id == node.id and active.fragment_id == frag_id
    resp = client.post("/api/anchor", json={"topic_id": node.id, "fragment_id": None})
    assert resp.status_code == 200
    active = AnchorService(ctx.conn).get_active()
    assert active.fragment_id is None
    # 非法话题 / 跨话题片段
    assert client.post("/api/anchor", json={"topic_id": "ghost"}).status_code == 404
    assert client.post("/api/anchor", json={"topic_id": node.id, "fragment_id": "ghost"}).status_code == 400


def test_injection_focus_before_short_term(ctx: AppContext):
    t = ctx.topics.nodes.create_topic("注入话题").id
    ctx.memory.append_message(topic_id=t, role="user", content="短期消息甲")
    budget = InjectionBudget(BudgetConfig(context_window=40_000, budget_ratio=0.25))
    retriever = Retriever(Selector(), ctx.topics)
    asm = InjectionAssembler(budget, retriever, knowledge_source=InjectionSource(ctx.conn))
    payload = asm.build(
        "查询", topic_id=t,
        focus_block="【聚焦片段·测试】摘要内容",
        short_term=ctx._short_term_items(t),
    )
    idx_focus = payload.text.index("聚焦片段")
    idx_short = payload.text.index("短期")
    assert idx_focus < idx_short


def test_session_context_returns_anchor_fragment(client):
    ctx = client.app.state.ctx
    node = ctx.topics.nodes.create_topic("上下文话题")
    frag_id = _seed_fragment(ctx.conn, node.id, closed=True)
    AnchorService(ctx.conn).set_active(node.id, frag_id)
    resp = client.get("/api/session/context")
    assert resp.status_code == 200
    af = resp.json().get("anchor_fragment")
    assert af is not None and af["id"] == frag_id and af["title"] == "饮食偏好片段"


def test_focus_cleared_after_switch(ctx: AppContext):
    from agent.tools.topic_tools import SwitchTopicTool

    t1 = ctx.topics.nodes.create_topic("来源话题").id
    t2 = ctx.topics.nodes.create_topic("目标话题").id
    frag_id = _seed_fragment(ctx.conn, t1, closed=True)
    AnchorService(ctx.conn).set_active(t1, frag_id)
    SwitchTopicTool(ctx.conn).run_sync(topic_id=t2, reason="切")
    active = AnchorService(ctx.conn).get_active()
    assert active.topic_id == t2 and active.fragment_id is None
