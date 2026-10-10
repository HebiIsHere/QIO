"""【独立验收 D · 第八轮状态恢复收尾】第③层证据：真实临时 sqlite + 完整 ASGI 路由。

覆盖本轮后端侧的反例（不依赖前端实现）：

- N1（契约 §1.4）：普通整板写入必须按**真实版本事实**保护。
  state.seq 与当前已保存 seq 不一致（旧版或未知版本）→ 409 stale_state，
  **不落库、不推进快照**；一致时仍然正常保存（回归保护）。
- N4（契约 §2/§5）：demo/advance 的 revert_rest 只处理这次明确展示给用户的
  decisionIds；执行前必须重核对象**当前内容**，说明之后新增的编辑不许被旧决定顺带删除。

这些用例在基线 b3245e5 上必须失败（红）；服务端按契约修好后必须全部通过。
跑法：cd backend && uv run --frozen pytest tests/test_state_recovery_d_acceptance.py -q
"""

from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from agent.api.server import create_app
from agent.config import Settings
from agent.credentials.store import MemoryKeyring
from agent.interactive import models

BOARD = "board_state_recovery_d"
DEMO_SEPARATE = "［演示］保持材料分开，分别给出说明"


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


def _put(client: TestClient, state: dict, **extra):
    return client.put(
        f"/api/interactive/boards/{BOARD}/state",
        json={"state": state, "reason": "test", **extra},
    )


def _put_current(client: TestClient, mutate) -> dict:
    """按当前服务端版本保存（state.seq 取自服务端读取，保证版本一致）。"""
    payload = _state(client)
    state = payload["state"]
    mutate(state)
    resp = _put(client, state)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _cards(client: TestClient) -> dict[str, dict]:
    return {card["id"]: card for card in _state(client)["state"]["cards"]}


def _intents(client: TestClient) -> dict[str, dict]:
    resp = client.get(f"/api/interactive/boards/{BOARD}/intents")
    assert resp.status_code == 200, resp.text
    return {item["title"]: item for item in resp.json()["intents"]}


