"""互动模式的共享数据含义（契约实现，Lead 维护）。

这里只放**跨模块共用的东西**：常量、状态构造函数、可见范围判定。
各模块自己的实现逻辑不放这里（board / board_store / submission / intents 各归其主）。

契约：docs/interactive-mode-contract.md
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any, Iterable

# --- 常量 ---------------------------------------------------------------

DEFAULT_BOARD_ID = "board_default"
DEFAULT_BOARD_TITLE = "互动板面"

CARD_KINDS = ("text", "file", "image", "code", "url", "reply")
#: 材料类卡片：添加材料本身不等于要求总结 / 比较 / 修改 / 执行
MATERIAL_KINDS = ("file", "image", "code", "url")
#: 注释由文字卡片构成；问题、说明、态度、优先级、任务要求统一通过注释表达
ANNOTATION_KINDS = ("text",)
#: QIO 产生的结果卡片（正式内容，不参与注释勾选语义）
REPLY_KIND = "reply"

INTENT_STATUSES = (
    "pending",
    "needs_update",
    "rejected",
    "waiting_dependency",
    "waiting_confirm",
    "running",
    "paused",
    "done",
    "failed",
    "cancelled",
)
#: 仍然「等待审批」的状态：材料变化时要标记 needs_update
OPEN_INTENT_STATUSES = ("pending", "needs_update", "waiting_dependency", "waiting_confirm")

EXPRESSION_KINDS = (
    "note_added",
    "note_edited",
    "note_deleted",
    "material_added",
    "material_removed",
    "link_added",
    "link_removed",
    "link_meaning_changed",
    "group_formed",
    "group_merged",
    "group_renamed",
    "group_membership_changed",
    "order_changed",
    "ordered_changed",
    "focus_selection",
    "layout_only",
)

#: 这些改动只影响显示，不作为意图依据
NON_INTENT_EXPRESSIONS = ("layout_only",)

DEFAULT_GROUP_PREFIX = "组"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def default_group_name(index: int) -> str:
    """默认组名只用于识别，不替用户补充关系含义。"""
    return f"{DEFAULT_GROUP_PREFIX} {index}"


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def loads(text: str | None, fallback: Any) -> Any:
    if not text:
        return fallback
    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        return fallback
    return value


# --- 状态构造 -----------------------------------------------------------


def empty_state(board_id: str) -> dict:
    return {
        "boardId": board_id,
        "seq": 0,
        "updatedAt": now_iso(),
        "cards": [],
        "groups": [],
        "links": [],
        "selection": [],
    }


def new_card(kind: str, content: str = "", **fields: Any) -> dict:
    """构造一张卡片。位置与大小属于板面状态，但不是意图依据。"""
    if kind not in CARD_KINDS:
        raise ValueError(f"unknown card kind: {kind}")
    stamp = now_iso()
    card = {
        "id": new_id("c"),
        "kind": kind,
        "content": content,
        "meta": {},
        "x": 0.0,
        "y": 0.0,
        "w": 240.0,
        "h": 120.0,
        "checked": False,
        "hidden": False,
        "folded": False,
        "bookmarked": False,
        "deleted": False,
        "createdAt": stamp,
        "updatedAt": stamp,
    }
    card.update(fields)
    if card["kind"] == REPLY_KIND:
        # QIO 结果卡片不参与注释勾选语义
        card["checked"] = False
    return card


def new_group(name: str, *, ordered: bool = False, default_name: bool = True, **fields: Any) -> dict:
    stamp = now_iso()
    group = {
        "id": new_id("g"),
        "name": name,
        "defaultName": default_name,
        "ordered": ordered,
        "x": 0.0,
        "y": 0.0,
        "w": 320.0,
        "h": 240.0,
        "members": [],
        "deleted": False,
        "createdAt": stamp,
        "updatedAt": stamp,
    }
    group.update(fields)
    return group


def new_link(src: str, dst: str, *, direction: bool = False, meaning: str = "") -> dict:
    stamp = now_iso()
    return {
        "id": new_id("l"),
        "src": src,
        "dst": dst,
        "direction": bool(direction),
        "meaning": meaning,
        "deleted": False,
        "createdAt": stamp,
        "updatedAt": stamp,
    }


# --- 可见范围判定（权限边界，唯一实现） -----------------------------------


def is_live(card: dict) -> bool:
    return not card.get("deleted", False)


def selectable_cards(state: dict) -> list[dict]:
    """本次允许 QIO 查看的注释。

    未勾选 = QIO 完全看不到其文字及注释链接；明确隐藏 = 退出讨论范围。
    QIO 自己的结果卡片不参与这个语义（它本来就不是用户的注释）。
    """
    result: list[dict] = []
    for card in state.get("cards", []):
        if not is_live(card):
            continue
        if card.get("kind") == REPLY_KIND:
            continue
        if not card.get("checked", False):
            continue
        if card.get("hidden", False):
            continue
        result.append(card)
    return result


def selectable_ids(state: dict) -> set[str]:
    return {card["id"] for card in selectable_cards(state)}


def card_by_id(state: dict, card_id: str) -> dict | None:
    for card in state.get("cards", []):
        if card.get("id") == card_id:
            return card
    return None


def live_card_ids(state: dict) -> set[str]:
    return {card["id"] for card in state.get("cards", []) if is_live(card)}


def group_of(state: dict, card_id: str) -> dict | None:
    for group in state.get("groups", []):
        if group.get("deleted"):
            continue
        if card_id in group.get("members", []):
            return group
    return None


def summarize_ids(ids: Iterable[str], *, limit: int = 6) -> str:
    """把一组 id 变成可读的短标签，用于影响说明。"""
    items = list(ids)
    if not items:
        return "（无）"
    head = "、".join(items[:limit])
    return head if len(items) <= limit else f"{head} 等 {len(items)} 项"
