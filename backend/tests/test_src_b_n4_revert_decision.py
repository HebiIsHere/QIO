"""【子智能体 B · N4】撤回决定执行前重核（本轮契约 §2 / §5）。

分层：
- ③ ASGI + 真实临时 sqlite：完整路由 + 直接 SELECT 核对「真正存储结果」与说明；
- 覆盖：等待期间新正文 / 新关系 / 新组成员 / 其他工作依赖、对象已不存在、对象未变化、
  重复请求、未展示项不得顺带处理、旧格式（无依据）记录、decisionIds 形状校验。

基线（b3245e5）上这些用例是红的：revert_rest 直接按旧对象 id 删除当前卡片，
新正文被删、原说明不更新，且不接受 decisionIds。
"""

from __future__ import annotations

import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from agent.api import interactive_intents as api_module
from agent.api.server import create_app
from agent.config import Settings
from agent.credentials.store import MemoryKeyring
from agent.interactive import intents, models

BOARD = "board_src_b_n4"
T_COMBINE = intents.DEMO_TITLES["combine"]
T_SEPARATE = intents.DEMO_TITLES["separate"]
T_FAILING = intents.DEMO_TITLES["failing"]


@pytest.fixture(autouse=True)
def _isolated_confirm_checks():
    """影响确认记录是进程内存：用例之间必须隔离。"""
    with intents._CONFIRM_CHECK_LOCK:
        intents._CONFIRM_CHECKS.clear()
    yield
    with intents._CONFIRM_CHECK_LOCK:
        intents._CONFIRM_CHECKS.clear()


@pytest.fixture()
def client(db_conn: sqlite3.Connection, settings: Settings):
    app = create_app(settings, db_conn)
    app.state.ctx.credentials._kr = MemoryKeyring()
    with TestClient(app) as c:
        yield c


# --- 脚手架 -------------------------------------------------------------


def _card(cid: str, kind: str, content: str = "", **fields) -> dict:
    card = models.new_card(kind, content, **fields)
    card["id"] = cid
    return card


def _meta(client: TestClient) -> dict:
    resp = client.get(f"/api/interactive/boards/{BOARD}/state")
    assert resp.status_code == 200, resp.text
    return resp.json()


def _put_state(client: TestClient, state: dict):
    return client.put(
        f"/api/interactive/boards/{BOARD}/state", json={"state": state, "reason": "test"}
    )


def _edit(client: TestClient, mutator) -> dict:
    """以当前已保存版本为基准改板面（N1：候选必须带上所依据的 seq）。"""
    state = json.loads(json.dumps(_meta(client)["state"]))
    mutator(state)
    resp = _put_state(client, state)
    assert resp.status_code == 200, resp.text
    return state


def _seed(client: TestClient) -> dict:
    state = {
        "boardId": BOARD,
        "seq": 0,
        "cards": [
            _card("m1", "file", "材料一", meta={"name": "a.pdf"}),
            _card("m2", "file", "材料二", meta={"name": "b.pdf"}),
            _card("n1", "text", "整理说明", checked=True),
        ],
        "groups": [],
        "links": [],
        "selection": [],
    }
    assert _put_state(client, state).status_code == 200
    created = client.post(f"/api/interactive/boards/{BOARD}/intents", json={"demo": True})
    assert created.status_code == 200, created.text
    return {item["title"]: item for item in created.json()["created"]}


