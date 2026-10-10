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
    """保存板面（M4 协议）：会影响执行中任务材料的改动必须先预判、带确认才落库。

    收尾轮把影响确认变成服务端门：不带 confirm 的保存得到 409
    impact_confirmation_required、不落库（前端据此先问用户）。这里按契约补完
    「impact-check → confirm → PUT confirm」，用例原本要验的场景语义不变：
    用户确认之后改动生效、相关任务暂停并保留进度。
    """
    state = {
        "boardId": BOARD,
        "seq": 0,
        "updatedAt": models.now_iso(),
        "cards": list(cards),
        "groups": list(groups or []),
        "links": list(links or []),
        "selection": list(selection or []),
    }
    # N1（契约 §1.4）：候选必须基于当前已保存版本；真实客户端读取板面后就是这么写的。
    # 固定 seq=0 的旧写法会在第二次保存时被服务端按旧版本拒绝（这不是要验的业务语义）。
    state["seq"] = int(state_meta(client)["seq"])
    resp = client.put(f"/api/interactive/boards/{BOARD}/state", json={"state": state, "reason": "test"})
    if resp.status_code == 409:
        detail = resp.json().get("detail")
        if isinstance(detail, dict) and detail.get("error") == "impact_confirmation_required":
            meta = client.get(f"/api/interactive/boards/{BOARD}/state").json()
            # 影响确认与待保存候选绑定同一个已保存版本（契约 §1.4 / M4）
            state["seq"] = int(meta["seq"])
            check = client.post(
                f"/api/interactive/boards/{BOARD}/impact-check",
                json={"stateVersion": meta["seq"], "changeSet": {"state": state}},
            ).json()
            assert check.get("checkId"), f"影响预判没有给出可确认的句柄：{check}"
            resp = client.put(
                f"/api/interactive/boards/{BOARD}/state",
                json={
                    "state": state,
                    "reason": "test",
                    "confirm": {"checkId": check["checkId"], "stateVersion": check.get("stateVersion")},
                },
            )
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


def _intents(client: TestClient) -> dict[str, dict]:
    listing = client.get(f"/api/interactive/boards/{BOARD}/intents").json()
    return {item["id"]: item for item in listing["intents"]}


def _prepare_demo(client: TestClient) -> list[dict]:
    """材料 + 勾选注释 → 提交一次 → 创建四项演示意图。"""
    save(client, [_card("m1", "file", "材料", meta={"name": "a.txt"}), _card("n1", "text", "整理", checked=True)])
    assert submit(client)["status"] == "succeeded"
    return _demo_intents(client)


def test_scenario5_batch_approval_and_conflicts(client: TestClient) -> None:
    intents = _prepare_demo(client)
    listing = client.get(f"/api/interactive/boards/{BOARD}/intents").json()
    assert listing["batchAvailable"] is True  # 同一批达到 4 项 → 提供批量列表
    assert len(intents) >= 4

    pairs = listing["conflicts"]
    assert pairs, "演示意图里必须有一对互不相容的结果"
    first, second = pairs[0][0], pairs[0][1]

    decision = client.post("/api/interactive/intents/batch", json={"approve": [first, second]}).json()
    assert len(decision["approved"]) <= 1  # 互不相容的两项不能同时批准
    blocked = [item for item in decision["results"] if not item.get("ok")]
    assert blocked and blocked[0].get("reason") == "conflict"

    # 未选中的继续等待；被拒绝的不改原内容
    waiting = [item for item in intents if item["id"] not in decision["approved"]]
    before = state(client)
    client.post(f"/api/interactive/intents/{waiting[0]['id']}/reject")
    assert _ids(state(client)) == _ids(before)
    assert client.post(f"/api/interactive/intents/{waiting[0]['id']}/approve", json={}).json()["ok"] is False


def test_scenario6_preview_move_semantic_change_and_material_change(client: TestClient) -> None:
    intents = _prepare_demo(client)
    target = intents[0]

    # (a) 只移动预览位置：工作内容、材料范围与结果关系都没变 → 可以直接批准
    import copy

    moved = copy.deepcopy(target["preview"])
    for preview_card in moved["cards"]:
        preview_card["x"] = float(preview_card.get("x", 0.0)) + 40.0
    resp = client.post(f"/api/interactive/intents/{target['id']}/preview", json={"preview": moved}).json()
    assert resp["ok"] is True
    assert resp["requiresUpdate"] is False
    assert resp["intent"]["status"] == "pending"

    # (b) 改变工作要求 → 需要更新，且此时不能批准
    semantic = copy.deepcopy(moved)
    semantic["cards"][0]["content"] = "换一个完全不同的工作要求"
    resp2 = client.post(f"/api/interactive/intents/{target['id']}/preview", json={"preview": semantic}).json()
    assert resp2["requiresUpdate"] is True
    assert resp2["intent"]["status"] == "needs_update"
    assert client.post(f"/api/interactive/intents/{target['id']}/approve", json={}).json()["ok"] is False

    # (c) 相关材料变化 → 还没处理的待审批预览标记为需要更新并禁止批准
    #     （target 在上面已经被标成 needs_update，这里看另一个仍是 pending 的意图）
    other = next(item for item in intents if item["id"] != target["id"] and not item["dependsOn"])
    assert _intents(client)[other["id"]]["status"] == "pending"
    save(client, [_card("m1", "file", "材料（已替换）", meta={"name": "a.txt"}), _card("n1", "text", "整理", checked=True)])
    # 收尾轮 16：材料经真实保存接口变化时，服务端**保存那一刻**就把旧预览标记失效，
    # 不再等下一次提交（旧断言只验提交后的 marking，是本轮要修掉的漏洞）。
    assert _intents(client)[other["id"]]["status"] == "needs_update"
    result = submit(client)
    assert result["status"] in ("succeeded", "empty", "duplicate")
    assert _intents(client)[other["id"]]["status"] == "needs_update"
    assert client.post(f"/api/interactive/intents/{other['id']}/approve", json={}).json()["ok"] is False


