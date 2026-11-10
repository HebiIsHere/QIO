"""R14-C / F14：删除无用的 continue_from_history 组合入口。

已核实（本轮契约 §3.6）：continue_from_history 只有测试调用，生产 server.py /
continue_from_fragment 工具都走 register_continuation；删除后既有测试改为
register_continuation + apply_continuation 两阶段契约。

两阶段契约要证明的边界：
* 点击只登记，不建片段、不推动画；
* 落实才封存旧开放片段 / 建接续片段；
* 异常（陈旧版本 CAS 失败）零写入：不建片段、Anchor 不动、版本不推进；
* 旧组合入口彻底不存在。
"""

from __future__ import annotations

import pytest

from agent.graph.anchors import AnchorService
from agent.services import navigation_version
from agent.services.navigation import NavigationVersionConflict, TopicNavigationService


def _topic(conn, topic_id: str = "topic_f14", name: str = "话题") -> None:
    ts = "2026-11-10T00:00:00+00:00"
    conn.execute(
        "INSERT INTO nodes (id, type, name, meta, created_at, updated_at) "
        "VALUES (?, 'topic', ?, '{}', ?, ?)",
        (topic_id, name, ts, ts),
    )
    conn.commit()


def _fragment(conn, fragment_id: str, topic_id: str, *, closed: bool = True) -> None:
    ts = "2026-11-10T00:00:00+00:00"
    conn.execute(
        "INSERT INTO fragments (id, topic_id, summary_version, created_at, closed_at) "
        "VALUES (?, ?, 0, ?, ?)",
        (fragment_id, topic_id, ts, ts if closed else None),
    )
    conn.commit()


def _open_fragments(conn, topic_id: str) -> list[str]:
    return [
        row["id"]
        for row in conn.execute(
            "SELECT id FROM fragments WHERE topic_id = ? AND closed_at IS NULL",
            (topic_id,),
        )
    ]


def _fragment_count(conn) -> int:
    return conn.execute("SELECT COUNT(*) c FROM fragments").fetchone()["c"]


def test_combined_entry_is_deleted(db_conn):
    nav = TopicNavigationService(db_conn)
    assert not hasattr(nav, "continue_from_history")
    assert not hasattr(TopicNavigationService, "continue_from_history")


def test_two_phase_register_then_apply_creates_exactly_one_continuation(db_conn):
    _topic(db_conn)
    _fragment(db_conn, "frag_a", "topic_f14", closed=True)
    _fragment(db_conn, "frag_c", "topic_f14", closed=False)
    nav = TopicNavigationService(db_conn)
    base_version = navigation_version.current(db_conn)

    registered = nav.register_continuation("topic_f14", "frag_a")
    assert registered["changed"] is True
    assert registered["historic"] is True
    # 点击只登记：旧开放片段仍在，没有新片段。
    assert _open_fragments(db_conn, "topic_f14") == ["frag_c"]
    assert _fragment_count(db_conn) == 2
    version_after_register = navigation_version.current(db_conn)
    assert version_after_register == base_version + 1

    result = nav.apply_continuation("topic_f14", registered["intent_id"])
    assert result.created_fragment_id is not None
    assert result.sealed_fragment_id == "frag_c"
    assert result.source_fragment_id == "frag_a"
    assert _open_fragments(db_conn, "topic_f14") == [result.created_fragment_id]
    assert _fragment_count(db_conn) == 3
    row = db_conn.execute(
        "SELECT source_fragment_id, relation_type, boundary_reason FROM fragments "
        "WHERE id = ?",
        (result.created_fragment_id,),
    ).fetchone()
    assert row["source_fragment_id"] == "frag_a"
    assert row["relation_type"] == "history_reopen"
    assert row["boundary_reason"] == "history_continuation"


def test_stale_version_apply_leaves_no_registration_or_fragment_changes(db_conn):
    _topic(db_conn)
    _fragment(db_conn, "frag_a", "topic_f14", closed=True)
    nav = TopicNavigationService(db_conn)
    registered = nav.register_continuation("topic_f14", "frag_a")
    stale_expected = registered["nav_version"]

    # 用户后来真实导航：版本推进，旧期望版本失效。
    _topic(db_conn, "topic_other", "另一个话题")
    _fragment(db_conn, "frag_b", "topic_other", closed=False)
    nav.enter_topic("topic_other", fragment_id="frag_b")
    version_before = navigation_version.current(db_conn)
    anchor_before = AnchorService(db_conn).get_active()
    fragments_before = _fragment_count(db_conn)

    with pytest.raises(NavigationVersionConflict):
        nav.apply_continuation(
            "topic_f14", registered["intent_id"], expected_nav_version=stale_expected
        )

    assert _fragment_count(db_conn) == fragments_before, "CAS 失败不得建片段"
    assert AnchorService(db_conn).get_active() == anchor_before, "CAS 失败不得改 Anchor"
    assert navigation_version.current(db_conn) == version_before, "失败不得推进版本"
    # 登记仍是待落实（可重试），没有半截状态。
    assert db_conn.execute(
        "SELECT resolved_fragment_id FROM continuation_intents WHERE intent_id = ?",
        (registered["intent_id"],),
    ).fetchone()["resolved_fragment_id"] is None


def test_two_phase_open_fragment_is_plain_continue(db_conn):
    _topic(db_conn)
    _fragment(db_conn, "frag_open", "topic_f14", closed=False)
    nav = TopicNavigationService(db_conn)
    version_before = navigation_version.current(db_conn)

    registered = nav.register_continuation("topic_f14", "frag_open")
    assert registered["opens_current"] is True
    assert registered["intent_id"] is None
    assert registered["changed"] is False
    assert navigation_version.current(db_conn) == version_before

    result = nav.enter_topic("topic_f14", fragment_id="frag_open")
    assert result.created_fragment_id is None
    assert result.fragment_id == "frag_open"
    assert _fragment_count(db_conn) == 1