def _advance(
    client: TestClient, intent_id: str, outcome: str, decision_ids: list[str] | None = None
) -> dict:
    body: dict = {"outcome": outcome}
    if decision_ids is not None:
        body["decisionIds"] = decision_ids
    resp = client.post(f"/api/interactive/intents/{intent_id}/demo/advance", json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _approve_and_done(client: TestClient, intent_id: str) -> list[str]:
    approved = client.post(f"/api/interactive/intents/{intent_id}/approve", json={})
    assert approved.status_code == 200 and approved.json()["intent"]["status"] == "running"
    done = _advance(client, intent_id, "done")
    assert done["ok"] is True, done
    return list(done["intent"]["applied"]["cardIds"])


def _stored_card(db_conn: sqlite3.Connection, card_id: str) -> dict | None:
    row = db_conn.execute(
        "SELECT state FROM board_states WHERE board_id = ?", (BOARD,)
    ).fetchone()
    assert row is not None
    state = json.loads(row["state"])
    for card in state.get("cards") or []:
        if str(card.get("id")) == card_id:
            return card
    return None


def _stored_revert(db_conn: sqlite3.Connection, intent_id: str) -> dict:
    row = db_conn.execute(
        "SELECT revert FROM board_intents WHERE id = ?", (intent_id,)
    ).fetchone()
    assert row is not None
    return models.loads(row["revert"], {})


def _stored_reason(db_conn: sqlite3.Connection, intent_id: str) -> str:
    row = db_conn.execute(
        "SELECT reason FROM board_intents WHERE id = ?", (intent_id,)
    ).fetchone()
    assert row is not None
    return str(row["reason"] or "")


def _link_by_meaning(db_conn: sqlite3.Connection, meaning: str) -> dict | None:
    row = db_conn.execute(
        "SELECT state FROM board_states WHERE board_id = ?", (BOARD,)
    ).fetchone()
    state = json.loads(row["state"])
    for link in state.get("links") or []:
        if str(link.get("meaning") or "") == meaning:
            return link
    return None


def _failing_with_pending_card(client: TestClient) -> tuple[str, str]:
    """失败任务 + 一张「撤回会影响别的工作」的结果卡片 → 撤回报告里出现待决定项。"""
    tasks = _seed(client)
    target = tasks[T_FAILING]
    (card_id,) = _approve_and_done(client, target["id"])
    # 用户把结果卡片与自己的注释连起来：撤回它会动到别的工作
    note_id = next(c["id"] for c in _meta(client)["state"]["cards"] if c["id"] == "n1")

    def add_link(state: dict) -> None:
        state["links"].append(models.new_link(card_id, note_id, meaning="我的依据"))

    _edit(client, add_link)
    failed = _advance(client, target["id"], "failed")
    pending = failed["revert"]["pendingDecision"]
    assert [p["id"] for p in pending] == [card_id], failed["revert"]
    return target["id"], card_id


def _failing_with_two_pending_cards(client: TestClient) -> tuple[str, list[str]]:
    """两项结果都进入待决定清单（用户把它们放进了自己新建的组）。"""
    tasks = _seed(client)
    target = tasks[T_SEPARATE]
    card_ids = _approve_and_done(client, target["id"])
    assert len(card_ids) == 2

    def add_group(state: dict) -> None:
        state["groups"].append(
            models.new_group("我的组", members=list(card_ids), default_name=False)
        )

    _edit(client, add_group)
    failed = _advance(client, target["id"], "failed")
    pending_ids = [p["id"] for p in failed["revert"]["pendingDecision"]]
    assert set(pending_ids) == set(card_ids), failed["revert"]
    return target["id"], card_ids


# --- 正常路径：对象未变化 → 按决定撤回，核对真正存储结果 -------------------


def test_unchanged_pending_item_reverts_and_repeat_request_is_honest(
    client: TestClient, db_conn: sqlite3.Connection
):
    intent_id, card_id = _failing_with_pending_card(client)
    # 决定之前：用户自己建立的关系确实在板面上（待决定项只涉及那张卡片）
    before_link = _link_by_meaning(db_conn, "我的依据")
    assert before_link is not None and before_link["deleted"] is False

    decided = _advance(client, intent_id, "revert_rest", decision_ids=[card_id])
    assert decided["ok"] is True, decided
    assert decided["decision"]["processed"] == [card_id]
    assert decided["decision"]["reconfirmed"] == []
    assert decided["decision"]["skipped"] == []
    assert decided["decision"]["remaining"] == []
    assert decided["decision"]["requested"] == [card_id]

    # 真正存储结果：卡片真的被撤回
    stored = _stored_card(db_conn, card_id)
    assert stored is not None and stored["deleted"] is True
    # 端点被撤回后，指向它的关系不再满足 G4（必须是活卡片之间）→ 不再留在板面上。
    # 这是「用户已授权的撤回」的直接后果，不是旧决定顺带删除未展示的东西。
    assert _link_by_meaning(db_conn, "我的依据") is None
    assert _stored_revert(db_conn, intent_id)["pendingDecision"] == []
    assert "撤回" in _stored_reason(db_conn, intent_id)

    # 重复请求：待决定清单已经空 → 如实说明「没有等待决定的撤回项」，不改动任何内容
    again = _advance(client, intent_id, "revert_rest", decision_ids=[card_id])
    assert again["ok"] is False
    assert again["reason"] == "nothing_pending"
    assert _stored_card(db_conn, card_id)["deleted"] is True


# --- 等待期间新正文：不删除，保留改动并重新说明 ----------------------------


def test_content_changed_after_explanation_is_kept_and_re_explained(
    client: TestClient, db_conn: sqlite3.Connection
):
    intent_id, card_id = _failing_with_pending_card(client)

    def write_new_text(state: dict) -> None:
        for card in state["cards"]:
            if card["id"] == card_id:
                card["content"] = "用户后来写的新正文"

    _edit(client, write_new_text)

    decided = _advance(client, intent_id, "revert_rest", decision_ids=[card_id])
    assert decided["ok"] is True, decided
    assert decided["decision"]["processed"] == []
    assert decided["decision"]["reconfirmed"] == [card_id]

    # 后续内容被保留，真正存储的也是新正文
    stored = _stored_card(db_conn, card_id)
    assert stored is not None and stored["deleted"] is False
    assert stored["content"] == "用户后来写的新正文"

    # 原说明被更新：刷新后的待决定项带新的原因与依据
    pending = decided["decision"]["remaining"]
    assert pending == [card_id]
    item = [p for p in decided["revert"]["pendingDecision"] if p["id"] == card_id][0]
    assert item["kind"] == "card"
    assert "改过" in item["reason"] and item["impact"]
    assert item.get("signature")
    assert decided["decision"]["requested"] == [card_id]

    # 用户看过新说明后再次决定 → 这次才真正撤回
    again = _advance(client, intent_id, "revert_rest", decision_ids=[card_id])
    assert again["decision"]["processed"] == [card_id]
    assert _stored_card(db_conn, card_id)["deleted"] is True


# --- 等待期间新关系：不删除，重新说明 -------------------------------------


def test_new_relation_after_explanation_is_kept_and_re_explained(
    client: TestClient, db_conn: sqlite3.Connection
):
    intent_id, card_id = _failing_with_pending_card(client)

    def add_new_link(state: dict) -> None:
        state["links"].append(models.new_link(card_id, "m1", meaning="我的新关系"))

    _edit(client, add_new_link)

    decided = _advance(client, intent_id, "revert_rest", decision_ids=[card_id])
    assert decided["decision"]["processed"] == []
    assert decided["decision"]["reconfirmed"] == [card_id]
    assert _stored_card(db_conn, card_id)["deleted"] is False
    item = [p for p in decided["revert"]["pendingDecision"] if p["id"] == card_id][0]
    assert "我的新关系" in item["impact"], item
    # 用户新建立的关系没有被顺带删除
    assert _link_by_meaning(db_conn, "我的新关系")["deleted"] is False


# --- 等待期间新组成员：不删除，重新说明 -----------------------------------


def test_new_group_membership_after_explanation_is_kept_and_re_explained(
    client: TestClient, db_conn: sqlite3.Connection
):
    intent_id, card_id = _failing_with_pending_card(client)

    def add_group(state: dict) -> None:
        state["groups"].append(
            models.new_group("我新建的组", members=[card_id, "n1"], default_name=False)
        )

    _edit(client, add_group)

    decided = _advance(client, intent_id, "revert_rest", decision_ids=[card_id])
    assert decided["decision"]["processed"] == []
    assert decided["decision"]["reconfirmed"] == [card_id]
    assert _stored_card(db_conn, card_id)["deleted"] is False
    item = [p for p in decided["revert"]["pendingDecision"] if p["id"] == card_id][0]
    assert "我新建的组" in item["impact"], item


# --- 等待期间出现别的工作依赖：不删除，重新说明 ----------------------------


def test_other_work_dependency_after_explanation_is_kept_and_re_explained(
    client: TestClient, db_conn: sqlite3.Connection
):
    intent_id, card_id = _failing_with_pending_card(client)
    listed = client.get(f"/api/interactive/boards/{BOARD}/intents").json()["intents"]
    separate = next(item for item in listed if item["title"] == T_SEPARATE)
    # 另一项仍在进行的工作把这张卡片当作依据（预览引用它）
    ref = client.post(
        f"/api/interactive/intents/{separate['id']}/preview",
        json={
            "preview": {
                "cards": [{"id": card_id, "content": "以后续工作引用它"}],
                "groups": [],
                "links": [],
                "note": "",
            }
        },
    )
    assert ref.status_code == 200, ref.text

    decided = _advance(client, intent_id, "revert_rest", decision_ids=[card_id])
    assert decided["decision"]["processed"] == []
    assert decided["decision"]["reconfirmed"] == [card_id]
    assert _stored_card(db_conn, card_id)["deleted"] is False
    item = [p for p in decided["revert"]["pendingDecision"] if p["id"] == card_id][0]
    assert "另一个仍在进行的工作" in item["impact"], item


# --- 对象已经不存在 / 未展示项不得顺带处理 ---------------------------------


def test_object_already_gone_is_reported_without_deleting_anything(
    client: TestClient, db_conn: sqlite3.Connection
):
    intent_id, card_id = _failing_with_pending_card(client)

    def delete_card(state: dict) -> None:
        for card in state["cards"]:
            if card["id"] == card_id:
                card["deleted"] = True

    _edit(client, delete_card)

    decided = _advance(client, intent_id, "revert_rest", decision_ids=[card_id])
    assert decided["ok"] is True
    assert decided["decision"]["processed"] == []
    assert decided["decision"]["gone"] == [card_id]
    assert decided["decision"]["remaining"] == []
    assert any("不在板面上" in line for line in decided["revert"]["kept"])
    assert _stored_card(db_conn, card_id)["deleted"] is True


def test_decision_ids_only_process_displayed_items(
    client: TestClient, db_conn: sqlite3.Connection
):
    intent_id, card_ids = _failing_with_two_pending_cards(client)
    first, second = card_ids

    decided = _advance(client, intent_id, "revert_rest", decision_ids=[first])
    assert decided["decision"]["processed"] == [first]
    assert decided["decision"]["remaining"] == [second]
    assert _stored_card(db_conn, first)["deleted"] is True
    assert _stored_card(db_conn, second)["deleted"] is False, "未展示项不得被顺带处理"

    # 未指定的项仍在待决定清单里（核对真正存储的报告）
    stored_pending = [p["id"] for p in _stored_revert(db_conn, intent_id)["pendingDecision"]]
    assert stored_pending == [second]

    # 不传 decisionIds（兼容语义）：处理剩下的待决定项
    rest = _advance(client, intent_id, "revert_rest")
    assert rest["decision"]["processed"] == [second]
    assert rest["decision"]["remaining"] == []
    assert _stored_card(db_conn, second)["deleted"] is True


def test_unknown_decision_ids_are_skipped_and_change_nothing(
    client: TestClient, db_conn: sqlite3.Connection
):
    intent_id, card_id = _failing_with_pending_card(client)

    decided = _advance(client, intent_id, "revert_rest", decision_ids=["i_not_mine"])
    assert decided["ok"] is False
    assert decided["reason"] == "nothing_selected"
    assert decided["decision"]["skipped"] == ["i_not_mine"]
    assert _stored_card(db_conn, card_id)["deleted"] is False
    stored_pending = [p["id"] for p in _stored_revert(db_conn, intent_id)["pendingDecision"]]
    assert stored_pending == [card_id]


# --- 旧格式记录：没有内容依据时先重新说明，不按 id 直接删 ------------------


def test_legacy_pending_item_without_basis_is_reconfirmed_not_deleted(
    client: TestClient, db_conn: sqlite3.Connection
):
    intent_id, card_id = _failing_with_pending_card(client)
    legacy = {
        "reverted": [],
        "kept": [],
        "pendingDecision": [
            {"id": card_id, "reason": "撤回会影响别的工作", "impact": "它是某条关系的一端"}
        ],
        "reasonText": "旧格式报告",
    }
    db_conn.execute(
        "UPDATE board_intents SET revert = ? WHERE id = ?", (models.dumps(legacy), intent_id)
    )

    decided = _advance(client, intent_id, "revert_rest", decision_ids=[card_id])
    assert decided["decision"]["processed"] == []
    assert decided["decision"]["reconfirmed"] == [card_id]
    assert _stored_card(db_conn, card_id)["deleted"] is False, "没有依据不得按 id 直接删"
    item = [p for p in decided["revert"]["pendingDecision"] if p["id"] == card_id][0]
    assert "旧格式" in item["reason"] and item.get("signature")

    # 重新说明之后用户再决定 → 有效决定，才真正撤回
    again = _advance(client, intent_id, "revert_rest", decision_ids=[card_id])
    assert again["decision"]["processed"] == [card_id]
    assert _stored_card(db_conn, card_id)["deleted"] is True


# --- 参数校验 -------------------------------------------------------------


def test_advance_rejects_non_list_decision_ids(client: TestClient):
    tasks = _seed(client)
    target = tasks[T_FAILING]
    resp = client.post(
        f"/api/interactive/intents/{target['id']}/demo/advance",
        json={"outcome": "revert_rest", "decisionIds": "i1"},
    )
    assert resp.status_code == 400, resp.text
