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
import json
import sqlite3
import sys
import time
import types

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


_LAST_ACCEPTOR: list = []


class RecordingAcceptor:
    """主循环受理器替身（阶段三）：记录每次受理参数，不调任何真实模型。"""

    def __init__(self, *, ok: bool = True, reason: str | None = None, raise_exc=None) -> None:
        self.calls: list[dict] = []
        self.ok = ok
        self.reason = reason
        self.raise_exc = raise_exc

    def __call__(self, **kwargs) -> dict:
        self.calls.append(dict(kwargs))
        if self.raise_exc is not None:
            raise self.raise_exc
        if not self.ok:
            return {"ok": False, "reason": self.reason or "fake 受理失败"}
        return {
            "ok": True,
            "turnId": f"turn_submit_{len(self.calls)}",
            "topicId": "topic-submit",
        }


def _acceptor() -> RecordingAcceptor:
    assert _LAST_ACCEPTOR, "还没有发生提交受理"
    return _LAST_ACCEPTOR[0]


def _submit(conn: sqlite3.Connection, acceptor=None, **kwargs) -> dict:
    chosen = acceptor if acceptor is not None else RecordingAcceptor()
    _LAST_ACCEPTOR[:] = [chosen]
    return asyncio.run(
        submission.submit_board(conn, BOARD, turn_acceptor=chosen, **kwargs)
    )


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


@pytest.fixture(autouse=True)
def understanding(monkeypatch):
    """旧独立理解链已退役（阶段三 §13.3）：本文件内任何调用都直接失败。

    新流程把提交作为主循环 turn 受理（turn_acceptor），不再创建 board_understanding
    记录；这里保留同名 fixture 作为**守卫**：一旦有代码偷偷调用旧链，用例立刻失败。
    """
    import agent.interactive as interactive_pkg

    calls: dict = {"requests": [], "runs": [], "adapters": [], "module": None}
    module = types.ModuleType("agent.interactive.understanding")

    def request_understanding(*args, **kwargs):  # noqa: ANN002, ANN003
        calls["requests"].append({"args": args, "kwargs": kwargs})
        raise AssertionError("新提交流程不得调用 request_understanding")

    async def run_understanding(*args, **kwargs):  # noqa: ANN002, ANN003
        calls["runs"].append({"args": args, "kwargs": kwargs})
        raise AssertionError("新提交流程不得调用 run_understanding")

    module.request_understanding = request_understanding
    module.run_understanding = run_understanding
    monkeypatch.setitem(sys.modules, "agent.interactive.understanding", module)
    monkeypatch.setattr(interactive_pkg, "understanding", module, raising=False)
    calls["module"] = module
    return calls


@pytest.fixture()
def client(db_conn: sqlite3.Connection, settings):
    from agent.storage import migration_p3_a

    migration_p3_a.apply(db_conn)  # 阶段三迁移 49（Lead 整合前在本分支手工应用）
    app = create_app(settings, db_conn)
    app.state.ctx.credentials._kr = MemoryKeyring()

    async def _fake_runner(turn_ctx) -> None:  # noqa: ANN001 - 模型替身
        turn_ctx.result = {"ok": True}
        turn_ctx.final_content = "fake answer"

    app.state.ctx.turns.set_runner(_fake_runner)
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(autouse=True)
def _p3a_migration(db_conn):
    """阶段三迁移 49：这些用例直接调 service（不走 Lead 整合后的 schema 登记）。"""
    from agent.storage import migration_p3_a

    migration_p3_a.apply(db_conn)
    yield


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


# --- 删除 × 未勾选：复核缺陷的边界（不得借「撤回」泄露） -------------------


def _stored_cards(conn: sqlite3.Connection) -> dict[str, dict]:
    state = board_store.load_board(conn, BOARD)["state"]
    return {card["id"]: card for card in state["cards"]}


