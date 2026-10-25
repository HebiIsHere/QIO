# -*- coding: utf-8 -*-
"""工作流 A：三分支整合后的迁移升级验证（integ）。

为什么需要这组测试（真实缺口）：

* 对话页 v2 / 互动页 v2 的来源库 schema_version 记录到 55 / 57，R9 来源库记录到 66；
  apply_migrations 只按版本号前进，页面库会整段跳过 R9 的 52/53/55，而 R9 的 57
  （要求绑定）**先于** 58（补偿）执行 —— cv 55 库升级会直接抛
  sqlite3.OperationalError: no such table: user_requirements。
* 三分支整合把页面线迁移改号到 67/68/69/70，并在 migrate.repair_branch_schema
  做一次「只补对象、不写版本号、不改数据」的兼容修复。

本文件构造五个来源库形状（空 / main 51 / 对话页 55 / 互动页 57 / R9 66），逐一验证：

1. 升级到 SCHEMA_VERSION 后按对象核对 REQUIRED_OBJECTS（表 / 列 / 索引，不只看版本号）；
2. 原有数据逐行保留，审批期限 / 确认进度 / 文件身份 / 意图与重试次数不变；
3. 关键外键 / 唯一约束 / 部分唯一索引在升级后可用；
4. 二次启动幂等；
5. 「版本号已登记但必需对象缺失」时由对象级复核 + 补偿重放补齐；
6. 兼容修复只执行 CREATE/ALTER、不写 schema_version、不改业务数据；
7. 统一补偿没有业务副作用（无 UPDATE / DELETE / DROP；唯一的 INSERT 是幂等 seed）。

来源库形状用统一执行列表的真实语句构造（不是手写 SQL 冒充）：
main 51 = target <= 51；页面 55/57 = main 51 + 67（原页面线 55 的 task_feed DDL）
（i2 再加 69 = 原页面线 56 的 P3-D DDL）；R9 66 = target <= 66。
"""

from __future__ import annotations

import logging
import sqlite3

import pytest

from agent.storage import migrate as migrate_module
from agent.storage.db import connect, transaction
from agent.storage.migrate import (
    REQUIRED_OBJECTS,
    apply_migrations,
    current_version,
    missing_objects,
    repair_branch_schema,
)
from agent.storage.schema import COMPENSATION_VERSION, MIGRATIONS, SCHEMA_VERSION

CLAUSE = "三分支整合（工作流 A）"
TS = "2026-10-20T10:00:00+00:00"
TS2 = "2026-10-21T11:30:00+00:00"


# ---------------------------------------------------------------------------
# 来源库形状构造（只用于测试；用统一执行列表的真实语句）
# ---------------------------------------------------------------------------


def _by_number() -> dict[int, list[str]]:
    return {int(number): list(statements) for number, statements in MIGRATIONS}


def _mark(conn: sqlite3.Connection, version: int) -> None:
    conn.execute(
        "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
        (int(version), TS),
    )


def _apply_prefix(conn: sqlite3.Connection, max_version: int) -> None:
    """按统一执行列表原样应用 target <= max_version（构造来源库形状）。"""
    migrate_module.current_version(conn)  # 确保 schema_version 表存在
    for target, statements in MIGRATIONS:
        if int(target) > max_version:
            break
        with transaction(conn):
            migrate_module._apply_statements(conn, int(target), statements)
            _mark(conn, int(target))


def _build_page_shaped(conn: sqlite3.Connection, version: int) -> None:
    """构造冻结页面线来源库形状。

    * cv 55 = main 51 + 旧 55 的 task_feed/task_ack DDL（整合后登记为 67）；
    * i2 57 = 上面 + 旧 56 的 P3-D DDL（整合后登记为 69）；i2 的版本记录是 51/55/56/57。
    """
    _apply_prefix(conn, 51)
    by = _by_number()
    with transaction(conn):
        migrate_module._apply_statements(conn, 67, by[67])
        _mark(conn, 55)
    if version >= 57:
        with transaction(conn):
            migrate_module._apply_statements(conn, 69, by[69])
            _mark(conn, 56)
            _mark(conn, 57)


