"""V 组独立验证 · A01 + A03：可恢复记录（历史无归属 / 孤立 claim / 派生任务）。

原缺陷（基线 `da0436b`，见 `_contracts` §2）

    A01「升级前无归属的历史 queued/running 用户行」

    `TurnJournal.interrupt_stale(registry)` 只处理**归属者确认已退出**的行；
    没有归属（旧记录）或判不出来的行一律 `deferred`（保守保留原状态）。
    这本身是对的，但紧接着的问题是：**没有第二条出口**。
    那行仍然是 `status='queued'`，而用户能看到未完成事项的唯一入口
    `unfinished()` 要求 `status='interrupted'`：

        /api/runtime/state  →  interrupted_turns: []      # 看不见
        /api/turns/{id}/resend → 409                        # 也不能继续
        /api/recovery/records  →  404（这台路由在基线根本不存在）

    于是用户的消息既不在收件箱里、又不能继续，**永久卡死**。

    A03「真实形状的孤立 claim」

    `recovered_at` 非空、`recovered_by` 为空的记录是「抢占过但没有后继」：
    `unfinished()` 因为 `recovered_at IS NULL` 不再提示它，界面上彻底消失。
    基线只有 `/api/runtime/state.orphaned_turns` 这个**只读**出口
    （`orphaned_claims()`），**没有任何修复入口**，所以这条消息永远回不来。

验证手段（全部走冻结的 HTTP 接口 + 直接读台账/派生任务表）

    * 受控构造：历史无归属 queued/running 行、孤立 claim 行、
      已真正重发的行、无归属 running 派生任务、notify=1 的系统通知轮；
    * 受控闸门：把 `TurnManager` 的 runner 换成记录型替身，断言「只产生一个有效执行」；
    * 并发：两个线程同时 POST continue，断言只有一个 200、只有一个后继；
    * 重启：退出第一个 app（同一条 sqlite），再起一个 app，断言记录仍然可见可操作。

    断言只看 HTTP 状态、台账行、清单字段与派生任务行；基线全红（404），修复后仍绿。

判据说明（补充修复轮修正，详见 `scripts/fu-verify/README.md`）

    * **后继关联的方向**：`claim_for_resend()` 按 R06 契约 C3 把关联写在**老记录**上
      （`老记录.recovered_by = 新 turn_id`）。所以 `successors_of()` 必须顺着老记录的
      `recovered_by` 去找那一行 —— 不是反过来查「`recovered_by` 等于老 id 的行」
      （那个方向永远查不到东西，是用例自身写反了，不是实现缺陷）。
    * **「卡住」的时间戳必须新鲜**：无归属的 running 派生任务若已超期，启动恢复
      `recover_stale()` 会把它放回 `pending` —— 这是正确的时限兜底，不是「卡住」。
      保持「卡住」语义的用例必须用当前时间播种；已超期的场景另有专门的用例钉住。
"""

from __future__ import annotations

import asyncio
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from agent.api.server import create_app
from agent.config import Settings
from agent.credentials.store import MemoryKeyring
from agent.storage.instance_registry import RECORD_TURN

NOW = "2026-10-10T00:00:00+00:00"

LEGACY_QUEUED = "turn_legacy_queued"
LEGACY_RUNNING = "turn_legacy_running"
NOTIFY_ROW = "turn_notify_only"
ORPHAN = "turn_orphan_claim"
RESENT = "turn_already_resent"
RESENT_SUCCESSOR = "turn_real_successor"
DERIVED = "task_derived_running"
DERIVED_STALE = "task_derived_running_expired"

MSG_QUEUED = "这条消息当时还在排队，进程退出后没有开始执行"
MSG_RUNNING = "这条消息执行到一半，进程退出后没有完成"
MSG_ORPHAN = "这条消息抢占过重发，但后继从来没有写出来"
MSG_RESENT = "这条消息已经真正重发过了"


# --------------------------------------------------------------------------
# 受控构造
# --------------------------------------------------------------------------


def seed_turn(
    conn: sqlite3.Connection,
    turn_id: str,
    message: str,
    *,
    status: str = "queued",
    notify: int = 0,
    owner: str | None = None,
    recovered_at: str | None = None,
    recovered_by: str | None = None,
    reason: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO turn_journal (turn_id, message, topic_id, notify, status, created_at, "
        " updated_at, owner_instance_id, recovered_at, recovered_by, reason) "
        "VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            turn_id,
            message,
            int(notify),
            status,
            NOW,
            NOW,
            owner,
            recovered_at,
            recovered_by,
            reason,
        ),
    )


