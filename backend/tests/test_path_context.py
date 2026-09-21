"""历史关系与路径上下文（阶段 3）。

核心验收：A→B→C 与 A→D 并存时，D 的上下文**不能**把 B/C 的决定当成前提
（那会让「从更早的地方重开一条路」被另一条路的结论污染）；B/C 若出现，
必须带「仅参考」的明确标注。
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from agent.api.bus import EventBus
from agent.config import Settings
from agent.credentials.store import MemoryKeyring
from agent.services.app import AppContext
from agent.storage.db import connect
from agent.storage.migrate import apply_migrations


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@pytest.fixture()
def ctx(tmp_path) -> AppContext:
    conn = connect(tmp_path / "path.db")
    apply_migrations(conn)
    app_ctx = AppContext(Settings(data_dir=tmp_path), conn, EventBus())
    app_ctx.credentials._kr = MemoryKeyring()
    return app_ctx


def _topic(ctx: AppContext, name: str = "话题") -> str:
    return ctx.topics.nodes.create_topic(name).id


def _fragment(
    ctx: AppContext,
    topic_id: str,
    fragment_id: str,
    *,
    summary: str | None = None,
    closed: bool = True,
    source: str | None = None,
    relation: str = "normal",
    message: str | None = None,
) -> str:
    ctx.conn.execute(
        "INSERT INTO fragments "
        "(id, topic_id, summary, summary_version, created_at, closed_at, meta, "
        " source_fragment_id, relation_type, content_version) "
        "VALUES (?, ?, ?, ?, ?, ?, '{}', ?, ?, 1)",
        (
            fragment_id,
            topic_id,
            summary,
            1 if summary else 0,
            _now(),
            _now() if closed else None,
            source,
            relation,
        ),
    )
    if summary:
        ctx.conn.execute(
            "INSERT INTO memory_index (id, fragment_id, topic_id, entity_ids, keywords, title, "
            "token_estimate, created_at) VALUES (?, ?, ?, '[]', '[]', ?, 10, ?)",
            (f"idx_{fragment_id}", fragment_id, topic_id, f"{fragment_id} 的标题", _now()),
        )
    if message:
        ctx.conn.execute(
            "INSERT INTO messages (id, fragment_id, role, content, content_type, created_at) "
            "VALUES (?, ?, 'user', ?, 'text', ?)",
            (f"msg_{fragment_id}", fragment_id, message, _now()),
        )
    ctx.conn.commit()
    return fragment_id


def _text_of(items: list) -> str:
    return "\n".join(item.text for item in items)


def test_path_premises_follow_the_chain_not_the_topic(ctx: AppContext):
    topic = _topic(ctx)
    _fragment(ctx, topic, "frag_a", summary="A：最初的前提")
    _fragment(ctx, topic, "frag_b", summary="B：沿着 A 做的决定", source="frag_a")
    _fragment(ctx, topic, "frag_c", summary="C：沿着 B 做的决定", source="frag_b")
    _fragment(ctx, topic, "frag_d", summary=None, closed=False, source="frag_a", relation="history_reopen")

    items = ctx.context_assembler.short_term_items(topic, fragment_id="frag_d")
    text = _text_of(items)

    # 直接来源是 A：它必须以「路径前提」的身份出现
    assert "【路径前提·直接来源】" in text
    assert "A：最初的前提" in text
    # B/C 不在 D 的路径上：不能作为前提出现
    premise_lines = [
        line for line in text.splitlines() if line.startswith("【路径前提")
    ]
    assert not any("B：" in line or "C：" in line for line in premise_lines)
    # 若 B/C 作为同话题背景出现，必须带「仅参考」标注
    for fragment_id in ("frag_b", "frag_c"):
        for item in items:
            if item.item_id == fragment_id:
                assert "仅参考" in item.text, f"{fragment_id} 以参考身份出现时必须标注"


def test_path_premises_include_older_ancestors(ctx: AppContext):
    topic = _topic(ctx)
    _fragment(ctx, topic, "frag_a", summary="A：第一层")
    _fragment(ctx, topic, "frag_b", summary="B：第二层", source="frag_a")
    _fragment(ctx, topic, "frag_c", summary=None, closed=False, source="frag_b")

    items = ctx.context_assembler.short_term_items(topic, fragment_id="frag_c")
    text = _text_of(items)

    assert "【路径前提·直接来源】" in text and "B：第二层" in text
    assert "【路径前提·更早的来源（第 2 层）】" in text and "A：第一层" in text


def test_sealed_fragment_without_summary_falls_back_to_raw_text(ctx: AppContext):
    """阶段 2 之后「已封存但还没摘要」是正常中间态：不能用「没有摘要」当「没有内容」。"""
    topic = _topic(ctx)
    _fragment(ctx, topic, "frag_a", summary=None, message="A 的原文结尾：我们决定先做滚动")
    _fragment(ctx, topic, "frag_b", summary=None, closed=False, source="frag_a")

    items = ctx.context_assembler.short_term_items(topic, fragment_id="frag_b")
    text = _text_of(items)

    assert "还没有摘要" in text
    assert "A 的原文结尾：我们决定先做滚动" in text


def test_ancestor_chain_has_depth_cap_and_cycle_protection(ctx: AppContext):
    topic = _topic(ctx)
    _fragment(ctx, topic, "f1", summary="1")
    _fragment(ctx, topic, "f2", summary="2", source="f1")
    _fragment(ctx, topic, "f3", summary="3", source="f2")
    _fragment(ctx, topic, "f4", summary="4", source="f3")
    _fragment(ctx, topic, "f5", summary="5", source="f4")

    capped = ctx.fragments.ancestors("f5", max_depth=3)
    assert [fid for fid, _ in capped] == ["f4", "f3", "f2"], "深度上限必须生效"

    # 坏数据：环。必须停下来，而且要如实报告不需要保护（这里断言不会死循环）
    ctx.conn.execute("UPDATE fragments SET source_fragment_id = 'f5' WHERE id = 'f1'")
    ctx.conn.commit()
    looped = ctx.fragments.ancestors("f5", max_depth=10)
    ids = [fid for fid, _ in looped]
    assert len(ids) == len(set(ids)), "祖先链不能重复同一条片段"
    assert "f5" not in ids, "自己不能出现在自己的祖先链里"


def test_validate_source_rejects_cross_topic_and_cycles(ctx: AppContext):
    topic_a = _topic(ctx, "话题A")
    topic_b = _topic(ctx, "话题B")
    _fragment(ctx, topic_a, "frag_a", summary="A")
    _fragment(ctx, topic_b, "frag_b", summary="B")

    with pytest.raises(ValueError):
        ctx.fragments.validate_source(topic_a, "frag_ghost")
    with pytest.raises(ValueError):
        ctx.fragments.validate_source(topic_a, "frag_b")  # 跨话题

    # create_child 只接受「存在、同话题」的来源
    with pytest.raises(ValueError):
        ctx.fragments.create_child(topic_a, source_fragment_id="frag_ghost")
    with pytest.raises(ValueError):
        ctx.fragments.create_child(topic_a, source_fragment_id="frag_b")  # 跨话题

    child = ctx.fragments.create_child(topic_a, source_fragment_id="frag_a")
    assert ctx.fragments.source_of(child) == "frag_a"

    # 环的判定（用于「改挂来源」这类操作）：
    # - 自指成环；- 挂到自己的后代下面成环；- 无关片段不成环
    assert ctx.fragments.would_cycle("frag_a", "frag_a") is True
    assert ctx.fragments.would_cycle("frag_a", child) is True
    assert ctx.fragments.would_cycle("frag_a", "frag_b") is False


def test_create_child_records_relation_and_reason(ctx: AppContext):
    topic = _topic(ctx)
    _fragment(ctx, topic, "frag_a", summary="A")

    child = ctx.fragments.create_child(
        topic,
        source_fragment_id="frag_a",
        relation_type="history_reopen",
        boundary_reason="history_continuation",
        same_stage=False,
    )

    row = ctx.conn.execute(
        "SELECT source_fragment_id, relation_type, boundary_reason, same_stage FROM fragments "
        "WHERE id = ?",
        (child,),
    ).fetchone()
    assert row["source_fragment_id"] == "frag_a"
    assert row["relation_type"] == "history_reopen"
    assert row["boundary_reason"] == "history_continuation"
    assert row["same_stage"] == 0


def test_path_items_are_deduplicated(ctx: AppContext):
    topic = _topic(ctx)
    _fragment(ctx, topic, "frag_a", summary="A")
    _fragment(ctx, topic, "frag_b", summary=None, closed=False, source="frag_a")

    items = ctx.context_assembler.short_term_items(topic, fragment_id="frag_b")
    ids = [item.item_id for item in items if item.item_id]

    assert len(ids) == len(set(ids)), f"同一片段不能注入两次：{ids}"


def test_other_topic_knowledge_is_labelled_as_reference_only(ctx: AppContext):
    """其他话题的知识不能伪装成本轮的结论（阶段 3 的来源与适用性）。"""
    from agent.knowledge.lifecycle import KnowledgeService

    topic = _topic(ctx, "当前话题")
    other = _topic(ctx, "另一个话题")
    item = KnowledgeService(ctx.conn).create(
        category="goal",
        content="另一个话题里的决定：先用方案 B 落地",
        topic_id=other,
    )
    # 只有 active 的知识会进注入；这里直接把 fixture 置为生效状态
    ctx.conn.execute("UPDATE knowledge SET state = 'active' WHERE id = ?", (item.id,))
    ctx.conn.commit()

    # 查询与那条知识有词面重叠，确保它真的被选中（否则测的是「没命中」而不是标签）
    payload = ctx.build_injection(
        "另一个话题里的决定：先用方案 B 落地，下一步怎么做",
        topic_id=topic,
        aux_topic_ids=[other],
    )

    assert "仅参考" in payload.text
    assert "不代表本轮已接受的结论" in payload.text