def _build_source(conn: sqlite3.Connection, kind: str) -> None:
    if kind == "empty":
        return
    if kind == "main":
        _apply_prefix(conn, 51)
    elif kind == "cv":
        _build_page_shaped(conn, 55)
    elif kind == "i2":
        _build_page_shaped(conn, 57)
    elif kind == "r9":
        _apply_prefix(conn, 66)
    else:  # pragma: no cover - 测试内部错误
        raise ValueError(f"未知来源库形状：{kind}")


@pytest.fixture()
def make_db(tmp_path):
    """按来源库形状建库；测试结束统一关闭。"""
    conns: list[sqlite3.Connection] = []

    def _make(kind: str) -> sqlite3.Connection:
        conn = connect(tmp_path / f"{kind}.db")
        conn.row_factory = sqlite3.Row
        _build_source(conn, kind)
        conns.append(conn)
        return conn

    yield _make
    for conn in conns:
        conn.close()


# ---------------------------------------------------------------------------
# 种子数据：代表性记录（话题/消息/附件/审批/板面/意图/重试/任务记录/R9 对象）
# ---------------------------------------------------------------------------


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def _insert(conn: sqlite3.Connection, table: str, values: dict) -> bool:
    cols = _columns(conn, table)
    if not cols:
        return False
    use = {key: value for key, value in values.items() if key in cols}
    names = ",".join(use)
    marks = ",".join("?" for _ in use)
    conn.execute(f"INSERT INTO {table} ({names}) VALUES ({marks})", list(use.values()))
    return True


