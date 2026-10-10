"""F03 定向回归：后继持久化成功、派发失败后，必须在**当前运行**中可恢复。

缺陷现场（核查注入：后继事务之后、派发之前抛错）
-----------------------------------------------

    claim_for_resend() 成功（老记录 + 新记录 + 关联已经落库，后继 = queued + 本实例归属）
      ↓  submit() 抛错
    接口 503、后继却一直躺在库里

后果：后继既不进内存队列（没被派发），又因为「归属者还活着」不进恢复清单 ——
用户在当前进程里看不到、点不到，只能等应用退出后的重启恢复。

修好之后：

* 派发失败的后继被写成显式的 `dispatch_failed` 状态（同一个 turn_id、同一个后继）；
* 它出现在收件箱里，动作是「重新执行 / 知道了」；
* 重新执行**复用同一个 turn_id**：不会产生第二个后继，也不静默宣称已执行；
* 提交之前失败（事务回滚）的老路径完全不变：老记录仍然可重发、零后继。

全部受控：临时库 + 会抛错的派发替身，不联网、不调用真实模型。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent.api.recovery_routes import build_router
from agent.services.recovery import (
    ACTION_CONTINUE,
    ACTION_IGNORE,
    DISPATCH_FAILED_PREFIX,
    KIND_USER_TURN,
    STATE_DISPATCH_FAILED,
    STATE_LEGACY_UNOWNED,
    RecoveryConflict,
    RecoveryDispatchError,
    RecoveryInbox,
)
from agent.storage.turn_journal import (
    INTERRUPTED,
    QUEUED,
    JournalWriteError,
    TurnJournal,
)

from test_fu_w1_support import (
    FakeTurns,
    add_turn,
    confirm_legacy_stopped,
    migrated_conn,
    register_self,
)


class _FlakyTurns:
    """派发替身：前 `fail_times` 次 submit 抛错（模拟「没有派发通道 / 队列满了」）。"""

    def __init__(self, fail_times: int = 1) -> None:
        self.fail_times = int(fail_times)
        self.calls: list[str] = []

    def submit(self, message, topic_id=None, *, notify=False, intent_id=None, turn_id=None):  # noqa: ANN001, ANN202
        self.calls.append(str(turn_id))
        if self.fail_times > 0:
            self.fail_times -= 1
            raise RuntimeError("injected: dispatch channel unavailable")
        return SimpleNamespace(turn_id=str(turn_id), status="accepted")


def _journal(conn, instance_id: str = "me") -> TurnJournal:
    return TurnJournal(conn, instance_id=instance_id)


def _rows(conn, parent_id: str) -> list:
    return list(
        conn.execute(
            "SELECT * FROM turn_journal WHERE turn_id != ? ORDER BY created_at", (parent_id,)
        ).fetchall()
    )


def _row(conn, turn_id: str):
    return conn.execute("SELECT * FROM turn_journal WHERE turn_id = ?", (turn_id,)).fetchone()


def _inbox(conn, registry, turns, journal) -> RecoveryInbox:
    return RecoveryInbox(conn, registry, turns=turns, instance_id="me", journal=journal)


# ---------------------------------------------------------------------------
# 派发调用期间失败
# ---------------------------------------------------------------------------


def test_dispatch_failure_leaves_a_retryable_single_successor(tmp_path):
    conn = migrated_conn(tmp_path)
    registry = register_self(conn, "me")
    add_turn(conn, "turn_old", message="这条要继续", status="queued")
    confirm_legacy_stopped(conn, KIND_USER_TURN, "turn_old")
    turns = _FlakyTurns(fail_times=1)
    journal = _journal(conn)
    inbox = _inbox(conn, registry, turns, journal)

    with pytest.raises(RecoveryDispatchError):
        inbox.take_over_and_continue(
            "turn_old", expected_class=STATE_LEGACY_UNOWNED, expected_status=QUEUED
        )

    rows = _rows(conn, "turn_old")
    assert len(rows) == 1, "只允许一个持久后继（不能因为派发失败再造一个）"
    successor = rows[0]
    assert successor["status"] == INTERRUPTED
    assert str(successor["reason"]).startswith(DISPATCH_FAILED_PREFIX), successor["reason"]
    assert successor["recovered_by"] in (None, ""), "后继自己还没有后继"
    parent = _row(conn, "turn_old")
    assert parent["recovered_at"] and parent["recovered_by"] == successor["turn_id"]
    assert parent["message"] == "这条要继续", "原文一字不动"

    # 当前运行里就能看见它，并给出可点的重试动作
    record = next(r for r in inbox.list_records().records if r.record_id == successor["turn_id"])
    assert record.state_class == STATE_DISPATCH_FAILED
    assert record.status == INTERRUPTED
    actions = {a.id: a for a in record.actions}
    assert actions[ACTION_CONTINUE].enabled is True
    assert actions[ACTION_IGNORE].enabled is True

    # 重试：**同一个** turn_id，不产生第二个后继
    retried = inbox.take_over_and_continue(successor["turn_id"], expected_class=STATE_DISPATCH_FAILED)
    assert retried["ok"] is True
    assert retried["turn_id"] == successor["turn_id"], "重试必须复用同一个后继"
    assert turns.calls == [successor["turn_id"], successor["turn_id"]]
    assert len(_rows(conn, "turn_old")) == 1
    assert _row(conn, successor["turn_id"])["status"] == QUEUED, "重新派发后回到正常生命周期"
    assert _row(conn, successor["turn_id"])["reason"] is None
    conn.close()


def test_retried_successor_can_finish_its_lifecycle(tmp_path):
    """重试之后它就是一个正常的 queued turn：running / terminal 都能写进去。"""
    conn = migrated_conn(tmp_path)
    registry = register_self(conn, "me")
    add_turn(conn, "turn_old", message="跑完它", status="queued")
    confirm_legacy_stopped(conn, KIND_USER_TURN, "turn_old")
    turns = _FlakyTurns(fail_times=1)
    journal = _journal(conn)
    inbox = _inbox(conn, registry, turns, journal)

    with pytest.raises(RecoveryDispatchError):
        inbox.take_over_and_continue("turn_old")
    successor_id = _rows(conn, "turn_old")[0]["turn_id"]
    inbox.take_over_and_continue(successor_id)

    journal.running(successor_id)
    assert _row(conn, successor_id)["status"] == "running"
    journal.terminal(successor_id, "completed")
    assert _row(conn, successor_id)["status"] == "completed"
    conn.close()


def test_second_retry_conflict_does_not_dispatch_twice(tmp_path):
    """重试两次（并发/重复点击语义）：只有一个能改到行，另一次明确 409。"""
    conn = migrated_conn(tmp_path)
    registry = register_self(conn, "me")
    add_turn(conn, "turn_old", message="只许派发一次", status="queued")
    confirm_legacy_stopped(conn, KIND_USER_TURN, "turn_old")
    turns = _FlakyTurns(fail_times=1)
    journal = _journal(conn)
    inbox = _inbox(conn, registry, turns, journal)

    with pytest.raises(RecoveryDispatchError):
        inbox.take_over_and_continue("turn_old")
    successor_id = _rows(conn, "turn_old")[0]["turn_id"]

    assert inbox.take_over_and_continue(successor_id)["ok"] is True
    with pytest.raises(RecoveryConflict):
        inbox.take_over_and_continue(successor_id)
    # 撞上的是同一个后继：追加重试的重复点击不会再造后继、也不会多派发一次
    assert turns.calls == [successor_id, successor_id]
    assert len(_rows(conn, "turn_old")) == 1
    conn.close()


def test_ignore_works_on_a_dispatch_failed_successor(tmp_path):
    conn = migrated_conn(tmp_path)
    registry = register_self(conn, "me")
    add_turn(conn, "turn_old", message="不想再跑了", status="queued")
    confirm_legacy_stopped(conn, KIND_USER_TURN, "turn_old")
    turns = _FlakyTurns(fail_times=1)
    journal = _journal(conn)
    inbox = _inbox(conn, registry, turns, journal)

    with pytest.raises(RecoveryDispatchError):
        inbox.take_over_and_continue("turn_old")
    successor_id = _rows(conn, "turn_old")[0]["turn_id"]

    result = inbox.ignore(successor_id, expected_class=STATE_DISPATCH_FAILED)
    assert result == {"ok": True, "ignored": True, "record_id": successor_id}
    assert _row(conn, successor_id)["status"] == "dismissed"
    assert _row(conn, "turn_old")["message"] == "不想再跑了"
    assert inbox.list_records().records == ()
    conn.close()


# ---------------------------------------------------------------------------
# HTTP：503 不是「已继续」，重试入口就在当前运行里
# ---------------------------------------------------------------------------


def test_api_surfaces_the_failure_and_allows_retry(tmp_path):
    conn = migrated_conn(tmp_path)
    registry = register_self(conn, "me")
    add_turn(conn, "turn_old", message="503 之后要能重试", status="queued")
    confirm_legacy_stopped(conn, KIND_USER_TURN, "turn_old")
    turns = _FlakyTurns(fail_times=1)
    ctx = SimpleNamespace(
        conn=conn,
        instances=registry,
        instance_id="me",
        turns=turns,
        turn_journal=_journal(conn),
    )
    app = FastAPI()
    app.include_router(build_router(ctx))

    with TestClient(app) as client:
        first = client.post(
            "/api/recovery/records/turn_old/continue",
            json={"expected_class": STATE_LEGACY_UNOWNED, "expected_status": QUEUED},
        )
        assert first.status_code == 503, first.text
        body = first.json()
        assert body["ok"] is False and body["accepted"] is False

        rows = _rows(conn, "turn_old")
        assert len(rows) == 1
        successor_id = rows[0]["turn_id"]

        listing = client.get("/api/recovery/records").json()
        listed = {r["record_id"]: r for r in listing["records"]}
        assert listed[successor_id]["state_class"] == STATE_DISPATCH_FAILED
        assert any(
            a["id"] == ACTION_CONTINUE and a["enabled"] for a in listed[successor_id]["actions"]
        )

        retry = client.post(
            f"/api/recovery/records/{successor_id}/continue",
            json={"expected_class": STATE_DISPATCH_FAILED},
        )
        assert retry.status_code == 200, retry.text
        assert retry.json()["turn_id"] == successor_id
        assert turns.calls == [successor_id, successor_id]
        assert len(_rows(conn, "turn_old")) == 1
    conn.close()


# ---------------------------------------------------------------------------
# 提交之前失败：老路径不变（整体回滚，零后继，老记录仍可重发）
# ---------------------------------------------------------------------------


def test_failure_before_the_commit_leaves_no_successor(tmp_path):
    conn = migrated_conn(tmp_path)
    registry = register_self(conn, "me")
    add_turn(conn, "turn_old", message="事务失败也要能重来", status="queued")
    confirm_legacy_stopped(conn, KIND_USER_TURN, "turn_old")
    turns = FakeTurns()
    journal = _journal(conn)

    def _boom(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        raise JournalWriteError("injected: journal write failed before commit")

    journal.claim_for_resend = _boom  # type: ignore[assignment]
    inbox = _inbox(conn, registry, turns, journal)

    with pytest.raises(JournalWriteError):
        inbox.take_over_and_continue("turn_old")
    assert _rows(conn, "turn_old") == [], "事务没有提交 → 一个后继都不该有"
    assert turns.calls == []
    parent = _row(conn, "turn_old")
    assert parent["recovered_at"] is None, "老记录仍然可以被重发"
    # 那次接管必须被退回去：否则这条记录会带着「本实例归属」从清单里消失，
    # 并且被「上次的写入者还在运行」挡死 —— 用户在当前运行里再也重试不了。
    assert parent["status"] == QUEUED and parent["owner_instance_id"] is None
    assert parent["reason"] is None
    assert inbox.list_records().records != (), "失败之后它必须仍然可见、可重试"

    # 通道恢复之后，同一个记录在当前运行里就能重试成功，且只产生一个后继
    del journal.claim_for_resend  # 撤销这次故障注入
    retried = inbox.take_over_and_continue("turn_old")
    assert retried["ok"] is True
    assert turns.submitted == [retried["turn_id"]]
    assert len(_rows(conn, "turn_old")) == 1
    assert _row(conn, "turn_old")["recovered_by"] == retried["turn_id"]
    conn.close()
