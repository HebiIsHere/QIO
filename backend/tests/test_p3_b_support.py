"""P3-B 定向测试共用桩（不是用例文件；test_ 前缀仅为同目录可导入）。"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from typing import Any, Iterable

from agent.interactive import board, board_store, models, runtime, tool_models


def prepare(conn) -> None:
    """应用迁移 50（分支内自测；Lead 整合时登记 schema.py + 补偿 51）。"""
    from agent.storage.migration_p3_b import apply

    apply(conn)


def make_board(conn, board_id: str = "board_p3") -> dict:
    return board_store.ensure_board(conn, board_id=board_id)


def add_submission(conn, board_id: str = "board_p3", submission_id: str = "sub_p3_1") -> str:
    conn.execute(
        "INSERT OR IGNORE INTO board_submissions (id, board_id, seq, status, created_at)"
        " VALUES (?, ?, 1, 'accepted', ?)",
        (submission_id, board_id, models.now_iso()),
    )
    return submission_id


def add_visible_card(conn, board_id: str = "board_p3", content: str = "用户材料", *, checked: bool = True) -> str:
    state = board_store.current_state(conn, board_id)
    state = board.add_card(state, "text", content)
    card_id = str(state["cards"][-1]["id"])
    if checked:
        state = board.set_checked(state, card_id, True)
    board_store.save_board(conn, board_id, state, reason="test")
    return card_id


def current_card(conn, board_id: str, card_id: str) -> dict:
    state = board_store.current_state(conn, board_id)
    for card in state.get("cards") or []:
        if str(card.get("id")) == str(card_id):
            return card
    raise AssertionError(f"card not found: {card_id}")


def intent_scope(
    *,
    read_card_ids: Iterable[str] = (),
    write_card_ids: Iterable[str] = (),
    group_ids: Iterable[str] = (),
    allowed_actions: Iterable[str] = (),
    max_new_cards: int = 0,
    max_new_groups: int = 0,
    max_new_links: int = 0,
) -> dict[str, Any]:
    return {
        "read_card_ids": list(read_card_ids),
        "write_card_ids": list(write_card_ids),
        "group_ids": list(group_ids),
        "allowed_actions": list(allowed_actions),
        "max_new_cards": max_new_cards,
        "max_new_groups": max_new_groups,
        "max_new_links": max_new_links,
    }


def intent_spec(
    client_ref: str = "a",
    *,
    title: str = "整理材料",
    goal: str = "把材料整理成结果",
    expected: str = "得到一张结果卡",
    scope: dict[str, Any] | None = None,
    depends_on: Iterable[tuple[str, str]] = (),
    conflicts_with: Iterable[tuple[str, str]] = (),
) -> dict[str, Any]:
    return {
        "client_ref": client_ref,
        "title": title,
        "goal": goal,
        "expected": expected,
        "basis": [],
        "scope": scope or intent_scope(),
        "depends_on": [{"kind": kind, "value": value} for kind, value in depends_on],
        "conflicts_with": [{"kind": kind, "value": value} for kind, value in conflicts_with],
    }


class FakeApp:
    """工具只依赖 app.conn；完整 AppContext 接线由 Lead 的整合冒烟覆盖。"""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn


def board_tools(app: FakeApp) -> dict[str, Any]:
    from agent.interactive.board_tools import build_board_tools

    return {tool.name: tool for tool in build_board_tools(app)}


@contextmanager
def bound(turn_id: str = "turn_p3", *, board_id: str = "board_p3", submission_id: str = "sub_p3_1"):
    context = runtime.BoardTurnContext(
        turn_id=turn_id,
        mode=runtime.MODE_INTERACTIVE,
        board_id=board_id,
        submission_id=submission_id,
        topic_id="topic_p3",
        attempt=1,
    )
    token = runtime.set_board_turn(context)
    try:
        yield context
    finally:
        runtime.clear_delivered_questions()
        runtime.reset_board_turn(token)
