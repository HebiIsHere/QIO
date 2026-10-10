"""A03 受控验收：孤立重发记录可见、可修复、可忽略，且「已忽略」不再是孤儿形状。

基线形状问题（本 worktree 实测）：`TurnJournal.dismiss()` 以前只写 `recovered_at`，
于是「用户已忽略」和「抢占成功但没有后继」在库里**完全一样**
（`recovered_at` 非空 + `recovered_by` 空）。后果有两个：

1. 用户刚点过「知道了」的记录，会被新的收件箱当成孤儿再列出来；
2. `repair_orphan()` 能把它「修复」回可重发 —— 用户明确说过的「知道了」被推翻，
   那条消息会被再执行一次。

修好之后：`dismiss` 写成 `status = 'dismissed'` 终态，孤儿只由
「抢占过（recovered_at 非空）+ 后继为空（recovered_by 空）」这一精确形状定义，
`repair` 只作用于它，已真正重发过的行必须被拒绝。
"""

from __future__ import annotations

import pytest

from agent.services.recovery import (
    ACTION_CONTINUE,
    ACTION_IGNORE,
    ACTION_REPAIR,
    STATE_ORPHAN,
    STATE_READY,
    RecoveryConflict,
    RecoveryInbox,
)
from agent.storage.turn_journal import (
    DISMISSED,
    INTERRUPTED,
    TurnJournal,
)

from test_fu_w1_support import (
    FakeTurns,
    add_instance,
    add_turn,
    confirm_legacy_stopped,
    migrated_conn,
    register_self,
    table_snapshot,
)

SEED_TIME = "2026-01-01T00:00:00+00:00"


def _classes(inbox: RecoveryInbox) -> dict[str, str]:
    return {record.record_id: record.state_class for record in inbox.list_records().records}


def _row(conn, turn_id: str):
    return conn.execute("SELECT * FROM turn_journal WHERE turn_id = ?", (turn_id,)).fetchone()


# -- 可见性与分类 -------------------------------------------------------------


def test_orphan_is_visible_and_classified(tmp_path):
    """孤儿必须在同一份清单里可见，并且类名与动作都要说明「先修复」。"""
    conn = migrated_conn(tmp_path)
    registry = register_self(conn, "me")
    add_instance(conn, "gone", exited=True, pid=111)
    add_turn(
        conn,
        "turn_orphan",
        message="抢占之后崩溃的消息",
        status="interrupted",
        owner="gone",
        recovered_at=SEED_TIME,
        recovered_by=None,
    )
    journal = TurnJournal(conn, instance_id="me", registry=registry)
    inbox = RecoveryInbox(conn, registry, turns=FakeTurns(), instance_id="me", journal=journal)

    assert _classes(inbox) == {"turn_orphan": STATE_ORPHAN}
    record = inbox.list_records().records[0]
    assert record.message == "抢占之后崩溃的消息"
    assert record.status == INTERRUPTED
    actions = {action.id: action for action in record.actions}
    assert actions[ACTION_REPAIR].enabled is True
    assert actions[ACTION_CONTINUE].enabled is False
    assert "先修复" in actions[ACTION_CONTINUE].reason
    assert actions[ACTION_IGNORE].enabled is True

    # 与既有出口一致：老的 orphaned_claims() 仍然能看到同一条（口径不打架）
    assert [row["turn_id"] for row in journal.orphaned_claims()] == ["turn_orphan"]
    conn.close()


