"""互动模式：QIO 回复、虚线预览、审批与撤回（子智能体 C）。

契约：docs/interactive-mode-contract.md §1.6 / §3。

这些用例只依赖冻结签名：board_store / models / intents，不碰网络与模型。
"""

from __future__ import annotations

import copy
import json
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent.api import interactive_intents as api_module
from agent.interactive import board_store, intents, models

BOARD = "board_intents_test"


# --- 测试脚手架 ---------------------------------------------------------


def _seed(conn):
    """两份材料 + 一条注释：演示意图都要有真实材料可引用。"""
    file_a = models.new_card("file", "材料 A", meta={"file": {"name": "a.pdf"}}, x=20.0, y=20.0)
    image_b = models.new_card("image", "材料 B", meta={"image": {"name": "b.png"}}, x=20.0, y=180.0)
    note = models.new_card("text", "只勾选这一条注释", checked=True, x=20.0, y=340.0)
    state = models.empty_state(BOARD)
    state["cards"] = [file_a, image_b, note]
    board_store.save_board(conn, BOARD, state, reason="test-seed")
    return {"a": file_a, "b": image_b, "note": note}


def _state(conn) -> dict:
    return board_store.load_board(conn, BOARD)["state"]


def _cards(conn) -> dict:
    return {card["id"]: card for card in _state(conn)["cards"]}


def _by_title(items: list[dict]) -> dict:
    return {item["title"]: item for item in items}


def _demo(conn) -> dict:
    return _by_title(intents.create_demo_intents(conn, board_id=BOARD))


def _listed(conn) -> dict:
    return {item["id"]: item for item in intents.list_intents(conn, BOARD)["intents"]}


def _t(key: str) -> str:
    return intents.DEMO_TITLES[key]


# --- 演示意图 -----------------------------------------------------------


def test_create_demo_intents_shape(db_conn):
    _seed(db_conn)
    created = intents.create_demo_intents(db_conn, board_id=BOARD)
    assert len(created) == 4
    assert all(item["demo"] is True for item in created)
    assert all(item["title"].startswith("［演示］") for item in created)

    combine, separate, followup, failing = created
    # 一对冲突：conflictsWith 有交集，且共用同一个非空 conflictKey
    assert combine["conflictsWith"] == [separate["id"]]
    assert separate["conflictsWith"] == [combine["id"]]
    assert combine["conflictKey"] and combine["conflictKey"] == separate["conflictKey"]
    # 一项依赖前项
    assert followup["dependsOn"] == [combine["id"]]
    # 一项会失败（写明是演示场景）
    assert "失败" in failing["reason"]
    assert "演示" in failing["progress"]["text"]
    # 预览要有位置、结构（组）与关系（链接），并且明确标注是演示
    preview = combine["preview"]
    assert preview["cards"] and preview["cards"][0]["kind"] == "reply"
    assert preview["cards"][0]["x"] > 0 and preview["cards"][0]["w"] > 0
    assert preview["groups"] and preview["groups"][0]["members"]
    assert preview["links"] and preview["links"][0]["meaning"]
    assert "演示" in preview["note"]
    # 影响说明要列出对象 / 任务 / 后果
    assert combine["impact"]["objects"] and combine["impact"]["tasks"]
    assert combine["impact"]["consequences"]
    # 重复点击演示入口不会重复创建
    again = intents.create_demo_intents(db_conn, board_id=BOARD)
    assert [item["id"] for item in again] == [item["id"] for item in created]


# --- 拒绝 ---------------------------------------------------------------


def test_reject_clears_preview_and_keeps_board_unchanged(db_conn):
    _seed(db_conn)
    target = _demo(db_conn)[_t("combine")]
    before = _state(db_conn)
    result = intents.reject_intent(db_conn, target["id"])
    assert result["ok"] is True
    assert result["intent"]["status"] == "rejected"
    # 预览消失
    assert result["intent"]["preview"]["cards"] == []
    assert result["intent"]["preview"]["groups"] == []
    # 板面原内容保留
    after = _state(db_conn)
    assert after["cards"] == before["cards"]
    assert after["groups"] == before["groups"]
    assert after["links"] == before["links"]


