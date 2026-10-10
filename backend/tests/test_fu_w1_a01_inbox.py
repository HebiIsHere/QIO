"""A01 受控验收：升级前无归属的未完成记录必须可见、可继续、只执行一次。

基线反例（本 worktree 实测）：`AppContext` 用带归属表的 registry 调
`interrupt_stale()` 时，无归属的 `queued` / `running` 历史行走 unknown 分支被
**保守保留**：`unfinished()` 看不到（它要求 `interrupted`）、`orphaned_claims()`
也没有 —— 消息原文在库里，用户在任何一个出口都点不到。

修好之后：`RecoveryInbox.list_records()` 把它们标成 `legacy_unowned` 列出来；
`take_over_and_continue()` 用条件 UPDATE 精确接管「无归属」这一种状态，
再沿用 `claim_for_resend()`（单事务 + 一次性），最后才 submit。
全部受控：假派发替身或最小 runner，不联网、不真实模型、不结束真实进程。
"""

from __future__ import annotations

import asyncio

import pytest

from agent.core.turn import TurnManager
from agent.services.recovery import (
    ACTION_CONTINUE,
    ACTION_IGNORE,
    ACTION_REQUEUE,
    KIND_DERIVED_TASK,
    KIND_USER_TURN,
    STATE_DERIVED_LEGACY,
    STATE_DERIVED_STALE,
    STATE_LEGACY_UNOWNED,
    STATE_ORPHAN,
    STATE_OWNER_UNKNOWN,
    STATE_READY,
    RecoveryConflict,
    RecoveryDispatchError,
    RecoveryInbox,
)
from agent.storage.instance_registry import InstanceRegistry
from agent.storage.turn_journal import TurnJournal

from test_fu_w1_support import (
    HOST,
    FakeTurns,
    add_derived,
    add_instance,
    add_turn,
    confirm_legacy_stopped,
    migrated_conn,
    register_self,
    shadow_conn,
    table_snapshot,
)

SEED_TIME = "2026-01-01T00:00:00+00:00"


def _classes(inbox: RecoveryInbox, **kwargs) -> dict[str, str]:
    listing = inbox.list_records(**kwargs)
    return {record.record_id: record.state_class for record in listing.records}


# -- 清单：分类与可见性 -------------------------------------------------------


def test_listing_separates_historical_states(tmp_path):
    """同一份库里七种形状并存：每种都被分到正确的类，坏的形状不被当成好的。"""
    conn = migrated_conn(tmp_path)
    registry = register_self(conn, "me")
    add_instance(conn, "gone", exited=True, pid=111)
    add_instance(conn, "live", exited=False, pid=222)

    add_turn(conn, "turn_ready", status="interrupted", owner="gone", created_at=SEED_TIME)
    add_turn(conn, "turn_legacy", status="queued", owner=None, created_at=SEED_TIME)
    add_turn(conn, "turn_ghost", status="queued", owner="ghost", created_at=SEED_TIME)
    add_turn(
        conn,
        "turn_orphan",
        status="interrupted",
        owner="gone",
        recovered_at=SEED_TIME,
        recovered_by=None,
        created_at=SEED_TIME,
    )
    add_derived(conn, "task_stale", owner="gone", created_at=SEED_TIME)
    add_derived(conn, "task_legacy", owner=None, created_at=SEED_TIME)

    inbox = RecoveryInbox(conn, registry, turns=FakeTurns(), instance_id="me")
    classes = _classes(inbox)

    assert classes["turn_ready"] == STATE_READY, "归属者确认退出的 interrupted 行 = 已确认可恢复"
    assert classes["turn_legacy"] == STATE_LEGACY_UNOWNED, "无归属的历史 queued 行必须可见"
    assert classes["turn_ghost"] == STATE_OWNER_UNKNOWN, "判不出来归属的行要如实单独表达"
    assert classes["turn_orphan"] == STATE_ORPHAN
    assert classes["task_stale"] == STATE_DERIVED_STALE
    assert classes["task_legacy"] == STATE_DERIVED_LEGACY
    assert "turn_live" not in classes and "task_live" not in classes
    conn.close()


