"""收尾轮反例测试（C）：08 影响确认约束完整保存 / 提交链（后端三条路径）。

正确行为期望：

路径 1：影响预判会给出真实判断与说明（ok / 受影响任务 / 汇总文字）；
        预判失败时返回 {"ok": false, "reason"}，并且**不改动任何状态**。
路径 2：checkId 绑定（板面版本、待保存内容、受影响任务）。此后任何保存让版本变化，
        或待保存内容与当时确认的内容不一致 → 确认被服务端拒绝（stale_check），不落库；
        没有 checkId 而保存又会影响执行中任务 → 服务端拒绝（不许前端出错后服务端无条件生效）。
路径 3：提交请求携带 baseStateVersion（+ 可选 confirmedCheckId）；
        版本与已确认候选不一致 → stale_state，不做提交记录，不更新基准。

普通不影响任务的保存不增加确认步骤（保存未动任何执行中任务的材料 → 直接 200）。
"""

from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from agent.api.server import create_app
from agent.config import Settings
from agent.credentials.store import MemoryKeyring
from agent.interactive import models

BOARD = "board_final_c_impact"


@pytest.fixture()
def client(db_conn: sqlite3.Connection, settings: Settings):
    app = create_app(settings, db_conn)
    app.state.ctx.credentials._kr = MemoryKeyring()
    with TestClient(app) as c:
        yield c


def _card(cid: str, kind: str, content: str = "", **fields) -> dict:
    card = models.new_card(kind, content, **fields)
    card["id"] = cid
    return card


def _state(client: TestClient) -> dict:
    resp = client.get(f"/api/interactive/boards/{BOARD}/state")
    assert resp.status_code == 200, resp.text
    return resp.json()


def _board(client: TestClient) -> dict:
    return _state(client)["state"]


def _save(client: TestClient, state: dict, **extra):
    return client.put(f"/api/interactive/boards/{BOARD}/state", json={"state": state, "reason": "test", **extra})


def _intents(client: TestClient) -> dict[str, dict]:
    listed = client.get(f"/api/interactive/boards/{BOARD}/intents")
    assert listed.status_code == 200, listed.text
    return {item["title"]: item for item in listed.json()["intents"]}


def _impacted_seed(client: TestClient) -> dict:
    """m1/m2 两份材料 + 一条注释；演示.combine 依赖这两份材料并被批准 → running。"""
    cards = [
        _card("m1", "file", "材料一", meta={"name": "a.pdf"}),
        _card("m2", "file", "材料二", meta={"name": "b.pdf"}),
        _card("n1", "text", "整理说明", checked=True),
    ]
    state = {
        "boardId": BOARD,
        "seq": 0,
        "updatedAt": models.now_iso(),
        "cards": cards,
        "groups": [],
        "links": [],
        "selection": [],
    }
    resp = _save(client, state)
    assert resp.status_code == 200, resp.text
    created = client.post(f"/api/interactive/boards/{BOARD}/intents", json={"demo": True})
    assert created.status_code == 200, created.text
    combine = _intents(client)["［演示］把材料归为一组并给出对比摘要"]
    approved = client.post(f"/api/interactive/intents/{combine['id']}/approve", json={}).json()
    assert approved["ok"] is True
    return {"state": state, "combine": combine, "saveResp": resp}


def _candidate(client: TestClient, **card_edits) -> dict:
    """在当前已保存状态上改若干卡片（可带字段），作为"待保存"候选。"""
    state = _board(client)
    by_id = {card["id"]: card for card in state["cards"]}
    for cid, edits in card_edits.items():
        if isinstance(edits, dict) and cid in by_id:
            by_id[cid] = {**by_id[cid], **edits}
    state["cards"] = [by_id.get(card["id"], card) for card in state["cards"]]
    return state


# --- 路径 1：影响预判 --------------------------------------------------------


