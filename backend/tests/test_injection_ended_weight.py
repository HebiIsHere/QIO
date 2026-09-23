from __future__ import annotations

import sqlite3

from agent.graph.nodes import NodeService
from agent.knowledge.inject import InjectionSource
from agent.knowledge.lifecycle import KnowledgeService
from agent.services.injection import BudgetConfig, InjectionAssembler, InjectionBudget


class _NoMemoryHits:
    """只测知识面：检索没有命中，避免记忆片段干扰断言。"""

    def search(self, query: str, *, anchor_topic_id: str | None = None, top_k: int = 6):
        return []


def _assembler(conn: sqlite3.Connection) -> InjectionAssembler:
    budget = InjectionBudget(BudgetConfig(context_window=40_000, budget_ratio=0.25))
    return InjectionAssembler(budget, _NoMemoryHits(), knowledge_source=InjectionSource(conn))


def _write(conn: sqlite3.Connection, *, content: str, user_node_id: str, ended: bool) -> str:
    ks = KnowledgeService(conn)
    item = ks.create(
        category="user_profile",
        content=content,
        node_ids=[user_node_id],
        provenance={"source": "onboarding"},
    )
    ks.submit(item.id)
    ks.verify(item.id, verified_by="user")
    ks.activate(item.id)
    if ended:
        ks.mark_ended(item.id, reason="user_confirmed")
    return item.id


def test_ended_knowledge_is_dropped_when_unrelated(db_conn: sqlite3.Connection):
    user_node_id = NodeService(db_conn).get_or_create_user_root().id
    active_id = _write(db_conn, content="用户偏好清淡饮食", user_node_id=user_node_id, ended=False)
    ended_id = _write(db_conn, content="用户最近在开发 QIO", user_node_id=user_node_id, ended=True)

    payload = _assembler(db_conn).build("今天吃点什么", user_node_id=user_node_id)
    injected = [item.item_id for item in payload.plan.knowledge]
    assert active_id in injected
    assert ended_id not in injected


def test_ended_knowledge_ranks_below_active_when_related(db_conn: sqlite3.Connection):
    user_node_id = NodeService(db_conn).get_or_create_user_root().id
    active_id = _write(db_conn, content="用户偏好清淡饮食", user_node_id=user_node_id, ended=False)
    ended_id = _write(db_conn, content="用户最近在开发 QIO 的记忆问题", user_node_id=user_node_id, ended=True)

    payload = _assembler(db_conn).build("QIO 的记忆问题怎么继续", user_node_id=user_node_id)
    order = [item.item_id for item in payload.plan.knowledge]
    assert ended_id in order, "相关内容仍应能被参考到"
    assert order.index(active_id) < order.index(ended_id)