# --- 批准与完成 ---------------------------------------------------------


def test_completion_applies_preview_as_real_board_content(db_conn):
    _seed(db_conn)
    target = _demo(db_conn)[_t("failing")]  # 这一项没有依赖
    approved = intents.approve_intent(db_conn, target["id"])
    assert approved["ok"] is True
    assert approved["intent"]["status"] == "running"

    done = intents.advance_intent(db_conn, target["id"], outcome="done")
    assert done["ok"] is True
    assert done["intent"]["status"] == "done"
    applied = done["intent"]["applied"]
    assert applied["cardIds"]

    created = _cards(db_conn)[applied["cardIds"][0]]
    # 用用户同样具备的板面操作：新增结果卡片（reply），不参与注释勾选
    assert created["kind"] == "reply"
    assert created["checked"] is False
    assert created["deleted"] is False
    assert created["meta"]["intentId"] == target["id"]
    # 演示结果必须写明是演示
    assert done["demo"] is True
    assert "演示" in done["intent"]["reason"]
    assert done["intent"]["progress"]["text"].endswith("（演示）")


# --- 冲突 ---------------------------------------------------------------


def test_conflicting_result_cannot_be_approved_at_the_same_time(db_conn):
    _seed(db_conn)
    demo = _demo(db_conn)
    combine, separate = demo[_t("combine")], demo[_t("separate")]

    first = intents.approve_intent(db_conn, combine["id"])
    assert first["ok"] is True and first["intent"]["status"] == "running"

    second = intents.approve_intent(db_conn, separate["id"])
    assert second["ok"] is False
    assert second["reason"] == "conflict"
    assert combine["id"] in second["conflictsWith"]
    assert _listed(db_conn)[separate["id"]]["status"] == "pending"

    # 冲突的一方被撤回之后，另一项才可以批准
    cancelled = intents.advance_intent(db_conn, combine["id"], outcome="cancelled")
    assert cancelled["ok"] is True
    third = intents.approve_intent(db_conn, separate["id"])
    assert third["ok"] is True and third["intent"]["status"] == "running"


def test_batch_conflicting_pair_neither_is_approved(db_conn):
    _seed(db_conn)
    demo = _demo(db_conn)
    combine, separate = demo[_t("combine")], demo[_t("separate")]
    followup, failing = demo[_t("followup")], demo[_t("failing")]

    listed = intents.list_intents(db_conn, BOARD)
    assert listed["batchAvailable"] is True  # 同一批达到 4 项
    assert any(set(group) == {combine["id"], separate["id"]} for group in listed["conflicts"])

    result = intents.batch_decide(
        db_conn,
        approve=[combine["id"], separate["id"], followup["id"], failing["id"]],
        reject=[],
    )
    reasons = {item["intentId"]: item.get("reason") for item in result["results"]}
    assert reasons[combine["id"]] == "conflict"
    assert reasons[separate["id"]] == "conflict"
    assert result["approved"] == [followup["id"], failing["id"]]

    statuses = {item_id: item["status"] for item_id, item in _listed(db_conn).items()}
    assert statuses[combine["id"]] == "pending"
    assert statuses[separate["id"]] == "pending"
    assert statuses[followup["id"]] == "waiting_dependency"
    assert statuses[failing["id"]] == "running"

    # 选择部分：批准一项、拒绝另一项；未选中的继续等待
    second = intents.batch_decide(db_conn, approve=[combine["id"]], reject=[separate["id"]])
    assert second["approved"] == [combine["id"]]
    assert second["rejected"] == [separate["id"]]
    statuses = {item_id: item["status"] for item_id, item in _listed(db_conn).items()}
    assert statuses[separate["id"]] == "rejected"
    assert statuses[failing["id"]] == "running"


# --- 依赖 ---------------------------------------------------------------


