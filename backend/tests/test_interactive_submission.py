"""互动模式：保存 / 可见范围 / 有效改动 / 提交 的测试（子智能体 B）。

覆盖契约 docs/interactive-mode-contract.md §1.4 / §1.5 的硬边界：

- 未勾选注释与其链接绝不进入 before / after；组名只在有可见成员时出现；
  链接两端都可见才出现；材料默认在范围内；
- 有效改动由前后两份状态求差得出（不是操作流水）；普通移动只有 layout_only、
  intentBearing=false；取消掉的选择不产生表达；
- 失败不更新基准、保留改动与勾选；empty / duplicate 不更新基准、不调用 QIO；
- 提交成功后勾选自动清空；保存路径绝不调用 QIO。
"""

from __future__ import annotations

import asyncio
import sqlite3

import pytest
from fastapi.testclient import TestClient

from agent.api.server import create_app
from agent.credentials.store import MemoryKeyring
from agent.interactive import board_store, intents, models, submission

BOARD = "board_b_test"

SECRET_NOTE = "未勾选注释的内容不许出现"
HIDDEN_NOTE = "明确隐藏的注释也不许出现"


# --- 工具 ---------------------------------------------------------------


def _state(cards=(), groups=(), links=(), selection=()) -> dict:
    return {
        "boardId": BOARD,
        "seq": 0,
        "updatedAt": models.now_iso(),
        "cards": list(cards),
        "groups": list(groups),
        "links": list(links),
        "selection": list(selection),
    }


def _note(content: str, *, checked: bool = False, **fields) -> dict:
    return models.new_card("text", content, checked=checked, **fields)


def _material(kind: str = "file", content: str = "", **fields) -> dict:
    return models.new_card(kind, content, **fields)


def _save(conn: sqlite3.Connection, state: dict, reason: str = "op") -> dict:
    return board_store.save_board(conn, BOARD, state, reason=reason)


def _submit(conn: sqlite3.Connection, **kwargs) -> dict:
    return asyncio.run(submission.submit_board(conn, BOARD, **kwargs))


def _dump(payload) -> str:
    return models.dumps(payload)


def _kind(expressions: list[dict]) -> list[str]:
    return [item["kind"] for item in expressions]


@pytest.fixture()
def marking(monkeypatch):
    """替换 intents.on_new_submission，记录提交成功后的标记调用。"""
    calls: list[dict] = []

    def fake(conn, *, board_id, submission_id, expressions):  # noqa: ANN001
        calls.append(
            {
                "boardId": board_id,
                "submissionId": submission_id,
                "expressions": expressions,
            }
        )
        return []

    monkeypatch.setattr(intents, "on_new_submission", fake)
    return calls


@pytest.fixture()
def client(db_conn: sqlite3.Connection, settings):
    app = create_app(settings, db_conn)
    app.state.ctx.credentials._kr = MemoryKeyring()
    with TestClient(app) as test_client:
        yield test_client


# --- 可见范围（权限边界） ------------------------------------------------


def test_material_visible_by_default_and_annotation_needs_check(db_conn):
    """材料默认在范围内；文字注释要勾选；隐藏与 reply 一律不在范围。"""
    note_checked = _note("勾选了的说明", checked=True)
    note_unchecked = _note(SECRET_NOTE)
    note_hidden = _note(HIDDEN_NOTE, checked=True, hidden=True)
    material = _material("file", "会议纪要.pdf", meta={"name": "会议纪要.pdf"})
    reply = models.new_card(models.REPLY_KIND, "QIO 的结果")
    state = _state([note_checked, note_unchecked, note_hidden, material, reply])
    visible = submission.visible_range(state)
    ids = [card["id"] for card in visible["cards"]]
    assert note_checked["id"] in ids
    assert material["id"] in ids, "材料默认就在范围内，不需要勾选"
    assert note_unchecked["id"] not in ids
    assert note_hidden["id"] not in ids
    assert reply["id"] not in ids
    assert visible["notVisibleCount"] == 3
    assert SECRET_NOTE not in _dump(visible)
    assert HIDDEN_NOTE not in _dump(visible)


