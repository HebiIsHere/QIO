"""工具调用历史（`tool_records`）：写入、读回、清理、迁移。

真实事故背景：工具卡只活在实时事件流里，刷新或重启之后那一轮"调了哪些工具、
为什么失败"在对话里就没了。这张表是给用户复盘用的正文，不是审计摘要。
"""

from __future__ import annotations

import sqlite3

import agent.core  # noqa: F401 - 与运行路径同序：agent.tools 必须在 agent.core 之后导入

from agent.storage.tool_records import (
    MAX_TEXT_CHARS,
    get_record,
    previews_for_turns,
    prune_outputs,
    record_tool_call,
)


def _record(conn, **over):
    payload = dict(
        turn_id="turn_1",
        topic_id="topic_1",
        call_id="call_1",
        seq=1,
        tool_name="fs_list",
        arguments={"path": "."},
        output="列目录成功",
        status="success",
        error="",
        duration_ms=12,
        save_output=True,
    )
    payload.update(over)
    return record_tool_call(conn, **payload)


def test_record_and_read_full(db_conn: sqlite3.Connection):
    rid = _record(db_conn, output="完整输出")
    assert rid and rid.startswith("tr_")
    row = get_record(db_conn, rid)
    assert row["arguments"] == {"path": "."}
    assert row["output"] == "完整输出"
    assert row["status"] == "success"
    assert row["output_missing"] is False
    assert row["duration_ms"] == 12


def test_secrets_are_redacted_before_storage(db_conn: sqlite3.Connection):
    rid = _record(
        db_conn,
        arguments={"path": ".", "api_key": "sk-live-abcdef123456"},
        output="token=sk-live-abcdef123456\nBearer abcdefghijklmn",
    )
    row = get_record(db_conn, rid)
    assert "sk-live-abcdef123456" not in row["output"]
    assert "***redacted***" in row["output"]
    assert row["arguments"]["api_key"] == "***redacted***"


def test_long_output_is_truncated_with_marker(db_conn: sqlite3.Connection):
    rid = _record(db_conn, output="A" * (MAX_TEXT_CHARS + 500))
    row = get_record(db_conn, rid)
    assert row["truncated"] is True
    assert len(row["output"]) < MAX_TEXT_CHARS + 200
    assert "已截断" in row["output"]


def test_output_at_limit_is_not_truncated(db_conn: sqlite3.Connection):
    rid = _record(db_conn, output="A" * MAX_TEXT_CHARS)
    assert get_record(db_conn, rid)["truncated"] is False


def test_save_output_off_keeps_metadata(db_conn: sqlite3.Connection):
    rid = _record(db_conn, output="不该被保存", save_output=False)
    row = get_record(db_conn, rid)
    assert row["output"] == ""
    assert row["output_missing"] is True
    assert row["missing_reason"] == "setting"
    assert row["arguments"] == {"path": "."}
    assert row["status"] == "success"


def test_same_call_written_twice_stays_one_row(db_conn: sqlite3.Connection):
    first = _record(db_conn)
    second = _record(db_conn)
    assert first == second
    assert db_conn.execute("SELECT COUNT(*) c FROM tool_records").fetchone()["c"] == 1


def test_previews_for_turns_order_and_shape(db_conn: sqlite3.Connection):
    first = _record(db_conn, call_id="c1", seq=1, output="X" * 1000)
    second = _record(db_conn, call_id="c2", seq=2, output="第二个")
    db_conn.execute(
        "UPDATE tool_records SET created_at = '2026-01-01T00:00:01+00:00' WHERE id = ?", (first,)
    )
    db_conn.execute(
        "UPDATE tool_records SET created_at = '2026-01-01T00:00:02+00:00' WHERE id = ?", (second,)
    )

    items = previews_for_turns(db_conn, ["turn_1"])

    assert [i["seq"] for i in items] == [1, 2]
    assert len(items[0]["preview"]) == 400       # 预览只给前 400 字
    assert items[0]["output_chars"] == 1000
    assert items[0]["title"] == "列出目录"        # 中文展示名由 tool_label 现算
    assert "output" not in items[0]              # 预览里没有全文
    assert items[1]["preview"] == "第二个"


def test_previews_for_unknown_turns_is_empty(db_conn: sqlite3.Connection):
    _record(db_conn)
    assert previews_for_turns(db_conn, ["turn_nope"]) == []
    assert previews_for_turns(db_conn, []) == []


def test_prune_clears_output_but_keeps_record(db_conn: sqlite3.Connection):
    old = _record(db_conn, call_id="old", output="很久以前的输出")
    db_conn.execute(
        "UPDATE tool_records SET created_at = '2020-01-01T00:00:00+00:00' WHERE id = ?", (old,)
    )
    fresh = _record(db_conn, call_id="fresh", output="今天的输出")

    purged = prune_outputs(db_conn, 90)

    assert purged == 1
    cleared = get_record(db_conn, old)
    assert cleared["output"] == ""
    assert cleared["output_missing"] is True
    assert cleared["missing_reason"] == "retention"
    assert cleared["arguments"] == {"path": "."}   # 参数与状态继续保留
    assert cleared["status"] == "success"
    assert get_record(db_conn, fresh)["output"] == "今天的输出"


def test_retention_zero_never_prunes(db_conn: sqlite3.Connection):
    old = _record(db_conn, call_id="old", output="很久以前的输出")
    db_conn.execute(
        "UPDATE tool_records SET created_at = '2020-01-01T00:00:00+00:00' WHERE id = ?", (old,)
    )
    assert prune_outputs(db_conn, 0) == 0
    assert get_record(db_conn, old)["output"] == "很久以前的输出"


def test_missing_record_returns_none(db_conn: sqlite3.Connection):
    assert get_record(db_conn, "tr_nope") is None


def test_legacy_db_gets_tool_records_table(tmp_path):
    """老库升级：19 版本之前没有这张表，迁移要补上，且不丢旧数据。"""
    from agent.storage.db import connect
    from agent.storage.migrate import apply_migrations
    from agent.storage.schema import MIGRATIONS

    conn = connect(tmp_path / "legacy.db")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_version ("
        " id INTEGER PRIMARY KEY, version INTEGER NOT NULL, applied_at TEXT NOT NULL)"
    )
    for target, statements in MIGRATIONS:
        if target > 19:
            break
        for stmt in statements:
            conn.execute(stmt)
        conn.execute(
            "INSERT INTO schema_version (version, applied_at) "
            "VALUES (?, '2026-01-01T00:00:00+00:00')",
            (target,),
        )
    conn.execute(
        "INSERT INTO tool_calls (id, tool_name, arguments, result, ok, created_at) "
        "VALUES ('tc_old', 'fs_list', '{}', '', 0, '2026-01-01T00:00:00+00:00')"
    )

    apply_migrations(conn)

    tables = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "tool_records" in tables
    assert conn.execute("SELECT COUNT(*) c FROM tool_calls").fetchone()["c"] == 1
    conn.close()