def test_dependency_waits_and_needs_confirmation_again(db_conn):
    _seed(db_conn)
    demo = _demo(db_conn)
    combine, followup = demo[_t("combine")], demo[_t("followup")]

    # 提前批准依赖项：不会自动开始
    first = intents.approve_intent(db_conn, followup["id"])
    assert first["ok"] is True
    assert first["intent"]["status"] == "waiting_dependency"
    assert first["waitingFor"] == [combine["id"]]

    # 前项没有完成：一直等，推进也不会开始
    pushed = intents.advance_intent(db_conn, followup["id"], outcome="done")
    assert pushed["ok"] is False and pushed["reason"] == "dependency_waiting"
    assert _listed(db_conn)[followup["id"]]["status"] == "waiting_dependency"

    # 前项完成 → 等待用户再次确认
    intents.approve_intent(db_conn, combine["id"])
    intents.advance_intent(db_conn, combine["id"], outcome="done")
    after = _listed(db_conn)[followup["id"]]
    assert after["status"] == "waiting_confirm"
    assert "确认" in after["reason"]

    # 没有 confirmDependency 就不会开始
    no_confirm = intents.approve_intent(db_conn, followup["id"])
    assert no_confirm["ok"] is False and no_confirm["reason"] == "confirm_required"
    assert _listed(db_conn)[followup["id"]]["status"] == "waiting_confirm"

    confirmed = intents.approve_intent(db_conn, followup["id"], confirm_dependency=True)
    assert confirmed["ok"] is True
    assert confirmed["intent"]["status"] == "running"


# --- 材料变化 -----------------------------------------------------------


def test_material_change_marks_needs_update_and_blocks_approval(db_conn):
    seed = _seed(db_conn)
    demo = _demo(db_conn)
    combine = demo[_t("combine")]

    marked = intents.on_new_submission(
        db_conn,
        board_id=BOARD,
        submission_id="sub_material_1",
        expressions=[
            {
                "id": "e1",
                "kind": "note_edited",
                "intentBearing": True,
                "cardIds": [seed["a"]["id"]],
                "groupId": None,
                "linkId": None,
            }
        ],
    )
    # 所有以这份材料为依据、且仍在等待审批的意图都被标记
    assert combine["id"] in marked
    marked_state = _listed(db_conn)[combine["id"]]
    assert marked_state["status"] == "needs_update"
    assert "变化" in marked_state["reason"]
    assert marked_state["progress"]["text"]

    blocked = intents.approve_intent(db_conn, combine["id"])
    assert blocked["ok"] is False
    assert blocked["reason"] == "needs_update"
    assert blocked["requiresUpdate"] is True

    # 已经是 needs_update 的不重复标记
    again = intents.on_new_submission(
        db_conn,
        board_id=BOARD,
        submission_id="sub_material_2",
        expressions=[
            {
                "id": "e2",
                "kind": "note_edited",
                "intentBearing": True,
                "cardIds": [seed["a"]["id"]],
                "groupId": None,
                "linkId": None,
            }
        ],
    )
    assert combine["id"] not in again


def test_layout_only_submission_does_not_mark_anything(db_conn):
    _seed(db_conn)
    _demo(db_conn)
    marked = intents.on_new_submission(
        db_conn,
        board_id=BOARD,
        submission_id="sub_layout",
        expressions=[
            {
                "id": "e1",
                "kind": "layout_only",
                "intentBearing": False,
                "cardIds": [],
                "groupId": None,
                "linkId": None,
            }
        ],
    )
    assert marked == []


# --- 预览调整 -----------------------------------------------------------


def test_preview_layout_change_can_still_be_approved(db_conn):
    _seed(db_conn)
    combine = _demo(db_conn)[_t("combine")]

    moved = copy.deepcopy(combine["preview"])
    moved["cards"][0]["x"] = moved["cards"][0]["x"] + 160
    moved["cards"][0]["y"] = moved["cards"][0]["y"] + 40
    result = intents.update_preview(db_conn, combine["id"], moved)
    assert result["ok"] is True
    assert result["requiresUpdate"] is False
    assert result["canApprove"] is True
    assert result["intent"]["status"] == "pending"
    assert "位置" in result["detail"]

    approved = intents.approve_intent(db_conn, combine["id"])
    assert approved["ok"] is True


