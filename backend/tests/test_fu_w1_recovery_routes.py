"""W1 端点验收：`api/recovery_routes.py` 的 `build_router(ctx)`（契约 §2.2）。

Lead 只需要在 `api/server.py` 里 include 一行；这里用一个最小的测试 app 把路由
本身端到端跑一遍（不碰 `api/server.py`），并把「继续之后真的执行」用最小 runner
验证 —— 只走 fake / 受控替身，不联网、不调用真实模型。
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent.api.recovery_routes import build_router
from agent.core.turn import TurnManager
from agent.storage.turn_journal import TurnJournal

from test_fu_w1_support import (
    FakeTurns,
    add_derived,
    add_instance,
    add_turn,
    migrated_conn,
    register_self,
)

SEED_TIME = "2026-01-01T00:00:00+00:00"


def _ctx(conn, registry, turns: Any, journal: TurnJournal | None = None) -> Any:
    return SimpleNamespace(
        conn=conn,
        instances=registry,
        instance_id="me",
        turns=turns,
        turn_journal=journal or TurnJournal(conn, instance_id="me", registry=registry),
    )


def _app(ctx) -> FastAPI:
    app = FastAPI()
    app.include_router(build_router(ctx))
    return app


def _wait_until(predicate, timeout: float = 2.0) -> bool:
    """受控等待：只等毫秒级，不长时间阻塞。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


# -- GET /api/recovery/records ----------------------------------------------


def test_get_records_returns_classified_listing(tmp_path):
    conn = migrated_conn(tmp_path)
    registry = register_self(conn, "me")
    add_instance(conn, "gone", exited=True, pid=111)
    add_turn(conn, "turn_legacy", message="升级前的消息", status="queued", owner=None,
             created_at=SEED_TIME)
    add_turn(conn, "turn_orphan", message="孤儿消息", status="interrupted", owner="gone",
             recovered_at=SEED_TIME, recovered_by=None, created_at=SEED_TIME)
    add_turn(conn, "turn_ready", message="可继续", status="interrupted", owner="gone",
             created_at=SEED_TIME)
    add_derived(conn, "task_stale", owner="gone", created_at=SEED_TIME)
    add_derived(conn, "task_legacy", owner=None, created_at=SEED_TIME)

    with TestClient(_app(_ctx(conn, registry, FakeTurns()))) as client:
        resp = client.get("/api/recovery/records")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert set(body) == {"records", "total", "shown", "truncated"}
        assert body["total"] == body["shown"] == 5 and body["truncated"] is False
        classes = {record["record_id"]: record["state_class"] for record in body["records"]}
        assert classes == {
            "turn_legacy": "legacy_unowned",
            "turn_orphan": "orphaned_claim",
            "turn_ready": "ready",
            "task_stale": "derived_stale",
            "task_legacy": "derived_legacy",
        }
        first = body["records"][0]
        assert set(first) == {
            "record_id",
            "kind",
            "state_class",
            "status",
            "message",
            "topic_id",
            "reason",
            "created_at",
            "updated_at",
            "owner_instance_id",
            "owner_state",
            "owner_note",
            "claim_generation",
            "attempts",
            "last_error",
            "confirmed_stopped",
            "actions",
        }
        orphan = next(r for r in body["records"] if r["record_id"] == "turn_orphan")
        assert orphan["message"] == "孤儿消息"
        assert orphan["owner_state"] == "dead"
        assert orphan["owner_note"]
        assert {action["id"] for action in orphan["actions"]} == {
            "continue",
            "repair",
            "ignore",
        }
        assert all({"id", "label", "enabled", "reason"} == set(a) for a in orphan["actions"])

        # 过滤与分页
        only_tasks = client.get("/api/recovery/records?kinds=derived_task").json()
        assert only_tasks["total"] == 2
        only_stale = client.get("/api/recovery/records?classes=derived_stale").json()
        assert [r["record_id"] for r in only_stale["records"]] == ["task_stale"]
        limited = client.get("/api/recovery/records?limit=2").json()
        assert (limited["total"], limited["shown"], limited["truncated"]) == (5, 2, True)
    conn.close()


# -- POST continue -----------------------------------------------------------