def test_listing_excludes_alive_completed_notify_resent_and_dismissed(tmp_path):
    conn = migrated_conn(tmp_path)
    registry = register_self(conn, "me")
    add_instance(conn, "live", exited=False, pid=222)
    add_instance(conn, "gone", exited=True, pid=111)

    add_turn(conn, "turn_live", status="running", owner="live")
    add_derived(conn, "task_live", owner="live")
    add_turn(conn, "turn_done", status="completed", owner="gone")
    add_turn(conn, "turn_notify", status="interrupted", notify=1, owner="gone")
    add_turn(
        conn,
        "turn_resent",
        status="interrupted",
        owner="gone",
        recovered_at=SEED_TIME,
        recovered_by="turn_successor",
    )
    add_turn(
        conn,
        "turn_dismissed",
        status="dismissed",
        owner="gone",
        recovered_at=SEED_TIME,
    )

    inbox = RecoveryInbox(conn, registry, turns=FakeTurns(), instance_id="me")
    assert inbox.list_records().records == (), (
        "活实例的记录、已完成、系统通知轮、已真正重发、已忽略的记录都不进清单"
    )
    conn.close()


def test_listing_is_read_only(tmp_path):
    """清单是只读的：unknown 绝不被批量改成死 / interrupted，什么都不动。"""
    conn = migrated_conn(tmp_path)
    registry = register_self(conn, "me")
    add_turn(conn, "turn_legacy", status="queued", owner=None)
    add_turn(conn, "turn_ghost", status="running", owner="ghost")
    add_derived(conn, "task_legacy", owner=None)
    before_turns = table_snapshot(conn, "turn_journal")
    before_tasks = table_snapshot(conn, "derived_tasks")
    before_owners = table_snapshot(conn, "record_owners")

    inbox = RecoveryInbox(conn, registry, turns=FakeTurns(), instance_id="me")
    for _ in range(3):
        inbox.list_records()

    assert table_snapshot(conn, "turn_journal") == before_turns
    assert table_snapshot(conn, "derived_tasks") == before_tasks
    assert table_snapshot(conn, "record_owners") == before_owners
    conn.close()


def test_unactionable_records_are_listed_with_a_reason(tmp_path):
    """动不了的记录照旧列出，但动作必须 enabled=False + 一句给用户看的原因。"""
    conn = migrated_conn(tmp_path)
    registry = register_self(conn, "me")
    add_turn(conn, "turn_ghost", status="queued", owner="ghost")
    add_derived(conn, "task_ghost", owner="ghost")

    inbox = RecoveryInbox(conn, registry, turns=FakeTurns(), instance_id="me")
    records = {record.record_id: record for record in inbox.list_records().records}

    ghost = records["turn_ghost"]
    assert ghost.state_class == STATE_OWNER_UNKNOWN
    by_id = {action.id: action for action in ghost.actions}
    assert by_id[ACTION_CONTINUE].enabled is False
    assert by_id[ACTION_IGNORE].enabled is False
    assert by_id[ACTION_CONTINUE].reason and "无法确认" in by_id[ACTION_CONTINUE].reason

    task = records["task_ghost"]
    assert task.state_class == STATE_DERIVED_LEGACY
    requeue = {action.id: action for action in task.actions}[ACTION_REQUEUE]
    assert requeue.enabled is False
    assert requeue.reason
    conn.close()


def test_listing_limit_reports_total_and_truncated(tmp_path):
    conn = migrated_conn(tmp_path)
    registry = register_self(conn, "me")
    for index in range(5):
        add_turn(
            conn,
            f"turn_{index}",
            status="interrupted",
            owner=None,
            created_at=f"2026-01-0{index + 1}T00:00:00+00:00",
        )

    inbox = RecoveryInbox(conn, registry, turns=FakeTurns(), instance_id="me")
    first = inbox.list_records(limit=2)
    assert (first.total, first.shown, first.truncated) == (5, 2, True)
    assert [record.record_id for record in first.records] == ["turn_4", "turn_3"], (
        "新的在前：用户最关心最近那次没跑完的"
    )
    # 未显示的记录没有消失：把 limit 抬上去就能全部取到
    full = inbox.list_records(limit=50)
    assert (full.total, full.shown, full.truncated) == (5, 5, False)
    assert {record.record_id for record in full.records} == {f"turn_{i}" for i in range(5)}
    conn.close()