def test_preview_semantic_change_requires_update(db_conn):
    _seed(db_conn)
    combine = _demo(db_conn)[_t("combine")]

    changed = copy.deepcopy(combine["preview"])
    changed["cards"][0]["content"] = "改成：只输出一份结论，不再比较"
    result = intents.update_preview(db_conn, combine["id"], changed)
    assert result["ok"] is True
    assert result["requiresUpdate"] is True
    assert result["canApprove"] is False
    assert result["intent"]["status"] == "needs_update"
    assert "预览" in result["intent"]["reason"]

    blocked = intents.approve_intent(db_conn, combine["id"])
    assert blocked["ok"] is False and blocked["reason"] == "needs_update"

    # 改关系含义同样算语义变化（链接数量不变、含义改变）
    other = _demo(db_conn)[_t("separate")]
    relinked = copy.deepcopy(other["preview"])
    relinked["cards"][0]["x"] = 999.0
    unrelated = intents.update_preview(db_conn, other["id"], relinked)
    assert unrelated["requiresUpdate"] is False
    relinked["cards"][0]["meta"] = {"note": "改变材料范围"}
    semantic = intents.update_preview(db_conn, other["id"], relinked)
    assert semantic["requiresUpdate"] is True


# --- 失败 / 取消撤回 ------------------------------------------------------


def test_failed_task_reverts_its_own_result(db_conn):
    _seed(db_conn)
    target = _demo(db_conn)[_t("failing")]
    intents.approve_intent(db_conn, target["id"])
    done = intents.advance_intent(db_conn, target["id"], outcome="done")
    card_id = done["intent"]["applied"]["cardIds"][0]
    assert _cards(db_conn)[card_id]["deleted"] is False

    failed = intents.advance_intent(db_conn, target["id"], outcome="failed")
    assert failed["ok"] is True
    assert failed["intent"]["status"] == "failed"
    report = failed["revert"]
    assert any(card_id in item for item in report["reverted"])
    assert report["pendingDecision"] == []
    assert _cards(db_conn)[card_id]["deleted"] is True
    # 失败原因要写清楚，并且不自动重试
    assert "演示" in failed["intent"]["reason"]
    assert "不会自动重试" in failed["intent"]["reason"]


def test_failed_task_keeps_user_edits(db_conn):
    _seed(db_conn)
    target = _demo(db_conn)[_t("failing")]
    intents.approve_intent(db_conn, target["id"])
    done = intents.advance_intent(db_conn, target["id"], outcome="done")
    card_id = done["intent"]["applied"]["cardIds"][0]

    # 用户后来修改了这张结果卡片
    state = _state(db_conn)
    for card in state["cards"]:
        if card["id"] == card_id:
            card["content"] = "用户后来改过的内容"
    board_store.save_board(db_conn, BOARD, state, reason="user-edit")

    failed = intents.advance_intent(db_conn, target["id"], outcome="failed")
    report = failed["revert"]
    assert any(card_id in item for item in report["kept"]), report
    assert any(card_id in item for item in report["kept"] if "保留" in item)
    kept_card = _cards(db_conn)[card_id]
    assert kept_card["deleted"] is False
    assert kept_card["content"] == "用户后来改过的内容"


def test_partial_revert_reverts_safe_parts_and_waits_for_decision(db_conn):
    seed = _seed(db_conn)
    target = _demo(db_conn)[_t("separate")]  # 两项结果、没有依赖
    intents.approve_intent(db_conn, target["id"])
    done = intents.advance_intent(db_conn, target["id"], outcome="done")
    first_id, second_id = done["intent"]["applied"]["cardIds"][:2]

    # 用户后来把第一张结果卡片与自己的注释连了起来：撤回它会动到别的工作
    state = _state(db_conn)
    state["links"].append(
        models.new_link(first_id, seed["note"]["id"], direction=False, meaning="我的依据")
    )
    board_store.save_board(db_conn, BOARD, state, reason="user-link")

    failed = intents.advance_intent(db_conn, target["id"], outcome="failed")
    report = failed["revert"]
    # 先撤回不受影响的部分
    assert any(second_id in item for item in report["reverted"]), report
    assert _cards(db_conn)[second_id]["deleted"] is True
    # 其余部分列出具体影响，等待用户决定
    assert report["pendingDecision"], report
    pending_ids = [item["id"] for item in report["pendingDecision"]]
    assert first_id in pending_ids
    assert all(item["reason"] and item["impact"] for item in report["pendingDecision"])
    assert _cards(db_conn)[first_id]["deleted"] is False
    # 用户自己建立的关系没有被撤回
    user_link = [l for l in _state(db_conn)["links"] if l["meaning"] == "我的依据"]
    assert user_link and user_link[0]["deleted"] is False

    # 用户决定撤回其余部分
    decided = intents.advance_intent(db_conn, target["id"], outcome="revert_rest")
    assert decided["ok"] is True
    assert decided["intent"]["revert"]["pendingDecision"] == []
    assert _cards(db_conn)[first_id]["deleted"] is True


