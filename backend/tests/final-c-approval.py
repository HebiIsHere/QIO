"""收尾轮反例测试（C）：16 材料变了，旧待审批预览仍可批准。

正确行为期望：

- 意图创建时服务端记录材料依据指纹；材料内容随后变化（仅位置 / 大小变化不算），
  保存后旧预览即被标记 needs_update（补充标记），审批时服务端**再次校验**：
  依据已变化 → 拒绝批准（needs_update + 更新原因），不管请求从单项、批量
  还是依赖等待后的再次确认进来。
- 材料没有变化时批准照常；暂停后由用户确认"按当前材料继续"照旧工作。
"""

from __future__ import annotations

import copy
import sqlite3

import pytest
from fastapi.testclient import TestClient

from agent.api.server import create_app
from agent.config import Settings
from agent.credentials.store import MemoryKeyring
from agent.interactive import models

BOARD = "board_final_c_approve"
COMBINE = "［演示］把材料归为一组并给出对比摘要"
SEPARATE = "［演示］保持材料分开，分别给出说明"
FOLLOWUP = "［演示］根据前一项的结论再整理一份清单"


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


def _save(client: TestClient, state: dict, **extra):
    return client.put(f"/api/interactive/boards/{BOARD}/state", json={"state": state, "reason": "test", **extra})


def _board(client: TestClient) -> dict:
    resp = client.get(f"/api/interactive/boards/{BOARD}/state")
    assert resp.status_code == 200, resp.text
    return resp.json()["state"]


def _intents(client: TestClient) -> dict[str, dict]:
    listed = client.get(f"/api/interactive/boards/{BOARD}/intents")
    assert listed.status_code == 200, listed.text
    return {item["title"]: item for item in listed.json()["intents"]}


def _seed(client: TestClient) -> dict:
    cards = [
        _card("m1", "file", "材料一", meta={"name": "a.pdf"}),
        _card("m2", "file", "材料二", meta={"name": "b.pdf"}),
        _card("n1", "text", "说明", checked=True),
    ]
    state = {
        "boardId": BOARD, "seq": 0, "updatedAt": models.now_iso(),
        "cards": cards, "groups": [], "links": [], "selection": [],
    }
    assert _save(client, state).status_code == 200
    created = client.post(f"/api/interactive/boards/{BOARD}/intents", json={"demo": True})
    assert created.status_code == 200, created.text
    return state


def _touch_material(client: TestClient, card_id: str, new_content: str) -> dict:
    """把材料内容改掉并保存（没有执行中任务时走普通保存）。"""
    state = _board(client)
    for card in state["cards"]:
        if card["id"] == card_id:
            card["content"] = new_content
    resp = _save(client, state)
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_material_change_blocks_pending_approval(client: TestClient):
    _seed(client)
    listed = _intents(client)
    assert listed[SEPARATE]["status"] == "pending"

    # 真实保存接口写入材料改动；用户未再提交板面
    _touch_material(client, "m1", "材料一（已替换）")
    # 保存时的补充标记：旧预览立即失效，不能等到下一次提交才被处理
    listed = _intents(client)
    assert listed[SEPARATE]["status"] == "needs_update", "材料变化后旧预览必须先失效"

    # 审批时服务端校验：拒绝批准并给出更新原因
    resp = client.post(f"/api/interactive/intents/{listed[SEPARATE]['id']}/approve", json={})
    body = resp.json()
    assert resp.status_code == 200
    assert body["ok"] is False
    assert body["reason"] == "needs_update"
    assert "材料" in body.get("detail", ""), "拒绝时必须说明更新原因"
    assert _intents(client)[SEPARATE]["status"] == "needs_update"


def test_material_change_blocks_batch_and_dependency_confirmations(client: TestClient):
    _seed(client)
    # 单独一项依赖：combine 先被批准并完成（没有别的路径碰到材料）
    listed = _intents(client)
    followup = listed[FOLLOWUP]
    assert client.post(f"/api/interactive/intents/{followup['id']}/approve", json={}).json()["ok"] is True
    assert _intents(client)[FOLLOWUP]["status"] == "waiting_dependency"

    combine = _intents(client)[COMBINE]
    client.post(f"/api/interactive/intents/{combine['id']}/approve", json={})
    client.post(f"/api/interactive/intents/{combine['id']}/demo/advance", json={"outcome": "done"})
    assert _intents(client)[FOLLOWUP]["status"] == "waiting_confirm"

    # 依赖等待期间材料变了（combine 已 done，改动不影响执行中任务 → 普通保存）
    _touch_material(client, "m1", "材料一（等待期间替换）")

    # 依赖等待后的再次确认同样要做服务端校验
    resp = client.post(
        f"/api/interactive/intents/{followup['id']}/approve", json={"confirmDependency": True}
    )
    body = resp.json()
    assert body["ok"] is False, "依赖等待后的确认不能放行依据已变化的预览"
    assert body["reason"] == "needs_update"
    assert _intents(client)[FOLLOWUP]["status"] == "needs_update"


def test_batch_approval_stays_consistent(client: TestClient):
    _seed(client)
    _touch_material(client, "m1", "材料一（批量前替换）")
    listed = _intents(client)
    batch = client.post(
        "/api/interactive/intents/batch",
        json={"approve": [listed[SEPARATE]["id"], listed[FOLLOWUP]["id"]], "reject": []},
    ).json()
    results = {item["intentId"]: item for item in batch["results"]}
    for title in (SEPARATE, FOLLOWUP):
        outcome = results[listed[title]["id"]]
        assert outcome["ok"] is False
        assert outcome["reason"] == "needs_update"
        assert _intents(client)[title]["status"] == "needs_update"


def test_position_only_change_does_not_invalidate_preview(client: TestClient):
    """普通位置移动不改变材料含义：预览不因此失效，批准照常。"""
    _seed(client)
    state = _board(client)
    for card in state["cards"]:
        if card["id"] == "m1":
            card["x"] = 333.0
            card["y"] = 999.0
    assert _save(client, state).status_code == 200

    listed = _intents(client)
    assert listed[COMBINE]["status"] == "pending", "位置变化不算材料变化"
    approved = client.post(f"/api/interactive/intents/{listed[COMBINE]['id']}/approve", json={})
    assert approved.json()["ok"] is True


def test_paused_resume_still_asks_user_and_recomputes(client: TestClient):
    _seed(client)
    listed = _intents(client)
    combine_id = listed[COMBINE]["id"]
    assert client.post(f"/api/interactive/intents/{combine_id}/approve", json={}).json()["ok"] is True

    # 执行中改材料：走完整的确认流程（预判 → 确认 → 保存）
    state = _board(client)
    for card in state["cards"]:
        if card["id"] == "m1":
            card["content"] = "材料一（确认后替换）"
    check = client.post(
        f"/api/interactive/boards/{BOARD}/impact-check",
        json={"stateVersion": client.get(f"/api/interactive/boards/{BOARD}/state").json()["seq"], "changeSet": {"state": state}},
    ).json()
    assert check["ok"] is True
    saved = _save(client, state, confirm={"checkId": check["checkId"]})
    assert saved.status_code == 200, saved.text
    assert _intents(client)[COMBINE]["status"] == "paused"

    # 不带确认不能自动继续；带确认才按当前材料继续
    first = client.post(f"/api/interactive/intents/{combine_id}/approve", json={}).json()
    assert first["ok"] is False and first["reason"] == "confirm_required"
    resumed = client.post(
        f"/api/interactive/intents/{combine_id}/approve", json={"confirmDependency": True}
    ).json()
    assert resumed["ok"] is True
    assert _intents(client)[COMBINE]["status"] == "running"