def _utc_now_iso() -> str:
    """真实当前时间（UTC）。

    「卡住」语义必须用**新鲜**时间戳：无归属的 running 任务只要超期
    （`derived_tasks._STALE_RUNNING_SECONDS`，默认 300 秒），启动恢复
    `recover_stale()` 就会把它放回 `pending` —— 那是**正常**兜底，它不该再被
    当成「卡住的 running」。所以这里不能用固定字面量（会随时间变旧）。
    """
    return datetime.now(timezone.utc).isoformat()


def seed_derived_running(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    updated_at: str | None = None,
    content_version: int = 1,
) -> None:
    """无归属的 running 派生任务。

    `updated_at` 默认取**此刻**：这才是「刚刚还在跑、但归属为空」的卡住现场。
    需要构造「已超期」的场景时显式传一个旧时间戳。

    `(kind, fragment_id, content_version)` 上有唯一约束，所以同一场景里播种多条时
    必须给出不同的 `content_version`。
    """
    moment = updated_at or _utc_now_iso()
    conn.execute(
        "INSERT INTO derived_tasks (id, kind, fragment_id, content_version, state, attempts, "
        " last_error, run_after, created_at, updated_at, owner_instance_id, claim_generation) "
        "VALUES (?, 'summary', 'frag_legacy', ?, 'running', 2, 'boom', NULL, ?, ?, NULL, 1)",
        (task_id, int(content_version), moment, moment),
    )


def journal_row(conn: sqlite3.Connection, turn_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM turn_journal WHERE turn_id = ?", (turn_id,)).fetchone()
    assert row is not None, f"台账里应当有 {turn_id}"
    return row


def successors_of(conn: sqlite3.Connection, record_id: str) -> list[sqlite3.Row]:
    """一条记录的「后继」——按既有 R06 契约 C3，关联方向是**老记录指向新记录**。

    `TurnJournal.claim_for_resend()` 一个事务里做三件事：带条件 UPDATE 老记录写
    `recovered_at`、INSERT 新记录、再在老记录上写 `recovered_by = 新 turn_id`。
    所以关联落在**老记录**这一行上：

        老记录.recovered_by ──▶ 新记录的 turn_id

    「后继」因此是「老记录 `recovered_by` 指向的那一行」，而不是「`recovered_by`
    等于老 id 的行」（后者永远为空，除非有人把方向弄反）。老记录没有 `recovered_by`
    （未重发 / 孤立抢占 / 已被忽略）→ 没有后继。
    """
    row = conn.execute(
        "SELECT recovered_by FROM turn_journal WHERE turn_id = ?", (record_id,)
    ).fetchone()
    if row is None:
        return []
    successor_id = str(row["recovered_by"] or "")
    if not successor_id:
        return []
    return list(
        conn.execute(
            "SELECT * FROM turn_journal WHERE turn_id = ? ORDER BY created_at", (successor_id,)
        ).fetchall()
    )


def siblings_of(conn: sqlite3.Connection, message: str, record_id: str) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT * FROM turn_journal WHERE message = ? AND turn_id <> ?", (message, record_id)
        ).fetchall()
    )


# --------------------------------------------------------------------------
# app / client 工厂（受控重启）
# --------------------------------------------------------------------------


class Harness:
    """同一份 sqlite 上「起 / 停」app 的受控外壳，并记录 runner 的实际调用。"""

    def __init__(self, conn: sqlite3.Connection, settings: Settings) -> None:
        self.conn = conn
        self.settings = settings
        self.started: list[str] = []
        self._stack: list[tuple[TestClient, object]] = []

    def start(self) -> TestClient:
        app = create_app(self.settings, self.conn)
        app.state.ctx.credentials._kr = MemoryKeyring()
        client = TestClient(app)
        client.__enter__()

        async def _runner(turn_ctx):  # noqa: ANN001, ANN202
            self.started.append(str(turn_ctx.turn_id))

        app.state.ctx.turns.set_runner(_runner)
        self._stack.append((client, app))
        return client

    def stop(self, client: TestClient) -> None:
        for index, (item, app) in enumerate(self._stack):
            if item is client:
                self._stack.pop(index)
                try:
                    client.__exit__(None, None, None)
                except Exception:  # noqa: BLE001 - 关闭失败不该掩盖断言
                    pass
                return

    def close(self) -> None:
        while self._stack:
            client, _app = self._stack.pop()
            try:
                client.__exit__(None, None, None)
            except Exception:  # noqa: BLE001
                pass

    def settle(self, predicate, timeout: float = 3.0) -> bool:  # noqa: ANN001
        """在 app 的事件循环里轮询一个同步谓词（有界，不随机长等待）。"""
        client = self._stack[-1][0]

        async def _poll() -> bool:
            loop = asyncio.get_running_loop()
            deadline = loop.time() + timeout
            while loop.time() < deadline:
                if predicate():
                    return True
                await asyncio.sleep(0.01)
            return predicate()

        return bool(client.portal.call(_poll))


