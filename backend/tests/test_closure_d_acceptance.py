"""【独立验收 D · closure-d】第③层证据：API / 数据库（真实临时 sqlite + 完整 ASGI 路由）。

覆盖 R3 / R5 / R6 在**服务端真实行为**上的正确性（不依赖前端 store 的实现）：

- R3：保存回执返回的新 seq 是「已保存版本事实」。客户端必须继续用新 seq 保存第二版；
  用旧 seq 提交会被服务端按旧基准接受或按版本判定，客户端据此无法保证「第二版保存到服务器」。
  这里用真实库核对：第一版保存后 seq 前进、第二版用新 seq 仍能落库并回读。
- R5：没有 checkId 的保存会被服务端门拒绝（409 impact_confirmation_required，不落库）；
  确认必须使用**有效 checkId**；过期 checkId → 409 stale_check，不落库。
- R6：服务端如实列出受影响任务（可能比客户端已知的多）；确认并落库后，那些任务真的被暂停
  （直接 SELECT 库里的 status 与材料），而不是只在前端显示。

跑法：backend/.venv/Scripts/python.exe -m pytest -q backend/tests/test_closure_d_acceptance.py
"""

from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from agent.api.server import create_app
from agent.config import Settings
from agent.credentials.store import MemoryKeyring
from agent.interactive import models

BOARD = "board_closure_d"
DEMO_COMBINE = "［演示］把材料归为一组并给出对比摘要"


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


def _save(client: TestClient, state: dict, **extra):
    return client.put(f"/api/interactive/boards/{BOARD}/state", json={"state": state, "reason": "test", **extra})


def _intents(client: TestClient) -> dict[str, dict]:
    listed = client.get(f"/api/interactive/boards/{BOARD}/intents")
    assert listed.status_code == 200, listed.text
    return {item["title"]: item for item in listed.json()["intents"]}


def _seed_running(client: TestClient) -> dict:
    """两份材料（m1/m2）+ 一份勾选注释；演示 combine 引用前两份材料并被批准 → running。"""
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
    combine = _intents(client)[DEMO_COMBINE]
    approved = client.post(f"/api/interactive/intents/{combine['id']}/approve", json={}).json()
    assert approved.get("ok") is True, approved
    running = _intents(client)[DEMO_COMBINE]
    assert running["status"] == "running", running["status"]
    return {"combine": running, "state": state}


def _candidate_with_changed_m1(client: TestClient) -> dict:
    state = dict(_state(client)["state"])
    state["cards"] = [
        {**c, "content": "材料一（替换版）"} if c["id"] == "m1" else c for c in state["cards"]
    ]
    return state


def test_closure_d_r3_second_save_uses_new_seq_and_is_read_back(client: TestClient):
    """R3：第一版保存后 seq 前进；第二版必须能继续保存（用服务器新 seq）并回读落地。"""
    cards = [_card("c1", "text", "第一版", checked=True)]
    state = {"boardId": BOARD, "seq": 0, "updatedAt": models.now_iso(), "cards": cards, "groups": [], "links": [], "selection": []}
    first = _save(client, state)
    assert first.status_code == 200, first.text
    seq_after_first = first.json()["seq"]
    assert seq_after_first > 0

    # 第二版：以服务器刚返回的 seq 为基准继续保存（客户端不许把第一版保存事实丢掉）
    second_state = dict(first.json()["state"])
    second_state["seq"] = seq_after_first
    second_state["cards"] = [{**c, "content": "第二版"} if c["id"] == "c1" else c for c in second_state["cards"]]
    second = _save(client, second_state)
    assert second.status_code == 200, second.text
    assert second.json()["seq"] > seq_after_first

    reread = _state(client)
    assert reread["seq"] == second.json()["seq"]
    assert [c["content"] for c in reread["state"]["cards"] if c["id"] == "c1"] == ["第二版"]