def test_unchecked_note_and_its_links_stay_out_of_before_and_after(db_conn, marking):
    """未勾选注释的文字与链接不得进入提交的前后状态。"""
    note_a = _note("允许查看的说明", checked=True)
    note_b = _note(SECRET_NOTE)
    note_c = _note("另一条未勾选说明")
    # 一条正常的链接（两端可见）与两条会被泄漏的链接
    link_ok = models.new_link(note_a["id"], note_a["id"], meaning="自关联")
    link_leak_1 = models.new_link(note_a["id"], note_b["id"], meaning="链接到未勾选注释")
    link_leak_2 = models.new_link(note_b["id"], note_c["id"], meaning="两端都未勾选")
    _save(
        db_conn,
        _state([note_a, note_b, note_c], links=[link_ok, link_leak_1, link_leak_2]),
    )

    result = _submit(db_conn, requested_visible=[note_b["id"]])
    assert result["status"] == "succeeded"
    assert marking and marking[0]["expressions"], "成功提交后才标记受影响意图"
    for payload in (result["before"], result["after"], result["visibleRange"]):
        text = _dump(payload)
        assert SECRET_NOTE not in text
        assert "另一条未勾选说明" not in text
        assert note_b["id"] not in text
        assert note_c["id"] not in text
        assert link_leak_1["id"] not in text
        assert link_leak_2["id"] not in text
    after_ids = [card["id"] for card in result["after"]["cards"]]
    assert after_ids == [note_a["id"]]
    assert "已忽略" in result["delivery"]["detail"], "客户端塞进来的未勾选卡片必须被忽略并说明"


def test_group_name_only_when_it_has_visible_member(db_conn):
    """组至少有一名可见成员才出现，且只列出可见成员；没有可见成员的组整体不出现。"""
    note_a = _note("可见成员", checked=True)
    note_b = _note(SECRET_NOTE)
    note_c = _note("另一个未勾选成员")
    group_ok = models.new_group("组 1", members=[note_a["id"], note_b["id"]])
    group_hidden = models.new_group("组 2", members=[note_b["id"], note_c["id"]])
    visible = submission.visible_range(
        _state([note_a, note_b, note_c], groups=[group_ok, group_hidden])
    )
    group_ids = [group["id"] for group in visible["groups"]]
    assert group_ids == [group_ok["id"]]
    assert visible["groups"][0]["members"] == [note_a["id"]]
    assert visible["groups"][0]["name"] == "组 1"
    text = _dump(visible)
    assert group_hidden["id"] not in text
    assert "组 2" not in text, "一名可见成员都没有的组不能靠组名间接暴露被隐藏注释"


def test_link_requires_both_ends_visible(db_conn):
    """链接必须两端都可见才进入提交载荷。"""
    note_a = _note("可见的一端", checked=True)
    note_b = _note(SECRET_NOTE)
    link = models.new_link(note_a["id"], note_b["id"], meaning="相关")
    state = _state([note_a, note_b], links=[link])
    assert submission.visible_range(state)["links"] == []

    note_b["checked"] = True
    visible = submission.visible_range(_state([note_a, note_b], links=[link]))
    assert [item["id"] for item in visible["links"]] == [link["id"]]


def test_selection_only_keeps_visible_cards(db_conn):
    note_a = _note("可见", checked=True)
    note_b = _note(SECRET_NOTE)
    visible = submission.visible_range(_state([note_a, note_b], selection=[note_a["id"], note_b["id"]]))
    assert visible["selection"] == [note_a["id"]]


# --- 保存 / 快照 / 草稿 --------------------------------------------------


def test_save_appends_snapshot_and_bumps_seq(db_conn):
    note = _note("一次操作", checked=True)
    first = _save(db_conn, _state([note]), reason="add-text")
    second = _save(db_conn, _state([note, _material("file", "a.pdf")]), reason="add-file")
    assert second["seq"] == first["seq"] + 1
    snapshots = board_store.list_board_states(db_conn, BOARD, limit=10)
    assert [item["seq"] for item in snapshots] == [second["seq"], first["seq"]]
    assert snapshots[0]["reason"] == "add-file"
    assert len(snapshots[0]["state"]["cards"]) == 2
    loaded = board_store.load_board(db_conn, BOARD)
    assert loaded["seq"] == second["seq"]
    assert loaded["state"]["cards"][0]["id"] == note["id"]