# --- 重启恢复 -----------------------------------------------------------


def test_recover_running_intents_pauses_and_stays_paused(db_conn):
    _seed(db_conn)
    target = _demo(db_conn)[_t("failing")]
    approved = intents.approve_intent(db_conn, target["id"])
    assert approved["intent"]["status"] == "running"

    # 模拟进程重启：running → paused，不自动继续
    recovery = intents.recover_running_intents(db_conn, BOARD)
    assert recovery["paused"] == [target["id"]]
    paused = _listed(db_conn)[target["id"]]
    assert paused["status"] == "paused"
    assert "不会自动继续" in paused["reason"]

    # 再读一次不会重复报告，状态保持暂停
    assert intents.recover_running_intents(db_conn, BOARD)["paused"] == []
    assert _listed(db_conn)[target["id"]]["status"] == "paused"

    # 暂停的任务不会因为「批准」而自动接着跑
    again = intents.approve_intent(db_conn, target["id"])
    assert again["ok"] is False and again["reason"] == "paused"
    assert _listed(db_conn)[target["id"]]["status"] == "paused"


# --- HTTP 路由（冻结路径） ------------------------------------------------


def _client(conn) -> TestClient:
    app = FastAPI()
    app.state.ctx = SimpleNamespace(conn=conn)
    app.include_router(api_module.router)
    return TestClient(app)


def test_intents_routes_follow_frozen_paths(db_conn):
    _seed(db_conn)
    client = _client(db_conn)

    created = client.post(f"/api/interactive/boards/{BOARD}/intents", json={"demo": True})
    assert created.status_code == 200
    payload = created.json()
    assert payload["demo"] is True and len(payload["created"]) == 4
    assert "演示" in payload["notice"]

    listed = client.get(f"/api/interactive/boards/{BOARD}/intents").json()
    assert listed["batchAvailable"] is True
    assert listed["recovery"] == {"paused": []}

    combine = _by_title(listed["intents"])[_t("combine")]
    approved = client.post(
        f"/api/interactive/intents/{combine['id']}/approve", json={"confirmDependency": False}
    )
    assert approved.status_code == 200 and approved.json()["ok"] is True

    done = client.post(
        f"/api/interactive/intents/{combine['id']}/demo/advance", json={"outcome": "done"}
    )
    assert done.status_code == 200 and done.json()["intent"]["status"] == "done"

    moved = copy.deepcopy(combine["preview"])
    moved["cards"][0]["x"] = moved["cards"][0]["x"] + 30
    preview = client.post(
        f"/api/interactive/intents/{combine['id']}/preview", json={"preview": moved}
    )
    assert preview.status_code == 200

    separate = _by_title(listed["intents"])[_t("separate")]
    rejected = client.post(f"/api/interactive/intents/{separate['id']}/reject")
    assert rejected.status_code == 200 and rejected.json()["ok"] is True

    batch = client.post(
        "/api/interactive/intents/batch",
        json={"approve": [], "reject": [separate["id"]]},
    )
    assert batch.status_code == 200
    assert batch.json()["results"][0]["intentId"] == separate["id"]

    # 参数校验
    assert client.post(f"/api/interactive/boards/{BOARD}/intents", json={}).status_code == 400
    assert (
        client.post(
            f"/api/interactive/intents/{combine['id']}/demo/advance", json={"outcome": "乱写"}
        ).status_code
        == 400
    )
    assert (
        client.post("/api/interactive/intents/batch", json={"approve": "x"}).status_code == 400
    )
    unknown = client.post("/api/interactive/intents/i_missing/approve", json={})
    assert unknown.status_code == 200
    assert unknown.json()["ok"] is False and unknown.json()["reason"] == "not_found"