def test_listing_filters_by_kind_and_class(tmp_path):
    conn = migrated_conn(tmp_path)
    registry = register_self(conn, "me")
    add_instance(conn, "gone", exited=True, pid=111)
    add_turn(conn, "turn_ready", status="interrupted", owner="gone")
    add_turn(conn, "turn_legacy", status="queued", owner=None)
    add_derived(conn, "task_stale", owner="gone")
    add_derived(conn, "task_legacy", owner=None)

    inbox = RecoveryInbox(conn, registry, turns=FakeTurns(), instance_id="me")
    assert set(_classes(inbox, kinds=[KIND_USER_TURN])) == {"turn_ready", "turn_legacy"}
    assert set(_classes(inbox, kinds="derived_task")) == {"task_stale", "task_legacy"}
    assert set(_classes(inbox, classes=[STATE_DERIVED_STALE])) == {"task_stale"}
    assert set(_classes(inbox, classes=[STATE_LEGACY_UNOWNED, STATE_READY])) == {
        "turn_legacy",
        "turn_ready",
    }
    assert inbox.list_records(kinds=["bogus"]).total == 0
    conn.close()


# -- 继续：实际执行 + 一次性 --------------------------------------------------


async def test_continue_legacy_unowned_executes_exactly_once(tmp_path):
    """历史无归属的 queued 行：继续 → 真的执行，且重复 / 并发只产生一个后继。"""
    conn = migrated_conn(tmp_path)
    registry = register_self(conn, "me")
    add_turn(conn, "turn_legacy", message="升级前排队的消息", status="queued", owner=None)

    executed: list[str] = []
    turns = TurnManager()

    async def runner(turn_ctx) -> None:  # noqa: ANN001 - 只记录执行
        executed.append(turn_ctx.turn_id)

    turns.set_runner(runner)
    inbox = RecoveryInbox(conn, registry, turns=turns, instance_id="me")
    # F01：无归属 = 库里没有证据说明旧执行者停了 —— 用户确认之后才允许继续。
    confirm_legacy_stopped(conn, KIND_USER_TURN, "turn_legacy")

    result = inbox.take_over_and_continue(
        "turn_legacy", expected_class=STATE_LEGACY_UNOWNED, expected_status="queued"
    )
    assert result["ok"] is True
    new_turn_id = result["turn_id"]
    assert new_turn_id != "turn_legacy"

    old = conn.execute(
        "SELECT * FROM turn_journal WHERE turn_id = 'turn_legacy'"
    ).fetchone()
    assert old["status"] == "interrupted"
    assert old["reason"] == "user_confirmed_takeover"
    assert old["owner_instance_id"] == "me"
    assert old["recovered_at"] and old["recovered_by"] == new_turn_id
    assert old["message"] == "升级前排队的消息", "消息原文一个字节都不能改"
    owners = {
        row["record_id"]: row["instance_id"]
        for row in conn.execute(
            "SELECT record_id, instance_id FROM record_owners WHERE record_type = 'turn'"
        ).fetchall()
    }
    assert owners["turn_legacy"] == "me"
    assert owners[new_turn_id] == "me"

    for _ in range(200):
        if executed:
            break
        await asyncio.sleep(0.01)
    assert executed == [new_turn_id], f"必须真的执行，且只执行一次；实际 {executed}"

    # 新 turn 属于活实例 → 不进清单（不能把自己刚提交的东西当成待办）
    assert new_turn_id not in _classes(inbox)

    # 重复点击：明确冲突，且不再产生后继
    with pytest.raises(RecoveryConflict):
        inbox.take_over_and_continue(
            "turn_legacy", expected_class=STATE_READY, expected_status="interrupted"
        )
    assert executed == [new_turn_id]
    rows = conn.execute(
        "SELECT COUNT(*) c FROM turn_journal WHERE turn_id != 'turn_legacy'"
    ).fetchone()["c"]
    assert rows == 1, "老记录 + 恰好一个新记录"
    conn.close()