def _seed(conn: sqlite3.Connection) -> dict[str, list[dict]]:
    """写入代表数据并返回升级前快照（只包含该版本真实存在的表/列）。"""
    seeded: set[str] = set()

    def S(table: str, values: dict) -> None:
        if _insert(conn, table, values):
            seeded.add(table)

    S("instances", dict(instance_id="inst-seed-1", pid=4242, host="seed-host",
                        started_at=TS, last_heartbeat=TS2))
    S("nodes", dict(id="node-topic-seed-1", type="topic", name="SEED-话题",
                    meta='{"seed":true}', created_at=TS, updated_at=TS))
    S("nodes", dict(id="node-user-seed-1", type="user", name="SEED-用户", meta="{}",
                    created_at=TS, updated_at=TS))
    S("edges", dict(id="edge-seed-1", src="node-user-seed-1", dst="node-topic-seed-1",
                    type="owns", weight=1.0, created_at=TS, updated_at=TS))
    S("fragments", dict(id="frag-seed-1", topic_id="node-topic-seed-1",
                        start_message_id="msg-seed-1", end_message_id="msg-seed-1",
                        summary="SEED-摘要", summary_model="fake-model", summary_version=2,
                        created_at=TS, closed_at=TS2, meta='{"seed":true}',
                        relation_type="continuation", boundary_reason="seed", same_stage=1,
                        content_version=3))
    S("messages", dict(id="msg-seed-1", fragment_id="frag-seed-1", role="user",
                       content="SEED-原始用户消息", content_type="text", raw='{"seed":true}',
                       created_at=TS, storage_tier="hot", turn_id="turn-seed-1"))
    S("attachments", dict(id="att-seed-1", message_id="msg-seed-1", turn_id="turn-seed-1",
                          topic_id="node-topic-seed-1", kind="file",
                          original_name="seed-original.txt",
                          stored_path="D:/seed/stored/seed-original.txt",
                          source_path="D:/seed/src/seed-original.txt", size_bytes=1234,
                          mtime=1729000000.5, sha256="a" * 64, state="ready",
                          created_at=TS, updated_at=TS2,
                          source_kind="desktop_path", origin="user_original",
                          file_id="VOL-SEED:FILE-1", content_sha256="b" * 64,
                          reference_state="available", material_enabled=1,
                          user_note="SEED 用户说明", last_verified_at=TS2,
                          legacy_paths='["D:/seed/legacy/seed-original.txt"]'))
    S("pending_approvals", dict(approval_id="appr-seed-1", kind="write_file",
                                payload='{"path":"D:/seed/x.txt"}', turn_id="turn-seed-1",
                                session_id="sess-seed-1", created_at=TS,
                                expires_at="2026-10-26T00:00:00+00:00", status="pending",
                                owner_instance_id="inst-seed-1", state="waiting",
                                operation='{"kind":"write_file"}', instruction_id="instr-seed-1",
                                instruction_text="SEED 指令原文", purpose="SEED purpose",
                                fingerprint='{"object":"seed"}', mode_at_request="restricted",
                                confirmed_stage=2, revisions=1, attempt=1,
                                requirement_id="req-seed-1", requirement_version=3))
    S("boards", dict(id="board-seed-1", title="SEED-板面", created_at=TS, updated_at=TS2,
                     meta='{"seed":true}'))
    S("board_states", dict(board_id="board-seed-1", seq=3, state='{"seed":true}',
                           reason="op", updated_at=TS2))
    S("board_submissions", dict(id="sub-seed-1", board_id="board-seed-1", seq=4,
                                status="applied", created_at=TS,
                                client_request_id="req-seed-1", turn_id="turn-seed-1"))
    S("board_intents", dict(id="intent-seed-1", board_id="board-seed-1",
                            submission_id="sub-seed-1", title="SEED-意图", status="pending",
                            created_at=TS, updated_at=TS2, goal="SEED 目标",
                            expected="SEED 预期", operations='[{"op":"add_node"}]',
                            basis='["msg-seed-1"]', intent_key="ik-seed-1",
                            understanding_id="und-seed-1", client_ref="cref-seed-1",
                            version=2, attempt=1, display='{"x":0.25,"y":0.75}'))
    S("board_intent_versions", dict(intent_id="intent-seed-1", version=1,
                                    payload='{"seed":true}', created_at=TS))
    S("board_operations", dict(id="bop-seed-1", board_id="board-seed-1",
                               intent_id="intent-seed-1", operation_key="op-key-seed-1",
                               action="add_node", status="applied", created_at=TS,
                               updated_at=TS2))
    S("board_intent_turns", dict(intent_id="intent-seed-1", board_id="board-seed-1",
                                 submission_id="sub-seed-1", turn_id="turn-seed-1",
                                 attempt=1, source="confirm", status="claimed",
                                 created_at=TS, updated_at=TS2))
    S("board_intent_retries", dict(id="retry-seed-1", series_id="series-seed-1",
                                   board_id="board-seed-1", submission_id="sub-seed-1",
                                   intent_id="intent-seed-1", authorization_version=2,
                                   attempts=2, manual_attempts=1, retry_limit=3,
                                   status="pending", turn_id="turn-seed-1",
                                   created_at=TS, updated_at=TS2))
    S("task_feed", dict(id="feed-seed-1", source="derived_task", source_id="task-seed-1",
                        title="SEED-任务", status="done", raw_status="completed",
                        requires_attention=0, acknowledged_at=TS2, summary="seed task",
                        updated_at=TS2))
    S("task_ack", dict(id="feed-seed-1", acknowledged_at=TS2))
    S("turn_journal", dict(turn_id="turn-seed-1", message="SEED 轮次消息",
                           topic_id="node-topic-seed-1", status="done", created_at=TS,
                           started_at=TS, ended_at=TS2, updated_at=TS2,
                           user_message_id="msg-seed-1", owner_instance_id="inst-seed-1",
                           reason_code="ok", actions='[]'))
    S("derived_tasks", dict(id="task-seed-1", kind="summary", fragment_id="frag-seed-1",
                            content_version=3, state="done", attempts=2, created_at=TS,
                            updated_at=TS2, owner_instance_id="inst-seed-1",
                            claim_generation=3))
    S("user_requirements", dict(id="req-seed-1",
                                text="SEED 用户要求：不要修改 D:/seed/x.txt",
                                provenance='{"message_id":"msg-seed-1"}', status="active",
                                limits='["no-write-x"]', turn_id="turn-seed-1",
                                topic_id="node-topic-seed-1", created_at=TS, updated_at=TS2,
                                version=3, structured_limits='["no-write-x"]',
                                structured_limits_state="structured",
                                structured_limits_source="seed:model"))
    S("authorization_records", dict(id="auth-seed-1", requirement_id="req-seed-1",
                                    operation_kind="write_file", fingerprint='{"f":"seed"}',
                                    decision="allow", mode="restricted", created_at=TS,
                                    requirement_version=3))
    S("approval_audit", dict(approval_id="appr-seed-1", seq=1, action="requested",
                             detail='{"seed":true}', at=TS))
    S("approval_history", dict(approval_id="appr-seed-1", kind="write_file",
                               outcome="completed", at=TS2))
    S("attachment_migration_audit", dict(attachment_id="att-seed-1", step="migrate",
                                         outcome="done", updated_at=TS2))
    S("file_origins", dict(volume="VOL-SEED", file_id="FILE-1",
                           path="D:/seed/src/seed-original.txt", origin="user_original",
                           attachment_id="att-seed-1", created_at=TS, updated_at=TS2))
    S("internal_data_identities", dict(identity_key="VOL-SEED:FILE-1",
                                       path="D:/seed/src/seed-original.txt", kind="file",
                                       active=1, created_at=TS, updated_at=TS2,
                                       birth_key="birth-seed-1"))
    conn.commit()
    return _snapshot(conn, seeded)