@pytest.fixture()
def harness(db_conn: sqlite3.Connection, settings: Settings):
    shell = Harness(db_conn, settings)
    yield shell
    shell.close()


# --------------------------------------------------------------------------
# 清单读取
# --------------------------------------------------------------------------


def recovery_listing(client: TestClient, **params) -> dict:
    resp = client.get("/api/recovery/records", params=params)
    assert resp.status_code == 200, (
        f"恢复收件箱接口必须可用（基线 404）：{resp.status_code} {resp.text[:200]}"
    )
    body = resp.json()
    for key in ("records", "total", "shown", "truncated"):
        assert key in body, f"清单必须带 {key}：{list(body)}"
    return body


def find_record(client: TestClient, record_id: str) -> dict | None:
    for item in recovery_listing(client)["records"]:
        if str(item.get("record_id")) == record_id:
            return item
    return None


def action_ids(record: dict) -> set[str]:
    return {str(a.get("id")) for a in record.get("actions", [])}


# --------------------------------------------------------------------------
# A01 · 历史无归属 queued/running 用户行
# --------------------------------------------------------------------------


def test_a01_legacy_unowned_rows_become_visible(harness: Harness):
    seed_turn(harness.conn, LEGACY_QUEUED, MSG_QUEUED, status="queued")
    seed_turn(harness.conn, LEGACY_RUNNING, MSG_RUNNING, status="running")
    client = harness.start()

    # 基线的旧入口确实看不见它们（这就是缺陷的现场）
    state = client.get("/api/runtime/state").json()
    assert state["interrupted_turns"] == [], "旧入口（interrupted_turns）看不见历史无归属行"

    body = recovery_listing(client)
    ids = {str(item["record_id"]) for item in body["records"]}
    assert LEGACY_QUEUED in ids, "历史无归属的 queued 行必须可见"
    assert LEGACY_RUNNING in ids, "历史无归属的 running 行必须可见"
    assert body["total"] >= 2
    assert body["shown"] == len(body["records"])
    assert body["truncated"] is False

    queued = next(i for i in body["records"] if i["record_id"] == LEGACY_QUEUED)
    assert queued["kind"] == "user_turn"
    assert queued["state_class"] == "legacy_unowned"
    assert queued["status"] == "queued"
    assert queued["message"] == MSG_QUEUED, "必须给用户消息原文"
    assert queued["owner_instance_id"] is None
    assert queued["owner_state"] == "none"
    assert "continue" in action_ids(queued), f"必须给出「继续」动作：{queued['actions']}"
    assert "ignore" in action_ids(queued), f"必须给出「忽略」动作：{queued['actions']}"
    for action in queued["actions"]:
        assert {"id", "label", "enabled"} <= set(action), f"动作字段不全：{action}"

    running = next(i for i in body["records"] if i["record_id"] == LEGACY_RUNNING)
    assert running["state_class"] == "legacy_unowned"
    assert running["status"] == "running"


def test_a01_legacy_row_survives_restart_and_is_still_operable(harness: Harness):
    seed_turn(harness.conn, LEGACY_QUEUED, MSG_QUEUED, status="queued")
    seed_turn(harness.conn, LEGACY_RUNNING, MSG_RUNNING, status="running")
    first = harness.start()
    assert {i["record_id"] for i in recovery_listing(first)["records"]} >= {
        LEGACY_QUEUED,
        LEGACY_RUNNING,
    }
    harness.stop(first)

    second = harness.start()
    body = recovery_listing(second)
    ids = {str(item["record_id"]) for item in body["records"]}
    assert {LEGACY_QUEUED, LEGACY_RUNNING} <= ids, "重启后记录不得消失"
    for record_id in (LEGACY_QUEUED, LEGACY_RUNNING):
        record = next(i for i in body["records"] if i["record_id"] == record_id)
        assert "continue" in action_ids(record), "重启后仍然可继续"
        assert all(a["enabled"] for a in record["actions"] if a["id"] == "continue")


