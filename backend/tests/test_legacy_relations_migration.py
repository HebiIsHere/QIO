"""迁移 16：旧数据的关系、内容版本与缺摘要补齐（阶段 3）。

原则是「不猜、不丢」：
* 能确认的旧来源关系保留（迁移 11 起只有「从历史继续」会写 source_fragment_id）；
* 不能确认的普通延续标成 unknown；
* 指向不存在片段 / 别的话题的坏关系断开，但不删任何消息或摘要；
* 缺摘要的旧封存片段登记一次幂等补齐任务（迁移本身不调用模型）。
"""

from __future__ import annotations

import sqlite3

from agent.storage.db import connect
from agent.storage.migrate import apply_migrations
from agent.storage.schema import MIGRATIONS

LEGACY_VERSION = 15  # 关系列在 14 加入、派生任务表在 15 加入；16 是本轮要验的迁移


def _apply_until(conn: sqlite3.Connection, limit: int) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_version ("
        " id INTEGER PRIMARY KEY, version INTEGER NOT NULL, applied_at TEXT NOT NULL)"
    )
    for target, statements in MIGRATIONS:
        if target > limit:
            break
        for stmt in statements:
            conn.execute(stmt)
        conn.execute(
            "INSERT INTO schema_version (version, applied_at) VALUES (?, '2026-01-01T00:00:00+00:00')",
            (target,),
        )


def _topic(conn: sqlite3.Connection, topic_id: str, name: str) -> None:
    conn.execute(
        "INSERT INTO nodes (id, type, name, meta, created_at, updated_at) "
        "VALUES (?, 'topic', ?, '{}', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')",
        (topic_id, name),
    )


def _legacy_fragment(
    conn: sqlite3.Connection,
    fragment_id: str,
    topic_id: str,
    *,
    source: str | None = None,
    closed: bool = True,
    summary: str | None = None,
    messages: int = 0,
) -> None:
    conn.execute(
        "INSERT INTO fragments (id, topic_id, summary, summary_version, created_at, closed_at, "
        " meta, source_fragment_id) VALUES (?, ?, ?, ?, ?, ?, '{}', ?)",
        (
            fragment_id,
            topic_id,
            summary,
            1 if summary else 0,
            "2026-01-01T00:00:00+00:00",
            "2026-01-01T00:00:00+00:00" if closed else None,
            source,
        ),
    )
    for i in range(messages):
        conn.execute(
            "INSERT INTO messages (id, fragment_id, role, content, content_type, created_at) "
            "VALUES (?, ?, 'user', ?, 'text', '2026-01-01T00:00:00+00:00')",
            (f"{fragment_id}_m{i}", fragment_id, f"{fragment_id} 的第 {i} 条"),
        )


def test_upgrade_backfills_relations_versions_and_summary_tasks(tmp_path):
    conn = connect(tmp_path / "legacy.db")
    _apply_until(conn, LEGACY_VERSION)
    _topic(conn, "t1", "话题一")
    _topic(conn, "t2", "话题二")

    # 可确认的历史接续关系
    _legacy_fragment(conn, "frag_base", "t1", summary="基础片段", messages=2)
    _legacy_fragment(conn, "frag_reopen", "t1", source="frag_base", summary="接续片段", messages=3)
    # 普通延续（分不清容量还是阶段）→ unknown
    _legacy_fragment(conn, "frag_plain", "t1", summary="普通延续", messages=1)
    # 坏关系一：来源不存在
    _legacy_fragment(conn, "frag_ghost_src", "t1", source="frag_missing", messages=1)
    # 坏关系二：来源在另一个话题
    _legacy_fragment(conn, "frag_other_topic", "t2", messages=1)
    _legacy_fragment(conn, "frag_cross", "t1", source="frag_other_topic", messages=1)
    # 缺摘要的旧封存片段：应当被登记补齐任务
    _legacy_fragment(conn, "frag_nosummary", "t1", messages=4)
    conn.commit()

    version = apply_migrations(conn)
    assert version >= 16

    def row(fragment_id: str) -> sqlite3.Row:
        return conn.execute("SELECT * FROM fragments WHERE id = ?", (fragment_id,)).fetchone()

    assert row("frag_reopen")["relation_type"] == "history_reopen"
    assert row("frag_reopen")["source_fragment_id"] == "frag_base"
    assert row("frag_plain")["relation_type"] == "unknown"
    assert row("frag_ghost_src")["source_fragment_id"] is None
    assert row("frag_ghost_src")["relation_type"] == "unknown"
    assert row("frag_cross")["source_fragment_id"] is None, "跨话题的来源必须断开"

    # 内容版本按消息条数补齐（派生任务据此校验迟到结果）
    assert row("frag_reopen")["content_version"] == 3
    assert row("frag_nosummary")["content_version"] == 4

    # 缺摘要的封存片段登记了补齐任务；已有摘要的不重复派发
    tasks = conn.execute(
        "SELECT fragment_id FROM derived_tasks WHERE kind = 'summary'"
    ).fetchall()
    task_fragments = {t["fragment_id"] for t in tasks}
    assert "frag_nosummary" in task_fragments
    assert "frag_reopen" not in task_fragments
    assert "frag_base" not in task_fragments


def test_upgrade_is_idempotent_and_keeps_messages(tmp_path):
    conn = connect(tmp_path / "legacy.db")
    _apply_until(conn, LEGACY_VERSION)
    _topic(conn, "t1", "话题一")
    _legacy_fragment(conn, "frag_nosummary", "t1", messages=3)
    conn.commit()
    before = [dict(r) for r in conn.execute("SELECT * FROM messages ORDER BY id").fetchall()]

    apply_migrations(conn)
    snapshot = [dict(r) for r in conn.execute("SELECT * FROM fragments ORDER BY id").fetchall()]
    tasks_after_first = conn.execute("SELECT COUNT(*) c FROM derived_tasks").fetchone()["c"]
    apply_migrations(conn)  # 再跑一遍：不报错、不重复派发、不改数据

    assert [dict(r) for r in conn.execute("SELECT * FROM fragments ORDER BY id").fetchall()] == snapshot
    assert conn.execute("SELECT COUNT(*) c FROM derived_tasks").fetchone()["c"] == tasks_after_first
    assert [dict(r) for r in conn.execute("SELECT * FROM messages ORDER BY id").fetchall()] == before
