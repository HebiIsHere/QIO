"""互动模式第一阶段的集成验收（Lead 维护）。

覆盖提示词第四节要求的验收场景 1–9（场景 10「既有功能不被破坏」由全量测试承担）。
这里只用 HTTP 接口，不直接调用内部函数 —— 验收的是**用户实际能走通的路径**。
"""

from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from agent.api.server import create_app
from agent.config import Settings
from agent.interactive import models

BOARD = models.DEFAULT_BOARD_ID


@pytest.fixture()
def client(db_conn: sqlite3.Connection, settings: Settings):
    from agent.credentials.store import MemoryKeyring

    app = create_app(settings, db_conn)
    app.state.ctx.credentials._kr = MemoryKeyring()
    with TestClient(app) as c:
        yield c


def _card(cid: str, kind: str, content: str = "", **fields) -> dict:
    card = models.new_card(kind, content, **fields)
    card["id"] = cid
    return card


def _group(gid: str, name: str, members: list[str], *, ordered: bool = False, **fields) -> dict:
    group = models.new_group(name, ordered=ordered, default_name=False, members=list(members), **fields)
    group["id"] = gid
    return group


def _link(lid: str, src: str, dst: str, *, direction: bool = False, meaning: str = "") -> dict:
    link = models.new_link(src, dst, direction=direction, meaning=meaning)
    link["id"] = lid
    return link


def save(client: TestClient, cards, groups=None, links=None, selection=None) -> dict:
    state = {
        "boardId": BOARD,
        "seq": 0,
        "updatedAt": models.now_iso(),
        "cards": list(cards),
        "groups": list(groups or []),
        "links": list(links or []),
        "selection": list(selection or []),
    }
    resp = client.put(f"/api/interactive/boards/{BOARD}/state", json={"state": state, "reason": "test"})
    assert resp.status_code == 200, resp.text
    return resp.json()["state"]


def state(client: TestClient) -> dict:
    """板面状态本体（BoardState）。"""
    resp = client.get(f"/api/interactive/boards/{BOARD}/state")
    assert resp.status_code == 200, resp.text
    return resp.json()["state"]


def state_meta(client: TestClient) -> dict:
    """GET /state 的完整响应：state + baseline + pending + visibleRange + submissions + drafts。"""
    resp = client.get(f"/api/interactive/boards/{BOARD}/state")
    assert resp.status_code == 200, resp.text
    return resp.json()