def test_save_path_never_calls_qio(db_conn, monkeypatch, marking):
    """保存只落库：既不投递 QIO（on_new_submission 不被调用），也不走 submit_board。"""

    def explode(*args, **kwargs):  # noqa: ANN001
        raise AssertionError("保存路径不允许调用 submit_board")

    monkeypatch.setattr(submission, "submit_board", explode)
    note = _note("保存不调用 QIO", checked=True)
    saved = _save(db_conn, _state([note]))
    assert saved["seq"] == 1
    assert marking == [], "保存过程中不允许标记意图（那是提交之后的事）"
    assert board_store.load_board(db_conn, BOARD)["state"]["cards"][0]["content"] == "保存不调用 QIO"


def test_draft_is_not_submission_content(db_conn, marking):
    board_store.save_draft(db_conn, BOARD, {"card-editor:c1": "写了一半的问题"})
    draft = board_store.get_draft(db_conn, BOARD)
    assert draft["drafts"] == {"card-editor:c1": "写了一半的问题"}
    assert draft["updatedAt"]
    assert marking == []
    # 草稿不进板面状态，也不进可见范围
    loaded = board_store.load_board(db_conn, BOARD)
    assert loaded["state"]["cards"] == []
    assert "写了一半的问题" not in _dump(submission.visible_range(loaded["state"]))


def test_draft_validation(db_conn):
    with pytest.raises(ValueError):
        board_store.save_draft(db_conn, BOARD, ["not-a-dict"])
    draft = board_store.save_draft(db_conn, BOARD, {"k": None})
    assert draft["drafts"] == {"k": ""}


# --- 有效改动（求差） ----------------------------------------------------


def test_diff_is_net_effect_not_operation_log(db_conn, marking):
    """提交前撤销掉的中间操作不形成表达。"""
    note = _note("先加后撤回的说明", checked=True)
    material = _material("file", "留下来的材料.pdf")
    _save(db_conn, _state([note, material]))
    assert _submit(db_conn)["status"] == "succeeded"
    baseline = submission.last_success_baseline(db_conn, BOARD)
    assert baseline is not None

    # 中间操作：加了又删（净效果为零）+ 一次普通移动
    after_delete = [_note("临时草稿"), material, note]
    _save(db_conn, _state(after_delete), reason="temp-add")
    moved = [dict(note), dict(material)]
    moved[0]["x"] = 120.0
    _save(db_conn, _state(moved), reason="cleanup")

    pending = board_store.load_pending(db_conn, BOARD)
    assert _kind(pending["expressions"]) == ["layout_only"], "只有位置变化时只留一条 layout_only"
    assert pending["expressions"][0]["intentBearing"] is False

    result = _submit(db_conn)
    assert result["status"] == "empty"
    assert result["baseline"]["updated"] is False
    assert submission.last_success_baseline(db_conn, BOARD)["seq"] == baseline["seq"]
    assert len(marking) == 1, "empty 不重复调用（只有前面那次成功提交标记过）"
    assert "没有可提交内容" in (result["submission"]["error"] or "")


def test_material_added_is_recorded_but_not_intent_bearing(db_conn, marking):
    _save(db_conn, _state([_material("file", "方案.pdf")]))
    result = _submit(db_conn)
    assert result["status"] == "succeeded"
    kinds = _kind(result["expressions"])
    assert kinds == ["material_added"]
    assert result["expressions"][0]["intentBearing"] is False
    assert result["after"]["cards"], "材料默认在范围内，要能进入提交载荷"
    assert marking and marking[0]["expressions"], "材料变化仍要记录并标记受影响意图（不假装是工作请求）"


def test_note_deleted_reports_retraction(db_conn, marking):
    note = _note("已经提交过的说明", checked=True)
    _save(db_conn, _state([note]))
    assert _submit(db_conn)["status"] == "succeeded"

    deleted = dict(note)
    deleted["deleted"] = True
    _save(db_conn, _state([deleted]), reason="delete")
    result = _submit(db_conn)
    assert result["status"] == "succeeded"
    assert _kind(result["expressions"]) == ["note_deleted"]
    assert result["expressions"][0]["intentBearing"] is False
    assert "已经提交过的说明" in result["expressions"][0]["summary"]
    assert result["after"]["cards"] == []


def test_selection_is_focus_range_and_cancel_produces_nothing(db_conn):
    note_a = _note("A", checked=True)
    note_b = _note("B", checked=True)
    selected = _state([note_a, note_b], selection=[note_a["id"], note_b["id"]])
    expressions = submission.diff_states(
        submission.project_snapshot(_state([note_a, note_b])),
        submission.project_snapshot(selected),
    )
    assert _kind(expressions) == ["focus_selection"]
    assert expressions[0]["intentBearing"] is True

    cancelled = submission.diff_states(
        submission.project_snapshot(selected),
        submission.project_snapshot(_state([note_a, note_b])),
    )
    assert expressions and "focus_selection" not in _kind(cancelled)