def test_take_over_conditional_update_only_one_request_wins(tmp_path):
    """两个连接（两个请求 / 两个实例）抢同一条：条件 UPDATE 只有一个命中。"""
    conn = migrated_conn(tmp_path)
    add_turn(conn, "turn_race", status="queued", owner=None)
    other = shadow_conn(conn)
    try:
        journal_a = TurnJournal(conn, instance_id="a")
        journal_b = TurnJournal(other, instance_id="b")

        assert journal_a.take_over("turn_race", expected_status="queued") is True
        assert journal_b.take_over("turn_race", expected_status="queued") is False, (
            "状态已经变化，第二个请求必须一行都不改"
        )
        row = conn.execute(
            "SELECT status, owner_instance_id FROM turn_journal WHERE turn_id = 'turn_race'"
        ).fetchone()
        assert row["status"] == "interrupted" and row["owner_instance_id"] == "a"
        # 有归属且不是「确认已退出」的记录，任何人都不许再抢
        assert journal_b.take_over("turn_race", expected_status="interrupted") is False
    finally:
        other.close()
        conn.close()


def test_take_over_only_accepts_confirmed_dead_owner(tmp_path):
    conn = migrated_conn(tmp_path)
    add_instance(conn, "gone", exited=True, pid=111)
    add_turn(conn, "turn_dead", status="running", owner="gone")
    add_turn(conn, "turn_ghost", status="running", owner="ghost")
    journal = TurnJournal(conn)

    assert journal.take_over(
        "turn_dead", expected_status="running", instance_id="me", dead_owner="gone"
    ) is True
    assert journal.take_over(
        "turn_ghost", expected_status="running", instance_id="me", dead_owner=None
    ) is False, "归属判不出来的行绝不能被抢"
    ghost = conn.execute(
        "SELECT status, owner_instance_id FROM turn_journal WHERE turn_id = 'turn_ghost'"
    ).fetchone()
    assert ghost["status"] == "running" and ghost["owner_instance_id"] == "ghost"
    conn.close()


def test_two_inboxes_on_the_same_record_create_one_successor(tmp_path):
    """两个收件箱（两条连接）先后继续同一条：只有一个后继、一次派发。"""
    conn = migrated_conn(tmp_path)
    registry = register_self(conn, "me")
    add_turn(conn, "turn_once", message="只许执行一次", status="queued", owner=None)
    other = shadow_conn(conn)
    try:
        turns_a, turns_b = FakeTurns(), FakeTurns()
        inbox_a = RecoveryInbox(conn, registry, turns=turns_a, instance_id="me")
        inbox_b = RecoveryInbox(other, registry, turns=turns_b, instance_id="me")
        confirm_legacy_stopped(conn, KIND_USER_TURN, "turn_once")

        first = inbox_a.take_over_and_continue(
            "turn_once", expected_class=STATE_LEGACY_UNOWNED, expected_status="queued"
        )
        assert first["ok"] is True
        with pytest.raises(RecoveryConflict):
            inbox_b.take_over_and_continue(
                "turn_once", expected_class=STATE_READY, expected_status="interrupted"
            )
        assert turns_a.submitted == [first["turn_id"]]
        assert turns_b.calls == []
        rows = conn.execute(
            "SELECT COUNT(*) c FROM turn_journal WHERE turn_id != 'turn_once'"
        ).fetchone()["c"]
        assert rows == 1
    finally:
        other.close()
        conn.close()


def test_restart_keeps_the_legacy_record_operable(tmp_path):
    """重启（新实例 + 新台账）之后，历史无归属的记录仍然可见；确认旧执行者已停止后可继续。"""
    conn = migrated_conn(tmp_path)
    add_turn(conn, "turn_legacy", message="重启前就存在的消息", status="queued", owner=None)

    first = register_self(conn, "first")
    journal_first = TurnJournal(conn, instance_id="first", registry=first)
    journal_first.interrupt_stale(first)
    row = conn.execute(
        "SELECT status FROM turn_journal WHERE turn_id = 'turn_legacy'"
    ).fetchone()
    assert row["status"] == "queued", "没有归属的历史行被保守保留（不是被改坏）"
    assert journal_first.unfinished() == [], "老出口看不到它 —— 这正是缺陷"
    first.mark_clean_exit()

    second = InstanceRegistry(conn, "second", pid=4243, host=HOST)
    second.start()
    journal_second = TurnJournal(conn, instance_id="second", registry=second)
    journal_second.interrupt_stale(second)
    turns = FakeTurns()
    inbox = RecoveryInbox(
        conn, second, turns=turns, instance_id="second", journal=journal_second
    )

    assert _classes(inbox) == {"turn_legacy": STATE_LEGACY_UNOWNED}
    # 重启不会让「无归属」变成「已证明停止」：确认是持久化的（F01）。
    confirm_legacy_stopped(conn, KIND_USER_TURN, "turn_legacy", instance_id="second")
    result = inbox.take_over_and_continue(
        "turn_legacy", expected_class=STATE_LEGACY_UNOWNED, expected_status="queued"
    )
    assert result["ok"] is True
    assert turns.submitted == [result["turn_id"]]
    assert turns.calls[0]["message"] == "重启前就存在的消息"
    conn.close()