def _snapshot(conn: sqlite3.Connection, tables: set[str]) -> dict[str, list[dict]]:
    snap: dict[str, list[dict]] = {}
    for table in sorted(tables):
        rows = [dict(row) for row in conn.execute(f"SELECT * FROM {table}")]
        snap[table] = rows
    return snap


def _assert_rows_preserved(conn: sqlite3.Connection, snapshot: dict[str, list[dict]],
                           *, exclude: set[str] | None = None) -> None:
    """升级前存在的每一行、每一列都必须原样可查（新加列允许出现默认值）。"""
    exclude = exclude or set()
    for table, rows in snapshot.items():
        if table in exclude:
            continue
        assert _columns(conn, table), f"{CLAUSE}：升级后表 {table} 缺失"
        for row in rows:
            where = " AND ".join(f"{name} IS ?" for name in row)
            count = conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE {where}", list(row.values())
            ).fetchone()[0]
            assert count == 1, f"{CLAUSE}：表 {table} 的升级前记录丢失或被改写：{row}"
        total = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        assert total == len(rows), (
            f"{CLAUSE}：表 {table} 行数变化（升级不应增删业务行）：{len(rows)} -> {total}")


def _assert_required_objects_present(conn: sqlite3.Connection) -> None:
    """按对象（表 / 列 / 索引）核对，不只看版本号。"""
    assert missing_objects(conn) == {"tables": [], "columns": [], "indexes": []}
    tables = {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    for table in REQUIRED_OBJECTS["tables"]:
        assert table in tables, f"{CLAUSE}：缺少必需表 {table}"
    for spec in REQUIRED_OBJECTS["columns"]:
        table, _, column = str(spec).partition(".")
        assert column in _columns(conn, table), f"{CLAUSE}：缺少必需列 {spec}"
    indexes = {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")
    }
    for index in REQUIRED_OBJECTS["indexes"]:
        assert index in indexes, f"{CLAUSE}：缺少必需索引 {index}"
    assert not [row for row in conn.execute("PRAGMA foreign_key_check")], (
        f"{CLAUSE}：升级后存在外键违规")


# ---------------------------------------------------------------------------
# 1. 五个来源库升级到最新版本
# ---------------------------------------------------------------------------


def test_empty_database_reaches_latest_and_is_idempotent(make_db):
    conn = make_db("empty")
    assert apply_migrations(conn) == SCHEMA_VERSION == COMPENSATION_VERSION >= 70
    _assert_required_objects_present(conn)
    rows = conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0]
    assert apply_migrations(conn) == SCHEMA_VERSION
    assert conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0] == rows


def test_main_51_upgrade_uses_compat_repair_and_preserves_rows(make_db, caplog):
    """健康 main 51：缺 R9/页面对象 → 兼容修复先补 DDL（只多一段 warning），数据不动。"""
    conn = make_db("main")
    snapshot = _seed(conn)
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="agent.storage.migrate"):
        assert apply_migrations(conn) == 70
    assert "兼容修复" in caplog.text, "main 51 缺 R9/页面对象，应触发一次兼容修复"
    _assert_required_objects_present(conn)
    _assert_rows_preserved(conn, snapshot)