def test_plain_move_never_produces_intent_bearing_expression(db_conn, marking):
    note = _note("位置变化不代表想法", checked=True)
    _save(db_conn, _state([note]))
    assert _submit(db_conn)["status"] == "succeeded"

    moved = dict(note)
    moved["x"] = 400.0
    moved["y"] = 260.0
    moved["w"] = 300.0
    _save(db_conn, _state([moved]), reason="move")

    result = _submit(db_conn)
    assert result["status"] == "empty"
    assert _kind(result["expressions"]) == ["layout_only"]
    assert result["expressions"][0]["intentBearing"] is False
    assert len(marking) == 1
    assert result["baseline"]["updated"] is False


# --- 提交：基准 / empty / duplicate / 失败 -------------------------------


def test_first_submission_marker_and_baseline(db_conn, marking):
    note = _note("第一条要提交的说明", checked=True)
    _save(db_conn, _state([note]))
    result = _submit(db_conn)
    assert result["status"] == "succeeded"
    assert result["before"]["firstSubmission"] is True
    assert result["before"]["cards"] == []
    assert result["baseline"]["firstSubmission"] is True
    assert result["baseline"]["updated"] is True
    assert result["baseline"]["previousSeq"] is None
    assert result["delivery"]["delivered"] is False
    assert "没有接入" in result["delivery"]["reason"]
    assert submission.last_success_baseline(db_conn, BOARD)["seq"] == result["submission"]["seq"]


def test_checked_cleared_after_success_and_kept_after_failure(db_conn, monkeypatch):
    note = _note("提交后取消勾选", checked=True)
    _save(db_conn, _state([note]))
    result = _submit(db_conn)
    assert result["checkedCleared"] == [note["id"]]
    stored = board_store.load_board(db_conn, BOARD)["state"]
    assert stored["cards"][0]["checked"] is False, "提交成功后自动取消勾选"
    assert stored["cards"][0]["deleted"] is False, "取消勾选不是删除或撤回"

    # 再勾选后让提交失败：勾选必须保留，基准不变
    baseline_seq = submission.last_success_baseline(db_conn, BOARD)["seq"]
    rechecked = dict(stored["cards"][0])
    rechecked["checked"] = True
    rechecked["content"] = "改过的文字"
    _save(db_conn, _state([rechecked]), reason="edit")

    def boom(before, after):  # noqa: ANN001
        raise RuntimeError("模拟求差失败")

    monkeypatch.setattr(submission, "diff_states", boom)
    failed = _submit(db_conn)
    assert failed["status"] == "failed"
    assert failed["delivery"]["delivered"] is False
    assert "未更新" in failed["delivery"]["reason"]
    assert failed["baseline"]["updated"] is False
    assert failed["checkedCleared"] == []
    assert submission.last_success_baseline(db_conn, BOARD)["seq"] == baseline_seq
    kept = board_store.load_board(db_conn, BOARD)["state"]
    assert kept["cards"][0]["checked"] is True, "失败要保留改动与本次注释选择"
    assert kept["cards"][0]["content"] == "改过的文字"
    assert failed["submission"]["error"]
    assert failed["delivery"]["marking"]["updated"] == []


def test_empty_submission_does_not_update_baseline_or_call_qio(db_conn, marking):
    _save(db_conn, _state([_note(SECRET_NOTE)]))
    result = _submit(db_conn)
    assert result["status"] == "empty"
    assert result["after"]["empty"] is True
    assert result["before"]["firstSubmission"] is True
    assert result["baseline"]["updated"] is False
    assert submission.last_success_baseline(db_conn, BOARD) is None
    assert marking == [], "empty 不调用 QIO（也不标记意图）"
    assert result["delivery"]["delivered"] is False
    assert "empty" in result["delivery"]["reason"]


