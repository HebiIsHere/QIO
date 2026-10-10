"""反例 R6（后端 B）：保存前按**真实影响**重新核对受影响任务范围。

反例复现路径：任务 A 执行中 → 用户改了 A 依赖的材料、影响预判当时只列了 A →
等待期间任务 B 被批准并开始执行（板面 seq 不变）→ 用户拿旧 checkId 确认保存。

正确行为（本文件的断言）：

1. 保存前服务端要按候选状态重算一次真实影响；出现「预判时没有列出的运行中受影响
   任务」时，旧 checkId 必须被拒绝（409 stale_check + scopeChanged=true）；
2. 拒绝时**不落库**：直接读 sqlite 核对 seq / 材料正文 / 快照数 / 提交数都没变，
   并且 A、B 都还是 running（未授权任务没有被暂停）；
3. 重新做一次影响预判（会列出完整范围）→ 确认 → 保存生效：材料落库、相关任务暂停
   且保留进度；
4. 判定依据真实影响，不比整个任务列表或总数量：无关任务的新增 / 结束、已暂停任务
   的增减都不该造成无意义的重复确认；
5. 已暂停任务只被报告、不会被重复暂停，也不该被当成新的运行任务反复阻断保存；
6. 单项批准与批量批准（POST /api/interactive/intents/batch）结果一致。

夹具说明：真实临时 sqlite 库（pytest 的 tmp_path + apply_migrations）+ 完整 ASGI 路由；
卡片与独立意图由夹具建立（意图用 intents._insert_intent，与创建接口同一实现），
**批准一律走真实接口**（POST /intents/{id}/approve 或 /intents/batch）。
"""

from __future__ import annotations

import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from agent.api.server import create_app
from agent.config import Settings
from agent.credentials.store import MemoryKeyring
from agent.interactive import intents as intents_module
from agent.interactive import models

BOARD_A = "board_b_r6_scope"
BOARD_B = "board_b_r6_batch"

#: 演示意图标题（与 intents.DEMO_TITLES 一致；测试里直接引用常量，避免复制字符串）
T_COMBINE = intents_module.DEMO_TITLES["combine"]
T_FAILING = intents_module.DEMO_TITLES["failing"]


@pytest.fixture(autouse=True)
def _isolated_confirm_checks():
    """影响确认记录是进程内存（_CONFIRM_CHECKS）：用例之间必须隔离。"""
    with intents_module._CONFIRM_CHECK_LOCK:
        intents_module._CONFIRM_CHECKS.clear()
    yield
    with intents_module._CONFIRM_CHECK_LOCK:
        intents_module._CONFIRM_CHECKS.clear()


@pytest.fixture()
def client(db_conn: sqlite3.Connection, settings: Settings):
    app = create_app(settings, db_conn)
    app.state.ctx.credentials._kr = MemoryKeyring()
    with TestClient(app) as c:
        yield c


# --- 夹具与读取工具 -----------------------------------------------------------


def _card(cid: str, kind: str, content: str = "", **fields) -> dict:
    card = models.new_card(kind, content, **fields)
    card["id"] = cid
    return card


def _seed(client: TestClient, board: str, cards: list[dict] | None = None) -> dict:
    """建立板面：m1/m2 两份材料 + 一条注释 n1（外加调用方给的卡片）。
    m1/m2 会被演示意图 combine / failing 同时引用，因此两者都是「相关任务」。"""
    state = {
        "boardId": board,
        "seq": 0,
        "updatedAt": models.now_iso(),
        "cards": [
            _card("m1", "file", "材料一", meta={"name": "a.pdf"}),
            _card("m2", "file", "材料二", meta={"name": "b.pdf"}),
            _card("n1", "text", "整理说明", checked=True),
            *(cards or []),
        ],
        "groups": [],
        "links": [],
        "selection": [],
    }
    resp = _save(client, board, state)
    assert resp.status_code == 200, resp.text
    return resp.json()["state"]


def _state(client: TestClient, board: str) -> dict:
    resp = client.get(f"/api/interactive/boards/{board}/state")
    assert resp.status_code == 200, resp.text
    return resp.json()


def _board(client: TestClient, board: str) -> dict:
    return _state(client, board)["state"]