def test_unchecked_then_deleted_note_never_leaks_back(db_conn, marking):
    """勾选并提交过 → 取消勾选 → 删除：文字 / id / 链接（含含义）都不许借「撤回」回到载荷。

    复核缺陷：project_baseline 的删除分支没有先确认「当前是否仍允许查看」，
    于是删除优先于未勾选，把未勾选注释的原文与链接又带进了 before 与 pending 预览。
    """
    other = _note("一直可见的另一条注释", checked=True)
    secret = _note(SECRET_NOTE, checked=True)
    link = models.new_link(secret["id"], other["id"], meaning="秘密关系含义")
    _save(db_conn, _state([other, secret], links=[link]))
    assert _submit(db_conn)["status"] == "succeeded"

    stored = _stored_cards(db_conn)
    gone = dict(stored[secret["id"]])
    # Phase 2：参与状态是唯一事实源（checked 只是历史兼容字段）。
    gone["participation"] = "excluded"
    gone["checked"] = False
    gone["deleted"] = True
    # other 保持允许查看（提交成功会清空勾选，这里重新勾上）
    kept_other = dict(stored[other["id"]])
    kept_other["checked"] = True
    _save(db_conn, _state([kept_other, gone]), reason="uncheck-delete")

    pending = _dump(board_store.load_pending(db_conn, BOARD)["expressions"])
    assert SECRET_NOTE not in pending, "pending 预览不得出现未勾选注释的文字"
    assert secret["id"] not in pending
    assert link["id"] not in pending
    assert "秘密关系含义" not in pending

    result = _submit(db_conn)
    for payload in (
        result["before"],
        result["after"],
        result["expressions"],
        result["visibleRange"],
        result["submission"],
    ):
        text = _dump(payload)
        assert SECRET_NOTE not in text
        assert secret["id"] not in text
        assert link["id"] not in text
        assert "秘密关系含义" not in text
    assert [card["id"] for card in result["before"]["cards"]] == [other["id"]]
    assert [card["id"] for card in result["after"]["cards"]] == [other["id"]]
    assert result["expressions"] == []
    assert result["status"] == "empty"


def test_hidden_then_deleted_note_never_leaks_back(db_conn, marking):
    """明确隐藏 + 删除：同样不得回到 before / 表达式 / pending。"""
    note = _note(HIDDEN_NOTE, checked=True)
    _save(db_conn, _state([note]))
    assert _submit(db_conn)["status"] == "succeeded"

    hidden = dict(_stored_cards(db_conn)[note["id"]])
    hidden["participation"] = "excluded"
    hidden["checked"] = False
    hidden["hidden"] = True
    hidden["deleted"] = True
    _save(db_conn, _state([hidden]), reason="hide-delete")

    pending = _dump(board_store.load_pending(db_conn, BOARD)["expressions"])
    assert HIDDEN_NOTE not in pending
    assert note["id"] not in pending

    result = _submit(db_conn)
    for payload in (result["before"], result["after"], result["expressions"], result["submission"]):
        assert HIDDEN_NOTE not in _dump(payload)
        assert note["id"] not in _dump(payload)


def test_checked_but_deleted_note_still_reports_retraction(db_conn, marking):
    """对照：仍然勾选（允许查看）时删除 → before 保留它并生成「撤回」表达。"""
    note = _note("允许查看的说明", checked=True)
    other = _note("另一条允许查看的说明", checked=True)
    link = models.new_link(note["id"], other["id"], meaning="相关")
    _save(db_conn, _state([note, other], links=[link]))
    assert _submit(db_conn)["status"] == "succeeded"

    stored = _stored_cards(db_conn)
    rechecked = dict(stored[note["id"]])
    rechecked["checked"] = True
    rechecked["deleted"] = True
    rechecked_other = dict(stored[other["id"]])
    rechecked_other["checked"] = True
    _save(db_conn, _state([rechecked, rechecked_other]), reason="recheck-delete")

    result = _submit(db_conn)
    assert result["status"] == "succeeded"
    kinds = _kind(result["expressions"])
    assert "note_deleted" in kinds, "仍允许查看的注释被删除，删除这件事本身可以被表达"
    assert "link_removed" in kinds, "两端都仍允许查看时，链接移除也可以被表达"
    assert [card["id"] for card in result["before"]["cards"]] == [note["id"], other["id"]]
    assert "允许查看的说明" in _dump(result["before"])
    assert [card["id"] for card in result["after"]["cards"]] == [other["id"]]
    assert note["id"] not in _dump(result["after"])