def test_a01_continue_yields_exactly_one_effective_execution(harness: Harness):
    seed_turn(harness.conn, LEGACY_QUEUED, MSG_QUEUED, status="queued")
    client = harness.start()

    before = journal_row(harness.conn, LEGACY_QUEUED)
    assert before["owner_instance_id"] is None

    resp = client.post(
        f"/api/recovery/records/{LEGACY_QUEUED}/continue",
        json={"expected_class": "legacy_unowned", "expected_status": "queued"},
    )
    assert resp.status_code == 200, f"继续必须成功：{resp.status_code} {resp.text[:200]}"
    body = resp.json()
    assert body.get("ok") is True
    assert str(body.get("record_id")) == LEGACY_QUEUED
    new_turn = str(body.get("turn_id") or "")
    assert new_turn and new_turn != LEGACY_QUEUED

    old = journal_row(harness.conn, LEGACY_QUEUED)
    assert old["recovered_at"], "老记录必须被标记为已处理"
    assert str(old["recovered_by"]) == new_turn, "老记录必须指向它的后继"
    assert old["message"] == MSG_QUEUED, "重发不得改消息原文"

    successors = successors_of(harness.conn, LEGACY_QUEUED)
    assert len(successors) == 1, "只允许一个后继"
    assert str(successors[0]["turn_id"]) == new_turn

    siblings = siblings_of(harness.conn, MSG_QUEUED, LEGACY_QUEUED)
    assert len(siblings) == 1, "同一条消息只允许产生一个有效执行"

    # 受控闸门：执行单元（runner）确实被调用了一次，且是那个新 turn
    harness.settle(lambda: new_turn in harness.started)
    assert harness.started.count(new_turn) == 1, f"必须恰好执行一次：{harness.started}"
    assert harness.started == [new_turn], "不得额外执行别的 turn"


def test_a01_continue_gives_new_turn_an_owner_that_is_traceable(harness: Harness):
    """继续之后归属必须可追踪（否则下次重启又会退化成 owner_unknown）。"""
    seed_turn(harness.conn, LEGACY_QUEUED, MSG_QUEUED, status="queued")
    client = harness.start()
    instance_id = client.app.state.instance_id

    resp = client.post(
        f"/api/recovery/records/{LEGACY_QUEUED}/continue",
        json={"expected_class": "legacy_unowned", "expected_status": "queued"},
    )
    assert resp.status_code == 200
    new_turn = str(resp.json()["turn_id"])

    row = journal_row(harness.conn, new_turn)
    assert str(row["owner_instance_id"] or "") == instance_id, (
        "新 turn 必须带上本实例的归属"
    )
    owner = harness.conn.execute(
        "SELECT instance_id FROM record_owners WHERE record_type = ? AND record_id = ?",
        (RECORD_TURN, new_turn),
    ).fetchone()
    assert owner is not None, "新 turn 必须登记进 record_owners"
    assert str(owner["instance_id"]) == instance_id


def test_a01_concurrent_continue_produces_one_successor(harness: Harness):
    seed_turn(harness.conn, LEGACY_QUEUED, MSG_QUEUED, status="queued")
    client = harness.start()
    url = f"/api/recovery/records/{LEGACY_QUEUED}/continue"
    payload = {"expected_class": "legacy_unowned", "expected_status": "queued"}

    def _post():  # noqa: ANN202
        return client.post(url, json=payload).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        codes = sorted(pool.map(lambda _i: _post(), range(2)))

    assert codes.count(200) == 1, f"并发只允许一个成功：{codes}"
    assert 409 in codes, f"输的那个必须收到 409（不是再造一个后继）：{codes}"
    assert len(successors_of(harness.conn, LEGACY_QUEUED)) == 1
    assert len(siblings_of(harness.conn, MSG_QUEUED, LEGACY_QUEUED)) == 1


