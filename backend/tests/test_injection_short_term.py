from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from agent.knowledge.inject import InjectionSource
from agent.selector.base import IndexedDoc
from agent.selector.selector import Selector
from agent.services.injection import (
    BudgetConfig,
    InjectionAssembler,
    InjectionBudget,
    PlannedItem,
)
from agent.graph.topics import TopicService
from agent.services.retrieval import Retriever


def _iso(days_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


from datetime import timedelta


def _seed(db_conn: sqlite3.Connection) -> None:
    now = _iso(0)
    for nid, ntype, name in (
        ("t_diet", "topic", "饮食偏好"),
        ("t_sql", "topic", "数据库设计"),
        ("user_root", "user", "用户"),
    ):
        db_conn.execute(
            "INSERT OR IGNORE INTO nodes (id, type, name, meta, created_at, updated_at) "
            "VALUES (?, ?, ?, '{}', ?, ?)",
            (nid, ntype, name, now, now),
        )
    for kid, category, content, node_ids in (
        ("k_diet", "general_fact", "用户偏好清淡饮食不吃辣", '["t_diet"]'),
        ("k_sql", "general_fact", "SQLite 迁移机制使用顺序脚本", '["t_sql"]'),
        ("k_user", "user_profile", "用户是开发者", '["user_root"]'),
    ):
        db_conn.execute(
            "INSERT OR IGNORE INTO knowledge (id, category, state, content, node_ids, created_at, updated_at, confidence) "
            "VALUES (?, ?, 'active', ?, ?, ?, ?, 0.9)",
            (kid, category, content, node_ids, now, now),
        )
    db_conn.execute(
        "INSERT OR IGNORE INTO fragments (id, topic_id, summary, created_at, closed_at) "
        "VALUES ('frag_sql', 't_sql', 'SQLite 表结构设计讨论', ?, ?)",
        (_iso(2), _iso(2)),
    )
    db_conn.execute(
        "INSERT OR IGNORE INTO memory_index (id, fragment_id, topic_id, entity_ids, keywords, title, token_estimate, created_at) "
        "VALUES ('idx_sql', 'frag_sql', 't_sql', '[]', '[\"sqlite\"]', 'SQLite 表结构', 10, ?)",
        (_iso(2),),
    )
    selector = Selector()
    selector.load(
        [IndexedDoc(doc_id="idx_sql", text="SQLite 表结构设计讨论", topic_id="t_sql", keywords=["sqlite"], created_at=_iso(2))],
        titles={"idx_sql": "SQLite 表结构"},
    )
    return Retriever(selector, TopicService(db_conn))


def _assembler(db_conn: sqlite3.Connection, context_window: int = 40_000) -> InjectionAssembler:
    budget = InjectionBudget(BudgetConfig(context_window=context_window, budget_ratio=0.25))
    retriever = _seed(db_conn)
    return InjectionAssembler(budget, retriever, knowledge_source=InjectionSource(db_conn))


def test_short_term_reserved_before_ranking(db_conn: sqlite3.Connection):
    asm = _assembler(db_conn, context_window=800)  # hard_cap=200
    short = [
        PlannedItem(
            source="memory",
            surface="topic_short",
            item_id="frag_open",
            text="[短期] 用户：今天吃了清淡的粤菜" * 20,
            tokens=150,
        )
    ]
    payload = asm.build("饮食", topic_id="t_diet", short_term=short)
    assert [i.item_id for i in payload.plan.short_term] == ["frag_open"]
    assert payload.plan.total_tokens >= 150
    assert "粤菜" in payload.text


def test_aux_topic_surface_injected(db_conn: sqlite3.Connection):
    asm = _assembler(db_conn)
    payload = asm.build("数据库设计", topic_id="t_diet", aux_topic_ids=["t_sql"])
    texts = payload.text
    assert "相关话题" in texts
    ids = {i.item_id for i in payload.plan.all_items}
    assert "k_sql" in ids
    assert "frag_sql" in ids


def test_new_topic_suggestion_block(db_conn: sqlite3.Connection):
    asm = _assembler(db_conn)
    payload = asm.build(
        "量子物理与弦理论",
        topic_id="t_diet",
        new_topic_candidate=True,
        new_topic_reason="消息与现有话题都不匹配",
    )
    assert "话题建议" in payload.text
    assert "量子物理" in payload.text or "新话题" in payload.text


def test_memory_retrieval_still_runs(db_conn: sqlite3.Connection):
    asm = _assembler(db_conn)
    payload = asm.build("sqlite 表结构", topic_id="t_sql")
    ids = {i.item_id for i in payload.plan.all_items}
    assert "idx_sql" in ids