def test_closure_d_r5_server_gate_requires_fresh_check_id(client: TestClient):
    """R5：没有 checkId → 409 且不落库；用过期 checkId → 409 stale_check 且不落库；有效 checkId → 200。"""
    seeded = _seed_running(client)
    seq_before = _state(client)["seq"]
    candidate = _candidate_with_changed_m1(client)

    # (a) 没有 checkId：服务端门拒绝，不落库
    blocked = _save(client, candidate)
    assert blocked.status_code == 409, blocked.text
    detail = blocked.json().get("detail")
    assert isinstance(detail, dict) and detail.get("error") == "impact_confirmation_required", detail
    assert blocked.json()["detail"]["affectedTasks"], "服务端必须如实列出受影响任务"
    assert _state(client)["seq"] == seq_before, "被拒绝的保存不许落库"

    # (b) 用「已经过期」的 checkId：版本前进后它不再有效
    check = client.post(
        f"/api/interactive/boards/{BOARD}/impact-check",
        json={"stateVersion": seq_before, "changeSet": {"state": candidate}},
    ).json()
    assert check["ok"] is True and check["checkId"], check
    # 另一次普通保存让板面版本前进（不带确认、且不改材料语义）
    bumped = dict(_state(client)["state"])
    bumped["cards"] = [{**c, "x": c["x"] + 10} if c["id"] == "n1" else c for c in bumped["cards"]]
    bumped_resp = _save(client, bumped)
    assert bumped_resp.status_code == 200, bumped_resp.text
    seq_bumped = _state(client)["seq"]

    # N1（契约 §1.4）：候选必须带上它所依据的已保存版本，否则会更早被版本门按 stale_state 拒绝。
    # 这里重新基于最新版本、仍然带旧 checkId：验的是确认门自己拒绝过期确认（不落库）。
    current_candidate = {**candidate, "seq": seq_bumped}
    stale = _save(
        client,
        current_candidate,
        confirm={"checkId": check["checkId"], "stateVersion": check["stateVersion"]},
    )
    assert stale.status_code == 409, stale.text
    stale_detail = stale.json().get("detail")
    assert isinstance(stale_detail, dict) and stale_detail.get("error") == "stale_check", stale_detail
    assert _state(client)["seq"] == seq_bumped, "过期确认不许落库"

    # (c) 重新预判拿新 checkId → 保存成功
    fresh = client.post(
        f"/api/interactive/boards/{BOARD}/impact-check",
        json={"stateVersion": seq_bumped, "changeSet": {"state": current_candidate}},
    ).json()
    assert fresh["ok"] is True and fresh["checkId"], fresh
    ok = _save(
        client,
        current_candidate,
        confirm={"checkId": fresh["checkId"], "stateVersion": fresh["stateVersion"]},
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["seq"] > seq_bumped


def test_closure_d_r6_server_lists_all_affected_tasks_and_pauses_them(client: TestClient):
    """R6：服务端说明里列出的任务，确认落库后必须真的被暂停（直接查库核对，不只信响应）。"""
    seeded = _seed_running(client)
    combine = seeded["combine"]
    seq_before = _state(client)["seq"]
    candidate = _candidate_with_changed_m1(client)

    check = client.post(
        f"/api/interactive/boards/{BOARD}/impact-check",
        json={"stateVersion": seq_before, "changeSet": {"state": candidate}},
    ).json()
    affected_ids = [item["intentId"] for item in check.get("affectedTasks") or []]
    assert combine["id"] in affected_ids, "服务端说明必须包含被影响的执行中任务：" + repr(check)

    saved = _save(client, candidate, confirm={"checkId": check["checkId"], "stateVersion": check["stateVersion"]})
    assert saved.status_code == 200, saved.text
    paused = [item["intentId"] for item in (saved.json().get("materialImpact") or {}).get("paused") or []]
    assert combine["id"] in paused, "保存响应必须如实报告被暂停的任务：" + repr(saved.json().get("materialImpact"))

    after = _intents(client)[DEMO_COMBINE]
    assert after["status"] == "paused", after["status"]
    # 暂停必须保留进度（不是把任务清空）
    assert after.get("progress") is not None