def test_continue_refuses_unknown_owner_and_changes_nothing(tmp_path):
    conn = migrated_conn(tmp_path)
    registry = register_self(conn, "me")
    add_turn(conn, "turn_ghost", status="queued", owner="ghost")
    before = table_snapshot(conn, "turn_journal")
    inbox = RecoveryInbox(conn, registry, turns=FakeTurns(), instance_id="me")

    with pytest.raises(RecoveryConflict) as excinfo:
        inbox.take_over_and_continue(
            "turn_ghost", expected_class=STATE_OWNER_UNKNOWN, expected_status="queued"
        )
    assert "无法确认" in str(excinfo.value)
    assert table_snapshot(conn, "turn_journal") == before
    conn.close()


def test_continue_without_a_dispatch_channel_changes_nothing(tmp_path):
    """没有执行通道时明确失败（不假装继续），而且这条记录一个字节都不动。"""
    conn = migrated_conn(tmp_path)
    registry = register_self(conn, "me")
    add_turn(conn, "turn_legacy", message="没有通道就别动我", status="queued", owner=None)
    before = table_snapshot(conn, "turn_journal")
    inbox = RecoveryInbox(conn, registry, instance_id="me")  # 没有 turns / submitter
    confirm_legacy_stopped(conn, KIND_USER_TURN, "turn_legacy")

    with pytest.raises(RecoveryDispatchError):
        inbox.take_over_and_continue(
            "turn_legacy", expected_class=STATE_LEGACY_UNOWNED, expected_status="queued"
        )
    assert table_snapshot(conn, "turn_journal") == before
    conn.close()


def test_notify_rows_are_never_listed_or_actionable(tmp_path):
    """系统通知轮（notify=1）永不进清单、永不可操作（四个动作都拒绝）。"""
    conn = migrated_conn(tmp_path)
    registry = register_self(conn, "me")
    add_turn(conn, "turn_notify", message="系统通知", status="interrupted", notify=1, owner=None)
    add_turn(
        conn,
        "turn_notify_orphan",
        message="系统通知（孤儿形状）",
        status="interrupted",
        notify=1,
        owner=None,
        recovered_at=SEED_TIME,
    )
    before = table_snapshot(conn, "turn_journal")
    inbox = RecoveryInbox(conn, registry, turns=FakeTurns(), instance_id="me")

    assert inbox.list_records().records == ()
    for record_id in ("turn_notify", "turn_notify_orphan"):
        with pytest.raises(RecoveryConflict):
            inbox.take_over_and_continue(record_id)
        assert inbox.ignore(record_id)["ok"] is False
        assert inbox.repair_orphan(record_id, expected_class=STATE_ORPHAN)["ok"] is False
    assert table_snapshot(conn, "turn_journal") == before
    conn.close()


def test_continue_refuses_completed_and_notify_rows(tmp_path):
    conn = migrated_conn(tmp_path)
    registry = register_self(conn, "me")
    add_turn(conn, "turn_done", status="completed", owner=None)
    add_turn(conn, "turn_notify", status="interrupted", notify=1, owner=None)
    inbox = RecoveryInbox(conn, registry, turns=FakeTurns(), instance_id="me")
    for record_id in ("turn_done", "turn_notify", "turn_missing"):
        with pytest.raises(RecoveryConflict):
            inbox.take_over_and_continue(record_id)
    conn.close()


def test_continue_uses_the_column_null_case_for_dead_owner(tmp_path):
    """升级前的行：归属只在归属表里，列上没有 —— 归属者确认退出时可以接管。"""
    conn = migrated_conn(tmp_path)
    registry = register_self(conn, "me")
    add_instance(conn, "gone", exited=True, pid=111)
    add_turn(conn, "turn_old", status="queued", owner="gone")
    conn.execute("UPDATE turn_journal SET owner_instance_id = NULL WHERE turn_id = 'turn_old'")

    inbox = RecoveryInbox(conn, registry, turns=FakeTurns(), instance_id="me")
    assert _classes(inbox)["turn_old"] == STATE_READY
    result = inbox.take_over_and_continue(
        "turn_old", expected_class=STATE_READY, expected_status="queued"
    )
    assert result["ok"] is True and result["turn_id"]
    conn.close()


