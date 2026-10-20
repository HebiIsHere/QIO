# -*- coding: utf-8 -*-
"""P2-A / 变化记录：record_effective_change 的契约（契约 §12.3）。

验收点：
* 正式生效的新增 / 实质修改记录一次，重复调用幂等（UNIQUE(chain_id, version)）；
* 维护自身来源（MAINTENANCE_ORIGINS）不计数、不形成循环触发；
* 空 id / 空 chain / version <= 0 不记录；
* created_turn_seq 取记录时调度状态里的 turn_seq（等待轮数按最老一条算的前提）；
* 跨重启后记录仍在，且重复版本仍判定为「已记录」。
"""

from __future__ import annotations

from agent.maintenance.changes import record_effective_change
from agent.maintenance.models import MAINTENANCE_ORIGINS
from agent.storage.db import connect, transaction

from test_p2_a_support import migrated_conn, pending_rows, state_row


def _record(conn, index: int = 1, **overrides):
    payload = {
        "knowledge_id": f"kid-{index}",
        "chain_id": f"chain-{index}",
        "version": 1,
        "source": "background_extraction",
    }
    payload.update(overrides)
    return record_effective_change(conn, **payload)


def test_records_active_change_with_contract_fields(tmp_path):
    conn = migrated_conn(tmp_path)
    try:
        assert _record(conn, occurred_at="2026-10-21T04:05:06+00:00") is True
        rows = pending_rows(conn)
        assert len(rows) == 1
        row = rows[0]
        assert row["knowledge_id"] == "kid-1"
        assert row["chain_id"] == "chain-1"
        assert int(row["version"]) == 1
        assert row["source"] == "background_extraction"
        assert row["occurred_at"] == "2026-10-21T04:05:06+00:00"
        assert int(row["created_turn_seq"]) == 0
        assert row["consumed_batch_seq"] is None
        assert int(row["seq"]) >= 1
    finally:
        conn.close()


def test_same_chain_version_is_idempotent(tmp_path):
    conn = migrated_conn(tmp_path)
    try:
        assert _record(conn, 1) is True
        # 同一条链的同一版本（即使 knowledge_id 变了）不再记第二行。
        assert _record(conn, 1, knowledge_id="kid-other") is False
        assert len(pending_rows(conn)) == 1
        # 同一链的下一版本是新的实质修改：记录。
        assert _record(conn, 1, knowledge_id="kid-1-v2", version=2) is True
        assert len(pending_rows(conn)) == 2
    finally:
        conn.close()


def test_maintenance_origins_are_not_counted(tmp_path):
    conn = migrated_conn(tmp_path)
    try:
        assert MAINTENANCE_ORIGINS  # 冻结集合非空
        for index, origin in enumerate(sorted(MAINTENANCE_ORIGINS), start=1):
            assert _record(conn, index, source=origin) is False
        assert pending_rows(conn) == []
        # 后台提炼不属于维护自身写入：必须计数（否则后台新增知识等不到维护）。
        assert _record(conn, 99, source="background_extraction") is True
        assert len(pending_rows(conn)) == 1
    finally:
        conn.close()


def test_maintenance_origin_match_ignores_surrounding_space(tmp_path):
    conn = migrated_conn(tmp_path)
    try:
        origin = sorted(MAINTENANCE_ORIGINS)[0]
        assert _record(conn, 1, source=f"  {origin}  ") is False
        assert pending_rows(conn) == []
    finally:
        conn.close()


def test_invalid_inputs_are_not_recorded(tmp_path):
    conn = migrated_conn(tmp_path)
    try:
        assert _record(conn, 1, knowledge_id="") is False
        assert _record(conn, 2, knowledge_id="   ") is False
        assert _record(conn, 3, chain_id="") is False
        assert _record(conn, 4, chain_id="  ") is False
        assert _record(conn, 5, version=0) is False
        assert _record(conn, 6, version=-3) is False
        assert _record(conn, 7, version="not-a-number") is False
        assert pending_rows(conn) == []
    finally:
        conn.close()


def test_occurred_at_defaults_to_now(tmp_path):
    conn = migrated_conn(tmp_path)
    try:
        assert _record(conn, 1) is True
        stamp = pending_rows(conn)[0]["occurred_at"]
        assert isinstance(stamp, str) and "T" in stamp  # ISO 时间
    finally:
        conn.close()


def test_created_turn_seq_follows_state_turn_seq(tmp_path):
    conn = migrated_conn(tmp_path)
    try:
        assert _record(conn, 1) is True
        assert int(pending_rows(conn)[0]["created_turn_seq"]) == 0
        # 有 1 条待处理变化时，真实成功用户 turn 让 turn_seq 前进到 1。
        from agent.maintenance.scheduler import KnowledgeMaintenanceScheduler

        scheduler = KnowledgeMaintenanceScheduler(conn, runner=object())
        scheduler.note_user_turn_completed("turn-1")
        assert int(state_row(conn)["turn_seq"]) == 1
        assert _record(conn, 2) is True
        second = pending_rows(conn)[1]
        assert int(second["created_turn_seq"]) == 1
    finally:
        conn.close()


def test_change_record_joins_outer_domain_transaction(tmp_path):
    """B 的领域写事务包裹调用时：领域回滚，变化记录也回滚（同生共死）。"""
    conn = migrated_conn(tmp_path)
    try:
        try:
            with transaction(conn):
                assert _record(conn, 1) is True
                raise RuntimeError("domain write failed")
        except RuntimeError:
            pass
        assert pending_rows(conn) == []

        with transaction(conn):
            assert _record(conn, 1) is True
        assert len(pending_rows(conn)) == 1
    finally:
        conn.close()


def test_records_and_idempotency_survive_restart(tmp_path):
    path = tmp_path / "p2a.db"
    conn = migrated_conn(tmp_path)
    assert _record(conn, 1) is True
    conn.close()

    reopened = connect(path)
    try:
        assert len(pending_rows(reopened)) == 1
        # 重启后重复的版本仍由 UNIQUE 判定为已记录。
        assert _record(reopened, 1) is False
        assert len(pending_rows(reopened)) == 1
        # 状态行也还在（迁移 46 插入的那一行）。
        assert int(state_row(reopened)["id"]) == 1
    finally:
        reopened.close()