def test_deleted_material_still_reports_retraction(db_conn, marking):
    """材料默认可见：删除材料仍然表达「撤回」（不受未勾选边界影响）。"""
    material = _material("file", "方案.pdf", meta={"name": "方案.pdf"})
    _save(db_conn, _state([material]))
    assert _submit(db_conn)["status"] == "succeeded"

    gone = dict(_stored_cards(db_conn)[material["id"]])
    gone["deleted"] = True
    _save(db_conn, _state([gone]), reason="delete-material")

    result = _submit(db_conn)
    assert result["status"] == "succeeded"
    kinds = _kind(result["expressions"])
    assert "material_removed" in kinds
    assert "方案.pdf" in result["expressions"][0]["summary"]
    assert [card["id"] for card in result["before"]["cards"]] == [material["id"]]
    assert result["after"]["cards"] == []
    assert "方案.pdf" not in _dump(result["after"])


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
    # 契约 §1.5：净效果为零且内容与上次成功提交一致 → duplicate（不是 empty）
    assert result["status"] == "duplicate"
    assert result["baseline"]["updated"] is False
    assert submission.last_success_baseline(db_conn, BOARD)["seq"] == baseline["seq"]
    assert len(marking) == 1, "duplicate 不重复调用（只有前面那次成功提交标记过）"
    assert "一致" in (result["submission"]["error"] or "")


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
    assert result["status"] == "duplicate", "纯位置变化后内容与上次成功提交一致（§1.5）"
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
    assert result["delivery"]["delivered"] is True
    assert result["delivery"]["status"] == "accepted"
    # 阶段三：受理 = 主循环 turn；不再有独立理解请求 id
    assert result["delivery"]["turnId"] == result["turnId"]
    assert result["delivery"]["understandingId"] is None
    assert submission.last_success_baseline(db_conn, BOARD)["seq"] == result["submission"]["seq"]


def test_checked_kept_after_success_and_failure_checked_cleared_is_empty(db_conn, monkeypatch):
    """契约 §2.2 新规则：整体许可**持续有效** —— 提交成功 / 失败都不再取消勾选，
    checkedCleared 恒为空数组（旧规则「提交后自动取消勾选」已删除）。"""
    note = _note("提交后许可仍然有效", checked=True)
    _save(db_conn, _state([note]))
    result = _submit(db_conn)
    assert result["checkedCleared"] == [], "checkedCleared 恒为空数组"
    stored = board_store.load_board(db_conn, BOARD)["state"]
    assert stored["cards"][0]["checked"] is True, "提交成功后整体许可持续有效"
    assert stored["cards"][0]["deleted"] is False, "提交不改卡片本身"

    # 让提交失败：勾选同样必须保留，基准不变
    baseline_seq = submission.last_success_baseline(db_conn, BOARD)["seq"]
    edited = dict(stored["cards"][0])
    edited["checked"] = True
    edited["content"] = "改过的文字"
    _save(db_conn, _state([edited]), reason="edit")

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
    assert kept["cards"][0]["checked"] is True, "失败要保留改动与整体许可"
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


# --- 受理与理解分开（§14.1 / §14.4） --------------------------------------


def test_success_creates_main_loop_turn(db_conn, understanding):
    """阶段三：成功提交 = 受理一个主循环 turn；不再创建独立理解请求。"""
    note = _note("受理要建立主循环轮次", checked=True)
    _save(db_conn, _state([note]))
    result = _submit(db_conn)

    assert result["status"] == "succeeded"
    assert result["delivery"]["delivered"] is True
    assert result["delivery"]["status"] == "accepted"
    assert result["delivery"]["understandingId"] is None
    turn_id = result["delivery"]["turnId"]
    assert turn_id, "受理成功必须给出主循环 turnId"
    assert result["turnId"] == turn_id

    assert len(_acceptor().calls) == 1
    call = _acceptor().calls[0]
    assert call["board_id"] == BOARD
    assert call["submission_id"] == result["submission"]["id"]
    assert call["resume"] is False
    assert "【互动板面提交】" in call["message"]
    assert result["submission"]["id"] in call["message"]
    assert "cards" not in call["message"], "程序消息不得把整份 JSON 快照塞给模型"

    # 旧理解链没有被调用、也没有新记录
    assert understanding["requests"] == []
    assert understanding["runs"] == []
    count = db_conn.execute("SELECT COUNT(*) AS n FROM board_understanding").fetchone()["n"]
    assert count == 0, "新流程不再产生 board_understanding 记录"