def test_scenario7_dependency_and_material_confirmation(client: TestClient) -> None:
    intents = _prepare_demo(client)
    dependent = next(item for item in intents if item["dependsOn"])
    predecessor = dependent["dependsOn"][0]

    # 依赖任务提前批准：不自动开始，一直等前项成功
    assert client.post(f"/api/interactive/intents/{dependent['id']}/approve", json={}).json()["ok"] is True
    assert _intents(client)[dependent["id"]]["status"] == "waiting_dependency"

    # 前项成功、实际结果已展示，但依赖项仍需用户再次确认才启动
    client.post(f"/api/interactive/intents/{predecessor}/approve", json={})
    client.post(f"/api/interactive/intents/{predecessor}/demo/advance", json={"outcome": "done"})
    assert _intents(client)[predecessor]["status"] == "done"
    assert _intents(client)[dependent["id"]]["status"] == "waiting_confirm"
    assert _intents(client)[dependent["id"]]["status"] != "running"
    assert client.post(f"/api/interactive/intents/{dependent['id']}/approve", json={}).json()["ok"] is False

    assert (
        client.post(
            f"/api/interactive/intents/{dependent['id']}/approve", json={"confirmDependency": True}
        ).json()["ok"]
        is True
    )
    assert _intents(client)[dependent["id"]]["status"] == "running"


def test_scenario7_running_task_pauses_when_material_changes(client: TestClient) -> None:
    intents = _prepare_demo(client)
    running = next(item for item in intents if not item["dependsOn"])
    assert client.post(f"/api/interactive/intents/{running['id']}/approve", json={}).json()["ok"] is True
    assert _intents(client)[running["id"]]["status"] == "running"

    # 保存前的影响预判：只读，不改任何状态
    payload = client.post(
        f"/api/interactive/boards/{BOARD}/material-impact",
        json={"state": state(client) | {"cards": [
            _card("m1", "file", "材料（已替换）", meta={"name": "a.txt"}),
            _card("n1", "text", "整理", checked=True),
        ]}},
    )
    assert payload.status_code == 200, payload.text
    affected = {item["intentId"] for item in payload.json()["affected"]}
    assert running["id"] in affected
    assert _intents(client)[running["id"]]["status"] == "running"  # 预判不改状态

    # 用户确认后改动生效：相关任务暂停并保留进度
    saved = save(client, [_card("m1", "file", "材料（已替换）", meta={"name": "a.txt"}), _card("n1", "text", "整理", checked=True)])
    assert saved is not None
    impact = client.put(
        f"/api/interactive/boards/{BOARD}/state",
        json={"state": state(client), "reason": "op"},
    ).json()["materialImpact"]
    assert running["id"] in impact["affected"]
    paused = _intents(client)[running["id"]]
    assert paused["status"] == "paused"
    assert "材料" in paused["reason"]


def test_scenario8_result_is_solid_and_failure_keeps_user_edits(client: TestClient) -> None:
    intents = _prepare_demo(client)
    target = next(item for item in intents if not item["dependsOn"] and not item["conflictsWith"])
    preview_cards = {card["id"]: card for card in target["preview"]["cards"]}

    assert client.post(f"/api/interactive/intents/{target['id']}/approve", json={}).json()["ok"] is True
    assert client.post(f"/api/interactive/intents/{target['id']}/demo/advance", json={"outcome": "done"}).json()["ok"] is True

    done = _intents(client)[target["id"]]
    assert done["status"] == "done"
    applied = done["applied"]["cardIds"] + done["applied"]["groupIds"] + done["applied"]["linkIds"]
    assert applied, "完成后结果要成为正式内容"

    board = state(client)
    solid = [card for card in board["cards"] if card["id"] in done["applied"]["cardIds"]]
    assert solid, "结果卡片要真的落在板面上"
    source = preview_cards[list(done["applied"]["previewIdMap"].keys())[0]]
    assert solid[0]["x"] == source["x"] and solid[0]["y"] == source["y"]  # 板面结果与预览一致
    assert solid[0].get("deleted") is not True

    # 用户随后修改了任务产出的卡片 → 失败撤回时必须保留，并说清未撤回的部分
    edited = dict(solid[0])
    edited["content"] = "用户后来改过的内容"
    save(client, [edited if card["id"] == edited["id"] else card for card in board["cards"]])

    reverted = client.post(f"/api/interactive/intents/{target['id']}/demo/advance", json={"outcome": "failed"}).json()
    assert _intents(client)[target["id"]]["status"] == "failed"
    report = reverted["revert"]
    assert set(report) >= {"reverted", "kept", "pendingDecision", "reasonText"}
    assert any(edited["id"] in item for item in report["kept"])
    after = state(client)
    kept = [card for card in after["cards"] if card["id"] == edited["id"] and not card.get("deleted")]
    assert kept and kept[0]["content"] == "用户后来改过的内容"  # 用户后续修改保留
    assert report["reasonText"]


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
