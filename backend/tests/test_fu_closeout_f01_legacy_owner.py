"""F01 定向回归：没有持久 owner 字段 ≠ 已经证明执行者停止。

核查现场（旧 main `6e073e9` 的 AppContext 起在事件门上）
------------------------------------------------------

旧版本**不写任何**进程 / 实例 / 心跳 / 归属记录（那一版的 schema 里根本没有
`instances` / `record_owners`，`turn_journal` 也没有归属列），所以「旧 runner 还在跑」
与「旧 runner 已经退出」在库里留下的形状**完全一样**：

    turn_journal:  status in ('queued','running'), owner_instance_id IS NULL
    record_owners: 没有这一行
    instances:     没有这一行（或没有对应的活跃实例）

任何「按状态猜一猜就接管」的做法都会在那个形状上启动第二次执行（核查实测：继续接口
返回 200，旧 runner 释放前第二次执行已经开始了）。所以本轮的规则是：

* 这个形状 = **没有证据**：记录仍然可见，但 continue / ignore / repair / requeue
  一律被阻止，并给出「为什么现在动不了」；
* 只有用户显式确认「旧执行者已停止」（`confirm_stopped`，本轮唯一的解锁入口）之后
  才允许操作；
* 对无归属的 running 派生任务用**同一套**规则（超时不再是充分的接管理由）。

全部受控：临时库 + 替身派发通道，不联网、不起真实进程、不调用真实模型。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from fastapi import FastAPI

from agent.api.recovery_routes import build_router
from agent.services import derived_tasks as dt
from agent.services.recovery import (
    ACTION_CONFIRM_STOPPED,
    ACTION_CONTINUE,
    ACTION_IGNORE,
    ACTION_REQUEUE,
    KIND_DERIVED_TASK,
    KIND_USER_TURN,
    STATE_DERIVED_LEGACY,
    STATE_LEGACY_UNOWNED,
    STATE_ORPHAN,
    RecoveryConflict,
    RecoveryInbox,
)
from agent.storage.turn_journal import TurnJournal

from test_fu_w1_support import (
    FakeTurns,
    add_derived,
    add_turn,
    confirm_legacy_stopped,
    migrated_conn,
    register_self,
    shadow_conn,
)


class _LiveTurns(FakeTurns):
    """「执行」这一侧：记录每一次真实派发（用来证明没有第二次执行）。"""

    def __init__(self) -> None:
        super().__init__()
        self.executed: list[str] = []

    def submit(self, message, topic_id=None, *, notify=False, intent_id=None, turn_id=None):  # noqa: ANN001, ANN202
        result = super().submit(
            message,
            topic_id,
            notify=notify,
            intent_id=intent_id,
            turn_id=turn_id,
        )
        self.executed.append(str(turn_id))
        return result


def _inbox(conn, registry, turns, *, instance_id: str = "me") -> RecoveryInbox:
    return RecoveryInbox(
        conn,
        registry,
        turns=turns,
        instance_id=instance_id,
        journal=TurnJournal(conn, instance_id=instance_id, registry=registry),
    )


def _actions(record) -> dict:  # noqa: ANN001
    return {action.id: action for action in record.actions}


def _successor_rows(conn, parent_id: str) -> list:
    return list(
        conn.execute(
            "SELECT * FROM turn_journal WHERE turn_id != ? ORDER BY created_at", (parent_id,)
        ).fetchall()
    )


# ---------------------------------------------------------------------------
# 旧版本执行中的确定性现场：形状相同 ⇒ 一律不接管
# ---------------------------------------------------------------------------


def test_old_version_open_row_is_visible_but_not_actionable(tmp_path):
    """旧版本留下的 queued / running 行：可见、给出原因，但一个动作都不许点。"""
    conn = migrated_conn(tmp_path)
    registry = register_self(conn, "me")  # 新版本自己是活的，但和这条记录无关
    add_turn(conn, "turn_old_running", message="旧进程还在跑的消息", status="running")
    add_turn(conn, "turn_old_queued", message="旧进程还在排队", status="queued")

    inbox = _inbox(conn, registry, FakeTurns())
    records = {r.record_id: r for r in inbox.list_records().records}

    for record_id in ("turn_old_running", "turn_old_queued"):
        record = records[record_id]
        assert record.state_class == STATE_LEGACY_UNOWNED
        assert record.owner_state == "none"
        assert record.confirmed_stopped is False
        assert "旧版本" in record.owner_note
        by_id = _actions(record)
        assert by_id[ACTION_CONTINUE].enabled is False
        assert by_id[ACTION_IGNORE].enabled is False
        assert "旧版本" in by_id[ACTION_CONTINUE].reason
        assert by_id[ACTION_CONFIRM_STOPPED].enabled is True, "必须给出唯一的解锁入口"
    conn.close()


def test_old_version_open_row_cannot_start_a_second_execution(tmp_path):
    """旧 runner「还在跑」：continue / ignore 全部 409，且没有任何一次真实派发。

    旧执行者对库不可见，所以这里能依赖的只有一件事：**新版本不给没有证据的记录
    任何会改变运行状态的入口**。断言写成「一次派发都没有 + 台账一行没变」。
    """
    conn = migrated_conn(tmp_path)
    registry = register_self(conn, "me")
    add_turn(conn, "turn_old", message="旧 runner 还在跑", status="running")
    turns = _LiveTurns()
    inbox = _inbox(conn, registry, turns)
    before = [tuple(row) for row in conn.execute("SELECT * FROM turn_journal").fetchall()]

    with pytest.raises(RecoveryConflict):
        inbox.take_over_and_continue("turn_old", expected_class=STATE_LEGACY_UNOWNED)
    assert inbox.ignore("turn_old", expected_class=STATE_LEGACY_UNOWNED)["ok"] is False
    assert inbox.repair_orphan("turn_old", expected_class=STATE_ORPHAN)["ok"] is False

    assert turns.executed == [], "没有证据就不能启动第二次执行"
    assert [tuple(row) for row in conn.execute("SELECT * FROM turn_journal").fetchall()] == before
    conn.close()


def test_endpoint_blocks_and_explains_before_confirmation(tmp_path):
    """HTTP 层：409 + 一句给用户看的原因；确认之后才 200。"""
    conn = migrated_conn(tmp_path)
    registry = register_self(conn, "me")
    add_turn(conn, "turn_old", message="旧版本的消息", status="queued")
    turns = _LiveTurns()
    ctx = SimpleNamespace(
        conn=conn,
        instances=registry,
        instance_id="me",
        turns=turns,
        turn_journal=TurnJournal(conn, instance_id="me", registry=registry),
    )
    app = FastAPI()
    app.include_router(build_router(ctx))

    with TestClient(app) as client:
        blocked = client.post(
            "/api/recovery/records/turn_old/continue",
            json={"expected_class": "legacy_unowned", "expected_status": "queued"},
        )
        assert blocked.status_code == 409, blocked.text
        assert blocked.json()["ok"] is False
        assert "旧版本" in blocked.json()["reason"]
        assert turns.executed == []

        listing = client.get("/api/recovery/records").json()
        record = next(r for r in listing["records"] if r["record_id"] == "turn_old")
        assert record["state_class"] == "legacy_unowned"
        assert record["confirmed_stopped"] is False
        assert {a["id"] for a in record["actions"]} >= {"continue", "ignore", "confirm_stopped"}

        confirmed = client.post("/api/recovery/records/turn_old/confirm-stopped", json={})
        assert confirmed.status_code == 200, confirmed.text
        assert confirmed.json()["confirmed"] is True

        again = client.post(
            "/api/recovery/records/turn_old/continue",
            json={"expected_class": "legacy_unowned", "expected_status": "queued"},
        )
        assert again.status_code == 200, again.text
        assert turns.executed == [again.json()["turn_id"]]
        assert len(_successor_rows(conn, "turn_old")) == 1
    conn.close()


def test_confirm_requires_a_genuinely_unowned_record(tmp_path):
    """有归属（活着 / 判不出来）的记录不需要、也不允许用确认绕过 owner 判定。"""
    from test_fu_w1_support import add_instance

    conn = migrated_conn(tmp_path)
    registry = register_self(conn, "me")
    add_instance(conn, "live", exited=False, pid=222)
    add_instance(conn, "gone", exited=True, pid=111)
    add_turn(conn, "turn_live", status="running", owner="live")
    add_turn(conn, "turn_ghost", status="running", owner="ghost")
    add_turn(conn, "turn_dead", status="interrupted", owner="gone")
    inbox = _inbox(conn, registry, FakeTurns())

    assert inbox.confirm_stopped("turn_live")["ok"] is False
    assert inbox.confirm_stopped("turn_ghost")["ok"] is False
    assert inbox.confirm_stopped("turn_dead")["ok"] is False
    # 确认是幂等的：同一条重复确认不报错、不产生第二条
    add_turn(conn, "turn_old", status="queued")
    first = inbox.confirm_stopped("turn_old")
    second = inbox.confirm_stopped("turn_old")
    assert first["ok"] is True and second["ok"] is True
    assert second["already_confirmed"] is True
    conn.close()


def test_two_new_instances_racing_still_produce_one_successor(tmp_path):
    """保留两个新版本实例并发争抢的用例：确认之后仍然只有一个后继、一次派发。"""
    conn = migrated_conn(tmp_path)
    registry = register_self(conn, "me")
    add_turn(conn, "turn_race", message="并发只许执行一次", status="queued")
    other = shadow_conn(conn)
    try:
        turns_a, turns_b = _LiveTurns(), _LiveTurns()
        inbox_a = _inbox(conn, registry, turns_a)
        inbox_b = _inbox(other, registry, turns_b)
        confirm_legacy_stopped(conn, KIND_USER_TURN, "turn_race")

        first = inbox_a.take_over_and_continue(
            "turn_race", expected_class=STATE_LEGACY_UNOWNED, expected_status="queued"
        )
        with pytest.raises(RecoveryConflict):
            inbox_b.take_over_and_continue("turn_race", expected_class="ready")
        assert first["ok"] is True
        assert turns_a.executed == [first["turn_id"]] and turns_b.executed == []
        assert len(_successor_rows(conn, "turn_race")) == 1
    finally:
        other.close()
        conn.close()


# ---------------------------------------------------------------------------
# 派生任务：与 turn 一致的所有权规则
# ---------------------------------------------------------------------------


def test_unowned_running_derived_task_is_not_reclaimed_by_timeout(tmp_path):
    """无归属 + 已超期：启动恢复不再自动放回（超时是猜测，不是证据）。"""
    conn = migrated_conn(tmp_path)
    registry = register_self(conn, "me")
    add_derived(conn, "task_old", owner=None, claim_generation=1, attempts=2, last_error="boom")
    conn.execute(
        "UPDATE derived_tasks SET updated_at = '2020-01-01T00:00:00+00:00' WHERE id = 'task_old'"
    )

    assert dt.recover_stale(conn, instance_id="me", registry=registry) == 0
    row = conn.execute("SELECT * FROM derived_tasks WHERE id = 'task_old'").fetchone()
    assert row["state"] == dt.STATE_RUNNING, "没有证据就不能接管，超时不足以放回"

    inbox = _inbox(conn, registry, FakeTurns())
    record = next(r for r in inbox.list_records(kinds=KIND_DERIVED_TASK).records)
    assert record.state_class == STATE_DERIVED_LEGACY
    by_id = _actions(record)
    assert by_id[ACTION_REQUEUE].enabled is False
    assert by_id[ACTION_CONFIRM_STOPPED].enabled is True
    assert inbox.requeue_derived("task_old", expected_state="running", expected_generation=1)[
        "ok"
    ] is False
    assert (
        conn.execute("SELECT state FROM derived_tasks WHERE id='task_old'").fetchone()["state"]
        == dt.STATE_RUNNING
    )


def test_confirmed_unowned_derived_task_can_be_requeued_once(tmp_path):
    conn = migrated_conn(tmp_path)
    registry = register_self(conn, "me")
    add_derived(conn, "task_old", owner=None, claim_generation=1, attempts=2, last_error="boom")
    inbox = _inbox(conn, registry, FakeTurns())

    assert inbox.confirm_stopped("task_old", expected_class=STATE_DERIVED_LEGACY)["ok"] is True
    result = inbox.requeue_derived("task_old", expected_state="running", expected_generation=1)
    assert result["ok"] is True and result["state"] == dt.STATE_PENDING
    row = conn.execute("SELECT * FROM derived_tasks WHERE id = 'task_old'").fetchone()
    assert row["state"] == dt.STATE_PENDING
    assert int(row["claim_generation"]) == 2
    assert int(row["attempts"]) == 2 and row["last_error"] == "boom", "失败历史必须原样保留"
    again = inbox.requeue_derived("task_old", expected_state="running", expected_generation=1)
    assert again["ok"] is False, "重复点击不得产生第二次重排"
    conn.close()