def test_orphan_repair_then_continue(tmp_path):
    """真实形状的孤立 claim：repair → 回到可继续 → 继续成功，且只产生一个后继。"""
    conn = migrated_conn(tmp_path)
    registry = register_self(conn, "me")
    add_turn(
        conn,
        "turn_orphan",
        message="修复之后才能继续",
        status="interrupted",
        owner=None,
        recovered_at=SEED_TIME,
        recovered_by=None,
    )
    turns = FakeTurns()
    journal = TurnJournal(conn, instance_id="me", registry=registry)
    inbox = RecoveryInbox(conn, registry, turns=turns, instance_id="me", journal=journal)

    # F01：无归属的孤儿同样要用户先确认「旧执行者已停止」（修复会把它变回可继续）。
    confirm_legacy_stopped(conn, "user_turn", "turn_orphan")
    repaired = inbox.repair_orphan("turn_orphan", expected_class=STATE_ORPHAN)
    assert repaired == {"ok": True, "repaired": True, "record_id": "turn_orphan"}
    row = _row(conn, "turn_orphan")
    assert row["recovered_at"] is None and row["status"] == INTERRUPTED
    assert row["message"] == "修复之后才能继续", "修复不改消息原文、不新建 turn"
    assert _classes(inbox) == {"turn_orphan": STATE_READY}

    result = inbox.take_over_and_continue(
        "turn_orphan", expected_class=STATE_READY, expected_status=INTERRUPTED
    )
    assert result["ok"] is True
    assert turns.submitted == [result["turn_id"]]
    assert _row(conn, "turn_orphan")["recovered_by"] == result["turn_id"]
    assert journal.orphaned_claims() == []
    conn.close()


def test_repair_refuses_a_record_that_was_really_resent(tmp_path):
    """已经真正重发过（有关联后继）的行不许被修复回可重发 —— 否则会执行两次。"""
    conn = migrated_conn(tmp_path)
    registry = register_self(conn, "me")
    add_turn(
        conn,
        "turn_resent",
        status="interrupted",
        owner=None,
        recovered_at=SEED_TIME,
        recovered_by="turn_successor",
    )
    journal = TurnJournal(conn, instance_id="me", registry=registry)
    inbox = RecoveryInbox(conn, registry, turns=FakeTurns(), instance_id="me", journal=journal)
    before = table_snapshot(conn, "turn_journal")

    result = inbox.repair_orphan("turn_resent", expected_class=STATE_ORPHAN)
    assert result["ok"] is False and result["repaired"] is False
    assert "真正重发" in result["reason"]
    assert table_snapshot(conn, "turn_journal") == before, "拒绝时一行都不改"

    # 台账层同样拒绝（不只是接口层的判断）
    assert journal.repair_orphan("turn_resent", "me") is False
    assert journal.recoverable("turn_resent") is None
    assert table_snapshot(conn, "turn_journal") == before
    conn.close()


def test_repair_refuses_wrong_class_and_other_shapes(tmp_path):
    conn = migrated_conn(tmp_path)
    registry = register_self(conn, "me")
    add_turn(conn, "turn_ready", status="interrupted", owner=None)
    add_turn(conn, "turn_done", status="completed", owner=None)
    add_turn(conn, "turn_notify", status="interrupted", notify=1, owner=None)
    inbox = RecoveryInbox(conn, registry, turns=FakeTurns(), instance_id="me")

    assert inbox.repair_orphan("turn_orphan", expected_class=STATE_READY)["ok"] is False
    assert inbox.repair_orphan("turn_ready", expected_class=STATE_ORPHAN)["ok"] is False
    assert inbox.repair_orphan("turn_done", expected_class=STATE_ORPHAN)["ok"] is False
    assert inbox.repair_orphan("turn_notify", expected_class=STATE_ORPHAN)["ok"] is False
    assert inbox.repair_orphan("turn_missing", expected_class=STATE_ORPHAN)["ok"] is False
    conn.close()


# -- 忽略：终态、原文保留、不产生后继 ---------------------------------------


def test_ignore_orphan_is_terminal_and_no_longer_an_orphan(tmp_path):
    conn = migrated_conn(tmp_path)
    registry = register_self(conn, "me")
    add_turn(
        conn,
        "turn_orphan",
        message="用户决定不继续这条",
        status="interrupted",
        owner=None,
        recovered_at=SEED_TIME,
        recovered_by=None,
    )
    journal = TurnJournal(conn, instance_id="me", registry=registry)
    inbox = RecoveryInbox(conn, registry, turns=FakeTurns(), instance_id="me", journal=journal)

    confirm_legacy_stopped(conn, "user_turn", "turn_orphan")
    result = inbox.ignore("turn_orphan", expected_class=STATE_ORPHAN)
    assert result == {"ok": True, "ignored": True, "record_id": "turn_orphan"}

    row = _row(conn, "turn_orphan")
    assert row["status"] == DISMISSED
    assert row["recovered_at"], "既有 dismiss 语义：留下处理时间"
    assert not row["recovered_by"], "不产生后继"
    assert row["message"] == "用户决定不继续这条", "不删除原消息"

    # 关键回归：用户忽略过的记录，不会再被当成孤儿列出来
    assert journal.orphaned_claims() == []
    assert inbox.list_records().records == ()
    assert inbox.repair_orphan("turn_orphan", expected_class=STATE_ORPHAN)["ok"] is False
    again = inbox.ignore("turn_orphan", expected_class=STATE_ORPHAN)
    assert again["ok"] is False and "忽略过" in again["reason"]
    conn.close()