def test_preview_payload_shape_is_json_serialisable(db_conn):
    """响应里的预览 / 影响说明 / 撤回报告都必须是可 JSON 序列化的普通结构。"""
    _seed(db_conn)
    target = _demo(db_conn)[_t("failing")]
    intents.approve_intent(db_conn, target["id"])
    intents.advance_intent(db_conn, target["id"], outcome="done")
    failed = intents.advance_intent(db_conn, target["id"], outcome="failed")
    dumped = json.dumps(failed, ensure_ascii=False)
    assert "演示" in dumped
    assert set(failed["revert"]) == {"reverted", "kept", "pendingDecision", "reasonText"}

# --- 执行中任务的材料保护（保存后判定 + 保存前预判） -----------------------


def test_preview_material_impact_is_read_only(db_conn):
    seed = _seed(db_conn)
    target = _demo(db_conn)[_t("failing")]
    intents.approve_intent(db_conn, target["id"])
    assert _listed(db_conn)[target["id"]]["status"] == "running"

    pending = _state(db_conn)
    for card in pending["cards"]:
        if card["id"] == seed["a"]["id"]:
            card["content"] = "材料 A（用户改过）"

    impact = intents.preview_material_impact(db_conn, board_id=BOARD, state=pending)
    assert len(impact["affected"]) == 1
    entry = impact["affected"][0]
    assert entry["intentId"] == target["id"]
    assert entry["title"] == target["title"]
    assert entry["materials"] and any("材料" in item for item in entry["materials"])
    assert "暂停" in entry["consequence"]

    # 只读：板面没保存，任务仍然是执行中
    assert _listed(db_conn)[target["id"]]["status"] == "running"
    assert _cards(db_conn)[seed["a"]["id"]]["content"] == "材料 A"


def test_on_board_saved_pauses_running_intent_when_material_changes(db_conn):
    seed = _seed(db_conn)
    demo = _demo(db_conn)
    running = demo[_t("failing")]
    waiting = demo[_t("combine")]
    intents.approve_intent(db_conn, running["id"])
    progress_before = _listed(db_conn)[running["id"]]["progress"]

    state = _state(db_conn)
    for card in state["cards"]:
        if card["id"] == seed["a"]["id"]:
            card["content"] = "材料 A（用户改过）"
    board_store.save_board(db_conn, BOARD, state, reason="user-edit-material")

    result = intents.on_board_saved(
        db_conn, board_id=BOARD, state=_state(db_conn), reason="user-edit-material"
    )
    assert result["affected"] == [running["id"]]
    assert [item["intentId"] for item in result["paused"]] == [running["id"]]
    assert "暂停" in result["paused"][0]["reason"]
    assert result["paused"][0]["progress"]["done"] == progress_before["done"]
    assert result["paused"][0]["progress"]["total"] == progress_before["total"]

    paused = _listed(db_conn)[running["id"]]
    assert paused["status"] == "paused"
    assert "不会自动重试" in paused["reason"]
    # 等待审批的意图不受影响
    assert _listed(db_conn)[waiting["id"]]["status"] == "pending"

    # 再保存一次不会重复暂停
    assert intents.on_board_saved(
        db_conn, board_id=BOARD, state=_state(db_conn), reason="op"
    ) == {"paused": [], "affected": []}


def test_on_board_saved_ignores_layout_and_unrelated_cards(db_conn):
    _seed(db_conn)
    target = _demo(db_conn)[_t("failing")]
    intents.approve_intent(db_conn, target["id"])

    state = _state(db_conn)
    for card in state["cards"]:
        card["x"] = card["x"] + 200  # 只移动位置：不是材料变化
    assert intents.on_board_saved(db_conn, board_id=BOARD, state=state) == {
        "paused": [],
        "affected": [],
    }

    state["cards"].append(models.new_card("text", "与任务无关的临时注释"))
    assert intents.on_board_saved(db_conn, board_id=BOARD, state=state)["paused"] == []
    assert _listed(db_conn)[target["id"]]["status"] == "running"

