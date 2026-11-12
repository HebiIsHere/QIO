# -*- coding: utf-8 -*-
"""PM-QIO032 #13：空白卡本身不计为有效提交（后端判定与前端一致）。

BRIEF #13：空白卡可以保留但本身不计为有效提交；独立有效的关系、删除等改动
按实际语义计算；没有有效内容时明确拒绝并给下一步；空提交判定与前端一致。

后端口径（与 i-state 对齐）：
- 判定依据：候选可见范围投影（after）vs 上次成功提交快照（before），布局变化不算内容；
- 空白文字卡（正文 trim 后为空且 meta 无非显示语义）被保留在板面与 after 快照里，
  但**新增本身不产生有效内容**；只加空白卡时 status=empty（不更新基准、不受理 Turn）；
- 与空白卡相关的独立关系 / 分组变化仍按实际语义计入内容；
- 删除一张已提交的卡片仍是真实改动（它改变了 QIO 已看到的内容）。
"""

from __future__ import annotations

import asyncio
import sqlite3

import pytest

from agent.interactive import board_store, models, submission

BOARD = "board_pm032_empty"


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


class RecordingAcceptor:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def __call__(self, **kwargs) -> dict:
        self.calls.append(dict(kwargs))
        return {"ok": True, "turnId": f"turn_empty_{len(self.calls)}", "topicId": "topic"}


def _save(conn: sqlite3.Connection, state: dict) -> dict:
    return board_store.save_board(conn, BOARD, state, reason="test")


def _submit(conn: sqlite3.Connection, acceptor=None) -> tuple[dict, RecordingAcceptor]:
    chosen = acceptor or RecordingAcceptor()
    return (
        asyncio.run(submission.submit_board(conn, BOARD, turn_acceptor=chosen)),
        chosen,
    )


def _content_kinds(expressions: list[dict]) -> list[str]:
    return [
        item["kind"]
        for item in expressions
        if item["kind"] not in submission.DISPLAY_ONLY_EXPRESSIONS
    ]


@pytest.fixture(autouse=True)
def _p3a_migration(db_conn):
    from agent.storage import migration_p3_a

    migration_p3_a.apply(db_conn)
    yield


def test_blank_card_alone_is_empty_not_effective_submission(db_conn):
    """只加一张空白卡：可以保存，但提交判 empty，不更新基准、不受理 Turn。"""
    conn = db_conn
    blank = models.new_card(
        "text", "   ", participation=models.PARTICIPATION_INCLUDED, x=10.0, y=10.0
    )
    _save(conn, _state(cards=[blank]))

    result, acceptor = _submit(conn)

    assert result["status"] == "empty"
    assert result["baseline"]["updated"] is False
    assert result["delivery"]["delivered"] is False
    assert acceptor.calls == [], "空提交不得创建主循环 turn"
    assert submission.last_success_baseline(conn, BOARD) is None
    assert "blank_card_added" in [item["kind"] for item in result["expressions"]]
    assert _content_kinds(result["expressions"]) == []
    # 空白卡本身仍在板面（不删用户数据）。
    assert [card["id"] for card in result["after"]["cards"]] == [blank["id"]]
    # 明确给下一步（不是只说「失败」）。
    reason = result["delivery"].get("reason") or ""
    assert "没有可提交内容" in reason


def test_blank_card_pending_shows_no_content(db_conn):
    """未提交改动（pending）与提交判定一致：只有空白卡时没有有效内容。"""
    conn = db_conn
    material = models.new_card("file", "会议纪要.pdf", meta={"name": "会议纪要.pdf"})
    _save(conn, _state(cards=[material]))
    first, _ = _submit(conn)
    assert first["status"] == "succeeded"

    blank = models.new_card(
        "text", "", participation=models.PARTICIPATION_INCLUDED, x=200.0, y=20.0
    )
    state = board_store.current_state(conn, BOARD)
    state["cards"].append(blank)
    _save(conn, state)

    pending = submission.refresh_pending(conn, BOARD)
    assert _content_kinds(pending["expressions"]) == []
    second, acceptor = _submit(conn)
    assert second["status"] == "empty"
    assert acceptor.calls == []


def test_link_to_blank_card_is_a_real_change(db_conn):
    """空白卡 + 一条独立关系：关系本身是有效改动，提交成功。"""
    conn = db_conn
    material = models.new_card("file", "资料.pdf", meta={"name": "资料.pdf"})
    _save(conn, _state(cards=[material]))
    first, _ = _submit(conn)
    assert first["status"] == "succeeded"

    blank = models.new_card(
        "text", "", participation=models.PARTICIPATION_INCLUDED, x=200.0, y=20.0
    )
    link = models.new_link(material["id"], blank["id"])
    state = board_store.current_state(conn, BOARD)
    state["cards"] = list(state["cards"]) + [blank]
    state["links"] = list(state.get("links") or []) + [link]
    _save(conn, state)

    pending = submission.refresh_pending(conn, BOARD)
    assert "link_added" in _content_kinds(pending["expressions"])
    second, acceptor = _submit(conn)
    assert second["status"] == "succeeded"
    assert len(acceptor.calls) == 1
    assert second["baseline"]["updated"] is True


def test_blank_card_edited_with_text_becomes_effective(db_conn):
    """空白卡后来写了字：内容修改是有效改动。"""
    conn = db_conn
    blank = models.new_card(
        "text", "", participation=models.PARTICIPATION_INCLUDED, x=10.0, y=10.0
    )
    _save(conn, _state(cards=[blank]))
    first, _ = _submit(conn)
    assert first["status"] == "empty"

    state = board_store.current_state(conn, BOARD)
    for card in state["cards"]:
        if card["id"] == blank["id"]:
            card["content"] = "补充说明"
    _save(conn, state)

    second, acceptor = _submit(conn)
    assert second["status"] == "succeeded"
    assert len(acceptor.calls) == 1
    # 第一次是空提交（不更新基准）：之后写字的卡对 QIO 而言仍是新注释。
    assert _content_kinds(second["expressions"]) == ["note_added"]


def test_deleting_a_submitted_card_still_counts_as_change(db_conn):
    """删除已提交的卡片：仍按实际语义计为有效改动。"""
    conn = db_conn
    note = models.new_card("text", "会被撤回的说明", participation=models.PARTICIPATION_INCLUDED)
    _save(conn, _state(cards=[note]))
    first, _ = _submit(conn)
    assert first["status"] == "succeeded"

    deleted = dict(note, deleted=True)
    _save(conn, _state(cards=[deleted]))
    second, acceptor = _submit(conn)
    assert second["status"] == "succeeded"
    assert "note_deleted" in _content_kinds(second["expressions"])
    assert len(acceptor.calls) == 1