def test_layout_only_after_success_is_duplicate_not_empty(db_conn, understanding):
    """契约 §1.5：纯位置变化后内容与上次成功提交一致 → duplicate（不是 empty）。"""
    material = _material("file", "", meta={"file": {"name": "材料.txt"}})
    acceptor = RecordingAcceptor()
    _save(db_conn, _state([material]))
    first = _submit(db_conn, acceptor=acceptor)
    assert first["status"] == "succeeded"
    baseline_seq = submission.last_success_baseline(db_conn, BOARD)["seq"]
    assert len(acceptor.calls) == 1

    moved = dict(material)
    moved["x"] = 120.0
    moved["y"] = 40.0
    _save(db_conn, _state([moved]), reason="move")

    result = _submit(db_conn, acceptor=acceptor)
    assert result["status"] == "duplicate", "纯位置变化后内容与上次成功提交一致"
    assert _kind(result["expressions"]) == ["layout_only"]
    assert result["baseline"]["updated"] is False
    assert submission.last_success_baseline(db_conn, BOARD)["seq"] == baseline_seq
    assert len(acceptor.calls) == 1, "duplicate 不受理第二个主循环 turn"
    assert result["delivery"]["delivered"] is False


def test_empty_board_first_submission_is_empty(db_conn, understanding):
    _save(db_conn, _state([]))
    result = _submit(db_conn)
    assert result["status"] == "empty"
    assert result["baseline"]["updated"] is False
    assert submission.last_success_baseline(db_conn, BOARD) is None
    assert _acceptor().calls == [], "empty 不创建主循环 turn"


def test_checked_is_kept_so_second_submit_is_duplicate_not_empty(db_conn, understanding):
    """契约 §2.2 新规则：提交不再清空勾选 —— 同样内容再提交是 duplicate（不是 empty），
    许可仍然有效（旧规则下第二次会因为勾选被自动清掉而变成 empty）。"""
    note = _note("提交后许可仍然有效", checked=True)
    acceptor = RecordingAcceptor()
    _save(db_conn, _state([note]))
    assert _submit(db_conn, acceptor=acceptor)["status"] == "succeeded"
    assert len(acceptor.calls) == 1

    result = _submit(db_conn, acceptor=acceptor)
    assert result["status"] == "duplicate"
    assert result["checkedCleared"] == []
    assert result["baseline"]["updated"] is False
    assert len(acceptor.calls) == 1, "duplicate 不创建第二个主循环 turn"
    assert board_store.load_board(db_conn, BOARD)["state"]["cards"][0]["checked"] is True


def test_empty_and_duplicate_never_create_main_loop_turn(db_conn, understanding):
    acceptor = RecordingAcceptor()
    # 未勾选注释 → empty
    _save(db_conn, _state([_note(SECRET_NOTE)]))
    assert _submit(db_conn, acceptor=acceptor)["status"] == "empty"
    assert acceptor.calls == [], "empty 不创建主循环 turn"

    note = _note("会被判重复的说明", checked=True)
    _save(db_conn, _state([note]), reason="add")
    assert _submit(db_conn, acceptor=acceptor)["status"] == "succeeded"
    assert len(acceptor.calls) == 1

    deleted = dict(note)
    deleted["deleted"] = True
    _save(db_conn, _state([deleted]), reason="delete")
    re_added = _note("会被判重复的说明", checked=True)
    _save(db_conn, _state([re_added]), reason="re-add")

    again = _submit(db_conn, acceptor=acceptor)
    assert again["status"] == "duplicate"
    assert len(acceptor.calls) == 1, "duplicate 不创建第二个主循环 turn"
    assert understanding["requests"] == []