def test_impact_check_reports_affected_tasks_and_binds(client: TestClient):
    _impacted_seed(client)
    meta = _state(client)
    candidate = _candidate(client, m1={"content": "材料一（替换版）"})

    resp = client.post(
        f"/api/interactive/boards/{BOARD}/impact-check",
        json={"stateVersion": meta["seq"], "changeSet": {"state": candidate}},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["checkId"], "预判成功必须给出可确认的 checkId"
    assert body["stateVersion"] == meta["seq"]
    assert body["summary"], "必须给出一句人能读懂的汇总说明"
    affected = {item["intentId"] for item in body["affectedTasks"]}
    assert body["impactConfirmationRequired"] is True
    assert any("材料" in (item.get("materials") or [""]).__str__() for item in body["affectedTasks"]), (
        "说明要提到受影响的材料"
    )

    # 预判是只读：不改状态、不暂停任务
    after = _state(client)
    assert after["seq"] == meta["seq"]
    assert _intents(client)["［演示］把材料归为一组并给出对比摘要"]["status"] == "running"


def test_impact_check_stale_state_version_fails_honestly(client: TestClient):
    _impacted_seed(client)
    meta = _state(client)
    candidate = _candidate(client, m1={"content": "材料一（替换版）"})

    resp = client.post(
        f"/api/interactive/boards/{BOARD}/impact-check",
        json={"stateVersion": meta["seq"] - 1, "changeSet": {"state": candidate}},
    )
    assert resp.status_code == 200, resp.text  # 服务返回 ok:false，故意的旧版本要如实说明
    body = resp.json()
    assert body["ok"] is False
    assert body["reason"], "预判失败要有真实原因"


def test_impact_failure_does_not_change_anything(client: TestClient):
    _impacted_seed(client)
    meta = _state(client)
    before_state = meta["state"]

    resp = client.post(
        f"/api/interactive/boards/{BOARD}/impact-check",
        json={"stateVersion": 999999, "changeSet": {"state": {}}},
    )
    body = resp.json()
    assert body["ok"] is False

    after = _state(client)
    assert after["seq"] == meta["seq"]
    assert after["state"] == before_state


# --- 路径 2：确认绑定版本、范围与内容 ---------------------------------------


def test_save_without_confirmation_is_rejected_by_server(client: TestClient):
    _impacted_seed(client)
    before = _state(client)

    affected_and_unconfirmed = _candidate(client, m1={"content": "材料一（替换版）"})
    resp = _save(client, affected_and_unconfirmed)
    assert resp.status_code in (400, 409), f"服务端必须拦下未确认的保存：{resp.status_code}"
    detail = resp.json()["detail"]
    assert detail["error"] == "impact_confirmation_required"
    assert _state(client)["seq"] == before["seq"], "拒绝时不得落库"
    saved_card = [c for c in _state(client)["state"]["cards"] if c["id"] == "m1"][0]
    assert saved_card["content"] == "材料一"


def test_confirm_then_save_pauses_task_and_keeps_progress(client: TestClient):
    _impacted_seed(client)
    meta = _state(client)
    candidate = _candidate(client, m1={"content": "材料一（替换版）"})

    check = client.post(
        f"/api/interactive/boards/{BOARD}/impact-check",
        json={"stateVersion": meta["seq"], "changeSet": {"state": candidate}},
    ).json()
    saved = _save(client, candidate, confirm={"checkId": check["checkId"]})
    assert saved.status_code == 200, saved.text
    assert saved.json()["materialImpact"]["paused"], "确认后这次保存应把执行中任务暂停并保留进度"

    listed = _intents(client)
    combine = listed["［演示］把材料归为一组并给出对比摘要"]
    assert combine["status"] == "paused"
    assert combine["impact"]["tasks"], "任务据依赖的材料被确认改动，任务暂停并说明原因"


def test_confirm_rejected_when_version_advanced(client: TestClient):
    _impacted_seed(client)
    meta = _state(client)
    candidate = _candidate(client, m1={"content": "材料一（替换版）"})

    check = client.post(
        f"/api/interactive/boards/{BOARD}/impact-check",
        json={"stateVersion": meta["seq"], "changeSet": {"state": candidate}},
    ).json()
    assert check["ok"] is True

    # 确认期间，已保存版本变化了（一次不影响任务的普通保存：位置调整不算材料，语义也不变？位置属于状态——保存会推进 seq）
    later_state = _candidate(client, n1={"y": 480.0})
    later_save = _save(client, later_state)
    assert later_save.status_code == 200, later_save.text

    stale = _save(client, candidate, confirm={"checkId": check["checkId"]})
    assert stale.status_code in (400, 409)
    assert stale.json()["detail"]["error"] == "stale_check"
    seq_after = _state(client)["seq"]
    assert seq_after == later_save.json()["seq"], "stale 的确认不得落库"


def test_confirm_cannot_release_changes_outside_the_checked_candidate(client: TestClient):
    """预判 A 在飞期间又改了 B：晚返回的说明只列 A；确认不能顺带放行未说明的 B。"""
    _impacted_seed(client)
    meta = _state(client)

    checked = _candidate(client, m1={"content": "材料一（替换版）"})
    check = client.post(
        f"/api/interactive/boards/{BOARD}/impact-check",
        json={"stateVersion": meta["seq"], "changeSet": {"state": checked}},
    ).json()
    assert check["ok"] is True

    bigger = _candidate(client, m1={"content": "材料一（替换版）"}, m2={"content": "材料二也变了"})
    assert bigger["cards"] != checked["cards"]

    resp = _save(client, bigger, confirm={"checkId": check["checkId"]})
    assert resp.status_code in (400, 409)
    detail = resp.json()["detail"]
    assert detail["error"] == "stale_check", "待保存内容与确认过的范围不一致 → 拒绝"
    card_m2 = [c for c in _state(client)["state"]["cards"] if c["id"] == "m2"][0]
    assert card_m2["content"] == "材料二"


def test_normal_unaffected_save_needs_no_confirmation(client: TestClient):
    _impacted_seed(client)
    candidate = _candidate(client, m1={"content": "材料一（替换版）"})
    # 先按约确认，材料一真正被替换，任务暂停
    check = client.post(
        f"/api/interactive/boards/{BOARD}/impact-check",
        json={"stateVersion": _state(client)["seq"], "changeSet": {"state": candidate}},
    ).json()
    saved = _save(client, candidate, confirm={"checkId": check["checkId"]})
    assert saved.status_code == 200

    # 之后改注释（不是任何任务的材料）：普通保存，不加确认步骤
    normal = _candidate(client, n1={"content": "注释更新", "checked": True})
    resp = _save(client, normal)
    assert resp.status_code == 200, resp.text
    assert resp.json()["materialImpact"]["paused"] == [], "没有执行中任务受影响 → 不该有暂停"


# --- 路径 3：提交绑定候选版本与服务端 ---------------------------------------


def test_submit_rejected_when_base_state_version_mismatched(client: TestClient):
    _impacted_seed(client)
    current_seq = _state(client)["seq"]

    resp = client.post(
        f"/api/interactive/boards/{BOARD}/submissions",
        json={"baseStateVersion": current_seq + 777},
    )
    assert resp.status_code in (400, 409)
    detail = resp.json()["detail"]
    assert detail["error"] == "stale_state"

    listed = client.get(f"/api/interactive/boards/{BOARD}/submissions")
    assert listed.json()["submissions"] == [], "stale_state 的提交不做任何记录"


def test_submit_accepts_matching_base_state_version(client: TestClient):
    _impacted_seed(client)
    _state(client)
    body = client.post(
        f"/api/interactive/boards/{BOARD}/submissions",
        json={"baseStateVersion": _state(client)["seq"]},
    )
    assert body.status_code == 200, body.text
    assert body.json()["status"] in ("succeeded", "empty", "duplicate")


def test_submit_with_confirmed_check_rejects_other_state(client: TestClient):
    """确认过的候选与当前已保存板面不一致 → stale_state，不落库。"""
    _impacted_seed(client)
    meta = _state(client)
    candidate = _candidate(client, m1={"content": "材料一（替换版）"})

    check = client.post(
        f"/api/interactive/boards/{BOARD}/impact-check",
        json={"stateVersion": meta["seq"], "changeSet": {"state": candidate}},
    ).json()
    saved = _save(client, candidate, confirm={"checkId": check["checkId"]})
    assert saved.status_code == 200

    # 保存之后板面又发生了语义变化（加了新卡片；不影响任务，普通保存）
    extra_state = _board(client)
    extra_state["cards"].append(_card("n2", "text", "后来加的注释"))
    later = _save(client, extra_state)
    assert later.status_code == 200

    resp = client.post(
        f"/api/interactive/boards/{BOARD}/submissions",
        json={"baseStateVersion": _state(client)["seq"], "confirmedCheckId": check["checkId"]},
    )
    assert resp.status_code in (400, 409)
    assert resp.json()["detail"]["error"] == "stale_state"


def test_submit_with_confirmed_check_matching_saved_state(client: TestClient):
    _impacted_seed(client)
    candidate = _candidate(client, m1={"content": "材料一（替换版）"})

    check = client.post(
        f"/api/interactive/boards/{BOARD}/impact-check",
        json={"stateVersion": _state(client)["seq"], "changeSet": {"state": candidate}},
    ).json()
    saved = _save(client, candidate, confirm={"checkId": check["checkId"]})
    assert saved.status_code == 200

    body = client.post(
        f"/api/interactive/boards/{BOARD}/submissions",
        json={"baseStateVersion": _state(client)["seq"], "confirmedCheckId": check["checkId"]},
    )
    assert body.status_code == 200, body.text
    assert body.json()["status"] in ("succeeded", "empty", "duplicate")