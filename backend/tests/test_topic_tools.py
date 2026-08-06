from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from agent.graph.anchors import AnchorService
from agent.tools.topic_tools import CreateTopicTool, SwitchTopicTool


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _seed_topics(db_conn: sqlite3.Connection) -> tuple[str, str]:
    now = _now()
    db_conn.execute(
        "INSERT INTO nodes (id, type, name, meta, created_at, updated_at) "
        "VALUES ('t_a', 'topic', '话题A', '{}', ?, ?)",
        (now, now),
    )
    db_conn.execute(
        "INSERT INTO nodes (id, type, name, meta, created_at, updated_at) "
        "VALUES ('t_b', 'topic', '话题B', '{}', ?, ?)",
        (now, now),
    )
    return "t_a", "t_b"


def _related_weights(db_conn: sqlite3.Connection, src: str, dst: str) -> float | None:
    row = db_conn.execute(
        "SELECT weight FROM edges WHERE src = ? AND dst = ? AND type = 'related'",
        (src, dst),
    ).fetchone()
    return row["weight"] if row else None


def test_switch_topic_updates_anchor_and_creates_edge(db_conn: sqlite3.Connection):
    t_a, t_b = _seed_topics(db_conn)
    anchors = AnchorService(db_conn)
    anchors.set_active(t_a)
    tool = SwitchTopicTool(db_conn)
    result = tool.run_sync(topic_id=t_b, reason="用户开始聊数据库")
    assert result.ok
    assert anchors.get_active().topic_id == t_b
    assert _related_weights(db_conn, t_a, t_b) == 1.0
    # 再次切换会累加权重
    tool.run_sync(topic_id=t_a, reason="回到话题A")
    assert _related_weights(db_conn, t_a, t_b) == 2.0


def test_switch_topic_rejects_unknown(db_conn: sqlite3.Connection):
    tool = SwitchTopicTool(db_conn)
    result = tool.run_sync(topic_id="t_missing", reason="x")
    assert result.ok is False
    assert "t_missing" in (result.error or "")


def test_create_topic_makes_node_anchor_edge(db_conn: sqlite3.Connection):
    t_a, _ = _seed_topics(db_conn)
    anchors = AnchorService(db_conn)
    anchors.set_active(t_a)
    tool = CreateTopicTool(db_conn)
    result = tool.run_sync(name="量子物理", reason="新话题")
    assert result.ok
    node = db_conn.execute(
        "SELECT id, type, name FROM nodes WHERE type = 'topic' AND name = '量子物理'"
    ).fetchone()
    assert node is not None
    assert anchors.get_active().topic_id == node["id"]
    assert _related_weights(db_conn, t_a, node["id"]) == 1.0


def test_create_topic_without_anchor(db_conn: sqlite3.Connection):
    tool = CreateTopicTool(db_conn)
    result = tool.run_sync(name="初始话题", reason="无锚点")
    assert result.ok
    node = db_conn.execute(
        "SELECT id FROM nodes WHERE type = 'topic' AND name = '初始话题'"
    ).fetchone()
    assert node is not None
    assert AnchorService(db_conn).get_active().topic_id == node["id"]
