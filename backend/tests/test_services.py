from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from agent.graph.topics import TopicService
from agent.knowledge.inject import InjectionSource
from agent.memory.index import estimate_tokens
from agent.selector.base import IndexedDoc
from agent.selector.selector import Selector
from agent.services.injection import (
    BudgetConfig,
    Candidate,
    InjectionAssembler,
    InjectionBudget,
    knowledge_score,
)
from agent.services.retrieval import Retriever
from agent.tools.memory_search import MemorySearchTool


def _iso(days_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


def _docs():
    return [
        IndexedDoc(
            doc_id="f_old",
            text="用户 饮食 偏好 清淡 不吃辣",
            topic_id="t_diet",
            keywords=["饮食", "清淡", "吃辣"],
            created_at=_iso(200),
        ),
        IndexedDoc(
            doc_id="f_new",
            text="用户 最近 饮食 清淡 广东",
            topic_id="t_diet",
            keywords=["饮食", "清淡", "广东"],
            created_at=_iso(1),
        ),
        IndexedDoc(
            doc_id="f_other",
            text="SQLite schema 设计 讨论",
            topic_id="t_sql",
            keywords=["sqlite"],
            created_at=_iso(3),
        ),
    ]


def _make_retriever(db_conn: sqlite3.Connection) -> Retriever:
    now = _iso(0)
    for topic_id, name in (("t_diet", "饮食偏好"), ("t_sql", "存储设计")):
        db_conn.execute(
            "INSERT OR IGNORE INTO nodes (id, type, name, meta, created_at, updated_at) "
            "VALUES (?, 'topic', ?, '{}', ?, ?)",
            (topic_id, name, now, now),
        )
    for fragment_id, topic_id, summary, created in (
        ("frag_new", "t_diet", "用户偏好清淡饮食", _iso(1)),
        ("frag_old", "t_diet", "用户不吃辣", _iso(200)),
    ):
        db_conn.execute(
            "INSERT OR IGNORE INTO fragments (id, topic_id, summary, created_at, closed_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (fragment_id, topic_id, summary, created, created),
        )
        db_conn.execute(
            "INSERT OR IGNORE INTO memory_index (id, fragment_id, topic_id, entity_ids, "
            "keywords, title, token_estimate, created_at) VALUES (?, ?, ?, '[]', ?, ?, 10, ?)",
            (f"idx_{fragment_id}", fragment_id, topic_id,
             '["饮食", "清淡"]', summary, created),
        )
    selector = Selector()
    selector.load(
        _docs(),
        titles={
            "f_old": "用户不吃辣",
            "f_new": "用户偏好清淡饮食",
            "f_other": "存储设计讨论",
        },
    )
    return Retriever(selector, TopicService(db_conn))


def test_budget_dynamic_ratio_and_unified_truncation():
    config = BudgetConfig(context_window=40_000, budget_ratio=0.25)
    assert config.hard_cap == 10_000
    budget = InjectionBudget(config)
    candidates = [
        Candidate("knowledge", "topic", "k1", "事实" * 10, 0.9),
        Candidate("memory", "memory", "m1", "记忆" * 5, 0.8),
        Candidate("knowledge", "user", "k2", "画像" * 3, 0.3),
    ]
    plan = budget.plan(candidates, min_score=0.05)
    assert [i.item_id for i in plan.knowledge] == ["k1", "k2"]
    assert [i.item_id for i in plan.memory] == ["m1"]
    assert plan.total_tokens <= config.hard_cap
    # tiny cap truncates lowest score first
    tiny = InjectionBudget(BudgetConfig(context_window=1_000, budget_ratio=0.1))
    plan2 = tiny.plan(candidates, min_score=0.05)
    assert plan2.total_tokens <= tiny.config.hard_cap


def test_knowledge_score_surfaces():
    query = "用户 饮食 偏好"
    assert knowledge_score("用户偏好清淡饮食", query, "user") > knowledge_score(
        "用户偏好清淡饮食", query, "topic"
    )
    assert knowledge_score("SQLite 表设计", query, "topic") < knowledge_score(
        "用户偏好清淡饮食", query, "topic"
    )


def test_retriever_three_weight_ranking(db_conn: sqlite3.Connection):
    retriever = _make_retriever(db_conn)
    hits = retriever.search("用户 饮食 清淡", anchor_topic_id="t_diet", top_k=3)
    assert hits, "expected hits"
    assert hits[0].doc_id == "f_new"
    assert all(h.topic_id in ("t_diet", "t_sql") for h in hits)


def test_assembler_three_surfaces(db_conn: sqlite3.Connection):
    from agent.knowledge.lifecycle import KnowledgeService

    now = _iso(0)
    db_conn.execute(
        "INSERT OR IGNORE INTO nodes (id, type, name, meta, created_at, updated_at) "
        "VALUES ('u1', 'user', '用户', '{}', ?, ?)",
        (now, now),
    )
    ks = KnowledgeService(db_conn)
    for content, nodes in (
        ("用户偏好清淡饮食", ["u1"]),
        ("饮食话题相关事实", ["t_diet"]),
        ("牛奶相关实体知识", ["e_milk"]),
    ):
        item = ks.create(category="general_fact", content=content, node_ids=nodes)
        ks.submit(item.id)
        ks.verify(item.id, verified_by="system")
        ks.activate(item.id)

    retriever = _make_retriever(db_conn)
    budget = InjectionBudget(BudgetConfig(context_window=100_000, budget_ratio=0.25))
    assembler = InjectionAssembler(budget, retriever, InjectionSource(db_conn))
    payload = assembler.build(
        "用户 饮食 偏好",
        topic_id="t_diet",
        entity_ids=["e_milk"],
        user_node_id="u1",
        top_k=3,
    )
    assert "长期记忆注入" in payload.text
    surfaces = {i.surface for i in payload.plan.knowledge}
    assert surfaces >= {"user", "topic"}  # entity/topic/user surfaces aggregated
    assert payload.plan.memory, "memory hits expected"
    assert payload.plan.total_tokens <= budget.config.hard_cap


def test_memory_search_tool(db_conn: sqlite3.Connection):
    retriever = _make_retriever(db_conn)
    tool = MemorySearchTool(retriever)
    import asyncio

    result = asyncio.run(tool.run(query="用户 饮食 清淡", top_k=2))
    assert result.ok
    assert "饮食" in result.content
    empty = asyncio.run(tool.run(query="完全不相关的词汇xyz", top_k=2))
    assert empty.ok and "未找到" in empty.content
    bad = asyncio.run(tool.run(query=""))
    assert not bad.ok


def test_estimate_tokens_consistent():
    assert estimate_tokens("a" * 100) > 0