def test_cv_55_upgrade_requires_compat_repair_and_preserves_rows(make_db, caplog):
    """cv 55 是必须靠兼容修复才能过的来源库（否则迁移 57 先于 58 直接失败）。"""
    conn = make_db("cv")
    assert current_version(conn) == 55
    snapshot = _seed(conn)
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="agent.storage.migrate"):
        assert apply_migrations(conn) == 70
    assert "兼容修复" in caplog.text, "cv 55 缺 R9 对象，必须先做兼容修复"
    _assert_required_objects_present(conn)
    _assert_rows_preserved(conn, snapshot)


def test_i2_57_upgrade_preserves_rows_and_task_state(make_db):
    conn = make_db("i2")
    assert current_version(conn) == 57
    snapshot = _seed(conn)
    assert apply_migrations(conn) == 70
    _assert_required_objects_present(conn)
    _assert_rows_preserved(conn, snapshot)
    row = conn.execute(
        "SELECT status, acknowledged_at, requires_attention FROM task_feed"
        " WHERE id='feed-seed-1'"
    ).fetchone()
    assert dict(row) == {"status": "done", "acknowledged_at": TS2, "requires_attention": 0}
    retry = conn.execute(
        "SELECT attempts, manual_attempts, retry_limit, status FROM board_intent_retries"
        " WHERE id='retry-seed-1'"
    ).fetchone()
    assert dict(retry) == {"attempts": 2, "manual_attempts": 1, "retry_limit": 3,
                           "status": "pending"}


def test_r9_66_upgrade_preserves_rows_and_key_fields(make_db):
    conn = make_db("r9")
    assert current_version(conn) == 66
    snapshot = _seed(conn)
    approval_before = dict(conn.execute(
        "SELECT expires_at, status, state, confirmed_stage, attempt, revisions"
        " FROM pending_approvals WHERE approval_id='appr-seed-1'").fetchone())
    attachment_before = dict(conn.execute(
        "SELECT file_id, content_sha256, sha256, reference_state, material_enabled"
        " FROM attachments WHERE id='att-seed-1'").fetchone())
    intent_before = dict(conn.execute(
        "SELECT attempt, version, status FROM board_intents"
        " WHERE id='intent-seed-1'").fetchone())
    requirement_before = dict(conn.execute(
        "SELECT version, structured_limits, structured_limits_state"
        " FROM user_requirements WHERE id='req-seed-1'").fetchone())
    authorization_before = dict(conn.execute(
        "SELECT revoked_at, decision, requirement_version"
        " FROM authorization_records WHERE id='auth-seed-1'").fetchone())
    assert apply_migrations(conn) == 70
    _assert_required_objects_present(conn)
    _assert_rows_preserved(conn, snapshot)
    assert dict(conn.execute(
        "SELECT expires_at, status, state, confirmed_stage, attempt, revisions"
        " FROM pending_approvals WHERE approval_id='appr-seed-1'").fetchone()) == approval_before
    assert dict(conn.execute(
        "SELECT file_id, content_sha256, sha256, reference_state, material_enabled"
        " FROM attachments WHERE id='att-seed-1'").fetchone()) == attachment_before
    assert dict(conn.execute(
        "SELECT attempt, version, status FROM board_intents"
        " WHERE id='intent-seed-1'").fetchone()) == intent_before
    assert dict(conn.execute(
        "SELECT version, structured_limits, structured_limits_state"
        " FROM user_requirements WHERE id='req-seed-1'").fetchone()) == requirement_before
    assert dict(conn.execute(
        "SELECT revoked_at, decision, requirement_version"
        " FROM authorization_records WHERE id='auth-seed-1'").fetchone()) == authorization_before
    assert conn.execute("SELECT COUNT(*) FROM task_feed").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM board_intent_retries").fetchone()[0] == 0


