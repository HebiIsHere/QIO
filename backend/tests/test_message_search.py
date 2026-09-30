"""已保存对话的原文检索：不依赖摘要，开放片段也查得到，并且能读回原文。

回归的真实缺口：`memory_search` 只检索 `memory_index`（封存后摘要成功的片段），
一条**还没有封存**的消息（摘要不存在、索引为零）就永远搜不到 —— 用户明明说过，
Agent 却说「未找到相关记忆」。
"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone

from agent.graph.topics import TopicService
from agent.selector.base import IndexedDoc
from agent.selector.selector import Selector
from agent.services.retrieval import Retriever
from agent.tools.memory_search import MemorySearchTool


def _iso(days_ago: float = 0) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


def _topic(conn: sqlite3.Connection, topic_id: str, name: str) -> None:
    now = _iso()
    conn.execute(
        "INSERT OR IGNORE INTO nodes (id, type, name, meta, created_at, updated_at) "
        "VALUES (?, 'topic', ?, '{}', ?, ?)",
        (topic_id, name, now, now),
    )
    conn.commit()


def _fragment(
    conn: sqlite3.Connection,
    fragment_id: str,
    topic_id: str,
    *,
    summary: str | None = None,
    closed: bool = False,
    created_at: str | None = None,
) -> None:
    at = created_at or _iso()
    conn.execute(
        "INSERT OR IGNORE INTO fragments (id, topic_id, summary, created_at, closed_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (fragment_id, topic_id, summary, at, at if closed else None),
    )
    conn.commit()


def _message(
    conn: sqlite3.Connection,
    message_id: str,
    fragment_id: str,
    content: str,
    *,
    role: str = "user",
    created_at: str | None = None,
) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO messages (id, fragment_id, role, content, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (message_id, fragment_id, role, content, created_at or _iso()),
    )
    conn.commit()


def _retriever(conn: sqlite3.Connection, docs: list[IndexedDoc] | None = None) -> Retriever:
    selector = Selector()
    docs = docs or []
    selector.load(
        docs,
        titles={d.doc_id: d.text[:20] for d in docs},
        token_estimates={d.doc_id: 10 for d in docs},
    )
    return Retriever(selector, TopicService(conn), conn=conn)


def test_open_fragment_message_is_searchable_and_returns_its_text(db_conn: sqlite3.Connection):
    """开放片段（没有摘要、没有索引）里的消息照样能搜到，并且给回原文。"""
    _topic(db_conn, "t_audit", "审计")
    _fragment(db_conn, "frag_open", "t_audit", created_at=_iso(1))
    _message(db_conn, "m1", "frag_open", "审计原文标记 青瓷松树", created_at=_iso(1))

    hits = _retriever(db_conn).search_messages("青瓷松树")

    assert [h.message_id for h in hits] == ["m1"]
    assert "青瓷松树" in hits[0].content
    assert hits[0].topic_id == "t_audit"
    assert hits[0].fragment_id == "frag_open"


def test_topic_id_is_a_real_filter(db_conn: sqlite3.Connection):
    """给了话题就是过滤，不是「偏向」：别的话题里同样命中也不算。"""
    _topic(db_conn, "t_a", "甲")
    _topic(db_conn, "t_b", "乙")
    _fragment(db_conn, "frag_a", "t_a", created_at=_iso(2))
    _fragment(db_conn, "frag_b", "t_b", created_at=_iso(1))
    _message(db_conn, "m_a", "frag_a", "青瓷松树 记在甲", created_at=_iso(2))
    _message(db_conn, "m_b", "frag_b", "青瓷松树 记在乙", created_at=_iso(1))

    retriever = _retriever(db_conn)

    assert [h.message_id for h in retriever.search_messages("青瓷松树")] == ["m_b", "m_a"]
    assert [h.message_id for h in retriever.search_messages("青瓷松树", topic_id="t_a")] == ["m_a"]


def test_deleted_message_disappears(db_conn: sqlite3.Connection):
    """删除要一致：消息没了，检索结果里也不能还留着它。"""
    _topic(db_conn, "t_audit", "审计")
    _fragment(db_conn, "frag_open", "t_audit")
    _message(db_conn, "m1", "frag_open", "青瓷松树")
    retriever = _retriever(db_conn)
    assert len(retriever.search_messages("青瓷松树")) == 1

    db_conn.execute("DELETE FROM messages WHERE id = 'm1'")
    db_conn.commit()

    assert retriever.search_messages("青瓷松树") == []


def test_tool_does_not_repeat_a_fragment_it_already_found_by_summary(db_conn: sqlite3.Connection):
    """同一片段已经由摘要命中，就不再重复列一次原文。"""
    _topic(db_conn, "t_audit", "审计")
    _fragment(db_conn, "frag_open", "t_audit", summary="青瓷松树 的审计记录", created_at=_iso(1))
    db_conn.execute(
        "INSERT OR IGNORE INTO memory_index (id, fragment_id, topic_id, entity_ids, keywords, "
        "title, token_estimate, created_at) VALUES ('idx_open', 'frag_open', 't_audit', '[]', "
        "'[\"青瓷松树\"]', '青瓷松树 的审计记录', 10, ?)",
        (_iso(1),),
    )
    _message(db_conn, "m1", "frag_open", "青瓷松树 的原文在此", created_at=_iso(1))
    docs = [
        IndexedDoc(
            doc_id="idx_open",
            text="青瓷松树 的审计记录",
            topic_id="t_audit",
            keywords=["青瓷松树"],
            created_at=_iso(1),
        )
    ]
    tool = MemorySearchTool(_retriever(db_conn, docs))

    result = asyncio.run(tool.run(query="青瓷松树"))

    assert result.ok
    assert "青瓷松树 的审计记录" in result.content
    assert "的原文在此" not in result.content


def test_tool_says_where_a_raw_hit_came_from(db_conn: sqlite3.Connection):
    """没有摘要可命中时，仍要把原文交给模型，并说明它不是摘要记忆。"""
    _topic(db_conn, "t_audit", "审计")
    _fragment(db_conn, "frag_open", "t_audit", created_at=_iso(1))
    _message(db_conn, "m1", "frag_open", "审计原文标记 青瓷松树", created_at=_iso(1))
    tool = MemorySearchTool(_retriever(db_conn))

    result = asyncio.run(tool.run(query="青瓷松树"))

    assert result.ok
    assert "青瓷松树" in result.content
    assert "保存的对话原文" in result.content
    assert "frag_open" in result.content


def test_tool_still_reports_nothing_found_when_there_really_is_nothing(db_conn: sqlite3.Connection):
    """真的没有就是没有：不能因为多了这条检索路径就编出结果。"""
    _topic(db_conn, "t_audit", "审计")
    _fragment(db_conn, "frag_open", "t_audit")
    _message(db_conn, "m1", "frag_open", "青瓷松树")
    tool = MemorySearchTool(_retriever(db_conn))

    result = asyncio.run(tool.run(query="完全无关的词 zzz"))

    assert result.ok
    assert "未找到" in result.content