def test_duplicate_submission_is_idempotent(db_conn, marking):
    note = _note("会被撤回又加回来的说明", checked=True)
    _save(db_conn, _state([note]))
    first = _submit(db_conn)
    assert first["status"] == "succeeded"
    baseline_seq = submission.last_success_baseline(db_conn, BOARD)["seq"]

    # 撤回后又加回同样的内容（新 id，但 QIO 看到的内容一致）
    deleted = dict(note)
    deleted["deleted"] = True
    _save(db_conn, _state([deleted]), reason="delete")
    re_added = _note("会被撤回又加回来的说明", checked=True)
    _save(db_conn, _state([re_added]), reason="re-add")

    result = _submit(db_conn)
    assert result["status"] == "duplicate"
    assert result["baseline"]["updated"] is False
    assert submission.last_success_baseline(db_conn, BOARD)["seq"] == baseline_seq
    assert len(marking) == 1, "duplicate 不重复调用、不重复标记"
    assert "一致" in (result["submission"]["error"] or "")
    stored = board_store.load_board(db_conn, BOARD)["state"]
    assert stored["cards"][0]["checked"] is True, "duplicate 不更新基准，也不清勾选"


def test_before_is_projected_to_current_visible_range(db_conn, marking):
    """两份状态都限于**本次**可见范围：这次没勾选的注释，连 before 里也不出现。"""
    note_a = _note("一直允许查看", checked=True)
    note_b = _note(SECRET_NOTE, checked=True)
    _save(db_conn, _state([note_a, note_b]))
    assert _submit(db_conn)["status"] == "succeeded"

    unchecked = dict(note_b)
    unchecked["checked"] = False
    _save(db_conn, _state([note_a, unchecked]), reason="uncheck")
    result = _submit(db_conn)
    assert result["status"] == "empty"
    assert SECRET_NOTE not in _dump(result["before"])
    assert [card["id"] for card in result["before"]["cards"]] == [note_a["id"]]

    # 再新增一条允许查看的注释：before 仍然只有 A（B 这次没勾选，不许出现在 before 里）
    note_c = _note("新加进来允许查看的说明", checked=True)
    _save(db_conn, _state([note_a, unchecked, note_c]), reason="add-note")
    result = _submit(db_conn)
    assert result["status"] == "succeeded"
    assert [card["id"] for card in result["before"]["cards"]] == [note_a["id"]]
    assert [card["id"] for card in result["after"]["cards"]] == [note_a["id"], note_c["id"]]
    assert {item["kind"] for item in result["expressions"]} == {"note_added"}
    assert SECRET_NOTE not in _dump(result["before"])
    assert SECRET_NOTE not in _dump(result["after"])


def test_failed_submission_records_row_without_baseline(db_conn, monkeypatch):
    note = _note("失败也要留痕", checked=True)
    _save(db_conn, _state([note]))

    def boom(before, after):  # noqa: ANN001
        raise RuntimeError("boom")

    monkeypatch.setattr(submission, "diff_states", boom)
    result = _submit(db_conn)
    assert result["status"] == "failed"
    row = db_conn.execute(
        "SELECT status, error FROM board_submissions WHERE board_id = ?", (BOARD,)
    ).fetchone()
    assert row["status"] == "failed"
    assert "boom" in row["error"]
    assert submission.last_success_baseline(db_conn, BOARD) is None


def test_group_and_link_expressions_are_derived_from_diff(db_conn):
    note_a = _note("A", checked=True)
    note_b = _note("B", checked=True)
    link = models.new_link(note_a["id"], note_b["id"], meaning="相关")
    before = submission.project_snapshot(_state([note_a, note_b]))
    group = models.new_group("组 1", members=[note_a["id"], note_b["id"]])
    after = submission.project_snapshot(_state([note_a, note_b], groups=[group], links=[link]))
    expressions = submission.diff_states(before, after)
    assert set(_kind(expressions)) == {"group_formed", "link_added"}
    assert all(item["intentBearing"] for item in expressions)


# --- HTTP 路由 ----------------------------------------------------------


def _client_state(cards) -> dict:
    return {
        "boardId": BOARD,
        "seq": 0,
        "updatedAt": models.now_iso(),
        "cards": cards,
        "groups": [],
        "links": [],
        "selection": [],
    }


