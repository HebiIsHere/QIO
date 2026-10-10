"""R4：把 TURN_END 的结束事实落进 turn_journal 台账（迁移 + record_facts/facts）。

背景：D 的实机 S6 确认「刷新后失败轮的重试入口消失」。前端已有本机留痕兜底，
但跨设备 / 清存储要靠后端权威路径 —— 本文件钉住存储层：
迁移只追加三列、旧行仍可读、写入-读回一致、脱敏后才落库、批量查询不 N+1、
坏 actions 不抛、没有记录的 turn 不伪造。

运行：cd backend; $env:PYTHONPATH='src'; .\.venv\Scripts\python.exe -m pytest tests/test_turn_journal_facts.py -q
"""

from __future__ import annotations

import json

import pytest

from agent.storage.turn_journal import REASON_TEXT, TurnJournal


@pytest.fixture()
def journal(db_conn) -> TurnJournal:
    return TurnJournal(db_conn)


def _legacy_row(
    conn,
    turn_id: str,
    *,
    status: str = "interrupted",
    reason: str | None = "running_at_restart",
    message: str = "旧消息",
) -> None:
    """模拟迁移之前就存在的行：只写历史列，新的三列保持 NULL。"""
    conn.execute(
        "INSERT INTO turn_journal (turn_id, message, topic_id, notify, status, created_at,"
        " updated_at, reason) VALUES (?,?,?,?,?,?,?,?)",
        (
            turn_id,
            message,
            "t1",
            0,
            status,
            "2026-10-07T00:00:00+00:00",
            "2026-10-07T00:00:01+00:00",
            reason,
        ),
    )
    conn.commit()


# -- 迁移 -------------------------------------------------------------------


def test_migration_adds_fact_columns_and_keeps_legacy_rows_readable(db_conn, journal):
    cols = {row["name"] for row in db_conn.execute("PRAGMA table_info(turn_journal)").fetchall()}
    assert {"reason_code", "stopped_by", "actions"} <= cols, "只追加三列，不改历史列"

    _legacy_row(db_conn, "turn_legacy")
    row = db_conn.execute("SELECT * FROM turn_journal WHERE turn_id = 'turn_legacy'").fetchone()
    assert row["message"] == "旧消息"  # 旧数据没被动过
    assert row["reason_code"] is None
    assert row["stopped_by"] is None
    assert row["actions"] is None

    # 旧行没有「结束事实」：状态词 + 既有的人话原因照给，不伪造原因；但这一行确实
    # **可恢复**（interrupted、notify=0、未被 claim），按 K3 读时投影给出真正可用的
    # resend（与 /api/turns/{id}/resend 准入一致），journal 行本身不变。
    facts = journal.facts(["turn_legacy"])["turn_legacy"]
    assert facts["status"] == "interrupted"
    assert facts["reason"] == REASON_TEXT["running_at_restart"]
    assert facts["reason_code"] is None
    assert facts["stopped_by"] is None
    assert facts["actions"] == ["resend"]


def test_upgrade_path_from_pre_fact_schema_keeps_existing_rows(db_conn, journal):
    """存量库升级：把库退回到「结束事实列还没有」的状态，再跑一次迁移 —— 旧行必须原样可读。"""
    from agent.storage.migrate import apply_migrations, current_version
    from agent.storage.schema import MIGRATIONS, SCHEMA_VERSION

    # 结束事实三列现在由哪一条迁移负责，由迁移定义自己决定（集成后编号不再是 28）——
    # 用例按「哪条迁移 ADD COLUMN reason_code」自适应，避免把号段写死在测试里。
    facts_version = max(
        target
        for target, statements in MIGRATIONS
        if any("ADD COLUMN reason_code" in str(statement) for statement in statements)
    )

    _legacy_row(db_conn, "turn_before_upgrade", status="completed", reason=None, message="升级前的消息")
    for column in ("reason_code", "stopped_by", "actions"):
        db_conn.execute(f"ALTER TABLE turn_journal DROP COLUMN {column}")
    db_conn.execute("DELETE FROM schema_version WHERE version >= ?", (facts_version,))
    db_conn.commit()
    assert "actions" not in {
        row["name"] for row in db_conn.execute("PRAGMA table_info(turn_journal)").fetchall()
    }

    assert current_version(db_conn) == facts_version - 1
    assert apply_migrations(db_conn) == SCHEMA_VERSION

    row = db_conn.execute(
        "SELECT * FROM turn_journal WHERE turn_id = 'turn_before_upgrade'"
    ).fetchone()
    assert row["message"] == "升级前的消息", "升级不得动旧数据"
    assert row["reason_code"] is None and row["stopped_by"] is None and row["actions"] is None
    facts = journal.facts(["turn_before_upgrade"])["turn_before_upgrade"]
    assert facts["status"] == "completed"
    assert facts["actions"] == []