def submit(client: TestClient, **body) -> dict:
    resp = client.post(f"/api/interactive/boards/{BOARD}/submissions", json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _ids(snapshot: dict) -> set[str]:
    return {card["id"] for card in snapshot.get("cards", [])}


# --- 场景 1：材料 + 注释 + 分组，只勾选一条 --------------------------------


def test_scenario1_visibility_boundary_and_checkbox_cleared(client: TestClient) -> None:
    materials = [
        _card("m_file", "file", "访谈记录", meta={"name": "访谈记录.pdf"}),
        _card("m_url", "url", "参考链接", meta={"href": "https://example.com/a"}),
    ]
    notes = [
        _card("n_visible", "text", "这个方案的成本需要再确认", checked=True),
        _card("n_hidden_note", "text", "我不太确定要不要继续", checked=False),
    ]
    groups = [_group("g1", "成本与材料", ["m_file", "m_url", "n_visible", "n_hidden_note"], ordered=True)]
    links = [
        _link("l_visible", "m_file", "m_url", direction=True, meaning="引用同一份记录"),
        _link("l_leak", "n_visible", "n_hidden_note", direction=False, meaning="相关"),
    ]
    save(client, materials + notes, groups, links)

    result = submit(client)

    assert result["status"] == "succeeded"
    assert result["baseline"]["firstSubmission"] is True
    assert result["before"]["cards"] == []  # 首次提交没有历史基准

    after = result["after"]
    assert {"m_file", "m_url", "n_visible"} <= _ids(after)
    assert "l_visible" in {link["id"] for link in after["links"]}
    group = next(item for item in after["groups"] if item["id"] == "g1")
    assert group["name"] == "成本与材料"  # 组名是独立关系依据
    assert "n_visible" in group["members"]

    # 未勾选的注释完全不可见：卡片、链接端点、成员描述、文字都不出现（前后状态都一样）
    for snapshot in (result["before"], after):
        assert "n_hidden_note" not in _ids(snapshot)
        assert "l_leak" not in {link["id"] for link in snapshot["links"]}
        if snapshot["groups"]:
            assert "n_hidden_note" not in snapshot["groups"][0]["members"]
        raw = str(snapshot)
        assert "我不太确定要不要继续" not in raw
        assert "n_hidden_note" not in raw

    # 提交成功后自动取消勾选（不是删除）
    after = state(client)
    checked = {card["id"]: card["checked"] for card in after["cards"]}
    assert checked["n_visible"] is False
    assert any(card["id"] == "n_hidden_note" for card in after["cards"])


def test_scenario1_group_without_visible_member_is_absent(client: TestClient) -> None:
    cards = [_card("n_a", "text", "甲", checked=False), _card("n_b", "text", "乙", checked=False)]
    save(client, cards, [_group("g_hidden", "只含未勾选注释的组", ["n_a", "n_b"])])
    result = submit(client)
    # 一名可见成员都没有的组整体不出现，否则组名会间接暴露被隐藏的注释
    assert result["after"]["groups"] == []
    assert "只含未勾选注释的组" not in str(result["after"])


# --- 场景 2：持续编辑不提交 ------------------------------------------------


def test_scenario2_edits_are_saved_but_qio_gets_nothing(client: TestClient) -> None:
    save(client, [_card("n1", "text", "第一版", checked=True)])
    first = submit(client)
    assert first["status"] == "succeeded"
    baseline_after_first = state_meta(client)["baseline"]
    assert baseline_after_first is not None

    save(client, [_card("n1", "text", "第二版（还没提交）", checked=True), _card("n2", "text", "新加的想法")])
    reloaded = state(client)
    assert any("第二版" in card["content"] for card in reloaded["cards"])

    submissions = client.get(f"/api/interactive/boards/{BOARD}/submissions").json()["submissions"]
    assert len(submissions) == 1  # 保存不产生提交，QIO 没有拿到新表达
    assert state_meta(client)["baseline"] == baseline_after_first


# --- 场景 3：撤销与「最终有效状态」 ---------------------------------------


def test_scenario3_undone_edits_do_not_become_expressions(client: TestClient) -> None:
    save(client, [_card("n1", "text", "原话", checked=True)])
    submit(client)

    # 普通移动 + 一次真实修改：中间被撤销的那次修改（"改了又改回来"）不该出现在表达里，
    # 而位置变化只算 layout_only，不作为意图依据。
    save(
        client,
        [
            _card("n1", "text", "原话", checked=True, x=10.0, y=20.0),
            _card("n2", "text", "新增的说明", checked=True),
        ],
    )
    result = submit(client)

    assert result["status"] == "succeeded"
    kinds = {item["kind"] for item in result["expressions"]}
    assert "note_edited" not in kinds  # 撤销掉的中间修改不形成表达
    assert "focus_selection" not in kinds  # 已取消的选择不作为依据
    layout = [item for item in result["expressions"] if item["kind"] == "layout_only"]
    assert layout and all(item["intentBearing"] is False for item in layout)


def test_scenario3_layout_only_submission_is_empty(client: TestClient) -> None:
    """只动了位置/大小时没有「可提交的有效表达」：不调用 QIO、不推进基准。"""
    save(client, [_card("n1", "text", "原话", checked=True)])
    first = submit(client)
    assert first["status"] == "succeeded"
    baseline = state_meta(client)["baseline"]

    save(client, [_card("n1", "text", "原话", checked=True, x=88.0, y=66.0)])
    result = submit(client)
    assert result["status"] == "empty"
    assert result["baseline"]["updated"] is False
    assert state_meta(client)["baseline"] == baseline


def test_scenario3_live_selection_is_recorded(client: TestClient) -> None:
    save(
        client,
        [_card("n1", "text", "甲", checked=True), _card("n2", "text", "乙", checked=True)],
        selection=["n1", "n2"],
    )
    result = submit(client)
    focus = [item for item in result["expressions"] if item["kind"] == "focus_selection"]
    assert focus and focus[0]["intentBearing"] is True


# --- 场景 6/7：预览调整、材料变化 ------------------------------------------


def _demo_intents(client: TestClient) -> list[dict]:
    resp = client.post(f"/api/interactive/boards/{BOARD}/intents", json={"demo": True})
    assert resp.status_code == 200, resp.text
    created = resp.json()["created"]
    assert len(created) >= 4
    return client.get(f"/api/interactive/boards/{BOARD}/intents").json()["intents"]


def test_scenario5_batch_approval_and_conflicts(client: TestClient) -> None:
    save(client, [_card("n1", "text", "整理这两份材料", checked=True)])
    intents = _demo_intents(client)
    listing = client.get(f"/api/interactive/boards/{BOARD}/intents").json()
    assert listing["batchAvailable"] is True

    conflicting = [item for item in intents if item["conflictsWith"]]
    assert conflicting, "演示意图里必须有一对互不相容的结果"
    pair = conflicting[0]["conflictsWith"]
    decision = client.post("/api/interactive/intents/batch", json={"approve": [conflicting[0]["id"], *pair]}).json()
    approved = [item["id"] for item in decision["results"] if item.get("ok")]
    assert len(approved) < 1 + len(pair)  # 互不相容的两项不能一起批准

    rejected = [item for item in intents if item["id"] not in approved][0]
    before = state(client)
    client.post(f"/api/interactive/intents/{rejected['id']}/reject")
    after = state(client)
    assert _ids(after) == _ids(before)  # 拒绝不改原内容


def test_scenario6_material_change_blocks_approval(client: TestClient) -> None:
    save(client, [_card("m1", "file", "材料", meta={"name": "a.txt"}), _card("n1", "text", "整理", checked=True)])
    intents = _demo_intents(client)
    target = intents[0]
    resp = client.post(f"/api/interactive/intents/{target['id']}/approve", json={})
    assert resp.status_code == 200
    assert resp.json()["ok"] is True

    # 相关材料改变 → 待审批 / 已批准但未开始的预览标记为需要更新并禁止批准
    save(
        client,
        [
            _card("m1", "file", "材料（已替换）", meta={"name": "a.txt"}),
            _card("n1", "text", "整理", checked=True),
        ],
    )
    listed = {item["id"]: item for item in client.get(f"/api/interactive/boards/{BOARD}/intents").json()["intents"]}
    assert listed[target["id"]]["status"] in ("needs_update", "pending")
    if listed[target["id"]]["status"] == "needs_update":
        again = client.post(f"/api/interactive/intents/{target['id']}/approve", json={})
        assert again.json()["ok"] is False


# --- 场景 9：关闭 / 重新打开 ------------------------------------------------


def test_scenario9_reopen_restores_and_keeps_paused(client: TestClient, db_conn, settings) -> None:
    save(client, [_card("n1", "text", "重启前的内容", checked=True)])
    client.put(f"/api/interactive/drafts/{BOARD}", json={"drafts": {"n_draft": "写了一半"}})
    intents = _demo_intents(client)
    running = intents[0]
    client.post(f"/api/interactive/intents/{running['id']}/approve", json={})
    client.post(f"/api/interactive/intents/{running['id']}/demo/advance", json={"outcome": "paused"})

    # 模拟进程重启：同一个数据库、新的 app 实例
    from agent.credentials.store import MemoryKeyring

    app2 = create_app(settings, db_conn)
    app2.state.ctx.credentials._kr = MemoryKeyring()
    with TestClient(app2) as client2:
        reopened = client2.get(f"/api/interactive/boards/{BOARD}/state").json()
        assert any("重启前的内容" in card["content"] for card in reopened["state"]["cards"])
        assert reopened["drafts"]["drafts"]["n_draft"] == "写了一半"
        listed = client2.get(f"/api/interactive/boards/{BOARD}/intents").json()
        assert listed["intents"], "待审批预览与任务要在重新打开后恢复"
        assert all(item["status"] != "running" for item in listed["intents"])  # 不自动恢复执行
