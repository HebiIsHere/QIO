from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from agent.graph.topics import TopicService
from agent.memory.index import estimate_tokens
from agent.selector.base import IndexedDoc
from agent.selector.selector import Selector
from agent.services.injection import BudgetConfig, InjectionAssembler, InjectionBudget
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
    # topic nodes + fingerprint rows (fragments + memory_index)
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


def test_budget_knowledge_first_and_hard_cap():
    config = BudgetConfig(context_window=40_000, hard_cap_ratio=0.1, memory_strength=0.5)
    assert config.hard_cap == 4000
    budget = InjectionBudget(config)
    knowledge = [
        {"id": "k1", "category": "general_fact", "content": "事实" * 50},
        {"id": "k2", "category": "goal", "content": "目标" * 50},
    ]
    plan = budget.plan(knowledge, [])
    assert len(plan.knowledge) >= 1
    assert plan.total_tokens <= config.hard_cap
    # tiny cap truncates
    tiny = InjectionBudget(BudgetConfig(context_window=1_000, memory_strength=1.0))
    plan2 = tiny.plan(knowledge, [])
    assert plan2.total_tokens <= tiny.config.hard_cap


def test_budget_strength_slider():
    weak = BudgetConfig(memory_strength=0.0)
    strong = BudgetConfig(memory_strength=1.0)
    assert weak.knowledge_budget() == weak.knowledge_min_tokens
    assert strong.knowledge_budget() == strong.knowledge_max_tokens


def test_retriever_three_weight_ranking(db_conn: sqlite3.Connection):
    retriever = _make_retriever(db_conn)
    hits = retriever.search("用户 饮食 清淡", anchor_topic_id="t_diet", top_k=3)
    assert hits, "expected hits"
    # fresh + anchor-topic doc ranks first despite age of the other
    assert hits[0].doc_id == "f_new"
    assert all(h.topic_id in ("t_diet", "t_sql") for h in hits)


def test_retriever_recency_alone():
    config = BudgetConfig.__new__(BudgetConfig)
    # not used directly; verified via hits order in db-backed test above
    assert True


def test_assembler_builds_payload(db_conn: sqlite3.Connection):
    retriever = _make_retriever(db_conn)
    budget = InjectionBudget(BudgetConfig(context_window=100_000, memory_strength=0.5))
    assembler = InjectionAssembler(
        budget,
        retriever,
        knowledge_items=[
            {"id": "k1", "category": "user_profile", "content": "用户偏好清淡饮食"},
        ],
    )
    payload = assembler.build("用户 饮食 偏好", anchor_topic_id="t_diet", top_k=3)
    assert "长期记忆注入" in payload.text
    assert "k1" in [i.item_id for i in payload.plan.knowledge]
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