# -- 写入 / 读回 -------------------------------------------------------------


def test_record_facts_round_trip(db_conn, journal):
    _legacy_row(db_conn, "turn_1", status="running", reason=None)

    journal.record_facts(
        "turn_1",
        reason_code="provider_error",
        reason="模型服务没有响应（连续 2 次）",
        stopped_by="system",
        actions=["retry", "resend"],
    )

    facts = journal.facts(["turn_1"])["turn_1"]
    assert facts["turn_id"] == "turn_1"
    assert facts["reason_code"] == "provider_error"
    assert facts["reason"] == "模型服务没有响应（连续 2 次）"
    assert facts["stopped_by"] == "system"
    assert facts["actions"] == ["retry", "resend"]
    # 存的是 JSON 数组字符串（列类型是 TEXT）
    raw = db_conn.execute(
        "SELECT actions FROM turn_journal WHERE turn_id = 'turn_1'"
    ).fetchone()["actions"]
    assert json.loads(raw) == ["retry", "resend"]


def test_record_facts_redacts_secrets_before_persisting(db_conn, journal):
    _legacy_row(db_conn, "turn_secret", reason=None)
    secret = "sk-1234567890abcdefghijklmn"

    journal.record_facts(
        "turn_secret",
        reason_code="provider_error",
        reason=f"调用失败：api_key={secret} 被拒绝",
        stopped_by="system",
        actions=["retry"],
    )

    raw = db_conn.execute(
        "SELECT reason FROM turn_journal WHERE turn_id = 'turn_secret'"
    ).fetchone()["reason"]
    assert secret not in raw, "密钥原文不得落库"
    assert "redacted" in raw.lower() or "***" in raw
    # 读回来的也不含原文
    assert secret not in (journal.facts(["turn_secret"])["turn_secret"]["reason"] or "")


def test_record_facts_on_missing_row_is_harmless(db_conn, journal):
    journal.record_facts(
        "turn_absent",
        reason_code="budget",
        reason="预算用完",
        stopped_by="system",
        actions=["continue"],
    )
    assert journal.facts(["turn_absent"]) == {}, "没有这一行就不出结果，不凭空建行"


# -- 批量查询 ---------------------------------------------------------------


def test_facts_batches_and_skips_unknown(db_conn, journal):
    for index in range(5):
        _legacy_row(db_conn, f"turn_{index}", status="failed", reason=None)
        journal.record_facts(
            f"turn_{index}",
            reason_code="tool_failed",
            reason=f"第 {index} 次失败",
            stopped_by=None,
            actions=["retry"],
        )

    # 用 sqlite 自己的语句回调数查询次数（Connection.execute 是只读属性，不能打桩）
    statements: list[str] = []
    db_conn.set_trace_callback(statements.append)
    try:
        facts = journal.facts([f"turn_{index}" for index in range(5)] + ["turn_missing"])
    finally:
        db_conn.set_trace_callback(None)

    assert set(facts) == {f"turn_{index}" for index in range(5)}
    assert facts["turn_2"]["reason"] == "第 2 次失败"
    assert facts["turn_2"]["actions"] == ["retry"]
    reads = [sql for sql in statements if "FROM turn_journal" in sql]
    assert len(reads) == 1, f"一次批量取回，不能 N+1：{reads}"


def test_facts_tolerates_broken_actions_json(db_conn, journal):
    _legacy_row(db_conn, "turn_broken", status="failed", reason=None)
    db_conn.execute(
        "UPDATE turn_journal SET actions = ? WHERE turn_id = 'turn_broken'", ("{不是 JSON",)
    )
    db_conn.commit()

    facts = journal.facts(["turn_broken"])["turn_broken"]
    assert facts["actions"] == [], "坏数据不抛异常，也不猜内容"


def test_facts_empty_input_returns_empty(db_conn, journal):
    assert journal.facts([]) == {}