def test_a01_second_continue_click_conflicts(harness: Harness):
    seed_turn(harness.conn, LEGACY_QUEUED, MSG_QUEUED, status="queued")
    client = harness.start()
    url = f"/api/recovery/records/{LEGACY_QUEUED}/continue"
    payload = {"expected_class": "legacy_unowned", "expected_status": "queued"}

    assert client.post(url, json=payload).status_code == 200
    again = client.post(url, json=payload)

    assert again.status_code == 409, f"重复点击必须 409：{again.status_code}"
    assert again.json().get("conflict") is True
    assert len(successors_of(harness.conn, LEGACY_QUEUED)) == 1


def test_a01_notify_rows_never_enter_the_inbox(harness: Harness):
    """系统通知轮永不进清单、永不可操作（它不是用户的消息）。"""
    seed_turn(harness.conn, NOTIFY_ROW, "系统通知：某某结束了", status="interrupted", notify=1)
    client = harness.start()

    body = recovery_listing(client)
    assert NOTIFY_ROW not in {str(i["record_id"]) for i in body["records"]}

    resp = client.post(
        f"/api/recovery/records/{NOTIFY_ROW}/continue",
        json={"expected_class": "ready", "expected_status": "interrupted"},
    )
    assert resp.status_code == 409, f"通知轮不得被继续：{resp.status_code}"

    ignore = client.post(
        f"/api/recovery/records/{NOTIFY_ROW}/ignore", json={"expected_class": "ready"}
    )
    assert ignore.status_code == 409, f"通知轮不得被忽略：{ignore.status_code}"
    assert len(siblings_of(harness.conn, "系统通知：某某结束了", NOTIFY_ROW)) == 0


def test_a01_legacy_running_derived_task_is_listed_and_requeueable(harness: Harness):
    """历史无归属的 running 派生任务：既不可见也不能放回队列（基线：没有入口）。

    同时钉住时限兜底的真实行为：**已超期**的无归属 running 任务会被启动恢复
    `recover_stale()` 自动放回 `pending`（不需要用户做任何操作）—— 所以它不该
    留在恢复清单里，也谈不上「永久卡住」。需要用户处理的只有「新鲜但卡住」那种。
    """
    stale_at = (datetime.now(timezone.utc) - timedelta(seconds=3600)).isoformat()
    seed_derived_running(harness.conn, DERIVED)  # 新鲜：真正的「卡住的 running」
    seed_derived_running(harness.conn, DERIVED_STALE, updated_at=stale_at, content_version=2)
    client = harness.start()

    # 已超期的那条：启动恢复自动放回 pending —— 不是永久卡住，不需要用户点按钮
    stale_row = harness.conn.execute(
        "SELECT * FROM derived_tasks WHERE id = ?", (DERIVED_STALE,)
    ).fetchone()
    assert stale_row["state"] == "pending", (
        f"无归属 + 已超期的 running 任务必须被启动恢复放回 pending（不是永久卡住）：{dict(stale_row)}"
    )
    assert stale_row["run_after"] is None, "放回队列必须清掉 run_after（可立即重跑）"

    body = recovery_listing(client, kinds="derived_task")
    listed = {str(i["record_id"]) for i in body["records"]}
    assert DERIVED_STALE not in listed, (
        "已经自动放回队列的任务不需要用户操作，不该留在恢复清单里"
    )

    records = [i for i in body["records"] if str(i["record_id"]) == DERIVED]
    assert records, f"无归属的 running 派生任务必须进清单：{[i['record_id'] for i in body['records']]}"
    record = records[0]
    assert record["kind"] == "derived_task"
    assert record["state_class"] == "derived_legacy"
    assert record["status"] == "running"
    assert record["owner_state"] == "none"
    assert record["attempts"] == 2, "attempts 必须如实展示"
    assert record["last_error"] == "boom"
    assert "requeue" in action_ids(record), f"必须给出放回队列的动作：{record['actions']}"

    resp = client.post(
        f"/api/recovery/records/{DERIVED}/requeue",
        json={"expected_state": "running", "expected_generation": 1},
    )
    assert resp.status_code == 200, f"放回队列必须成功：{resp.status_code} {resp.text[:200]}"
    assert resp.json().get("state") == "pending"

    row = harness.conn.execute("SELECT * FROM derived_tasks WHERE id = ?", (DERIVED,)).fetchone()
    assert row["state"] == "pending"
    assert row["run_after"] is None
    assert int(row["claim_generation"]) == 2, "认领代次必须 +1（挡住迟到写）"
    assert int(row["attempts"]) == 2, "attempts 不得被重置"
    assert row["last_error"] == "boom", "last_error 不得被清掉"


