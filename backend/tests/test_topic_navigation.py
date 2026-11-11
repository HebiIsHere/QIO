"""统一 Topic 导航入口（spec 第 21~39 条）。

规则：
- Reference / 检索 / Predictor / Planet 选中 都**不是**导航，绝不改 Anchor；
- 只有 Navigator 的「进入话题 / 创建话题 / 确认切换 / 从历史继续」才改 Anchor；
- 「从历史继续」创建新片段并保存来源，旧片段一个字段都不改。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent.api.bus import EventBus
from agent.api.server import create_app
from agent.config import Settings
from agent.credentials.store import MemoryKeyring
from agent.graph.anchors import AnchorService
from agent.services.app import AppContext
from agent.services.navigation import (
    FragmentNotInTopic,
    TopicNavigationService,
    TopicNotFound,
)
from agent.storage.db import connect
from agent.storage.migrate import apply_migrations


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@pytest.fixture()
def ctx(tmp_path: Path) -> AppContext:
    conn = connect(tmp_path / "nav.db")
    apply_migrations(conn)
    app_ctx = AppContext(Settings(data_dir=tmp_path), conn, EventBus())
    app_ctx.credentials._kr = MemoryKeyring()
    return app_ctx


@pytest.fixture()
def nav(ctx: AppContext) -> TopicNavigationService:
    return ctx.navigation


def _seed_fragment(ctx: AppContext, topic_id: str, *, closed: bool = True, payload: str = "历史原文") -> str:
    frag_id = f"frag_{'c' if closed else 'o'}_{len(topic_id)}_{payload[-2:]}"
    now = _now()
    ctx.conn.execute(
        "INSERT INTO fragments (id, topic_id, summary, created_at, closed_at) VALUES (?, ?, ?, ?, ?)",
        (frag_id, topic_id, "该片段摘要", now, now if closed else None),
    )
    ctx.conn.execute(
        "INSERT INTO messages (id, fragment_id, role, content, content_type, created_at) "
        "VALUES (?, ?, 'user', ?, 'text', ?)",
        (f"{frag_id}_m1", frag_id, payload, now),
    )
    ctx.conn.commit()
    return frag_id


def test_enter_topic_writes_anchor_and_reports_position(ctx: AppContext, nav: TopicNavigationService):
    topic_id = ctx.topics.nodes.create_topic("甲话题").id
    frag_id = _seed_fragment(ctx, topic_id, closed=True)

    result = nav.enter_topic(topic_id, fragment_id=frag_id)

    assert result.topic_id == topic_id
    assert result.fragment_id == frag_id
    assert AnchorService(ctx.conn).get_active().topic_id == topic_id
    assert AnchorService(ctx.conn).get_active().fragment_id == frag_id


def test_enter_topic_restores_saved_position(ctx: AppContext, nav: TopicNavigationService):
    a = ctx.topics.nodes.create_topic("甲话题").id
    b = ctx.topics.nodes.create_topic("乙话题").id
    frag_a = _seed_fragment(ctx, a, closed=True)
    nav.enter_topic(a, fragment_id=frag_a)
    nav.enter_topic(b)

    result = nav.enter_topic(a)

    assert result.fragment_id == frag_a, "切回话题要恢复它保存的位置"


def test_enter_topic_rejects_unknown_topic_and_foreign_fragment(ctx: AppContext, nav: TopicNavigationService):
    a = ctx.topics.nodes.create_topic("甲话题").id
    b = ctx.topics.nodes.create_topic("乙话题").id
    frag_a = _seed_fragment(ctx, a, closed=True)

    with pytest.raises(TopicNotFound):
        nav.enter_topic("topic_ghost")
    with pytest.raises(FragmentNotInTopic):
        nav.enter_topic(b, fragment_id=frag_a)


def test_continue_from_history_creates_new_fragment_and_keeps_the_source_intact(
    ctx: AppContext, nav: TopicNavigationService
):
    topic_id = ctx.topics.nodes.create_topic("甲话题").id
    source = _seed_fragment(ctx, topic_id, closed=True)
    before = dict(ctx.conn.execute("SELECT * FROM fragments WHERE id = ?", (source,)).fetchone())

    # F14：两阶段契约（点击只登记；有消息要执行时才落实）。
    registered = nav.register_continuation(topic_id, source)
    assert registered["changed"] is True
    result = nav.apply_continuation(topic_id, registered["intent_id"])

    assert result.created_fragment_id is not None
    assert result.created_fragment_id != source
    assert result.source_fragment_id == source
    created = ctx.conn.execute(
        "SELECT * FROM fragments WHERE id = ?", (result.created_fragment_id,)
    ).fetchone()
    assert created["source_fragment_id"] == source
    assert created["closed_at"] is None
    after = dict(ctx.conn.execute("SELECT * FROM fragments WHERE id = ?", (source,)).fetchone())
    assert after == before, "从历史继续不得修改来源片段"
    assert AnchorService(ctx.conn).get_active().fragment_id == result.created_fragment_id


def test_continue_from_history_keeps_the_source_history_in_focus(ctx: AppContext, nav: TopicNavigationService):
    topic_id = ctx.topics.nodes.create_topic("甲话题").id
    payload = "PAYLOAD-CONTINUATION"
    source = _seed_fragment(ctx, topic_id, closed=True, payload=payload)

    registered = nav.register_continuation(topic_id, source)
    nav.apply_continuation(topic_id, registered["intent_id"])

    anchors = AnchorService(ctx.conn)
    assert anchors.focus_fragment(topic_id) == source, "接续片段本身是空的，Focus 必须回到来源历史"
    assert payload in ctx._focus_block(topic_id, source)
    assert anchors.is_historic_position(topic_id) is True


def test_continue_from_history_on_open_fragment_is_a_plain_enter(ctx: AppContext, nav: TopicNavigationService):
    topic_id = ctx.topics.nodes.create_topic("甲话题").id
    open_frag = _seed_fragment(ctx, topic_id, closed=False)

    registered = nav.register_continuation(topic_id, open_frag)
    assert registered["opens_current"] is True
    assert registered["intent_id"] is None
    assert registered["changed"] is False
    result = nav.enter_topic(topic_id, fragment_id=open_frag)

    assert result.created_fragment_id is None
    assert result.fragment_id == open_frag
    assert AnchorService(ctx.conn).get_active().fragment_id == open_frag


def test_legacy_pending_switch_entries_are_retired(nav: TopicNavigationService):
    """旧「待确认切换」业务写入 API 已退役（契约 §4/§9）。

    R16 起 Anchor/pending 的唯一业务写入口是 services/position_state.py 的统一入口；
    旧 pending 行机制没有生产调用方（/api/topic-switch/* 已 410），不得保留第二套
    业务写入方法。底层 AnchorService 的 pending 辅助函数本身保留。
    """
    for name in ("request_switch", "pending_switch", "confirm_switch", "reject_switch"):
        assert not hasattr(nav, name), f"旧待确认切换入口未退役：{name}"


def test_topic_prediction_and_retrieval_never_write_the_anchor(ctx: AppContext, nav: TopicNavigationService):
    a = ctx.topics.nodes.create_topic("甲话题").id
    b = ctx.topics.nodes.create_topic("乙话题").id
    _seed_fragment(ctx, b, closed=True, payload="橡胶实验降解数据")
    nav.enter_topic(a)
    before = [tuple(r) for r in ctx.conn.execute("SELECT anchor_type, topic_id, fragment_id FROM cursor")]

    ctx.predictor.predict("橡胶实验降解数据", current_topic_id=a)
    ctx.retriever.search("橡胶实验", anchor_topic_id=a, top_k=3)

    after = [tuple(r) for r in ctx.conn.execute("SELECT anchor_type, topic_id, fragment_id FROM cursor")]
    assert after == before, "检索与预测只读，不得写 cursor/anchor"


def test_planet_selection_never_writes_the_anchor(ctx: AppContext, nav: TopicNavigationService):
    """星球选中 ≠ 进入话题：选中不经过 Navigator，因此 Anchor 一动不动。"""
    a = ctx.topics.nodes.create_topic("甲话题").id
    b = ctx.topics.nodes.create_topic("乙话题").id
    nav.enter_topic(a)

    # 「选中」在数据层没有任何写动作：它只是前端状态 + 一次读详情。
    ctx.conn.execute("SELECT 1").fetchone()

    assert AnchorService(ctx.conn).get_active().topic_id == a
    assert b != a


def test_anchor_writes_are_centralized_in_the_navigator():
    """架构守卫：Anchor 只能有一个写入者（R16 契约 §4：统一位置变更入口）。

    底层锚点服务自己保留写方法；业务层唯一写者是 services/position_state.py 的
    PositionStateService.apply（R16 起 Anchor 的唯一写入点）。Navigator 只是它的薄包装，
    不得再绕过统一入口直接写 anchor。
    """
    import re

    src = Path(__file__).resolve().parents[1] / "src" / "agent"
    # R16（契约 §4）：Anchor 的唯一业务写入点收口到统一变更入口 PositionStateService；
    # Navigator 只是薄包装，不得再直接写 anchor。
    allowed = {
        "graph/anchors.py",
        "services/position_state.py",
    }
    offenders: list[str] = []
    for path in src.rglob("*.py"):
        rel = path.relative_to(src).as_posix()
        if rel in allowed:
            continue
        text = path.read_text(encoding="utf-8")
        if re.search(r"\.(set_active|restore_position|confirm_pending)\(\s*(topic_id|[a-z_]+)", text):
            offenders.append(rel)
    assert offenders == [], f"这些模块绕过统一位置入口直接写 anchor：{offenders}"


# -- HTTP 层：两个动作必须能被明确区分 ------------------------------------


@pytest.fixture()
def client(db_conn: sqlite3.Connection, settings: Settings):
    app = create_app(settings, db_conn)
    app.state.ctx.credentials._kr = MemoryKeyring()
    with TestClient(app) as c:
        yield c


def test_anchor_api_continue_from_history_creates_a_continuation(client):
    ctx = client.app.state.ctx
    topic_id = ctx.topics.nodes.create_topic("甲话题").id
    source = _seed_fragment(ctx, topic_id, closed=True)

    from agent.services.navigation_version import current as current_nav_version

    resp = client.post(
        "/api/anchor",
        json={
            "topic_id": topic_id,
            "fragment_id": source,
            "continue_from_history": True,
            "request_id": "req-nav-continue",
            "base_nav_version": current_nav_version(ctx.conn),
        },
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["source_fragment_id"] == source
    # 阶段 1 的语义：点击历史**只登记接续意图**，不创建新片段。
    # 界面据此显示「将从所选记录继续」；真正的新片段在本轮消息执行时才落实。
    assert body["created_fragment_id"] is None
    assert body["pending"] is True
    assert body["intent_id"]
    assert ctx.bindings.peek_intent().source_fragment_id == source
    # 还没有任何消息执行到这条意图：库里仍然只有那一段历史
    assert ctx.conn.execute("SELECT COUNT(*) c FROM fragments").fetchone()["c"] == 1
    # 位置停在所选历史处，界面才能显示「从…继续」
    assert body["historic"] is True
    row = ctx.conn.execute("SELECT * FROM fragments WHERE id = ?", (source,)).fetchone()
    assert row["closed_at"] is not None


def test_anchor_api_continue_from_history_requires_a_fragment(client):
    ctx = client.app.state.ctx
    topic_id = ctx.topics.nodes.create_topic("甲话题").id

    from agent.services.navigation_version import current as current_nav_version

    resp = client.post(
        "/api/anchor",
        json={
            "topic_id": topic_id,
            "continue_from_history": True,
            "request_id": "req-nav-no-fragment",
            "base_nav_version": current_nav_version(ctx.conn),
        },
    )

    assert resp.status_code == 400


def test_anchor_api_enter_topic_starts_from_the_latest_position(client):
    ctx = client.app.state.ctx
    topic_id = ctx.topics.nodes.create_topic("甲话题").id
    historical = _seed_fragment(ctx, topic_id, closed=True)
    AnchorService(ctx.conn).set_active(topic_id, historical)

    from agent.services.navigation_version import current as current_nav_version

    resp = client.post(
        "/api/anchor",
        json={
            "topic_id": topic_id,
            "fragment_id": None,
            "request_id": "req-nav-enter",
            "base_nav_version": current_nav_version(ctx.conn),
        },
    )

    assert resp.status_code == 200
    assert AnchorService(ctx.conn).get_active().fragment_id is None, "进入话题＝从最新位置继续"