def test_turn_acceptance_failure_keeps_changes_checks_and_baseline(db_conn, understanding):
    """阶段三：主循环受理失败 → 按 failed 处理（不回退旧理解、不更新基准）。"""
    note = _note("受理失败要保留勾选", checked=True)
    _save(db_conn, _state([note]))

    class BoomAcceptor:
        def __call__(self, **kwargs):  # noqa: ANN003
            raise RuntimeError("模拟受理失败")

    result = _submit(db_conn, acceptor=BoomAcceptor())

    assert result["status"] == "failed", "受理没成立就不是可靠受理，不能算 succeeded"
    assert result["delivery"]["delivered"] is False
    assert result["delivery"]["status"] == "failed"
    assert "受理失败" in result["delivery"]["reason"]
    assert "模拟受理失败" in (result["submission"]["error"] or "")
    assert result["baseline"]["updated"] is False
    assert result["checkedCleared"] == []
    assert submission.last_success_baseline(db_conn, BOARD) is None
    stored = board_store.load_board(db_conn, BOARD)["state"]
    assert stored["cards"][0]["checked"] is True, "受理失败要保留改动与本次注释选择"
    assert (
        db_conn.execute("SELECT COUNT(*) AS n FROM board_understanding").fetchone()["n"] == 0
    ), "受理失败不得留下理解请求（也不得回退旧理解链）"
    assert understanding["requests"] == []


def test_missing_turn_acceptor_fails_acceptance_without_crashing(db_conn):
    """没有主循环受理器 → 受理不可用：按 failed 处理（不抛异常、保留勾选、不更新基准、
    也绝不回退旧理解链）。"""
    note = _note("没有主循环受理器", checked=True)
    _save(db_conn, _state([note]))

    # 直接调 service、不注入 turn_acceptor（模拟没有主循环可用的调用点）
    result = asyncio.run(submission.submit_board(db_conn, BOARD))
    assert result["status"] == "failed"
    assert "主循环受理器不可用" in result["delivery"]["reason"]
    assert result["checkedCleared"] == []
    assert submission.last_success_baseline(db_conn, BOARD) is None
    assert db_conn.execute("SELECT COUNT(*) AS n FROM board_understanding").fetchone()["n"] == 0


async def test_adapter_provider_is_not_used_for_legacy_understanding(db_conn, understanding):
    """阶段三：提交不再启动独立理解模型；adapter_provider 仅兼容保留（不产生调用）。"""
    note = _note("旧 provider 不再使用", checked=True)
    _save(db_conn, _state([note]))
    sentinel = object()
    acceptor = RecordingAcceptor()

    async def provider():
        return sentinel

    result = await submission.submit_board(
        db_conn, BOARD, adapter_provider=provider, turn_acceptor=acceptor
    )
    assert result["status"] == "succeeded"
    assert result["delivery"]["turnId"], "受理成功给的是主循环 turnId"
    assert "background" not in result["delivery"], "不再有后台理解调度事实"
    for _ in range(5):
        await asyncio.sleep(0)
    assert understanding["runs"] == []
    assert understanding["adapters"] == []
    assert len(acceptor.calls) == 1
    assert result["delivery"]["understandingId"] is None