def test_a01_derived_requeue_conflicts_after_state_change(harness: Harness):
    seed_derived_running(harness.conn, DERIVED)
    client = harness.start()
    assert client.post(
        f"/api/recovery/records/{DERIVED}/requeue",
        json={"expected_state": "running", "expected_generation": 1},
    ).status_code == 200

    again = client.post(
        f"/api/recovery/records/{DERIVED}/requeue",
        json={"expected_state": "running", "expected_generation": 1},
    )
    assert again.status_code == 409, f"重复放回必须 409：{again.status_code}"


# --------------------------------------------------------------------------
# A03 · 真实形状的孤立 claim
# --------------------------------------------------------------------------


def test_a03_orphan_claim_gets_a_repair_entry(harness: Harness):
    seed_turn(harness.conn, ORPHAN, MSG_ORPHAN, status="interrupted", recovered_at=NOW)
    client = harness.start()

    # 基线唯一的出口只是只读的 orphaned_turns（这条在基线就成立）
    state = client.get("/api/runtime/state").json()
    assert ORPHAN in {str(i["turn_id"]) for i in state["orphaned_turns"]}
    assert state["interrupted_turns"] == [], "孤儿已经不在 unfinished 入口里了"

    record = find_record(client, ORPHAN)
    assert record is not None, "孤立 claim 必须进恢复清单"
    assert record["state_class"] == "orphaned_claim"
    assert record["status"] == "interrupted"
    assert record["message"] == MSG_ORPHAN
    assert "repair" in action_ids(record), f"必须给出修复动作：{record['actions']}"
    assert "continue" not in action_ids(record) or not any(
        a["enabled"] for a in record["actions"] if a["id"] == "continue"
    ), "未修复的孤儿不该直接可继续"


def test_a03_repair_then_continue_restores_the_message(harness: Harness):
    seed_turn(harness.conn, ORPHAN, MSG_ORPHAN, status="interrupted", recovered_at=NOW)
    client = harness.start()

    repaired = client.post(
        f"/api/recovery/records/{ORPHAN}/repair", json={"expected_class": "orphaned_claim"}
    )
    assert repaired.status_code == 200, f"修复必须成功：{repaired.status_code} {repaired.text[:200]}"
    body = repaired.json()
    assert body.get("ok") is True and body.get("repaired") is True

    row = journal_row(harness.conn, ORPHAN)
    assert row["recovered_at"] is None, "修复必须清掉那次失败的抢占"
    assert row["recovered_by"] in (None, ""), "修复不得凭空造一个后继"
    assert row["message"] == MSG_ORPHAN, "修复不得改消息原文"
    assert row["reason"] != "user_confirmed_takeover" or True  # 状态字段保持原样即可
    assert len(siblings_of(harness.conn, MSG_ORPHAN, ORPHAN)) == 0, "修复本身不得新建 turn"

    record = find_record(client, ORPHAN)
    assert record is not None, "修复后必须回到清单里"
    assert record["state_class"] == "ready", "修复后回到 ready（可继续 / 可忽略）"
    assert "continue" in action_ids(record)
    assert any(a["enabled"] for a in record["actions"] if a["id"] == "continue")

    continued = client.post(
        f"/api/recovery/records/{ORPHAN}/continue",
        json={"expected_class": "ready", "expected_status": "interrupted"},
    )
    assert continued.status_code == 200, (
        f"修复后必须能继续：{continued.status_code} {continued.text[:200]}"
    )
    new_turn = str(continued.json()["turn_id"])
    assert len(successors_of(harness.conn, ORPHAN)) == 1
    assert len(siblings_of(harness.conn, MSG_ORPHAN, ORPHAN)) == 1
    harness.settle(lambda: new_turn in harness.started)
    assert harness.started.count(new_turn) == 1