# -- 派生任务重排 -------------------------------------------------------------


def test_requeue_derived_bumps_generation_and_keeps_attempts(tmp_path):
    conn = migrated_conn(tmp_path)
    registry = register_self(conn, "me")
    add_instance(conn, "gone", exited=True, pid=111)
    add_derived(
        conn,
        "task_stale",
        owner="gone",
        claim_generation=2,
        attempts=3,
        last_error="模型超时",
    )
    inbox = RecoveryInbox(conn, registry, turns=FakeTurns(), instance_id="me")
    assert _classes(inbox)["task_stale"] == STATE_DERIVED_STALE

    result = inbox.requeue_derived(
        "task_stale", expected_state="running", expected_generation=2
    )
    assert result == {"ok": True, "state": "pending", "record_id": "task_stale"}
    row = conn.execute("SELECT * FROM derived_tasks WHERE id = 'task_stale'").fetchone()
    assert row["state"] == "pending"
    assert row["run_after"] is None
    assert int(row["claim_generation"]) == 3, "代次必须 +1：迟到的写回由它挡住"
    assert int(row["attempts"]) == 3, "失败次数原样保留（不重置历史）"
    assert row["last_error"] == "模型超时"
    assert row["owner_instance_id"] == "me"
    # 旧归属清掉：这条任务回到队列，不再「在谁手上」
    assert (
        conn.execute(
            "SELECT 1 FROM record_owners WHERE record_type = 'derived_task' "
            "AND record_id = 'task_stale'"
        ).fetchone()
        is None
    )
    # 已经在目标状态：重复点击是冲突，不改任何东西
    again = inbox.requeue_derived("task_stale", expected_state="running", expected_generation=3)
    assert again["ok"] is False
    row2 = conn.execute("SELECT * FROM derived_tasks WHERE id = 'task_stale'").fetchone()
    assert int(row2["claim_generation"]) == 3
    conn.close()


def test_requeue_derived_refuses_alive_owner_and_wrong_generation(tmp_path):
    conn = migrated_conn(tmp_path)
    registry = register_self(conn, "me")
    add_instance(conn, "live", exited=False, pid=222)
    add_derived(conn, "task_live", owner="live", claim_generation=2)
    add_derived(conn, "task_ghost", owner="ghost", claim_generation=5)
    inbox = RecoveryInbox(conn, registry, turns=FakeTurns(), instance_id="me")

    alive = inbox.requeue_derived("task_live", expected_state="running", expected_generation=2)
    assert alive["ok"] is False and "还在运行" in alive["reason"]
    ghost = inbox.requeue_derived("task_ghost", expected_state="running", expected_generation=5)
    assert ghost["ok"] is False and "无法确认" in ghost["reason"]
    for task_id in ("task_live", "task_ghost"):
        row = conn.execute("SELECT * FROM derived_tasks WHERE id = ?", (task_id,)).fetchone()
        assert row["state"] == "running", "拒绝时一行都不改"

    # 归属者确认退出、但代次对不上 → 一样拒绝
    add_instance(conn, "gone", exited=True, pid=111)
    add_derived(conn, "task_dead", owner="gone", claim_generation=7)
    stale = inbox.requeue_derived("task_dead", expected_state="running", expected_generation=6)
    assert stale["ok"] is False and "代次" in stale["reason"]
    conn.close()


def test_requeue_derived_refuses_completed_and_missing(tmp_path):
    conn = migrated_conn(tmp_path)
    registry = register_self(conn, "me")
    add_derived(conn, "task_done", state="completed", owner=None)
    inbox = RecoveryInbox(conn, registry, turns=FakeTurns(), instance_id="me")
    assert inbox.requeue_derived("task_done", expected_state="completed", expected_generation=0)[
        "ok"
    ] is False
    assert inbox.requeue_derived("task_missing")["ok"] is False
    row = conn.execute("SELECT state FROM derived_tasks WHERE id = 'task_done'").fetchone()
    assert row["state"] == "completed"
    conn.close()
