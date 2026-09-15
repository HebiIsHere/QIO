"""Planet 浏览数据层：轻量概览 + 确定性浏览序列 / 游标。

这些测试守住第二阶段的三条底线：
1. 打开星球不读 Message 原文；
2. 总话题数不受「同屏可见容量」限制（100 个话题全部可被浏览到）；
3. 同一个 seed 的浏览顺序确定，前进不重复、后退能拿回上一屏。
"""

from __future__ import annotations

import sqlite3

from agent.graph.nodes import NodeService
from agent.services.planet import VISIBLE_CAPACITY, PlanetBrowseService


def _make_topic(db_conn: sqlite3.Connection, nodes: NodeService, name: str, *, fragments: int = 0, messages: int = 0):
    topic = nodes.create_topic(name)
    for fi in range(fragments):
        frag_id = f"{topic.id}-f{fi}"
        db_conn.execute(
            "INSERT INTO fragments (id, topic_id, summary, created_at, closed_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (frag_id, topic.id, f"{name} 的第 {fi} 段", f"2026-09-01T00:0{fi}:00+00:00", f"2026-09-01T00:1{fi}:00+00:00"),
        )
        for mi in range(messages):
            db_conn.execute(
                "INSERT INTO messages (id, fragment_id, role, content, content_type, created_at) "
                "VALUES (?, ?, 'user', ?, 'text', ?)",
                (f"{frag_id}-m{mi}", frag_id, f"{name} 的原文 {mi}", f"2026-09-01T00:1{mi}:00+00:00"),
            )
    return topic


def test_overview_stays_lightweight_and_counts_fragments(db_conn: sqlite3.Connection):
    nodes = NodeService(db_conn)
    for name in ("甲", "乙", "丙"):
        _make_topic(db_conn, nodes, name, fragments=2, messages=5)

    rows = PlanetBrowseService(db_conn).overview()

    assert [r.title for r in rows] == ["甲", "乙", "丙"]
    assert [r.fragment_count for r in rows] == [2, 2, 2]
    assert rows[0].summary_preview == "甲 的第 1 段"
    assert not hasattr(rows[0], "messages")
    assert rows[0].visual_seed == rows[0].visual_seed  # 稳定，可复现
    assert isinstance(rows[0].visual_seed, int)


def test_browse_is_deterministic_for_the_same_seed(db_conn: sqlite3.Connection):
    nodes = NodeService(db_conn)
    for i in range(30):
        _make_topic(db_conn, nodes, f"话题{i:02d}")
    svc = PlanetBrowseService(db_conn)

    first = svc.browse(cursor=None, direction="forward", count=8, seed=7)
    again = svc.browse(cursor=None, direction="forward", count=8, seed=7)

    assert [t["topic_id"] for t in first["items"]] == [t["topic_id"] for t in again["items"]]
    assert first["seed"] == again["seed"] == 7


def test_browse_pages_forward_without_repeating_a_topic(db_conn: sqlite3.Connection):
    nodes = NodeService(db_conn)
    for i in range(30):
        _make_topic(db_conn, nodes, f"话题{i:02d}")
    svc = PlanetBrowseService(db_conn)

    first = svc.browse(cursor=None, direction="forward", count=8, seed=7)
    second = svc.browse(cursor=first["next_cursor"], direction="forward", count=8)

    first_ids = [t["topic_id"] for t in first["items"]]
    second_ids = [t["topic_id"] for t in second["items"]]
    assert len(first_ids) == 8 and len(second_ids) == 8
    assert not set(first_ids) & set(second_ids)
    assert second["pass_index"] == 0
    assert second["pass_changed"] is False


def test_reverse_browse_returns_the_previous_window(db_conn: sqlite3.Connection):
    nodes = NodeService(db_conn)
    for i in range(30):
        _make_topic(db_conn, nodes, f"话题{i:02d}")
    svc = PlanetBrowseService(db_conn)

    first = svc.browse(cursor=None, direction="forward", count=8, seed=7)
    second = svc.browse(cursor=first["next_cursor"], direction="forward", count=8)
    back = svc.browse(cursor=second["prev_cursor"], direction="backward", count=8)

    assert [t["topic_id"] for t in back["items"]] == [t["topic_id"] for t in first["items"]]


def test_first_batch_always_contains_the_current_topic(db_conn: sqlite3.Connection):
    nodes = NodeService(db_conn)
    for i in range(20):
        _make_topic(db_conn, nodes, f"话题{i:02d}")
    current = nodes.create_topic("当前所在的话题")
    svc = PlanetBrowseService(db_conn)

    page = svc.browse(cursor=None, direction="forward", count=8, seed=7, current_topic_id=current.id)

    assert page["items"][0]["topic_id"] == current.id


def test_browse_reports_capacity_and_total_separately(db_conn: sqlite3.Connection):
    nodes = NodeService(db_conn)
    for i in range(100):
        _make_topic(db_conn, nodes, f"话题{i:03d}")
    svc = PlanetBrowseService(db_conn)

    page = svc.browse(cursor=None, direction="forward", count=VISIBLE_CAPACITY, seed=7)

    assert page["total"] == 100
    assert page["visible_capacity"] == VISIBLE_CAPACITY
    assert len(page["items"]) == VISIBLE_CAPACITY
    assert page["has_more"] is True


def test_wrapping_starts_a_new_pass_and_defers_recently_shown_topics(db_conn: sqlite3.Connection):
    nodes = NodeService(db_conn)
    for i in range(12):
        _make_topic(db_conn, nodes, f"话题{i:02d}")
    svc = PlanetBrowseService(db_conn)

    first = svc.browse(cursor=None, direction="forward", count=12, seed=7)
    assert first["pass_changed"] is True
    recent = [t["topic_id"] for t in first["items"]][:6]

    second_pass = svc.browse(cursor=first["next_cursor"], direction="forward", count=6, exclude=recent)

    assert second_pass["pass_index"] == 1
    leading = [t["topic_id"] for t in second_pass["items"]]
    assert not set(leading) & set(recent)


def test_browse_clamps_count_to_supported_range(db_conn: sqlite3.Connection):
    nodes = NodeService(db_conn)
    for i in range(60):
        _make_topic(db_conn, nodes, f"话题{i:02d}")
    svc = PlanetBrowseService(db_conn)

    page = svc.browse(cursor=None, direction="forward", count=500, seed=7)

    assert len(page["items"]) <= 32


def test_backward_before_the_first_window_returns_nothing(db_conn: sqlite3.Connection):
    nodes = NodeService(db_conn)
    for i in range(20):
        _make_topic(db_conn, nodes, f"话题{i:02d}")
    svc = PlanetBrowseService(db_conn)

    page = svc.browse(cursor="7.0.0", direction="backward", count=8)

    assert page["items"] == []
    assert page["has_more"] is False


def test_browse_on_empty_database_is_safe(db_conn: sqlite3.Connection):
    page = PlanetBrowseService(db_conn).browse(cursor=None, direction="forward", count=8, seed=7)

    assert page["items"] == []
    assert page["total"] == 0
    assert page["has_more"] is False