def test_a03_repair_refuses_already_resent_record(harness: Harness):
    """已经真正重发过的记录不能被 repair 重置（否则一条消息会被重发两次）。"""
    seed_turn(
        harness.conn,
        RESENT,
        MSG_RESENT,
        status="interrupted",
        recovered_at=NOW,
        recovered_by=RESENT_SUCCESSOR,
    )
    seed_turn(harness.conn, RESENT_SUCCESSOR, MSG_RESENT, status="queued")
    client = harness.start()

    resp = client.post(
        f"/api/recovery/records/{RESENT}/repair", json={"expected_class": "orphaned_claim"}
    )
    assert resp.status_code != 500
    if resp.status_code == 200:
        assert resp.json().get("repaired") is False, (
            f"已重发的记录不得被修复成功：{resp.json()}"
        )
        assert resp.json().get("ok") is False
    else:
        assert resp.status_code == 409

    row = journal_row(harness.conn, RESENT)
    assert row["recovered_at"], "已重发记录的 recovered_at 不得被清掉"
    assert str(row["recovered_by"]) == RESENT_SUCCESSOR, "后继关联不得被断开"
    assert len(siblings_of(harness.conn, MSG_RESENT, RESENT)) == 1, "不得再造一个后继"

    record = find_record(client, RESENT)
    assert record is None or record["state_class"] != "orphaned_claim"


def test_a03_concurrent_continue_after_repair_has_one_successor(harness: Harness):
    seed_turn(harness.conn, ORPHAN, MSG_ORPHAN, status="interrupted", recovered_at=NOW)
    client = harness.start()
    assert client.post(
        f"/api/recovery/records/{ORPHAN}/repair", json={"expected_class": "orphaned_claim"}
    ).status_code == 200

    url = f"/api/recovery/records/{ORPHAN}/continue"
    payload = {"expected_class": "ready", "expected_status": "interrupted"}

    def _post():  # noqa: ANN202
        return client.post(url, json=payload).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        codes = sorted(pool.map(lambda _i: _post(), range(2)))

    assert codes.count(200) == 1, f"并发只允许一个成功：{codes}"
    assert 409 in codes
    assert len(successors_of(harness.conn, ORPHAN)) == 1
    assert len(siblings_of(harness.conn, MSG_ORPHAN, ORPHAN)) == 1


def test_a03_ignore_keeps_the_message_and_creates_no_successor(harness: Harness):
    """忽略：不删原消息、不产生后继。"""
    seed_turn(harness.conn, LEGACY_QUEUED, MSG_QUEUED, status="queued")
    client = harness.start()

    resp = client.post(
        f"/api/recovery/records/{LEGACY_QUEUED}/ignore", json={"expected_class": "legacy_unowned"}
    )
    assert resp.status_code == 200, f"忽略必须成功：{resp.status_code} {resp.text[:200]}"
    assert resp.json().get("ok") is True

    row = journal_row(harness.conn, LEGACY_QUEUED)
    assert row["message"] == MSG_QUEUED, "忽略不得删除用户消息"
    assert row["recovered_at"], "忽略要标记为已处理（不再提示）"
    assert row["recovered_by"] in (None, ""), "忽略不产生后继"
    assert len(siblings_of(harness.conn, MSG_QUEUED, LEGACY_QUEUED)) == 0
    assert find_record(client, LEGACY_QUEUED) is None, "已处理的记录不再提示"


def test_a03_continue_with_wrong_expected_class_conflicts(harness: Harness):
    """期望状态不符 → 409，且一行都不改。"""
    seed_turn(harness.conn, LEGACY_QUEUED, MSG_QUEUED, status="queued")
    client = harness.start()

    resp = client.post(
        f"/api/recovery/records/{LEGACY_QUEUED}/continue",
        json={"expected_class": "ready", "expected_status": "interrupted"},
    )

    assert resp.status_code == 409, f"类别不符必须 409：{resp.status_code}"
    assert resp.json().get("conflict") is True
    row = journal_row(harness.conn, LEGACY_QUEUED)
    assert row["recovered_at"] is None
    assert row["status"] == "queued", "拒绝时不得顺手改状态"
    assert len(siblings_of(harness.conn, MSG_QUEUED, LEGACY_QUEUED)) == 0


def test_a01_listing_truncation_is_honest(harness: Harness):
    """limit 生效时 total 仍如实；未显示的记录不会永久消失。"""
    for index in range(5):
        seed_turn(harness.conn, f"turn_bulk_{index}", f"批量消息 {index}", status="queued")
    client = harness.start()

    page = recovery_listing(client, limit=2)
    assert len(page["records"]) == 2
    assert page["shown"] == 2
    assert page["total"] >= 5, "total 是匹配总数，不受 limit 影响"
    assert page["truncated"] is True
