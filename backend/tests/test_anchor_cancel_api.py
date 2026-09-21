"""取消接续登记这条路由（阶段 1 / 阶段 5 界面行为）。

界面契约：点击历史只登记，「下一条消息才生效」；发送前取消必须是零成本的
—— 不发消息就不留痕迹（不产生空片段），并且取消后界面上的提示要能自己消失。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent.api.bus import EventBus
from agent.api.server import create_app
from agent.config import Settings
from agent.credentials.store import MemoryKeyring
from agent.services.app import AppContext
from agent.storage.db import connect
from agent.storage.migrate import apply_migrations


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    conn = connect(tmp_path / "nav.db")
    apply_migrations(conn)
    settings = Settings(data_dir=tmp_path)
    app = create_app(settings, conn)
    ctx: AppContext = app.state.ctx
    ctx.credentials._kr = MemoryKeyring()
    with TestClient(app) as c:
        yield c


def _seed_closed_fragment(ctx: AppContext, topic_id: str) -> str:
    ctx.conn.execute(
        "INSERT INTO fragments (id, topic_id, summary, created_at, closed_at) "
        "VALUES ('frag_old', ?, '旧摘要', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')",
        (topic_id,),
    )
    ctx.conn.execute(
        "INSERT INTO messages (id, fragment_id, role, content, content_type, created_at) "
        "VALUES ('frag_old_m1', 'frag_old', 'user', '旧原文', 'text', '2026-01-01T00:00:00+00:00')"
    )
    ctx.conn.commit()
    return "frag_old"


def test_cancel_continuation_leaves_no_trace(client: TestClient):
    ctx: AppContext = client.app.state.ctx
    topic = ctx.topics.nodes.create_topic("甲话题").id
    _seed_closed_fragment(ctx, topic)

    # 点击历史：只登记意图，不建片段
    registered = client.post(
        "/api/anchor",
        json={"topic_id": topic, "fragment_id": "frag_old", "continue_from_history": True},
    ).json()
    assert registered["pending"] is True
    assert ctx.conn.execute("SELECT COUNT(*) c FROM fragments").fetchone()["c"] == 1

    # 发送前取消：意图作废，仍然没有任何新片段
    cancelled = client.post("/api/anchor/continue/cancel").json()
    assert cancelled == {"ok": True, "cancelled": True}
    assert ctx.bindings.peek_intent() is None
    assert ctx.conn.execute("SELECT COUNT(*) c FROM fragments").fetchone()["c"] == 1

    # 再取消一次：幂等，不报错
    again = client.post("/api/anchor/continue/cancel").json()
    assert again == {"ok": True, "cancelled": False}


def test_cancel_broadcasts_anchor_event_so_the_hint_can_disappear(client: TestClient):
    """取消之后必须广播 ANCHOR（pending 字段为空），界面上的提示才会自己消失。

    这里直接看总线历史，而不是用 TestClient 再开一条 SSE：
    TestClient 的流式请求和同步请求共用一个 portal，另起线程订阅会死锁
    （实测确认过），那种测法测的是测试框架而不是产品行为。
    """
    from agent.api.events import EventType

    ctx: AppContext = client.app.state.ctx
    topic = ctx.topics.nodes.create_topic("甲话题").id
    _seed_closed_fragment(ctx, topic)
    client.post(
        "/api/anchor",
        json={"topic_id": topic, "fragment_id": "frag_old", "continue_from_history": True},
    )
    before = len(ctx.bus._history)  # noqa: SLF001 - 测试直接读总线历史，避免再开一条 SSE

    client.post("/api/anchor/continue/cancel")

    published = [e for e in list(ctx.bus._history)[before:] if e.type == EventType.ANCHOR]
    assert published, "取消之后必须广播 ANCHOR"
    assert published[-1].data.get("pending_intent_id") is None