async def test_turn_acceptor_failure_flips_submission_to_failed(db_conn, understanding):
    """受理失败（主循环不可用）→ 提交 failed、不更新基准、保留勾选；不产生理解记录。"""
    note = _note("主循环受理失败", checked=True)
    _save(db_conn, _state([note]))

    class BoomAcceptor:
        def __call__(self, **kwargs):  # noqa: ANN003
            raise RuntimeError("模拟主循环不可用")

    result = await submission.submit_board(db_conn, BOARD, turn_acceptor=BoomAcceptor())
    assert result["status"] == "failed"
    assert "模拟主循环不可用" in (result["submission"]["error"] or "")
    assert result["delivery"]["delivered"] is False
    assert result["delivery"]["understandingId"] is None
    assert db_conn.execute("SELECT COUNT(*) AS n FROM board_understanding").fetchone()["n"] == 0
    assert understanding["requests"] == []


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
    assert result["delivery"]["delivered"] is True
    assert result["delivery"]["status"] == "accepted"
    # 阶段三：受理 = 主循环 turn；understandingId 明确为 None
    assert result["delivery"]["turnId"] == result["turnId"]
    assert result["delivery"]["understandingId"] is None
    assert result["checkedCleared"] == [], "契约 §2.2：checkedCleared 恒为空数组"

    state_after = client.get(f"/api/interactive/boards/{BOARD}/state").json()
    assert state_after["baseline"]["seq"] == result["submission"]["seq"]
    assert state_after["state"]["cards"][0]["checked"] is True, "提交不清许可，勾选保持"

    history = client.get(f"/api/interactive/boards/{BOARD}/history").json()
    assert history["snapshots"], "每次保存都追加快照（撤销 / 重做的依据）"
    assert history["snapshots"][0]["state"]["cards"][0]["checked"] is True

    listing = client.get(f"/api/interactive/boards/{BOARD}/submissions").json()
    assert listing["submissions"][0]["status"] == "succeeded"
    row_delivery = listing["submissions"][0]["delivery"]
    assert row_delivery["delivered"] is True
    assert row_delivery["status"] == "accepted"
    assert row_delivery["turnId"] == result["delivery"]["turnId"]
    assert row_delivery["understandingId"] is None
    assert "第一次提交" in row_delivery["detail"]


def test_api_submit_accepts_main_loop_turn_without_legacy_model(client, understanding):
    """阶段三：API 提交同样走主循环受理；旧理解模型注入点不再被使用。"""
    sentinel = object()

    async def override():
        return sentinel

    client.app.state.interactive_adapter_provider = override
    note = _note("走接口的受理", checked=True)
    client.put(f"/api/interactive/boards/{BOARD}/state", json={"state": _client_state([note])})
    submitted = client.post(f"/api/interactive/boards/{BOARD}/submissions", json={})
    assert submitted.status_code == 200, submitted.text
    result = submitted.json()
    assert result["status"] == "succeeded"
    assert result["delivery"]["delivered"] is True
    assert result["delivery"]["turnId"] == result["turnId"]
    assert result["turnId"], "响应必须带主循环 turnId"
    assert result["delivery"]["understandingId"] is None
    assert understanding["requests"] == []
    assert understanding["runs"] == []


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
    # Phase 2：旧 url 卡不再作为默认可见的材料通道（旧 code/url -> excluded）；
    # 这份测试关心的是「材料不需要勾选即可见」，因此第二份材料也用文件卡。
    material_b = _material("file", "规格.pdf", meta={"name": "规格.pdf"})
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


# --- F4 结果卡续用 / F6 冻结视图（本轮新增） --------------------------------


def _reply(content: str, *, checked: bool, **fields) -> dict:
    return models.new_card("reply", content, checked=checked, **fields)


def _result_exprs(result: dict, kind: str) -> list[dict]:
    return [item for item in result["expressions"] if item["kind"] == kind]


def test_checked_result_card_enters_scope_with_body_and_source(db_conn, marking):
    """F4：只有用户明确许可过的结果卡才进入范围，并带**正文与来源**。"""
    note = _note("接着上次结论继续", checked=True)
    reply = _reply(
        "上次的结论正文",
        checked=True,
        meta={
            "reply": {
                "intentId": "i1",
                "understandingId": "u1",
                "executionId": "x1",
                "stepIndex": 0,
            }
        },
    )
    _save(db_conn, _state([note, reply]))
    result = _submit(db_conn)
    assert result["status"] == "succeeded"
    assert reply["id"] in [card["id"] for card in result["after"]["cards"]]
    added = _result_exprs(result, "result_added")
    assert len(added) == 1, result["expressions"]
    assert added[0]["content"] == "上次的结论正文"
    assert added[0]["source"]["executionId"] == "x1"
    assert added[0]["intentBearing"] is False, "QIO 自己的结果不是用户提出的新工作"
    assert added[0]["summary"].startswith("新增可见结果卡")


