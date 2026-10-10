"""F04 定向回归：孤立 claim 的修复必须遵守 owner 状态，且回退清单不得复活被排除项。

缺陷现场（核查）
----------------

* 专用恢复清单（`RecoveryInbox.list_records`）**过滤活 owner**，但
  `/api/runtime/state` 的 `orphaned_claims()` 没有同等过滤 —— 前端把回退数据并进同一份
  清单时，被排除的那条又冒出来了；
* `repair_orphan()` 不检查 owner 是否存活：活 owner 的 orphan 被修复接口以 200 接受，
  claim 被清掉（那个活实例如果还在完成它的重发，用户就看着一条「被抢走」的记录）。

修好之后：repair / continue / ignore 共用**同一套 owner 判定**（alive / unknown 一律拒绝，
dead / 无归属+已确认才放行），并且写入本身带 owner 条件（列与权威归属不一致时命中 0 行）。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent.api.recovery_routes import build_router
from agent.services.recovery import (
    ACTION_CONFIRM_STOPPED,
    ACTION_IGNORE,
    ACTION_REPAIR,
    KIND_USER_TURN,
    STATE_ORPHAN,
    RecoveryInbox,
)
from agent.storage.turn_journal import TurnJournal

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


def _orphan(conn, turn_id: str, *, owner: str | None) -> None:
    add_turn(
        conn,
        turn_id,
        message="抢占之后没有写出后继",
        status="interrupted",
        owner=owner,
        recovered_at=SEED_TIME,
        recovered_by=None,
    )


def _inbox(conn, registry, *, instance_id: str = "me") -> RecoveryInbox:
    return RecoveryInbox(
        conn,
        registry,
        turns=FakeTurns(),
        instance_id=instance_id,
        journal=TurnJournal(conn, instance_id=instance_id, registry=registry),
    )


def _classes(inbox: RecoveryInbox) -> dict[str, str]:
    return {r.record_id: r.state_class for r in inbox.list_records().records}


def _actions(record) -> dict:  # noqa: ANN001
    return {action.id: action for action in record.actions}


# ---------------------------------------------------------------------------
# 活 owner：不可见、不可修、claim 不动
# ---------------------------------------------------------------------------


def test_live_owner_orphan_is_not_listed_and_not_repairable(tmp_path):
    conn = migrated_conn(tmp_path)
    registry = register_self(conn, "me")
    add_instance(conn, "live", exited=False, pid=222)
    _orphan(conn, "turn_live_orphan", owner="live")
    journal = TurnJournal(conn, instance_id="me", registry=registry)
    inbox = _inbox(conn, registry)
    before = table_snapshot(conn, "turn_journal")

    assert "turn_live_orphan" not in _classes(inbox), "活 owner 的记录不是恢复项"
    assert journal.orphaned_claims() == [], (
        "回退清单必须与专用清单同一套 owner 判定（否则前端合并会把排除项复活）"
    )

    result = inbox.repair_orphan("turn_live_orphan", expected_class=STATE_ORPHAN)
    assert result["ok"] is False and result["repaired"] is False
    assert "还在运行" in result["reason"]
    assert table_snapshot(conn, "turn_journal") == before, "拒绝时一行都不改"
    assert journal.orphaned_claims() == []
    conn.close()


def test_continue_and_ignore_agree_with_repair_on_a_live_owner(tmp_path):
    conn = migrated_conn(tmp_path)
    registry = register_self(conn, "me")
    add_instance(conn, "live", exited=False, pid=222)
    add_turn(conn, "turn_live", status="running", owner="live")
    inbox = _inbox(conn, registry)
    before = table_snapshot(conn, "turn_journal")

    from agent.services.recovery import RecoveryConflict

    assert inbox.ignore("turn_live", expected_class="ready")["ok"] is False
    with pytest.raises(RecoveryConflict):
        inbox.take_over_and_continue("turn_live")
    assert table_snapshot(conn, "turn_journal") == before
    conn.close()


# ---------------------------------------------------------------------------
# 已确认退出的旧孤儿：修复路径必须保留
# ---------------------------------------------------------------------------


def test_confirmed_dead_owner_orphan_is_still_repairable(tmp_path):
    conn = migrated_conn(tmp_path)
    registry = register_self(conn, "me")
    add_instance(conn, "gone", exited=True, pid=111)
    _orphan(conn, "turn_dead_orphan", owner="gone")
    journal = TurnJournal(conn, instance_id="me", registry=registry)
    inbox = _inbox(conn, registry)

    assert _classes(inbox)["turn_dead_orphan"] == STATE_ORPHAN
    assert [row["turn_id"] for row in journal.orphaned_claims()] == ["turn_dead_orphan"]

    record = next(r for r in inbox.list_records().records)
    actions = _actions(record)
    assert actions[ACTION_REPAIR].enabled is True
    assert actions[ACTION_IGNORE].enabled is True
    assert ACTION_CONFIRM_STOPPED not in actions, "已确认退出的归属不需要再确认一次"

    assert inbox.repair_orphan("turn_dead_orphan", expected_class=STATE_ORPHAN)["ok"] is True
    row = conn.execute(
        "SELECT * FROM turn_journal WHERE turn_id = 'turn_dead_orphan'"
    ).fetchone()
    assert row["recovered_at"] is None and row["recovered_by"] in (None, "")
    assert row["message"] == "抢占之后没有写出后继", "修复不改原文"
    assert journal.orphaned_claims() == []
    conn.close()


# ---------------------------------------------------------------------------
# 判不出来（unknown）：列出、但绝不修复
# ---------------------------------------------------------------------------


def test_unknown_owner_orphan_is_listed_but_not_repairable(tmp_path):
    conn = migrated_conn(tmp_path)
    registry = register_self(conn, "me")
    _orphan(conn, "turn_ghost_orphan", owner="ghost")
    journal = TurnJournal(conn, instance_id="me", registry=registry)
    inbox = _inbox(conn, registry)
    before = table_snapshot(conn, "turn_journal")

    assert _classes(inbox)["turn_ghost_orphan"] == STATE_ORPHAN
    assert [row["turn_id"] for row in journal.orphaned_claims()] == ["turn_ghost_orphan"], (
        "unknown 不是「活」：它仍然要被列出来（只是不能动）"
    )
    record = next(r for r in inbox.list_records().records)
    actions = _actions(record)
    assert actions[ACTION_REPAIR].enabled is False
    assert actions[ACTION_IGNORE].enabled is False
    assert "无法确认" in actions[ACTION_REPAIR].reason

    result = inbox.repair_orphan("turn_ghost_orphan", expected_class=STATE_ORPHAN)
    assert result["ok"] is False and "无法确认" in result["reason"]
    assert table_snapshot(conn, "turn_journal") == before
    conn.close()


# ---------------------------------------------------------------------------
# 无归属：确认之后可修；并发/重复只有一次生效
# ---------------------------------------------------------------------------


def test_unowned_orphan_repair_needs_the_stop_confirmation(tmp_path):
    conn = migrated_conn(tmp_path)
    registry = register_self(conn, "me")
    _orphan(conn, "turn_old_orphan", owner=None)
    inbox = _inbox(conn, registry)
    before = table_snapshot(conn, "turn_journal")

    refused = inbox.repair_orphan("turn_old_orphan", expected_class=STATE_ORPHAN)
    assert refused["ok"] is False and "旧版本" in refused["reason"]
    assert table_snapshot(conn, "turn_journal") == before

    confirm_legacy_stopped(conn, KIND_USER_TURN, "turn_old_orphan")
    assert inbox.repair_orphan("turn_old_orphan", expected_class=STATE_ORPHAN)["ok"] is True
    again = inbox.repair_orphan("turn_old_orphan", expected_class=STATE_ORPHAN)
    assert again["ok"] is False, "重复请求不得产生第二次修复"
    conn.close()


def test_repair_uses_a_conditional_write_on_the_owner_column(tmp_path):
    """库里的归属列与调用方按权威判定算出来的归属不一致时：命中 0 行，宁可不修。"""
    conn = migrated_conn(tmp_path)
    add_instance(conn, "gone", exited=True, pid=111)
    add_instance(conn, "live", exited=False, pid=222)

    # 归属表说「gone（已退出）」，但列上写的是 live —— 形状不一致，保守拒绝
    _orphan(conn, "turn_inconsistent", owner="gone")
    conn.execute(
        "UPDATE turn_journal SET owner_instance_id = 'live' WHERE turn_id = 'turn_inconsistent'"
    )
    journal = TurnJournal(conn, instance_id="me", registry=register_self(conn, "me"))
    assert journal.repair_orphan("turn_inconsistent", "me", owner_key="gone") is False
    assert (
        conn.execute(
            "SELECT recovered_at FROM turn_journal WHERE turn_id = 'turn_inconsistent'"
        ).fetchone()["recovered_at"]
        == SEED_TIME
    ), "拒绝时不得清掉抢占记录"

    # 反向：列是 NULL（归属表才是权威镜像）→ 允许修；列上有别的实例 → 拒绝
    _orphan(conn, "turn_mirror", owner="gone")
    conn.execute("UPDATE turn_journal SET owner_instance_id = NULL WHERE turn_id = 'turn_mirror'")
    assert journal.repair_orphan("turn_mirror", "me", owner_key="gone") is True
    conn.close()


def test_endpoint_repair_of_a_live_owner_orphan_is_a_409(tmp_path):
    conn = migrated_conn(tmp_path)
    registry = register_self(conn, "me")
    add_instance(conn, "live", exited=False, pid=222)
    _orphan(conn, "turn_live_orphan", owner="live")
    ctx = SimpleNamespace(
        conn=conn,
        instances=registry,
        instance_id="me",
        turns=FakeTurns(),
        turn_journal=TurnJournal(conn, instance_id="me", registry=registry),
    )
    app = FastAPI()
    app.include_router(build_router(ctx))

    with TestClient(app) as client:
        resp = client.post(
            "/api/recovery/records/turn_live_orphan/repair",
            json={"expected_class": STATE_ORPHAN},
        )
        assert resp.status_code == 409, resp.text
        assert resp.json()["repaired"] is False
        assert (
            conn.execute(
                "SELECT recovered_at FROM turn_journal WHERE turn_id = 'turn_live_orphan'"
            ).fetchone()["recovered_at"]
            == SEED_TIME
        )
        # claim 与归属都没有被动过
        assert registry.owner_instance_id("turn", "turn_live_orphan") == "live"
    conn.close()
