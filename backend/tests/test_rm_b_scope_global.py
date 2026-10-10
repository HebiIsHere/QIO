"""B 组 M02：知识范围归属（默认全局 / 指定话题 / 旧无归属 unresolved）。

用真实 `InjectionAssembler` + `InjectionSource` 验证「真正进入全局注入路径」，
不联网、不用真实模型。
"""

from __future__ import annotations

import sqlite3

import pytest

from agent.graph.nodes import NodeService
from agent.knowledge.inject import InjectionSource
from agent.knowledge.lifecycle import KnowledgeService
from agent.knowledge.scope import (
    is_injectable,
    is_unresolved,
    resolve_scope,
)
from agent.services.injection import BudgetConfig, InjectionAssembler, InjectionBudget


class _NoMemoryHits:
    def search(self, query: str, *, anchor_topic_id: str | None = None, top_k: int = 6):
        return []


def _assembler(conn: sqlite3.Connection) -> InjectionAssembler:
    budget = InjectionBudget(BudgetConfig(context_window=40_000, budget_ratio=0.25))
    return InjectionAssembler(budget, _NoMemoryHits(), knowledge_source=InjectionSource(conn))


def _active(
    ks: KnowledgeService,
    content: str,
    *,
    node_ids: list[str] | None = None,
    topic_id: str | None = None,
):
    item = ks.create(category="general_fact", content=content, node_ids=node_ids, topic_id=topic_id)
    ks.submit(item.id)
    ks.verify(item.id, verified_by="system")
    return ks.activate(item.id)


def _injected(conn: sqlite3.Connection, *, query: str, topic_id: str | None = None, user_node_id=None) -> str:
    payload = _assembler(conn).build(query, topic_id=topic_id, user_node_id=user_node_id)
    return payload.text


# -- M02：默认归属全局 --------------------------------------------------------


def test_default_create_attaches_to_user_global_node(db_conn):
    ks = KnowledgeService(db_conn)
    user_node_id = NodeService(db_conn).get_or_create_user_root().id

    item = _active(ks, "回答先给结论再给理由")

    assert item.node_ids == [user_node_id]
    assert item.scope_global is True
    assert item.scope_unresolved is False
    resolution = resolve_scope(db_conn, node_ids=item.node_ids, provenance=item.provenance)
    assert resolution.global_scope is True and resolution.unresolved is False


def test_default_form_knowledge_reaches_the_global_injection_path(db_conn):
    ks = KnowledgeService(db_conn)
    _active(ks, "用户偏好先讲逻辑再给代码")

    # 调用方不传 user_node_id：全局面必须自己解析出来（默认表单的真实路径）
    text = _injected(db_conn, query="怎么给我讲？", user_node_id=None)
    assert "用户偏好先讲逻辑再给代码" in text

    # 也不依赖 topic_id 兼容字段
    stored = db_conn.execute(
        "SELECT topic_id FROM knowledge WHERE content LIKE '%先讲逻辑%'"
    ).fetchone()
    assert stored["topic_id"] is None


def test_topic_scoped_knowledge_only_in_its_own_topic(db_conn):
    ks = KnowledgeService(db_conn)
    topic_a = NodeService(db_conn).create_topic("话题A").id
    topic_b = NodeService(db_conn).create_topic("话题B").id
    _active(ks, "只在话题A生效的结论", node_ids=[topic_a])

    assert "只在话题A生效的结论" in _injected(db_conn, query="结论", topic_id=topic_a)
    assert "只在话题A生效的结论" not in _injected(db_conn, query="结论", topic_id=topic_b)
    assert "只在话题A生效的结论" not in _injected(db_conn, query="结论", topic_id=None)