def test_post_continue_dispatches_exactly_one_turn(tmp_path):
    """端到端：继续 → 真的执行；重复点击 409，且不再产生第二个后继。"""
    conn = migrated_conn(tmp_path)
    registry = register_self(conn, "me")
    add_turn(conn, "turn_legacy", message="继续我", status="queued", owner=None)

    executed: list[str] = []
    turns = TurnManager()

    async def runner(turn_ctx) -> None:  # noqa: ANN001 - 只记录执行
        executed.append(turn_ctx.turn_id)

    turns.set_runner(runner)

    with TestClient(_app(_ctx(conn, registry, turns))) as client:
        # F01：无归属记录先要一次显式确认（端点级）。
        blocked = client.post(
            "/api/recovery/records/turn_legacy/continue",
            json={"expected_class": "legacy_unowned", "expected_status": "queued"},
        )
        assert blocked.status_code == 409, blocked.text
        assert "旧版本" in blocked.json()["reason"]
        confirmed = client.post("/api/recovery/records/turn_legacy/confirm-stopped", json={})
        assert confirmed.status_code == 200, confirmed.text
        assert confirmed.json()["confirmed"] is True

        resp = client.post(
            "/api/recovery/records/turn_legacy/continue",
            json={"expected_class": "legacy_unowned", "expected_status": "queued"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["ok"] is True and body["record_id"] == "turn_legacy"
        assert body["turn_id"] and body["turn_id"] != "turn_legacy"
        assert body["status"] in ("accepted", "queued")
        assert _wait_until(lambda: len(executed) == 1), "继续之后必须真的被执行"
        assert executed == [body["turn_id"]]

        again = client.post(
            "/api/recovery/records/turn_legacy/continue",
            json={"expected_class": "ready", "expected_status": "interrupted"},
        )
        assert again.status_code == 409, again.text
        assert again.json()["ok"] is False and again.json()["conflict"] is True
        assert again.json()["reason"]
        assert executed == [body["turn_id"]]

        # 清单里不再有需要用户处理的东西（新 turn 属于活实例）
        listing = client.get("/api/recovery/records").json()
        assert listing["records"] == []
    conn.close()


def test_post_continue_conflicts_are_explicit(tmp_path):
    conn = migrated_conn(tmp_path)
    registry = register_self(conn, "me")
    add_turn(conn, "turn_legacy", status="queued", owner=None)
    add_turn(conn, "turn_done", status="completed", owner=None)
    add_turn(conn, "turn_ghost", status="queued", owner="ghost")
    turns = FakeTurns()

    with TestClient(_app(_ctx(conn, registry, turns))) as client:
        # 客户端看到的状态已过期
        stale = client.post(
            "/api/recovery/records/turn_legacy/continue",
            json={"expected_class": "legacy_unowned", "expected_status": "running"},
        )
        assert stale.status_code == 409 and "目标状态" in stale.json()["reason"]
        # 分类对不上
        wrong_class = client.post(
            "/api/recovery/records/turn_legacy/continue",
            json={"expected_class": "ready", "expected_status": "queued"},
        )
        assert wrong_class.status_code == 409
        # 归属判不出来的行：不许抢（一行不改）
        unknown = client.post(
            "/api/recovery/records/turn_ghost/continue",
            json={"expected_class": "owner_unknown", "expected_status": "queued"},
        )
        assert unknown.status_code == 409 and "无法确认" in unknown.json()["reason"]
        # 已完成 / 不存在
        for record_id in ("turn_done", "turn_missing"):
            resp = client.post(
                f"/api/recovery/records/{record_id}/continue", json={}
            )
            assert resp.status_code == 409, resp.text
        assert turns.calls == [], "所有冲突都必须在派发之前被挡住"
        row = conn.execute(
            "SELECT status, owner_instance_id FROM turn_journal WHERE turn_id = 'turn_legacy'"
        ).fetchone()
        assert row["status"] == "queued" and row["owner_instance_id"] is None
    conn.close()


# -- POST repair / ignore / requeue -----------------------------------------


def test_post_repair_then_conflict_for_resent_record(tmp_path):
    conn = migrated_conn(tmp_path)
    registry = register_self(conn, "me")
    add_turn(conn, "turn_orphan", status="interrupted", owner=None,
             recovered_at=SEED_TIME, recovered_by=None)
    add_turn(conn, "turn_resent", status="interrupted", owner=None,
             recovered_at=SEED_TIME, recovered_by="turn_successor")

    with TestClient(_app(_ctx(conn, registry, FakeTurns()))) as client:
        # F01：无归属的孤儿要先确认旧执行者已停止（修复会把它变回可继续）。
        assert (
            client.post(
                "/api/recovery/records/turn_orphan/repair",
                json={"expected_class": "orphaned_claim"},
            ).status_code
            == 409
        )
        assert (
            client.post(
                "/api/recovery/records/turn_orphan/confirm-stopped", json={}
            ).status_code
            == 200
        )
        ok = client.post(
            "/api/recovery/records/turn_orphan/repair",
            json={"expected_class": "orphaned_claim"},
        )
        assert ok.status_code == 200, ok.text
        assert ok.json() == {"ok": True, "repaired": True, "record_id": "turn_orphan"}

        resent = client.post(
            "/api/recovery/records/turn_resent/repair",
            json={"expected_class": "orphaned_claim"},
        )
        assert resent.status_code == 409, resent.text
        body = resent.json()
        assert body["ok"] is False and body["repaired"] is False and body["reason"]

        # 已经修好的行不再是孤儿，再修一次也是 409
        again = client.post(
            "/api/recovery/records/turn_orphan/repair",
            json={"expected_class": "orphaned_claim"},
        )
        assert again.status_code == 409
    conn.close()


def test_post_ignore_is_one_shot_and_keeps_the_message(tmp_path):
    conn = migrated_conn(tmp_path)
    registry = register_self(conn, "me")
    add_turn(conn, "turn_legacy", message="用户不想继续", status="queued", owner=None)

    with TestClient(_app(_ctx(conn, registry, FakeTurns()))) as client:
        assert (
            client.post(
                "/api/recovery/records/turn_legacy/ignore",
                json={"expected_class": "legacy_unowned"},
            ).status_code
            == 409
        )
        assert (
            client.post(
                "/api/recovery/records/turn_legacy/confirm-stopped", json={}
            ).status_code
            == 200
        )
        ok = client.post(
            "/api/recovery/records/turn_legacy/ignore",
            json={"expected_class": "legacy_unowned"},
        )
        assert ok.status_code == 200, ok.text
        assert ok.json() == {"ok": True, "ignored": True, "record_id": "turn_legacy"}
        again = client.post(
            "/api/recovery/records/turn_legacy/ignore",
            json={"expected_class": "legacy_unowned"},
        )
        assert again.status_code == 409, again.text
        assert again.json()["ignored"] is False
        listing = client.get("/api/recovery/records").json()
        assert listing["records"] == []

    row = conn.execute(
        "SELECT status, message, recovered_by FROM turn_journal WHERE turn_id = 'turn_legacy'"
    ).fetchone()
    assert row["status"] == "dismissed"
    assert row["message"] == "用户不想继续", "不删除原消息"
    assert not row["recovered_by"], "不产生后继"
    conn.close()


def test_router_works_with_the_real_app_context(tmp_path):
    """用**真实 AppContext**（真实属性名、真实启动恢复）串一遍：只挂一行就能用。

    这里自己把路由 include 到 `create_app` 的 app 上（Lead 负责在 server.py 里挂
    同一行）；执行用最小 runner，不调用真实模型。
    """
    conn = migrated_conn(tmp_path)
    add_turn(conn, "turn_legacy", message="真实上下文里的历史消息", status="queued", owner=None)

    from agent.api.server import create_app
    from agent.config import Settings
    from agent.credentials.store import MemoryKeyring

    app = create_app(Settings(data_dir=tmp_path), conn)
    app.include_router(build_router(app.state.ctx))
    executed: list[str] = []

    with TestClient(app) as client:
        ctx = app.state.ctx
        ctx.credentials._kr = MemoryKeyring()  # 不碰系统钥匙串
        # 真实启动恢复：无归属的历史行被保守保留，收件箱照样能看见它
        assert ctx.turn_journal.unfinished() == []

        async def runner(turn_ctx) -> None:  # noqa: ANN001 - 只记录执行
            executed.append(turn_ctx.turn_id)

        ctx.turns.set_runner(runner)

        listing = client.get("/api/recovery/records").json()
        classes = {record["record_id"]: record["state_class"] for record in listing["records"]}
        assert classes.get("turn_legacy") == "legacy_unowned", listing

        # F01：真实上下文里也要先确认「旧执行者已停止」（无归属 = 没有证据）。
        assert (
            client.post(
                "/api/recovery/records/turn_legacy/continue",
                json={"expected_class": "legacy_unowned", "expected_status": "queued"},
            ).status_code
            == 409
        )
        assert (
            client.post(
                "/api/recovery/records/turn_legacy/confirm-stopped", json={}
            ).status_code
            == 200
        )
        resp = client.post(
            "/api/recovery/records/turn_legacy/continue",
            json={"expected_class": "legacy_unowned", "expected_status": "queued"},
        )
        assert resp.status_code == 200, resp.text
        new_turn_id = resp.json()["turn_id"]
        assert _wait_until(lambda: executed == [new_turn_id]), (
            f"真实上下文里继续之后必须真的执行；executed={executed}"
        )
        assert (
            client.post(
                "/api/recovery/records/turn_legacy/continue",
                json={"expected_class": "ready", "expected_status": "interrupted"},
            ).status_code
            == 409
        )
    conn.close()


def test_post_requeue_derived_bumps_generation(tmp_path):
    conn = migrated_conn(tmp_path)
    registry = register_self(conn, "me")
    add_instance(conn, "gone", exited=True, pid=111)
    add_derived(conn, "task_stale", owner="gone", claim_generation=2, attempts=4,
                last_error="超时")

    with TestClient(_app(_ctx(conn, registry, FakeTurns()))) as client:
        ok = client.post(
            "/api/recovery/records/task_stale/requeue",
            json={"expected_state": "running", "expected_generation": 2},
        )
        assert ok.status_code == 200, ok.text
        assert ok.json() == {"ok": True, "state": "pending", "record_id": "task_stale"}
        again = client.post(
            "/api/recovery/records/task_stale/requeue",
            json={"expected_state": "running", "expected_generation": 3},
        )
        assert again.status_code == 409, again.text
        missing = client.post("/api/recovery/records/task_missing/requeue", json={})
        assert missing.status_code == 409
    row = conn.execute("SELECT * FROM derived_tasks WHERE id = 'task_stale'").fetchone()
    assert row["state"] == "pending"
    assert int(row["claim_generation"]) == 3
    assert int(row["attempts"]) == 4 and row["last_error"] == "超时"
    conn.close()