@pytest.mark.parametrize("kind", ["main", "cv", "i2", "r9"])
def test_second_start_is_idempotent(make_db, kind):
    conn = make_db(kind)
    snapshot = _seed(conn)
    assert apply_migrations(conn) == 70
    versions_after_first = [row[0] for row in conn.execute(
        "SELECT version FROM schema_version ORDER BY version")]
    assert apply_migrations(conn) == 70
    assert [row[0] for row in conn.execute(
        "SELECT version FROM schema_version ORDER BY version")] == versions_after_first
    _assert_required_objects_present(conn)
    _assert_rows_preserved(conn, snapshot)


# ---------------------------------------------------------------------------
# 2. 兼容修复（repair_branch_schema）的边界
# ---------------------------------------------------------------------------


def test_repair_branch_schema_backfills_objects_without_writing_version_or_data(make_db):
    conn = make_db("cv")
    snapshot = _seed(conn)
    version_rows_before = [
        (row[0], row[1]) for row in conn.execute(
            "SELECT version, applied_at FROM schema_version ORDER BY version")
    ]
    assert repair_branch_schema(conn) is True
    assert current_version(conn) == 55, "兼容修复不得推进版本号"
    assert [
        (row[0], row[1]) for row in conn.execute(
            "SELECT version, applied_at FROM schema_version ORDER BY version")
    ] == version_rows_before, "兼容修复不得写 schema_version"
    _assert_required_objects_present(conn)
    _assert_rows_preserved(conn, snapshot)
    assert apply_migrations(conn) == 70


def test_complete_objects_do_not_replay_compat_repair(make_db, caplog):
    """对象已齐的 66 库（模拟 67..70 版本行丢失的崩溃中间态）不应再跑兼容修复。"""
    conn = make_db("empty")
    assert apply_migrations(conn) == 70
    conn.execute("DELETE FROM schema_version WHERE version IN (67, 68, 69, 70)")
    conn.commit()
    assert current_version(conn) == 66
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="agent.storage.migrate"):
        assert apply_migrations(conn) == 70
    assert "兼容修复" not in caplog.text, "对象齐全时不应触发兼容修复"
    _assert_required_objects_present(conn)


def test_main_51_degraded_attachments_is_repaired_by_lower_bound(make_db):
    """main 51 缺 main 时代必需表（attachments）：下界 50 的兼容修复要能自愈。"""
    conn = make_db("main")
    snapshot = _seed(conn)
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("DROP TABLE attachments")
    conn.commit()
    conn.execute("PRAGMA foreign_keys = ON")
    assert apply_migrations(conn) == 70
    _assert_required_objects_present(conn)
    assert _columns(conn, "attachments"), "attachments 应被补偿重建"
    _assert_rows_preserved(conn, snapshot, exclude={"attachments"})


# ---------------------------------------------------------------------------
# 3. 版本号已登记但必需对象缺失 → 对象级复核 + 补偿重放
# ---------------------------------------------------------------------------


def test_version_70_missing_required_tables_are_replayed(make_db):
    conn = make_db("empty")
    assert apply_migrations(conn) == 70
    snapshot = _seed(conn)
    dropped: set[str] = set()
    for table in ("task_ack", "user_requirements", "internal_data_identities"):
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute(f"DROP TABLE {table}")
        conn.commit()
        conn.execute("PRAGMA foreign_keys = ON")
        dropped.add(table)
        assert apply_migrations(conn) == 70
        _assert_required_objects_present(conn)
        assert _columns(conn, table), f"{table} 应被补偿重放重建"
        # 被删表的行丢失是本用例自己造成的（不是迁移改数据）；其它表必须逐行保留。
        _assert_rows_preserved(conn, snapshot, exclude=dropped)


def test_branch_db_missing_page_tables_is_repaired_before_upgrade(make_db):
    conn = make_db("i2")
    snapshot = _seed(conn)
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("DROP TABLE task_feed")
    conn.execute("DROP TABLE board_intent_turns")
    conn.commit()
    conn.execute("PRAGMA foreign_keys = ON")
    assert apply_migrations(conn) == 70
    _assert_required_objects_present(conn)
    assert _columns(conn, "task_feed") and _columns(conn, "board_intent_turns")
    _assert_rows_preserved(conn, snapshot, exclude={"task_feed", "board_intent_turns"})