def test_unchecked_or_hidden_result_card_never_enters_scope(db_conn, marking):
    """F4：未许可 / 明确隐藏的结果卡整条不可见 —— 正文一个字都不进载荷。"""
    note = _note("问题", checked=True)
    unlicensed = _reply("未许可的结果正文", checked=False)
    hidden = _reply("被隐藏的结果正文", checked=True, hidden=True)
    _save(db_conn, _state([note, unlicensed, hidden]))
    result = _submit(db_conn)
    ids = [card["id"] for card in result["after"]["cards"]]
    assert unlicensed["id"] not in ids and hidden["id"] not in ids
    assert "未许可的结果正文" not in _dump(result["after"])
    assert "被隐藏的结果正文" not in _dump(result["after"])
    assert not _result_exprs(result, "result_added")


def test_unlicensing_a_result_card_immediately_leaves_scope(db_conn, marking):
    """F4：取消许可立即退出范围，且不产生新的结果表达。"""
    note = _note("问题", checked=True)
    reply = _reply("结论 A", checked=True)
    _save(db_conn, _state([note, reply]))
    first = _submit(db_conn)
    assert _result_exprs(first, "result_added")
    revoked = dict(reply)
    revoked["checked"] = False
    _save(db_conn, _state([note, revoked]))
    second = _submit(db_conn)
    assert reply["id"] not in [card["id"] for card in second["after"]["cards"]]
    assert not _result_exprs(second, "result_added")


def test_result_body_change_is_result_updated(db_conn, marking):
    """F4：已可见结果卡的正文变化 → result_updated（同样带正文与来源）。"""
    note = _note("问题", checked=True)
    reply = _reply("结论 v1", checked=True, meta={"reply": {"intentId": "i1"}})
    _save(db_conn, _state([note, reply]))
    assert _submit(db_conn)["status"] == "succeeded"
    changed = dict(reply)
    changed["content"] = "结论 v2"
    _save(db_conn, _state([note, changed]))
    second = _submit(db_conn)
    assert second["status"] == "succeeded"
    updated = _result_exprs(second, "result_updated")
    assert len(updated) == 1, second["expressions"]
    assert updated[0]["content"] == "结论 v2"
    assert updated[0]["source"]["intentId"] == "i1"


def test_frozen_view_is_passed_and_invalid_view_is_dropped(db_conn):
    """F6：合法 view 原样交给受理器并落 delivery；非法 view 整块丢弃记为 null。"""
    note = _note("问题", checked=True)
    _save(db_conn, _state([note]))
    view = {"x": 10.0, "y": 20.0, "w": 800.0, "h": 600.0, "scale": 1.5}
    acceptor = RecordingAcceptor()
    first = _submit(db_conn, acceptor=acceptor, view=view)
    assert first["status"] == "succeeded"
    assert first["delivery"]["view"] == view
    assert acceptor.calls[-1]["view"] == view, "冻结视图随受理交给主循环"

    second = _submit(db_conn, acceptor=acceptor, view={"x": "bad"})
    # 相同内容 + 非法 view：duplicate（不重复受理），非法 view 整块丢弃记为 null
    assert second["status"] == "duplicate"
    assert second["delivery"]["view"] is None
    assert len(acceptor.calls) == 1


def test_view_alone_does_not_change_content_or_fingerprint(db_conn):
    """F6：纯 view 变化不构成新意图，也不让内容失效（内容指纹与求差都不含 view）。"""
    note = _note("问题", checked=True)
    _save(db_conn, _state([note]))
    first = _submit(db_conn, view={"x": 0.0, "y": 0.0, "w": 400.0, "h": 300.0, "scale": 1.0})
    second = _submit(db_conn, view={"x": 100.0, "y": 50.0, "w": 400.0, "h": 300.0, "scale": 2.0})
    assert first["status"] == "succeeded"
    assert second["status"] == "duplicate", second
    assert second["expressions"] == [], "纯 view 变化不产生新的表达"