def test_api_save_state_visible_range_and_drafts(client):
    note = _note("走接口的说明", checked=True)
    secret = _note(SECRET_NOTE)
    put = client.put(
        f"/api/interactive/boards/{BOARD}/state",
        json={"state": _client_state([note, secret]), "reason": "add-text"},
    )
    assert put.status_code == 200, put.text
    body = put.json()
    assert body["ok"] is True and body["seq"] == 1
    assert [item["kind"] for item in body["pending"]["expressions"]] == ["note_added"]

    got = client.get(f"/api/interactive/boards/{BOARD}/state").json()
    assert got["seq"] == 1
    assert [card["id"] for card in got["state"]["cards"]] == [note["id"], secret["id"]]
    assert SECRET_NOTE not in models.dumps(got["visibleRange"])
    assert [card["id"] for card in got["visibleRange"]["cards"]] == [note["id"]]
    assert got["baseline"] is None

    visible = client.get(f"/api/interactive/boards/{BOARD}/visible-range").json()
    assert [card["id"] for card in visible["visibleRange"]["cards"]] == [note["id"]]

    drafts = client.put(
        f"/api/interactive/drafts/{BOARD}", json={"drafts": {"card-editor": "草稿文字"}}
    )
    assert drafts.status_code == 200
    assert client.get(f"/api/interactive/drafts/{BOARD}").json()["drafts"] == {
        "card-editor": "草稿文字"
    }


def test_api_submit_history_and_submissions(client):
    note = _note("提交进来的说明", checked=True)
    client.put(f"/api/interactive/boards/{BOARD}/state", json={"state": _client_state([note])})
    submitted = client.post(
        f"/api/interactive/boards/{BOARD}/submissions",
        json={"requestedVisible": [note["id"]], "note": "第一次提交"},
    )
    assert submitted.status_code == 200, submitted.text
    result = submitted.json()
    assert result["status"] == "succeeded"
    assert result["before"]["firstSubmission"] is True
    assert result["delivery"]["delivered"] is False
    assert "没有接入" in result["delivery"]["reason"]
    assert result["checkedCleared"] == [note["id"]]

    state_after = client.get(f"/api/interactive/boards/{BOARD}/state").json()
    assert state_after["baseline"]["seq"] == result["submission"]["seq"]
    assert state_after["state"]["cards"][0]["checked"] is False

    history = client.get(f"/api/interactive/boards/{BOARD}/history").json()
    assert history["snapshots"], "每次保存都追加快照（撤销 / 重做的依据）"
    assert history["snapshots"][0]["state"]["cards"][0]["checked"] is False

    listing = client.get(f"/api/interactive/boards/{BOARD}/submissions").json()
    assert listing["submissions"][0]["status"] == "succeeded"
    assert listing["submissions"][0]["delivery"]["delivered"] is False
    assert "第一次提交" in listing["submissions"][0]["delivery"]["detail"]


def test_api_validation_errors(client):
    bad_state = client.put(
        f"/api/interactive/boards/{BOARD}/state", json={"state": {"cards": "no"}}
    )
    assert bad_state.status_code == 400
    bad_requested = client.post(
        f"/api/interactive/boards/{BOARD}/submissions", json={"requestedVisible": "card-1"}
    )
    assert bad_requested.status_code == 400
    bad_note = client.post(
        f"/api/interactive/boards/{BOARD}/submissions", json={"note": "x" * 3000}
    )
    assert bad_note.status_code == 400
    bad_drafts = client.put(f"/api/interactive/drafts/{BOARD}", json={"drafts": "no"})
    assert bad_drafts.status_code == 400
    bad_board = client.get("/api/interactive/boards/..%2Fetc/state")
    assert bad_board.status_code in (400, 404)


def test_api_material_visible_without_check(client):
    """验收场景 1：两份材料 + 两条注释、只勾选一条 → 前后状态看得到材料、看不到未勾选注释。"""
    material_a = _material("file", "材料一.pdf", meta={"name": "材料一.pdf"})
    material_b = _material("url", "https://example.com/spec", meta={"href": "https://example.com/spec"})
    note_checked = _note("这条允许查看", checked=True)
    note_unchecked = _note(SECRET_NOTE)
    client.put(
        f"/api/interactive/boards/{BOARD}/state",
        json={"state": _client_state([material_a, material_b, note_checked, note_unchecked])},
    )
    result = client.post(f"/api/interactive/boards/{BOARD}/submissions", json={}).json()
    assert result["status"] == "succeeded"
    ids = [card["id"] for card in result["after"]["cards"]]
    assert material_a["id"] in ids and material_b["id"] in ids
    assert note_checked["id"] in ids
    assert note_unchecked["id"] not in ids
    assert SECRET_NOTE not in models.dumps(result["after"])
    kinds = {item["kind"] for item in result["expressions"]}
    assert kinds == {"note_added", "material_added"}
