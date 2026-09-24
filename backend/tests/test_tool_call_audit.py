"""轨迹表必须记住「为什么失败」。

真实事故：`tool_calls` 里 71 条失败记录的 `result` 全是空字符串 ——
原因只存在于 trace，轨迹表本身回答不了「它当时为什么失败」。
"""

from __future__ import annotations

import sqlite3

import pytest

from agent.api.bus import EventBus
from agent.config import Settings
from agent.credentials.store import MemoryKeyring
from agent.services.app import AppContext
from agent.storage.db import connect
from agent.storage.migrate import apply_migrations
from agent.storage.schema import MIGRATIONS


@pytest.fixture()
def ctx(tmp_path) -> AppContext:
    conn = connect(tmp_path / "app.db")
    apply_migrations(conn)
    app_ctx = AppContext(Settings(data_dir=tmp_path), conn, EventBus())
    app_ctx.credentials._kr = MemoryKeyring()
    return app_ctx


def test_migration_adds_error_column_to_tool_calls(db_conn: sqlite3.Connection):
    cols = {r["name"] for r in db_conn.execute("PRAGMA table_info(tool_calls)")}
    assert "error" in cols


def test_legacy_tool_calls_table_gets_error_column(tmp_path):
    """老库升级：tool_calls 没有 error 列时要补上，且不丢旧行。"""
    conn = connect(tmp_path / "legacy.db")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_version ("
        " id INTEGER PRIMARY KEY, version INTEGER NOT NULL, applied_at TEXT NOT NULL)"
    )
    for target, statements in MIGRATIONS:
        if target > 18:
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

    cols = {r["name"] for r in conn.execute("PRAGMA table_info(tool_calls)")}
    assert "error" in cols
    row = conn.execute("SELECT tool_name, error FROM tool_calls WHERE id = 'tc_old'").fetchone()
    assert row["tool_name"] == "fs_list"
    assert row["error"] is None
    conn.close()


def test_failed_tool_call_records_its_reason(ctx: AppContext):
    ctx._record_tool_call(
        {
            "tool_name": "fs_list",
            "arguments": {"path": "."},
            "ok": False,
            "result": "",
            "error": "找不到工作区：ws_dedd9eeb2ffb",
        }
    )
    row = ctx.conn.execute(
        "SELECT tool_name, error, ok FROM tool_calls ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    assert row["tool_name"] == "fs_list"
    assert row["error"] == "找不到工作区：ws_dedd9eeb2ffb"
    assert row["ok"] == 0


def test_successful_tool_call_has_no_error(ctx: AppContext):
    """成功调用不写失败原因（与 result 列同样的空值约定：空串而不是 NULL）。"""
    ctx._record_tool_call({"tool_name": "now", "arguments": {}, "ok": True, "result": "2026-09-24"})
    row = ctx.conn.execute(
        "SELECT error, ok FROM tool_calls ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    assert row["ok"] == 1
    assert row["error"] == ""


# ---------- 工具调用历史（tool_records） ----------

def test_tool_record_is_written_with_full_output(ctx: AppContext):
    """同一行审计之外，还要写一份用户能回看的完整记录。"""
    record_id = ctx._record_tool_call(
        {
            "tool_name": "fs_list",
            "arguments": {"path": "."},
            "ok": False,
            "result": "列目录失败：找不到路径",
            "error": "列目录失败：找不到路径",
            "call_id": "call_x",
            "turn_id": "turn_x",
            "seq": 1,
            "duration_ms": 7,
            "status": "failed",
        }
    )
    assert record_id
    row = ctx.conn.execute(
        "SELECT turn_id, call_id, output, status, error, duration_ms, seq, truncated "
        "FROM tool_records WHERE id = ?",
        (record_id,),
    ).fetchone()
    assert row["turn_id"] == "turn_x"
    assert row["call_id"] == "call_x"
    assert row["seq"] == 1
    assert row["status"] == "failed"
    assert row["duration_ms"] == 7
    assert "找不到路径" in row["output"]      # 输出全文，不是 200 字预览
    assert row["error"] == "列目录失败：找不到路径"
    assert row["truncated"] == 0


def test_record_outputs_setting_off_skips_output(ctx: AppContext):
    """关掉「保存输出全文」：参数、状态、失败原因照旧保留，只是没有正文。"""
    ctx.settings_store.set("tools.record_outputs", "0")
    record_id = ctx._record_tool_call(
        {
            "tool_name": "fs_read",
            "arguments": {"path": "C:/secret.txt"},
            "ok": True,
            "result": "不该被保存的正文",
            "error": "",
            "call_id": "call_y",
            "turn_id": "turn_y",
            "seq": 1,
            "duration_ms": 3,
            "status": "success",
        }
    )
    row = ctx.conn.execute(
        "SELECT output, output_missing, missing_reason, arguments FROM tool_records WHERE id = ?",
        (record_id,),
    ).fetchone()
    assert row["output"] == ""
    assert row["output_missing"] == 1
    assert row["missing_reason"] == "setting"
    assert "secret.txt" in row["arguments"]


def test_legacy_payload_without_new_fields_still_writes_a_record(ctx: AppContext):
    """老调用点（没有 turn_id / seq / status）也要能写，不能因为缺字段崩掉。"""
    record_id = ctx._record_tool_call(
        {"tool_name": "now", "arguments": {}, "ok": True, "result": "2026-09-24"}
    )
    row = ctx.conn.execute(
        "SELECT turn_id, status, output FROM tool_records WHERE id = ?", (record_id,)
    ).fetchone()
    assert row["turn_id"] == ""
    assert row["status"] == "success"
    assert row["output"] == "2026-09-24"