def test_dismissed_record_cannot_be_repaired_or_resent(tmp_path):
    """老的 `/api/turns/{id}/dismiss` 路径同样不再制造「孤儿形状」。"""
    conn = migrated_conn(tmp_path)
    journal = TurnJournal(conn)
    add_turn(conn, "turn_ack", status="interrupted", owner=None)
    assert journal.recoverable("turn_ack") is not None

    assert journal.dismiss("turn_ack") is True
    assert journal.dismiss("turn_ack") is False, "知道了只成功一次"
    row = _row(conn, "turn_ack")
    assert row["status"] == DISMISSED and row["recovered_at"]
    assert journal.unfinished() == []
    assert journal.orphaned_claims() == []
    assert journal.repair_orphan("turn_ack") is False
    assert journal.claim_for_resend("turn_ack", "turn_ack_new") is False
    assert (
        conn.execute("SELECT 1 FROM turn_journal WHERE turn_id = 'turn_ack_new'").fetchone()
        is None
    )
    conn.close()


def test_ignore_legacy_unowned_takes_over_first_without_successor(tmp_path):
    conn = migrated_conn(tmp_path)
    registry = register_self(conn, "me")
    add_turn(conn, "turn_legacy", status="queued", owner=None)
    journal = TurnJournal(conn, instance_id="me", registry=registry)
    inbox = RecoveryInbox(conn, registry, turns=FakeTurns(), instance_id="me", journal=journal)

    confirm_legacy_stopped(conn, "user_turn", "turn_legacy")
    result = inbox.ignore("turn_legacy", expected_class="legacy_unowned")
    assert result["ok"] is True
    row = _row(conn, "turn_legacy")
    assert row["status"] == DISMISSED
    assert row["owner_instance_id"] == "me", "先接管再标记知晓"
    owner = conn.execute(
        "SELECT instance_id FROM record_owners WHERE record_type = 'turn' AND record_id = ?",
        ("turn_legacy",),
    ).fetchone()
    assert owner["instance_id"] == "me"
    total = conn.execute("SELECT COUNT(*) c FROM turn_journal").fetchone()["c"]
    assert total == 1, "忽略不产生后继 turn"
    conn.close()


def test_ignore_refuses_unknown_owner_without_changing_anything(tmp_path):
    conn = migrated_conn(tmp_path)
    registry = register_self(conn, "me")
    add_turn(conn, "turn_ghost", status="queued", owner="ghost")
    before = table_snapshot(conn, "turn_journal")
    inbox = RecoveryInbox(conn, registry, turns=FakeTurns(), instance_id="me")

    result = inbox.ignore("turn_ghost", expected_class="owner_unknown")
    assert result["ok"] is False and "无法确认" in result["reason"]
    assert table_snapshot(conn, "turn_journal") == before
    conn.close()


def test_ignore_refuses_alive_owner(tmp_path):
    conn = migrated_conn(tmp_path)
    registry = register_self(conn, "me")
    add_instance(conn, "live", exited=False, pid=222)
    add_turn(conn, "turn_live", status="running", owner="live")
    before = table_snapshot(conn, "turn_journal")
    inbox = RecoveryInbox(conn, registry, turns=FakeTurns(), instance_id="me")

    result = inbox.ignore("turn_live", expected_class=STATE_READY)
    assert result["ok"] is False and "还在运行" in result["reason"]
    assert table_snapshot(conn, "turn_journal") == before
    conn.close()
