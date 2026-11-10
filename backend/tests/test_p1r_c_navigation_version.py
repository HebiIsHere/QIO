"""P1R-C / P1-04：单调导航版本（CAS）的定向测试。

只证明程序事实：版本单调、CAS 原子、冲突结构化、迁移只追加且可补偿。
不涉及模型语义。
"""

from __future__ import annotations

import pytest

from agent.services import navigation_version as nv
from agent.storage.db import connect, transaction
from agent.storage.migrate import REQUIRED_OBJECTS
from agent.storage.schema import COMPENSATION_VERSION, MIGRATIONS, SCHEMA_VERSION


def _statements(version: int) -> list[str]:
    return [str(s) for target, stmts in MIGRATIONS if target == version for s in stmts]


# ── 迁移登记（只追加 + 可补偿）──────────────────────────────────────────


def test_navigation_version_table_is_registered_and_compensable() -> None:
    assert SCHEMA_VERSION == COMPENSATION_VERSION == max(target for target, _ in MIGRATIONS)
    assert "navigation_version" in REQUIRED_OBJECTS["tables"]
    compensation = " ".join(_statements(COMPENSATION_VERSION)).lower()
    assert "create table if not exists navigation_version" in compensation
    # 新迁移是独立的追加版本，不覆盖既有版本号。
    migration = _statements(77)
    assert migration and "navigation_version" in " ".join(migration).lower()


def test_migration_module_apply_is_idempotent(tmp_path) -> None:
    from agent.storage import migration_p1r_navigation as migration

    conn = connect(tmp_path / "nav-migration.db")
    try:
        first = migration.apply(conn)
        assert first, "首次应用必须真的执行建表"
        assert nv.current(conn) == 0
        assert migration.apply(conn) == [], "重复应用必须无操作（幂等）"
        assert nv.current(conn) == 0
    finally:
        conn.close()


# ── 版本读写 ────────────────────────────────────────────────────────────


def test_current_starts_at_zero_and_advance_records_source(db_conn) -> None:
    assert nv.current(db_conn) == 0
    first = nv.advance(
        db_conn, source="user_enter_topic", topic_id="topic-a", fragment_id="frag-1", turn_id="turn-1"
    )
    assert first == 1 == nv.current(db_conn)
    snapshot = nv.snapshot(db_conn)
    assert snapshot["version"] == 1
    assert snapshot["source"] == "user_enter_topic"
    assert snapshot["topic_id"] == "topic-a"
    assert snapshot["fragment_id"] == "frag-1"
    assert snapshot["turn_id"] == "turn-1"
    second = nv.advance(db_conn, source="user_enter_topic", topic_id="topic-b")
    assert second == 2 == nv.current(db_conn)
    assert nv.snapshot(db_conn)["topic_id"] == "topic-b"


def test_compare_and_advance_is_cas(db_conn) -> None:
    nv.advance(db_conn, source="base")  # → 1
    assert nv.compare_and_advance(db_conn, 1, source="tool") is True
    assert nv.current(db_conn) == 2
    # 同一个 expected 只能成功一次（重复确认/网络重放拿到 False，不会重复推进）。
    assert nv.compare_and_advance(db_conn, 1, source="tool") is False
    assert nv.current(db_conn) == 2
    assert nv.compare_and_advance(db_conn, 2, source="tool") is True
    assert nv.current(db_conn) == 3


def test_cas_or_advance_conflict_carries_structured_facts(db_conn) -> None:
    nv.advance(db_conn, source="user_enter_topic", topic_id="topic-a")  # → 1
    with pytest.raises(nv.NavigationVersionConflict) as caught:
        nv.cas_or_advance(db_conn, 0, source="switch_topic", topic_id="topic-b", turn_id="turn-1")
    conflict = caught.value
    assert conflict.code == "nav_version_conflict"
    assert conflict.status_code == 409
    assert conflict.expected == 0
    assert conflict.actual == 1
    assert conflict.source == "switch_topic"
    payload = conflict.as_dict()
    assert payload["code"] == "nav_version_conflict"
    assert payload["expected"] == 0 and payload["actual"] == 1
    assert payload["message"]
    # 冲突不推进版本（失败是失败，不是成功）。
    assert nv.current(db_conn) == 1
    # expected=None 表示「无绑定的新用户导航」：无条件推进。
    assert nv.cas_or_advance(db_conn, None, source="user_enter_topic") == 2
    # matches 是只读判断。
    assert nv.matches(db_conn, 2) is True
    assert nv.matches(db_conn, 1) is False
    assert nv.matches(db_conn, None) is True


def test_cas_failure_rolls_back_other_writes_in_same_transaction(db_conn) -> None:
    nv.advance(db_conn, source="user_enter_topic", topic_id="topic-a")  # → 1
    db_conn.execute("CREATE TABLE IF NOT EXISTS _nav_probe (v TEXT)")
    with pytest.raises(nv.NavigationVersionConflict):
        with transaction(db_conn):
            db_conn.execute("INSERT INTO _nav_probe (v) VALUES ('anchor-write')")
            nv.cas_or_advance(db_conn, 0, source="switch_topic")
    assert db_conn.execute("SELECT COUNT(*) FROM _nav_probe").fetchone()[0] == 0, (
        "CAS 失败必须让同一事务里的导航写入整体回滚"
    )
    assert nv.current(db_conn) == 1


def test_version_survives_reconnect(tmp_path) -> None:
    from agent.storage.migrate import apply_migrations

    path = tmp_path / "nav-persist.db"
    conn = connect(path)
    try:
        apply_migrations(conn)
        assert nv.advance(conn, source="user_enter_topic", topic_id="topic-a") == 1
    finally:
        conn.close()
    reopened = connect(path)
    try:
        assert nv.current(reopened) == 1, "版本必须持久化（刷新/重启不能回到旧值）"
        assert nv.advance(reopened, source="user_enter_topic", topic_id="topic-b") == 2
    finally:
        reopened.close()


def test_current_without_table_is_zero_not_crash(tmp_path) -> None:
    """未迁移的旧库：只读版本按 0（不假装推进）；推进仍会如实失败。"""
    conn = connect(tmp_path / "nav-unmigrated.db")
    try:
        assert nv.current(conn) == 0
        assert nv.matches(conn, 0) is True
        with pytest.raises(Exception):
            nv.advance(conn, source="user_enter_topic")
    finally:
        conn.close()
