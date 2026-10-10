"""F05 定向回归：读取失败不能伪装成「空的恢复清单」。

缺陷现场（核查）
----------------

`RecoveryService._turn_records` / `_derived_records` 捕获 SQLite 错误后**返回空数组**，
于是 `GET /api/recovery/records` 仍然 200 + `total = 0`。核查时故障前有 1 条可见记录，
单独让 SELECT 抛 OperationalError 之后，接口错误地返回空清单 —— 界面上看起来
「未完成事项都没有了」，而实际上一条都没读到。

修好之后：任一来源读不动 → `RecoveryReadError` → HTTP 503 + 明确原因；
前端保留上一次可见的内容并给出可重试原因（前端用例见
`frontend/src/.../fu-f01f10-recovery-read-failure.spec.ts`）。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent.api.recovery_routes import build_router
from agent.services.recovery import RecoveryInbox, RecoveryReadError
from agent.storage.turn_journal import TurnJournal

from test_fu_w1_support import FakeTurns, add_derived, add_turn, migrated_conn, register_self

SEED_TIME = "2026-01-01T00:00:00+00:00"


def _inbox(conn, registry) -> RecoveryInbox:  # noqa: ANN001
    return RecoveryInbox(
        conn,
        registry,
        turns=FakeTurns(),
        instance_id="me",
        journal=TurnJournal(conn, instance_id="me", registry=registry),
    )


def _app(conn, registry) -> FastAPI:  # noqa: ANN001
    ctx = SimpleNamespace(
        conn=conn,
        instances=registry,
        instance_id="me",
        turns=FakeTurns(),
        turn_journal=TurnJournal(conn, instance_id="me", registry=registry),
    )
    app = FastAPI()
    app.include_router(build_router(ctx))
    return app


def _break_table(conn, table: str, *, into: str) -> None:  # noqa: ANN001
    conn.execute(f"ALTER TABLE {table} RENAME TO {into}")


def _restore_table(conn, into: str, *, table: str) -> None:  # noqa: ANN001
    conn.execute(f"ALTER TABLE {into} RENAME TO {table}")


# ---------------------------------------------------------------------------
# 服务层：读失败抛错，不返回空清单
# ---------------------------------------------------------------------------


def test_turn_read_failure_raises_instead_of_returning_an_empty_listing(tmp_path):
    conn = migrated_conn(tmp_path)
    registry = register_self(conn, "me")
    add_turn(conn, "turn_visible", message="故障前可见", status="interrupted", owner=None)
    add_derived(conn, "task_visible", owner=None, created_at=SEED_TIME)
    inbox = _inbox(conn, registry)
    assert inbox.list_records().total == 2, "故障前有可见记录"

    _break_table(conn, "turn_journal", into="turn_journal_broken")
    try:
        with pytest.raises(RecoveryReadError) as excinfo:
            inbox.list_records()
        assert "turn_journal" in str(excinfo.value)
    finally:
        _restore_table(conn, "turn_journal_broken", table="turn_journal")

    # 恢复之后照旧可读（重试路径真的能恢复）
    assert inbox.list_records().total == 2
    conn.close()


def test_derived_read_failure_raises_instead_of_returning_an_empty_listing(tmp_path):
    conn = migrated_conn(tmp_path)
    registry = register_self(conn, "me")
    add_turn(conn, "turn_visible", message="故障前可见", status="interrupted", owner=None)
    add_derived(conn, "task_visible", owner=None, created_at=SEED_TIME)
    inbox = _inbox(conn, registry)

    _break_table(conn, "derived_tasks", into="derived_tasks_broken")
    try:
        with pytest.raises(RecoveryReadError) as excinfo:
            inbox.list_records()
        assert "derived_tasks" in str(excinfo.value)
    finally:
        _restore_table(conn, "derived_tasks_broken", table="derived_tasks")

    assert inbox.list_records().total == 2
    conn.close()


def test_missing_source_is_only_an_error_when_it_was_requested(tmp_path):
    """清单按 kind 过滤：没被请求的来源读不动，不该影响这次请求。"""
    conn = migrated_conn(tmp_path)
    registry = register_self(conn, "me")
    add_derived(conn, "task_visible", owner=None, created_at=SEED_TIME)
    inbox = _inbox(conn, registry)

    _break_table(conn, "turn_journal", into="turn_journal_broken")
    try:
        listing = inbox.list_records(kinds=["derived_task"])
        assert [record.record_id for record in listing.records] == ["task_visible"]
        with pytest.raises(RecoveryReadError):
            inbox.list_records()
    finally:
        _restore_table(conn, "turn_journal_broken", table="turn_journal")
    conn.close()


# ---------------------------------------------------------------------------
# HTTP：503 + 明确原因（绝不 200 + 空清单）
# ---------------------------------------------------------------------------


def test_api_returns_503_and_never_a_healthy_empty_listing(tmp_path):
    conn = migrated_conn(tmp_path)
    registry = register_self(conn, "me")
    add_turn(conn, "turn_visible", message="故障前可见", status="interrupted", owner=None)

    with TestClient(_app(conn, registry)) as client:
        before = client.get("/api/recovery/records")
        assert before.status_code == 200
        assert before.json()["total"] == 1

        _break_table(conn, "turn_journal", into="turn_journal_broken")
        try:
            broken = client.get("/api/recovery/records")
            assert broken.status_code == 503, broken.text
            body = broken.json()
            assert body["ok"] is False and body["incomplete"] is True
            assert body["total"] is None, "读失败时不许给一个像样的总数"
            assert body["records"] == []
            assert "turn_journal" in body["reason"]
        finally:
            _restore_table(conn, "turn_journal_broken", table="turn_journal")

        again = client.get("/api/recovery/records")
        assert again.status_code == 200
        assert again.json()["total"] == 1
    conn.close()


def test_api_reports_which_source_failed(tmp_path):
    conn = migrated_conn(tmp_path)
    registry = register_self(conn, "me")
    add_turn(conn, "turn_visible", status="interrupted", owner=None)
    add_derived(conn, "task_visible", owner=None, created_at=SEED_TIME)

    with TestClient(_app(conn, registry)) as client:
        _break_table(conn, "derived_tasks", into="derived_tasks_broken")
        try:
            body = client.get("/api/recovery/records").json()
            assert "derived_tasks" in body["reason"]
        finally:
            _restore_table(conn, "derived_tasks_broken", table="derived_tasks")

        assert client.get("/api/recovery/records").status_code == 200
    conn.close()
