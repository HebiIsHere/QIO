"""收尾轮反例测试（C）：18 演示成组结果与批准预览不一致。

正确行为期望：

- 批准的成组 / 成员调整必须通过用户同样可做的操作（移入 / 合并 / 成员调整）实现：
  材料在旧组、批准把它与摘要放进新组 → 落地后新组**包含全部批准成员**（摘要 + 材料），
  材料从旧组移出（成员调整），旧组不再占用它；一张卡仍然只属于一个组。
- normalize_state 不得静默裁掉批准成员后仍报告完成；无法按批准内容落地时，
  生效前明确拒绝（ok=False + 原因），意图保持可处理状态，板面不变。
"""

from __future__ import annotations

import asyncio
import copy
import sqlite3

from agent.interactive import board_store, intents, models

BOARD = "board_final_c_apply"


def _card(cid: str, kind: str, content: str = "", **fields) -> dict:
    card = models.new_card(kind, content, **fields)
    card["id"] = cid
    return card


def _save(conn: sqlite3.Connection, state: dict) -> dict:
    return board_store.save_board(conn, BOARD, state, reason="test")


def _state(conn: sqlite3.Connection) -> dict:
    return board_store.load_board(conn, BOARD)["state"]


def _seed(conn: sqlite3.Connection, *, old_group_members: list[str]) -> dict:
    cards = [
        _card("A", "file", "材料一", meta={"name": "a.pdf"}),
        _card("B", "file", "材料二", meta={"name": "b.pdf"}),
        _card("N", "text", "说明", checked=True),
    ]
    old = models.new_group("旧组", default_name=False, members=list(old_group_members))
    old["id"] = "g_old"
    state = {
        "boardId": BOARD, "seq": 0, "updatedAt": models.now_iso(),
        "cards": cards, "groups": [old], "links": [], "selection": [],
    }
    _save(conn, state)
    return state


def _approve_and_done(conn: sqlite3.Connection, title: str) -> dict:
    listed = {item["title"]: item for item in intents.create_demo_intents(conn, board_id=BOARD)}
    approved = intents.approve_intent(conn, listed[title]["id"])
    assert approved["ok"] is True, approved
    done = intents.advance_intent(conn, listed[title]["id"], outcome="done")
    assert done["ok"] is True, done
    return done


def test_materials_move_from_old_group_into_new_group(db_conn):
    """反例：材料已在旧组；批准把它与摘要放进新组 → 新组不能只剩摘要。"""
    _seed(db_conn, old_group_members=["A"])
    done = _approve_and_done(db_conn, "［演示］把材料归为一组并给出对比摘要")
    assert done["ok"] is True

    state = _state(db_conn)
    applied = done["intent"]["applied"]
    groups = {g["id"]: g for g in state["groups"]}
    new_group = groups[applied["groupIds"][0]]

    # 全部批准成员落地：两张材料 + 摘要，都在新组
    assert sorted(new_group["members"]) == sorted(["A", "B", applied["cardIds"][0]]), (
        f"新组成员必须与批准预览一致，实际：{new_group['members']}"
    )
    # 成员调整：A 从旧组移出（旧组因此消失）
    assert "A" not in (groups.get("g_old", {}).get("members") or [])
    assert "g_old" not in groups, "旧组失去全部成员后自动消失（G3）"
    # 一张卡只属于一个组
    seen: dict[str, int] = {}
    for group in state["groups"]:
        for member in group["members"]:
            seen[member] = seen.get(member, 0) + 1
    assert all(count == 1 for count in seen.values())
    # 默认组名保留
    assert new_group["name"] == "默认组名"


def test_old_group_keeps_its_other_members(db_conn):
    _seed(db_conn, old_group_members=["A", "C_EXTRA"])
    # 让 C_EXTRA 真存在
    state = _state(db_conn)
    state["cards"].append(_card("C_EXTRA", "file", "第三份材料", meta={"name": "c.pdf"}))
    _save(db_conn, state)

    done = _approve_and_done(db_conn, "［演示］把材料归为一组并给出对比摘要")
    state = _state(db_conn)
    groups = {g["id"]: g for g in state["groups"]}
    old_group = groups.get("g_old")
    assert old_group is not None and old_group["members"] == ["C_EXTRA"], (
        "旧组的其他成员必须保留，只有被批准移入新组的成员被移出"
    )


def test_ordered_preview_group_lands_in_approved_order(db_conn):
    """预览组是有序组：落地顺序必须与批准预览一致（成员按预览顺序连续编号）。"""
    _seed(db_conn, old_group_members=[])
    # 直接构造一份"有序组"预览的意图（演示入口之外的可控预览，走同一套落地机制）
    created = intents._insert_intent(
        db_conn,
        board_id=BOARD,
        title="［演示］按顺序整理三份材料",
        summary="把 A、B 与摘要按固定顺序放入有序组。",
        preview={
            "cards": [
                {**_card("p_sum", "reply", "顺序摘要（演示预览）"), "x": 40.0, "y": 60.0},
            ],
            "groups": [
                {**models.new_group("默认组名", ordered=True, default_name=True), "id": "g_pre", "members": ["A", "B", "p_sum"]},
            ],
            "links": [],
            "note": "演示预览：批准后按这个顺序落地。",
        },
    )
    approved = intents.approve_intent(db_conn, created["id"])
    assert approved["ok"] is True
    done = intents.advance_intent(db_conn, created["id"], outcome="done")
    assert done["ok"] is True

    state = _state(db_conn)
    applied = done["intent"]["applied"]
    landed = [g for g in state["groups"] if g["id"] == applied["groupIds"][0]][0]
    assert landed["members"] == ["A", "B", applied["cardIds"][0]], f"顺序必须与预览一致，实际 {landed['members']}"
    assert landed["ordered"] is True


def test_impossible_apply_is_refused_before_any_effect(db_conn):
    """预览引用的成员已不在板面上 → 生效前明确拒绝；意图保持可处理状态，板面不变。"""
    _seed(db_conn, old_group_members=[])
    from agent.interactive import intents

    before_seq = board_store.load_board(db_conn, BOARD)["seq"]
    before_state = copy.deepcopy(_state(db_conn))

    created = intents._insert_intent(
        db_conn,
        board_id=BOARD,
        title="［演示］引用已消失的成员",
        preview={
            "cards": [{"id": "p_new", "kind": "reply", "content": "摘要", "x": 0.0, "y": 0.0}],
            "groups": [
                {"id": "g_pre", "name": "默认组名", "defaultName": True, "ordered": False, "members": ["p_new", "gone_card"]},
            ],
            "links": [],
            "note": "预览引用了一张已经不在板面上的卡片",
        },
    )
    approved = intents.approve_intent(db_conn, created["id"])
    assert approved["ok"] is True

    done = intents.advance_intent(db_conn, created["id"], outcome="done")
    assert done["ok"] is False, "无法按批准内容落地时必须在生效前拒绝"
    assert done["reason"] in ("cannot_apply", "refused")
    assert done["detail"], "拒绝必须给出真实原因"
    assert _state(db_conn) == before_state, "板面不得被部分改动"
    assert board_store.load_board(db_conn, BOARD)["seq"] == before_seq
    assert _get_status(db_conn, created["id"]) == "running", "意图保持可处理状态（没有被悄悄标成 done）"


def _get_status(conn: sqlite3.Connection, intent_id: str) -> str:
    row = conn.execute("SELECT status FROM board_intents WHERE id = ?", (intent_id,)).fetchone()
    return row["status"]