def _save(client: TestClient, board: str, state: dict, **extra):
    return client.put(
        f"/api/interactive/boards/{board}/state",
        json={"state": state, "reason": "test-r6", **extra},
    )


def _intents(client: TestClient, board: str) -> dict[str, dict]:
    listed = client.get(f"/api/interactive/boards/{board}/intents")
    assert listed.status_code == 200, listed.text
    return {item["title"]: item for item in listed.json()["intents"]}


def _by_id(client: TestClient, board: str) -> dict[str, dict]:
    return {item["id"]: item for item in _intents(client, board).values()}


def _demo_intents(client: TestClient, board: str) -> dict[str, dict]:
    """建演示意图：combine 与 failing 都把 m1/m2 当作 materialRefs → 两项相关任务。"""
    created = client.post(f"/api/interactive/boards/{board}/intents", json={"demo": True})
    assert created.status_code == 200, created.text
    listed = _intents(client, board)
    assert set(listed) >= {T_COMBINE, T_FAILING}, f"演示意图没有建齐：{list(listed)}"
    return listed


def _approve(client: TestClient, intent_id: str, **body) -> dict:
    resp = client.post(f"/api/interactive/intents/{intent_id}/approve", json=body or {})
    assert resp.status_code == 200, resp.text
    return resp.json()


def _approve_ok(client: TestClient, intent_id: str) -> dict:
    result = _approve(client, intent_id)
    assert result.get("ok") is True, json.dumps(result, ensure_ascii=False)
    return result


def _candidate(client: TestClient, board: str, **card_edits) -> dict:
    """在当前已保存状态上改若干卡片，作为「待保存」候选。"""
    state = _board(client, board)
    by_id = {card["id"]: card for card in state["cards"]}
    for cid, edits in card_edits.items():
        assert cid in by_id, f"候选里要改的卡片不存在：{cid}"
        by_id[cid] = {**by_id[cid], **(edits or {})}
    state["cards"] = [by_id.get(card["id"], card) for card in state["cards"]]
    return state


def _add_card(state: dict, card: dict) -> dict:
    out = json.loads(json.dumps(state))
    out["cards"].append(card)
    return out