# ---------------------------------------------------------------------------
# 4. 关键外键 / 唯一约束 / 部分唯一索引在升级后可用
# ---------------------------------------------------------------------------


def test_constraints_are_usable_after_upgrade(make_db):
    conn = make_db("empty")
    assert apply_migrations(conn) == 70
    _seed(conn)

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO board_intent_turns (intent_id, board_id, created_at, updated_at)"
            " VALUES ('no-such-intent', 'board-seed-1', ?, ?)", (TS, TS))

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO board_intent_retries (id, series_id, board_id, intent_id,"
            " created_at, updated_at) VALUES ('retry-dup', 'series-seed-1',"
            " 'board-seed-1', 'intent-seed-1', ?, ?)", (TS, TS))

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO board_submissions (id, board_id, seq, status, created_at,"
            " client_request_id) VALUES ('sub-dup', 'board-seed-1', 99, 'applied', ?,"
            " 'req-seed-1')", (TS,))


# ---------------------------------------------------------------------------
# 5. 统一补偿的副作用边界
# ---------------------------------------------------------------------------


def test_unified_compensation_is_additive_ddl_only():
    """补偿 70 不启动工具 / 不重授权 / 不消费审批 / 不重置重试：只有建对象 + 1 条幂等 seed。"""
    comp = [str(stmt) for target, statements in MIGRATIONS
            if int(target) == COMPENSATION_VERSION for stmt in statements]
    heads = [" ".join(stmt.split()).upper() for stmt in comp]
    assert heads, f"{CLAUSE}：补偿 {COMPENSATION_VERSION} 没有语句"
    for head in heads:
        assert not head.startswith(("UPDATE", "DELETE", "DROP", "PRAGMA", "TRUNCATE")), (
            f"{CLAUSE}：补偿含改/删数据语句：{head[:80]}")
        if head.startswith("INSERT"):
            assert head.startswith("INSERT OR IGNORE"), (
                f"{CLAUSE}：补偿里的 INSERT 必须幂等：{head[:80]}")
    inserts = [head for head in heads if head.startswith("INSERT")]
    assert len(inserts) == 1 and "KNOWLEDGE_MAINTENANCE_STATE" in inserts[0], (
        f"{CLAUSE}：补偿只应有 1 条继承的幂等单行 seed：{inserts}")
    blob = "\n".join(comp)
    assert "structured_limits_state = 'unresolved'" not in blob
    assert "revoked_at = COALESCE(revoked_at" not in blob
    for token in ("task_feed", "task_ack", "board_intent_turns", "board_intent_retries",
                  "user_requirements", "internal_data_identities"):
        assert token in blob, f"{CLAUSE}：统一补偿缺少页面线/R9 对象：{token}"
    assert "board_intents" in blob and "ADD COLUMN display" in blob, (
        f"{CLAUSE}：统一补偿缺少 P3-D 的 board_intents.display 列")
    for index in ("idx_task_feed_updated", "idx_task_feed_source",
                  "idx_board_intent_turns_turn", "idx_board_intent_retries_intent"):
        assert index in blob, f"{CLAUSE}：统一补偿缺少页面线索引：{index}"


def test_intent_and_retry_counts_are_not_reset_by_upgrade(make_db):
    """迁移不得重置意图尝试/重试次数（i2 57 库升级后原值不变）。"""
    conn = make_db("i2")
    _seed(conn)
    before = {
        "intent": dict(conn.execute(
            "SELECT attempt, version FROM board_intents WHERE id='intent-seed-1'").fetchone()),
        "retry": dict(conn.execute(
            "SELECT attempts, manual_attempts, retry_limit FROM board_intent_retries"
            " WHERE id='retry-seed-1'").fetchone()),
    }
    assert apply_migrations(conn) == 70
    after = {
        "intent": dict(conn.execute(
            "SELECT attempt, version FROM board_intents WHERE id='intent-seed-1'").fetchone()),
        "retry": dict(conn.execute(
            "SELECT attempts, manual_attempts, retry_limit FROM board_intent_retries"
            " WHERE id='retry-seed-1'").fetchone()),
    }
    assert after == before