def _seed(client: TestClient) -> dict:
    """两份材料 + 一条勾选注释。"""
    cards = [
        _card("m1", "file", "材料一", meta={"name": "a.pdf"}),
        _card("m2", "file", "材料二", meta={"name": "b.pdf"}),
        _card("n1", "text", "我的注释", checked=True),
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
    first = _put(client, state)
    assert first.status_code == 200, first.text
    return {"note_id": "n1"}


def _seed_pending_revert(client: TestClient, *, link_both: bool = False) -> dict:
    """演示「保持材料分开」：执行完成后把结果卡片与用户注释连起来 → 失败撤回时进入待决定。

    返回 {"intent_id", "applied_ids", "pending_ids"}。
    """
    seed = _seed(client)
    created = client.post(f"/api/interactive/boards/{BOARD}/intents", json={"demo": True})
    assert created.status_code == 200, created.text
    target = _intents(client)[DEMO_SEPARATE]
    approved = client.post(f"/api/interactive/intents/{target['id']}/approve", json={}).json()
    assert approved.get("ok") is True, approved
    done = client.post(
        f"/api/interactive/intents/{target['id']}/demo/advance", json={"outcome": "done"}
    )
    assert done.status_code == 200, done.text
    applied = [str(x) for x in done.json()["intent"]["applied"]["cardIds"]]
    assert len(applied) >= 2, applied

    # 用户自己建立关系：结果卡片与自己的注释相连 → 撤回会动到别的工作
    targets = applied if link_both else applied[:1]

    def mutate(state: dict) -> None:
        for card_id in targets:
            state["links"].append(
                models.new_link(card_id, seed["note_id"], direction=False, meaning="我的依据")
            )

    _put_current(client, mutate)

    failed = client.post(
        f"/api/interactive/intents/{target['id']}/demo/advance", json={"outcome": "failed"}
    )
    assert failed.status_code == 200, failed.text
    report = failed.json()["revert"]
    pending = [str(item["id"]) for item in report["pendingDecision"]]
    assert pending, report
    return {"intent_id": target["id"], "applied_ids": applied, "pending_ids": pending}


def test_sr_n1_stale_whole_board_write_is_rejected(client: TestClient):
    """N1：用旧版本事实的普通整板写入必须 409 stale_state，且不落库、不推进快照。"""
    _seed(client)
    # 用户正常保存一次：版本前进，记录下新的已保存内容与 seq
    saved = _put_current(
        client, lambda state: state["cards"][0].__setitem__("content", "材料一（已保存）")
    )
    assert saved["seq"] == _state(client)["seq"]

    # 另一个页面/一次迟到的写入仍然带着**旧版本**（seq=0）和不同内容
    current = _state(client)["state"]
    stale = {**current, "seq": 0}
    stale["cards"] = [
        {**c, "content": "迟到的旧候选（会覆盖已保存版本）"} if c["id"] == "m1" else c
        for c in stale["cards"]
    ]
    stale_resp = _put(client, stale)
    assert stale_resp.status_code == 409, (
        "用旧版本事实的整板写入被接受了（N1）：" + stale_resp.text
    )
    detail = stale_resp.json().get("detail")
    assert isinstance(detail, dict), detail
    assert detail.get("error") == "stale_state", detail
    assert str(detail.get("reason") or "").strip(), "必须给出可读的真实原因"

    after = _state(client)
    assert after["seq"] == saved["seq"], "被拒绝的写入推进了版本/快照"
    assert [
        c["content"] for c in after["state"]["cards"] if c["id"] == "m1"
    ] == ["材料一（已保存）"], "被拒绝的写入落库了"


def test_sr_n1_matching_seq_still_saves(client: TestClient):
    """回归保护：版本一致时普通保存必须照常成功（保护不能变成拦住正常保存）。"""
    _seed(client)
    first = _put_current(client, lambda state: state["cards"][0].__setitem__("content", "第一版"))
    second = _put_current(client, lambda state: state["cards"][0].__setitem__("content", "第二版"))
    assert second["seq"] > first["seq"]
    assert [c["content"] for c in _state(client)["state"]["cards"] if c["id"] == "m1"] == ["第二版"]


def test_sr_n4_revert_rest_only_processes_explicitly_chosen_decisions(client: TestClient):
    """N4：revert_rest 只处理这次明确展示给用户的 decisionIds，未展示项不得被顺带处理。"""
    seeded = _seed_pending_revert(client, link_both=True)
    pending = seeded["pending_ids"]
    assert len(pending) >= 2, pending
    chosen, other = pending[0], pending[1]

    resp = client.post(
        f"/api/interactive/intents/{seeded['intent_id']}/demo/advance",
        json={"outcome": "revert_rest", "decisionIds": [chosen]},
    )
    assert resp.status_code == 200, resp.text

    cards = _cards(client)
    assert cards[chosen]["deleted"] is True, "明确选择的决定项没有被执行"
    assert cards[other]["deleted"] is False, "未被这次展示/选择的决定项被顺带处理了（N4）"
    report = resp.json().get("revert") or {}
    remaining = [str(item["id"]) for item in report.get("pendingDecision") or []]
    assert other in remaining, "未处理的项必须仍然等待用户决定：" + repr(report)


def test_sr_n4_revert_rest_rechecks_content_before_executing(client: TestClient):
    """N4：执行前重核对象当前内容 —— 说明之后新增的编辑不许被旧决定顺带删除。"""
    seeded = _seed_pending_revert(client, link_both=False)
    target = seeded["pending_ids"][0]

    # 用户在「等待决定」之后又改了这张卡片（新增内容），并新建了一条关系
    def mutate(state: dict) -> None:
        for card in state["cards"]:
            if card["id"] == target:
                card["content"] = "用户后来补写的新内容"
        state["links"].append(
            models.new_link(
                target, seeded["applied_ids"][1], direction=False, meaning="后来建立的关系"
            )
        )

    _put_current(client, mutate)
    assert _cards(client)[target]["content"] == "用户后来补写的新内容"

    resp = client.post(
        f"/api/interactive/intents/{seeded['intent_id']}/demo/advance",
        json={"outcome": "revert_rest", "decisionIds": [target]},
    )
    assert resp.status_code == 200, resp.text

    card = _cards(client)[target]
    assert card["deleted"] is False, "旧决定把等待期间新增了内容的卡片顺带删掉了（N4）"
    assert card["content"] == "用户后来补写的新内容", "等待期间的新编辑被旧决定删掉了（N4）"
    links = [
        link
        for link in _state(client)["state"]["links"]
        if link.get("meaning") == "后来建立的关系" and not link.get("deleted")
    ]
    assert links, "等待期间新建的关系被旧决定顺带删掉了（N4）"