def _impact_check(client: TestClient, board: str, state: dict, version: int | None = None) -> dict:
    body = {"stateVersion": _state(client, board)["seq"] if version is None else version,
            "changeSet": {"state": state}}
    resp = client.post(f"/api/interactive/boards/{board}/impact-check", json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()


# --- 直接读 sqlite 的事实核对工具 --------------------------------------------


def _db_seq(conn: sqlite3.Connection, board: str) -> int:
    row = conn.execute("SELECT seq FROM board_states WHERE board_id = ?", (board,)).fetchone()
    assert row is not None, "板面必须已经落库"
    return int(row["seq"])


def _db_card_content(conn: sqlite3.Connection, board: str, card_id: str) -> str:
    row = conn.execute("SELECT state FROM board_states WHERE board_id = ?", (board,)).fetchone()
    state = json.loads(row["state"])
    for card in state.get("cards") or []:
        if str(card.get("id")) == card_id:
            return str(card.get("content") or "")
    return "<缺失>"


def _db_snapshots(conn: sqlite3.Connection, board: str) -> int:
    return int(
        conn.execute(
            "SELECT COUNT(*) c FROM board_state_snapshots WHERE board_id = ?", (board,)
        ).fetchone()["c"]
    )


def _db_submissions(conn: sqlite3.Connection, board: str) -> tuple[int, int]:
    total = int(
        conn.execute(
            "SELECT COUNT(*) c FROM board_submissions WHERE board_id = ?", (board,)
        ).fetchone()["c"]
    )
    succeeded = int(
        conn.execute(
            "SELECT COUNT(*) c FROM board_submissions WHERE board_id = ? AND status = 'succeeded'",
            (board,),
        ).fetchone()["c"]
    )
    return total, succeeded


def _db_intents(conn: sqlite3.Connection, board: str) -> dict[str, dict]:
    rows = conn.execute(
        "SELECT id, title, status, progress FROM board_intents WHERE board_id = ? ORDER BY rowid",
        (board,),
    ).fetchall()
    return {
        row["id"]: {
            "title": row["title"],
            "status": row["status"],
            "progress": json.loads(row["progress"] or "{}"),
        }
        for row in rows
    }


def _db_fingerprint(conn: sqlite3.Connection, board: str) -> dict:
    """落库事实的指纹：任何一项变化都说明「拒绝时不该有写入」被破坏了。"""
    total, succeeded = _db_submissions(conn, board)
    return {
        "seq": _db_seq(conn, board),
        "cards": {
            card_id: _db_card_content(conn, board, card_id) for card_id in ("m1", "m2", "n1")
        },
        "snapshots": _db_snapshots(conn, board),
        "submissions": total,
        "succeeded": succeeded,
        "intents": {iid: (info["status"], info["progress"].get("done"), info["progress"].get("total"))
                    for iid, info in _db_intents(conn, board).items()},
    }


def _assert_fingerprint_same(before: dict, after: dict) -> None:
    assert before == after, "被拒绝的保存不得落库 / 不得推进快照 / 不得改任务状态：" + json.dumps(
        {"before": before, "after": after}, ensure_ascii=False
    )


# --- 场景 1：R6 核心反例 —— 等待期间 B 被批准并运行，板面 seq 不变 ------------


def test_new_running_task_cannot_be_released_by_old_check_id(
    client: TestClient, db_conn: sqlite3.Connection
):
    """A 运行中 → 只按「改 m1、只影响 A」做了预判 → 等待期间 B 被批准并开始执行
    （板面 seq 不变）→ 用旧 checkId 保存：必须被拒，且不落库、不暂停任何任务。"""
    _seed(client, BOARD_A)
    tasks = _demo_intents(client, BOARD_A)
    task_a = tasks[T_COMBINE]
    task_b = tasks[T_FAILING]
    _approve_ok(client, task_a["id"])
    assert _by_id(client, BOARD_A)[task_a["id"]]["status"] == "running"
    assert _by_id(client, BOARD_A)[task_b["id"]]["status"] == "pending"

    _state(client, BOARD_A)
    candidate = _candidate(client, BOARD_A, m1={"content": "材料一（替换）"})

    # 预判当时：只有 A 在执行中 → 只列 A，checkId 也只绑定 A
    check = _impact_check(client, BOARD_A, candidate)
    assert check["ok"] is True
    assert check["impactConfirmationRequired"] is True
    assert [item["intentId"] for item in check["affectedTasks"]] == [task_a["id"]]
    with intents_module._CONFIRM_CHECK_LOCK:
        record = dict(intents_module._CONFIRM_CHECKS[check["checkId"]])
    assert record["runningTaskIds"] == [task_a["id"]], "记录必须区分需要确认的运行中任务"
    assert record["taskIds"] == [task_a["id"]]

    # 等待期间：B 被**真实接口**批准并开始执行；板面完全没有保存过 → seq 不变
    before_approve = _state(client, BOARD_A)
    _approve_ok(client, task_b["id"])
    after_approve = _state(client, BOARD_A)
    assert after_approve["seq"] == before_approve["seq"], "批准任务不改板面：seq 不变"
    statuses = {iid: item["status"] for iid, item in _by_id(client, BOARD_A).items()}
    assert statuses[task_a["id"]] == "running" and statuses[task_b["id"]] == "running"

    # 保存同一份预判过的候选：候选内容没变，只多了预判时没有列出的运行中任务 B
    assert _state(client, BOARD_A)["seq"] == check["stateVersion"]
    fingerprint_before = _db_fingerprint(db_conn, BOARD_A)
    rejected = _save(client, BOARD_A, candidate, confirm={"checkId": check["checkId"]})
    assert rejected.status_code == 409, rejected.text
    detail = rejected.json()["detail"]
    assert detail["error"] == "stale_check"
    assert detail.get("scopeChanged") is True, "范围变化必须被明确标出来"
    assert detail.get("reason"), "必须给出「为什么要重新说明」的原因"
    assert task_b["title"] in detail["reason"] or "重新" in detail["reason"]
    listed = {item["intentId"] for item in detail["affectedTasks"]}
    assert listed == {task_a["id"], task_b["id"]}, "409 要带当前全量受影响范围"
    assert {item["status"] for item in detail["affectedTasks"]} == {"running"}

    # 拒绝时：不落库、不暂停未授权任务、不推进快照
    _assert_fingerprint_same(fingerprint_before, _db_fingerprint(db_conn, BOARD_A))
    assert _db_card_content(db_conn, BOARD_A, "m1") == "材料一", "材料没有落库"
    assert _db_snapshots(db_conn, BOARD_A) == fingerprint_before["snapshots"]
    statuses = {iid: item["status"] for iid, item in _by_id(client, BOARD_A).items()}
    assert statuses[task_a["id"]] == "running", "A 没有被暂停"
    assert statuses[task_b["id"]] == "running", "未授权的新增运行任务不得被暂停"

    # 重新预判 → 完整范围 → 确认 → 生效（落库 + 相关任务暂停并保留进度）
    progress_before = {iid: item["progress"] for iid, item in _by_id(client, BOARD_A).items()}
    again = _candidate(client, BOARD_A, m1={"content": "材料一（替换）"})
    recheck = _impact_check(client, BOARD_A, again)
    assert recheck["ok"] is True
    assert {item["intentId"] for item in recheck["affectedTasks"]} == {task_a["id"], task_b["id"]}
    applied = _save(client, BOARD_A, again, confirm={"checkId": recheck["checkId"]})
    assert applied.status_code == 200, applied.text
    assert _db_card_content(db_conn, BOARD_A, "m1") == "材料一（替换）"
    paused = {item["intentId"] for item in applied.json()["materialImpact"]["paused"]}
    assert paused == {task_a["id"], task_b["id"]}
    final = _by_id(client, BOARD_A)
    assert final[task_a["id"]]["status"] == "paused"
    assert final[task_b["id"]]["status"] == "paused"
    assert final[task_a["id"]]["progress"]["done"] == progress_before[task_a["id"]]["done"]
    assert final[task_a["id"]]["progress"]["total"] == progress_before[task_a["id"]]["total"]

# --- 场景 2：新增无关运行任务不得造成无意义重复确认 ---------------------------


def _real_task(
    client: TestClient,
    conn: sqlite3.Connection,
    board: str,
    title: str,
    refs: list[str],
    status: str = "running",
) -> dict:
    """夹具建立合法独立意图（材料依据明确），并经真实 approve 状态机进入 running。

    只用于「与被改动材料无关的运行中任务」这类场景；本文件断言的行为全部走真实接口。
    执行者身份用**当前 app 的 instance_id**：否则恢复流程会把它当成别的进程遗留的
    任务降级为暂停（那是另一个契约，不是本文件要测的东西）。
    """
    created = intents_module._insert_intent(
        conn, board_id=board, title=title, status="pending", material_refs=refs
    )
    if status == "running":
        result = intents_module.approve_intent(
            conn, created["id"], instance_id=getattr(client.app.state, "instance_id", None)
        )
        assert result.get("ok") is True, result
    return created


def test_unrelated_new_running_task_does_not_block_save(
    client: TestClient, db_conn: sqlite3.Connection
):
    """新增的运行中任务不依赖被改动的材料 → 不是新的受影响任务，旧确认仍然有效。"""
    _seed(client, BOARD_A, cards=[_card("m3", "file", "材料三", meta={"name": "c.pdf"})])
    tasks = _demo_intents(client, BOARD_A)
    task_a = tasks[T_COMBINE]
    _approve_ok(client, task_a["id"])  # A：材料依据 m1/m2
    task_e = _real_task(client, db_conn, BOARD_A, "只看材料三的任务", ["m3"])  # E：材料依据 m3

    candidate = _candidate(client, BOARD_A, m1={"content": "材料一（替换）"})
    check = _impact_check(client, BOARD_A, candidate)
    assert check["ok"] is True
    assert [item["intentId"] for item in check["affectedTasks"]] == [task_a["id"]], (
        "材料三没有被改动 → 只看材料三的运行中任务不在受影响范围里"
    )
    with intents_module._CONFIRM_CHECK_LOCK:
        assert intents_module._CONFIRM_CHECKS[check["checkId"]]["runningTaskIds"] == [task_a["id"]]

    saved = _save(client, BOARD_A, candidate, confirm={"checkId": check["checkId"]})
    assert saved.status_code == 200, saved.text
    assert [item["intentId"] for item in saved.json()["materialImpact"]["paused"]] == [task_a["id"]]
    final = _by_id(client, BOARD_A)
    assert final[task_e["id"]]["status"] == "running", "无关任务不该被暂停"
    assert final[task_a["id"]]["status"] == "paused"

    # 反向对照：真正相关的新增运行任务必须仍然被拦住
    task_f = _real_task(client, db_conn, BOARD_A, "也依赖材料一的任务", ["m1"])
    other = _candidate(client, BOARD_A, m1={"content": "材料一（再替换）"})
    other_check = _impact_check(client, BOARD_A, other)
    assert task_f["id"] in {item["intentId"] for item in other_check["affectedTasks"]}


# --- 场景 3：相关任务结束（不再运行）不算「新的运行中任务」 -------------------


def test_finished_running_task_is_not_treated_as_new(
    client: TestClient, db_conn: sqlite3.Connection
):
    """等待期间另一项相关任务被执行完 → 运行中被影响集合缩小，不算新影响。"""
    _seed(client, BOARD_A)
    tasks = _demo_intents(client, BOARD_A)
    task_a, task_b = tasks[T_COMBINE], tasks[T_FAILING]
    _approve_ok(client, task_a["id"])
    _approve_ok(client, task_b["id"])
    candidate = _candidate(client, BOARD_A, m1={"content": "材料一（替换）"})
    check = _impact_check(client, BOARD_A, candidate)
    assert {item["intentId"] for item in check["affectedTasks"]} == {task_a["id"], task_b["id"]}

    advanced = client.post(
        f"/api/interactive/intents/{task_b['id']}/demo/advance", json={"outcome": "done"}
    )
    assert advanced.status_code == 200, advanced.text
    assert advanced.json()["ok"] is True
    assert _by_id(client, BOARD_A)[task_b["id"]]["status"] == "done"

    newest = _candidate(client, BOARD_A, m1={"content": "材料一（对最新板面重新改写）"})
    fresh_check = _impact_check(client, BOARD_A, newest)
    assert [item["intentId"] for item in fresh_check["affectedTasks"]] == [task_a["id"]], (
        "已经结束的任务不再运行，也不算受影响任务"
    )
    saved = _save(client, BOARD_A, newest, confirm={"checkId": fresh_check["checkId"]})
    assert saved.status_code == 200, saved.text
    final = _by_id(client, BOARD_A)
    assert final[task_a["id"]]["status"] == "paused"
    assert final[task_b["id"]]["status"] == "done", "已完成的任务不得被改回暂停"


# --- 场景 4：相关任务已经处于暂停 → 只报告、不重复阻断保存 --------------------


def test_paused_task_is_reported_but_does_not_block_repeated_save(
    client: TestClient, db_conn: sqlite3.Connection
):
    """A 已经因为材料变化暂停；再保存同一份改动时它只被报告，不得被当成新的运行任务。"""
    _seed(client, BOARD_A)
    tasks = _demo_intents(client, BOARD_A)
    task_a = tasks[T_COMBINE]
    _approve_ok(client, task_a["id"])

    first = _candidate(client, BOARD_A, m1={"content": "材料一（替换）"})
    check = _impact_check(client, BOARD_A, first)
    saved = _save(client, BOARD_A, first, confirm={"checkId": check["checkId"]})
    assert saved.status_code == 200, saved.text
    assert _by_id(client, BOARD_A)[task_a["id"]]["status"] == "paused"

    again = _candidate(client, BOARD_A, m1={"content": "材料一（替换）"})
    recheck = _impact_check(client, BOARD_A, again)
    assert recheck["ok"] is True
    assert recheck["impactConfirmationRequired"] is False, "只剩已暂停任务 → 不需要额外确认"
    assert [item["status"] for item in recheck["affectedTasks"]] == ["paused"]
    assert recheck["affectedTasks"][0]["consequence"], "已暂停任务仍要如实说明后果"

    retry = _save(client, BOARD_A, again, confirm={"checkId": recheck["checkId"]})
    assert retry.status_code == 200, retry.text
    assert retry.json()["materialImpact"]["paused"] == [], "已暂停任务不得被重复暂停"
    assert _by_id(client, BOARD_A)[task_a["id"]]["status"] == "paused"


# --- 场景 5：批量批准与单项批准结果一致 ---------------------------------------


def test_batch_approval_has_same_effect_as_single(client: TestClient, db_conn: sqlite3.Connection):
    """同样的时间线（预判未列入的任务被批准为运行中）在批量批准路径下结论一致。"""
    _seed(client, BOARD_B)
    tasks = _demo_intents(client, BOARD_B)
    task_a, task_b = tasks[T_COMBINE], tasks[T_FAILING]
    _approve_ok(client, task_a["id"])
    candidate = _candidate(client, BOARD_B, m1={"content": "材料一（替换）"})
    check = _impact_check(client, BOARD_B, candidate)
    assert [item["intentId"] for item in check["affectedTasks"]] == [task_a["id"]]

    batch = client.post(
        "/api/interactive/intents/batch", json={"approve": [task_b['id']]}
    )
    assert batch.status_code == 200, batch.text
    assert _by_id(client, BOARD_B)[task_b["id"]]["status"] == "running", batch.text
    assert _state(client, BOARD_B)["seq"] == check["stateVersion"], "批量批准不推进板面版本"

    fingerprint_before = _db_fingerprint(db_conn, BOARD_B)
    rejected = _save(
        client,
        BOARD_B,
        _candidate(client, BOARD_B, m1={"content": "材料一（替换）"}),
        confirm={"checkId": check["checkId"]},
    )
    assert rejected.status_code == 409, rejected.text
    detail = rejected.json()["detail"]
    assert detail["error"] == "stale_check" and detail["scopeChanged"] is True
    assert {item["intentId"] for item in detail["affectedTasks"]} == {task_a["id"], task_b["id"]}
    _assert_fingerprint_same(fingerprint_before, _db_fingerprint(db_conn, BOARD_B))
    assert _by_id(client, BOARD_B)[task_b["id"]]["status"] == "running"

    settled = _candidate(client, BOARD_B, m1={"content": "材料一（替换）"})
    recheck = _impact_check(client, BOARD_B, settled)
    assert {item["intentId"] for item in recheck["affectedTasks"]} == {task_a["id"], task_b["id"]}
    applied = _save(client, BOARD_B, settled, confirm={"checkId": recheck["checkId"]})
    assert applied.status_code == 200, applied.text
    assert _by_id(client, BOARD_B)[task_a["id"]]["status"] == "paused"
    assert _by_id(client, BOARD_B)[task_b["id"]]["status"] == "paused"


# --- 场景 6：确认之后范围又变 → 仍按 stale_check 拒绝（不落库） ---------------


def test_scope_change_after_confirmation_is_rejected_without_writing(
    client: TestClient, db_conn: sqlite3.Connection
):
    """确认之后板面内容又变了 → 也按 stale_check 拒绝；scopeChanged 如实标注为 false。"""
    _seed(client, BOARD_A)
    tasks = _demo_intents(client, BOARD_A)
    _approve_ok(client, tasks[T_COMBINE]["id"])
    candidate = _candidate(client, BOARD_A, m1={"content": "材料一（替换）"})
    check = _impact_check(client, BOARD_A, candidate)

    # 等待 / 确认期间板面又保存了一次（加了新卡片）
    later = _add_card(_board(client, BOARD_A), _card("n9", "text", "后来又加的注释"))
    assert _save(client, BOARD_A, later).status_code == 200

    fingerprint_before = _db_fingerprint(db_conn, BOARD_A)
    rejected = _save(client, BOARD_A, candidate, confirm={"checkId": check["checkId"]})
    assert rejected.status_code == 409, rejected.text
    detail = rejected.json()["detail"]
    assert detail["error"] == "stale_check"
    assert detail["scopeChanged"] is False
    assert detail["affectedTasks"], "仍然要有当前受影响范围"
    assert _db_card_content(db_conn, BOARD_A, "m1") == "材料一", "确认过期 → 材料不得落库"
    _assert_fingerprint_same(fingerprint_before, _db_fingerprint(db_conn, BOARD_A))