def test_scope_switch_keeps_one_entry_and_no_duplicate_injection(db_conn):
    ks = KnowledgeService(db_conn)
    topic = NodeService(db_conn).create_topic("切换话题").id
    item = _active(ks, "范围切换标记 SCOPE_SWITCH")

    # 指定话题：只在该话题生效（全局那一轮看不到）
    ks.set_scope(item.id, node_ids=[topic], topic_id=topic)
    topic_text = _injected(db_conn, query="切换", topic_id=topic)
    assert topic_text.count("范围切换标记 SCOPE_SWITCH") == 1
    assert "范围切换标记 SCOPE_SWITCH" not in _injected(db_conn, query="切换", topic_id=None)

    # 切回全局：任何一轮（含话题轮）都能看到，且每轮只出现一次
    user_node_id = NodeService(db_conn).get_or_create_user_root().id
    ks.set_scope(item.id, node_ids=[user_node_id], topic_id=None)
    assert "范围切换标记 SCOPE_SWITCH" in _injected(db_conn, query="切换", topic_id=None)
    global_text = _injected(db_conn, query="切换", topic_id=topic)
    assert global_text.count("范围切换标记 SCOPE_SWITCH") == 1

    # 同时挂在「你」和话题上：两个 surface 命中同一条 → 去重后仍只注入一次
    ks.set_scope(item.id, node_ids=[user_node_id, topic], topic_id=topic)
    both = _injected(db_conn, query="切换", topic_id=topic)
    assert both.count("范围切换标记 SCOPE_SWITCH") == 1

    # 条目没有丢、没有复制
    rows = db_conn.execute(
        "SELECT COUNT(*) AS n FROM knowledge WHERE content LIKE '%SCOPE_SWITCH%'"
    ).fetchone()["n"]
    assert rows == 1


# -- M02：旧无归属条目 unresolved（不是静默全局）-------------------------------


def _insert_legacy_active(conn: sqlite3.Connection, *, knowledge_id: str, content: str) -> None:
    conn.execute(
        "INSERT INTO knowledge (id, category, state, content, node_ids, created_at, updated_at, activated_at) "
        "VALUES (?, 'general_fact', 'active', ?, '[]', ?, ?, ?)",
        (knowledge_id, content, "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"),
    )


def test_legacy_unattributed_entry_is_unresolved_not_global(db_conn):
    _insert_legacy_active(db_conn, knowledge_id="kn_rm_b_legacy", content="旧无归属条目 LEGACY_NO_SCOPE")

    resolution = resolve_scope(db_conn, node_ids=[], provenance={})
    assert resolution.unresolved is True
    assert resolution.global_scope is False
    assert resolution.nodes == []
    assert is_unresolved(node_ids=[], provenance={}) is True

    # 仍然是 active（保守保留），但**不进入全局注入路径**
    assert db_conn.execute(
        "SELECT state FROM knowledge WHERE id = 'kn_rm_b_legacy'"
    ).fetchone()["state"] == "active"
    assert "LEGACY_NO_SCOPE" not in _injected(db_conn, query="旧条目", user_node_id=None)

    item = KnowledgeService(db_conn).get("kn_rm_b_legacy")
    assert item is not None and item.scope_unresolved is True
    assert is_injectable(db_conn, item) is False


def test_unresolved_entry_can_be_scoped_by_management_action(db_conn):
    _insert_legacy_active(db_conn, knowledge_id="kn_rm_b_legacy2", content="旧无归属条目 FIXABLE")
    ks = KnowledgeService(db_conn)
    topic = NodeService(db_conn).create_topic("用户手动归属").id

    updated = ks.set_scope("kn_rm_b_legacy2", node_ids=[topic], topic_id=topic)
    assert updated.scope_unresolved is False
    assert updated.node_ids == [topic]
    assert "旧无归属条目 FIXABLE" in _injected(db_conn, query="旧条目", topic_id=topic)
    assert "旧无归属条目 FIXABLE" not in _injected(db_conn, query="旧条目", user_node_id=None)


def test_legacy_topic_id_is_the_last_reliable_clue(db_conn):
    """只有 topic_id 的旧行（迁移 3 之前）按话题判定，不算 unresolved。"""
    topic = NodeService(db_conn).create_topic("旧话题线索").id
    db_conn.execute(
        "INSERT INTO knowledge (id, category, state, content, topic_id, node_ids, created_at, updated_at) "
        "VALUES ('kn_rm_b_topiconly', 'general_fact', 'active', '只有 topic_id 的旧行', ?, '[]', ?, ?)",
        (topic, "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"),
    )
    resolution = resolve_scope(db_conn, node_ids=[], topic_id=topic, provenance={})
    assert resolution.unresolved is False
    assert resolution.nodes == [topic]
    assert resolution.migrated is True


def test_unresolved_entries_are_not_duplicated_into_global_surface(db_conn):
    """混合场景：同一问答里既有全局条目、也有 unresolved 条目，互不串台。"""
    ks = KnowledgeService(db_conn)
    _active(ks, "全局条目 MIX_GLOBAL")
    _insert_legacy_active(db_conn, knowledge_id="kn_rm_b_mix", content="无归属条目 MIX_LEGACY")

    text = _injected(db_conn, query="混合", user_node_id=None)
    assert "全局条目 MIX_GLOBAL" in text
    assert "无归属条目 MIX_LEGACY" not in text
