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


# -- Focus 质量：必须让模型看到「这段历史最后讨论到哪、形成了什么结论」 ----


def _seed_history(ctx: AppContext, topic_id: str, contents: list[tuple[str, str]], *, closed: bool, title: str = "历史片段", summary: str | None = None) -> str:
    from agent.memory.fragment import new_id

    frag_id = new_id("frag")
    now = _now()
    ctx.conn.execute(
        "INSERT INTO fragments (id, topic_id, summary, summary_version, created_at, closed_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (frag_id, topic_id, summary if summary is not None else f"{title} 的摘要", 1, now, now if closed else None),
    )
    for i, (role, content) in enumerate(contents):
        ctx.conn.execute(
            "INSERT INTO messages (id, fragment_id, role, content, content_type, created_at) "
            "VALUES (?, ?, ?, ?, 'text', ?)",
            (f"{frag_id}_m{i}", frag_id, role, content, now),
        )
    ctx.conn.execute(
        "UPDATE fragments SET start_message_id = ?, end_message_id = ? WHERE id = ?",
        (f"{frag_id}_m0", f"{frag_id}_m{len(contents) - 1}", frag_id),
    )
    if closed:
        ctx.conn.execute(
            "INSERT INTO memory_index (id, fragment_id, topic_id, entity_ids, keywords, "
            "title, token_estimate, created_at) VALUES (?, ?, ?, '[]', '[]', ?, 10, ?)",
            (new_id("idx"), frag_id, topic_id, title, now),
        )
    ctx.conn.commit()
    return frag_id


def test_focus_block_keeps_tail_decision(ctx: AppContext):
    """12 条消息的发展过程：开头与结论都要在，中间省略。"""
    topic = ctx.topics.nodes.create_topic("长片段话题").id
    contents = [("user", f"第 {i} 轮讨论") for i in range(10)]
    contents[0] = ("user", "开场：我们在讨论缓存方案")
    contents[10 - 1] = ("user", "最终决定：采用内存 + 定期落盘")
    contents += [
        ("assistant", "记录：理由是延迟低"),
        ("user", "未解决问题：冷启动回放还没做"),
    ]
    frag = _seed_history(ctx, topic, contents, closed=True)

    block = ctx._focus_block(topic, frag)
    assert "开场" in block, "开头背景不能丢"
    assert "最终决定" in block, "结尾的最终结论必须进入 Focus"
    assert "未解决问题" in block, "用户最后的问题必须进入 Focus"
    assert "省略" in block, "中间内容被省略时要有明确标记"


def test_focus_block_single_message(ctx: AppContext):
    topic = ctx.topics.nodes.create_topic("单条话题").id
    frag = _seed_history(ctx, topic, [("user", "只有一条消息")], closed=True)
    block = ctx._focus_block(topic, frag)
    assert "只有一条消息" in block
    assert "省略" not in block


def test_focus_block_short_fragment_has_no_ellipsis(ctx: AppContext):
    topic = ctx.topics.nodes.create_topic("短片段话题").id
    frag = _seed_history(
        ctx, topic, [("user", "甲"), ("assistant", "乙"), ("user", "丙")], closed=True
    )
    block = ctx._focus_block(topic, frag)
    for text in ("甲", "乙", "丙"):
        assert text in block
    assert "省略" not in block


def test_focus_block_open_fragment_without_summary_is_not_empty(ctx: AppContext):
    """开放片段没有 summary 时也必须给出可用内容。"""
    topic = ctx.topics.nodes.create_topic("开放片段话题").id
    frag = _seed_history(
        ctx,
        topic,
        [("user", f"开放消息 {i}") for i in range(5)],
        closed=False,
        summary="",
    )
    block = ctx._focus_block(topic, frag)
    assert "开放消息 0" in block and "开放消息 4" in block
    assert len(block) > 40


def test_focus_block_summary_failure_degrades_to_messages(ctx: AppContext):
    """摘要生成失败（空摘要）时不能给出几乎空的 Focus。"""
    topic = ctx.topics.nodes.create_topic("摘要失败话题").id
    frag = _seed_history(
        ctx, topic, [("user", "第一条"), ("assistant", "最后一条结论")], closed=True, summary=""
    )
    block = ctx._focus_block(topic, frag)
    assert "第一条" in block and "最后一条结论" in block


def test_focus_block_respects_token_cap(ctx: AppContext):
    """长文本片段不得把 Focus 撑成全文注入。"""
    from agent.memory.index import estimate_tokens
    from agent.services.params import FOCUS

    topic = ctx.topics.nodes.create_topic("超长片段话题").id
    long_text = "长文本" * 3000
    frag = _seed_history(
        ctx,
        topic,
        [("user", f"{i}:{long_text}") for i in range(12)],
        closed=True,
        summary=long_text,
    )
    block = ctx._focus_block(topic, frag)
    assert estimate_tokens(block) <= FOCUS.max_tokens


def test_focus_block_cross_topic_and_missing(ctx: AppContext):
    t1 = ctx.topics.nodes.create_topic("话题甲").id
    t2 = ctx.topics.nodes.create_topic("话题乙").id
    frag = _seed_history(ctx, t1, [("user", "内容")], closed=True)
    assert ctx._focus_block(t2, frag) == ""
    assert ctx._focus_block(t1, "frag_missing") == ""


# -- Focus 与普通检索去重 -------------------------------------------------


def test_focus_and_retrieval_do_not_duplicate(ctx: AppContext):
    """Focus 已经给了 A13，检索又搜到 A13 时只能注入一份。"""
    from agent.services.injection import InjectionAssembler, InjectionBudget, BudgetConfig
    from agent.services.retrieval import Retriever
    from agent.selector.selector import Selector

    topic = ctx.topics.nodes.create_topic("去重话题").id
    frag = _seed_history(
        ctx,
        topic,
        [("user", "MESSAGE-PAYLOAD-XYZ 用户问：为什么 anchor 不等于 latest fragment")],
        closed=True,
        title="Anchor 生命周期",
        summary="SUMMARY-PAYLOAD-XYZ：anchor 表示用户明确选择的历史位置",
    )
    ctx._refresh_selector()
    block = ctx._focus_block(topic, frag)
    assert "SUMMARY-PAYLOAD-XYZ" in block

    budget = InjectionBudget(BudgetConfig(context_window=40_000, budget_ratio=0.25))
    asm = InjectionAssembler(budget, ctx.retriever, knowledge_source=InjectionSource(ctx.conn))

    # 1) 不加 Focus 时，检索通路本身能命中这个片段
    baseline = asm.build("anchor 生命周期 为什么", topic_id=topic)
    assert "SUMMARY-PAYLOAD-XYZ" in baseline.text
    assert any("idx" in i.item_id or i.item_id for i in baseline.plan.memory)

    # 2) 同一片段既被 Focus 选中，又被检索命中 → 只能出现一次
    payload = asm.build(
        "anchor 生命周期 为什么",
        topic_id=topic,
        focus_block=block,
        focus_item_id=frag,
    )
    assert payload.text.count("SUMMARY-PAYLOAD-XYZ") == 1, "同一片段不得重复注入"
    assert payload.text.count("MESSAGE-PAYLOAD-XYZ") == 1, "Focus 正文同样只能出现一次"
    # 去重发生在预算之前：计划里不应留下重复项
    ids = [i.item_id for i in payload.plan.all_items]
    assert ids.count(frag) <= 1
