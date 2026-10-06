"""板面操作语义：卡片、选择、拖动、分组、顺序、关系链接（子智能体 A 负责实现）。

契约：docs/interactive-mode-contract.md §1.2 / §1.3 / §3。

本文件里的函数都是**纯函数**：输入板面状态，返回新的板面状态，不碰数据库、不碰网络。
前端同名函数 `frontend/src/interactive/board.ts` 与这里语义一致。

说明：当前是骨架占位实现，只保证「能 import、能跑通最小路径」。
A 负责替换为完整实现（分组规则 / 有序组合并 / 序号连续 / 拖动预演等）。
"""

from __future__ import annotations

from typing import Any

from agent.interactive import models


def normalize_state(state: dict) -> dict:
    """落实契约 §1.2 的全部不变式，返回**新**状态（不原地改）。

    占位实现只做最简单的三件事：补齐字段、去掉重复成员、隐藏与勾选互斥。
    """
    result: dict[str, Any] = {
        "boardId": state.get("boardId", models.DEFAULT_BOARD_ID),
        "seq": int(state.get("seq", 0) or 0),
        "updatedAt": state.get("updatedAt") or models.now_iso(),
        "cards": [dict(card) for card in state.get("cards", [])],
        "groups": [],
        "links": [],
        "selection": list(state.get("selection", []) or []),
    }
    seen_members: set[str] = set()
    for group in state.get("groups", []):
        members: list[str] = []
        for card_id in group.get("members", []) or []:
            if card_id in seen_members:
                continue
            seen_members.add(card_id)
            members.append(card_id)
        item = dict(group)
        item["members"] = members
        result["groups"].append(item)
    live = models.live_card_ids(result)
    result["links"] = [
        dict(link)
        for link in state.get("links", [])
        if not link.get("deleted") and link.get("src") in live and link.get("dst") in live
    ]
    for card in result["cards"]:
        if card.get("hidden"):
            card["checked"] = False
        if card.get("kind") == models.REPLY_KIND:
            card["checked"] = False
    result["selection"] = [cid for cid in result["selection"] if cid in live]
    return result


def preview_drop(state: dict, card_id: str, x: float, y: float) -> dict:
    """拖动期间显示「将要加入的组 / 插入位置」，放下才真正完成操作。"""
    return {"groupId": None, "index": None, "mergesWith": None}


def drop_card(state: dict, card_id: str, x: float, y: float) -> dict:
    """放下：重叠成组 / 加入已有组 / 合并两组 / 有序组按落点插入。占位实现只移动位置。"""
    cards = [dict(card) for card in state.get("cards", [])]
    for card in cards:
        if card.get("id") == card_id:
            card["x"] = float(x)
            card["y"] = float(y)
            card["updatedAt"] = models.now_iso()
            break
    next_state = dict(state)
    next_state["cards"] = cards
    return {
        "state": normalize_state(next_state),
        "groupId": None,
        "merged": False,
        "index": None,
    }
