"""板面操作语义：卡片、选择、拖动、分组、顺序、关系链接（子智能体 A 负责实现）。

契约：docs/interactive-mode-contract.md §1.2 / §1.3 / §3。

本文件里的函数都是**纯函数**：输入板面状态，返回新的板面状态，不碰数据库、不碰网络。
前端同名函数 frontend/src/interactive/board.ts 与这里语义一致（同一批规则、同一批默认值）。

三条容易搞错、这里写死的语义：

1. **位置只影响显示**：普通移动 / 缩放不构成意图依据；组框位置由成员自动推导，用户不直接改。
2. **方向不解释成因果**：链接的 direction 只是用户写明的方向，meaning 是用户写的原话，
   系统不补写、不推断「谁支持谁 / 谁先谁后」。
3. **拖动期间只预演**：preview_drop 不产生任何状态变化，drop_card 才完成操作；
   中断拖动时调用方只要丢掉预演结果即可（状态本来就没动过）。

预演字段含义（签名见契约 §3，字段语义在此固定）：

- groupId：放下后卡片将落入的**已存在**的组；将与未分组卡片成组时组还不存在，给 None。
- index：在 groupId 里的插入位置（0 起）；不落入已有组时给 None。
- mergesWith：将要合并进来的对象 id —— 两个已有组合并时是**被并入**的那一组 id；
  与未分组卡片重叠成组时是那张**卡片** id；否则 None。
"""

from __future__ import annotations

import copy
import re
from typing import Any, Iterable

from agent.interactive import models

#: 组框相对成员的留白（组名与序号占顶部一行）。位置只影响显示。
GROUP_PAD_X = 16.0
GROUP_PAD_TOP = 30.0
GROUP_PAD_BOTTOM = 16.0

DEFAULT_CARD_W = 240.0
DEFAULT_CARD_H = 120.0
#: 新卡片默认错开摆放，避免完全重叠（重叠会触发自动成组）
DEFAULT_CARD_STEP = 28.0
DEFAULT_CARD_ORIGIN = 60.0
#: 复制卡片的偏移
DUPLICATE_OFFSET = 24.0

#: update_card 允许改的字段（id / createdAt 不允许被覆盖）
_CARD_PATCH_KEYS = (
    "kind",
    "content",
    "meta",
    "x",
    "y",
    "w",
    "h",
    "checked",
    "hidden",
    "folded",
    "bookmarked",
    "deleted",
)
_LINK_PATCH_KEYS = ("src", "dst", "direction", "meaning", "deleted")

_DEFAULT_NAME_RE = re.compile(rf"^{re.escape(models.DEFAULT_GROUP_PREFIX)}\s*(\d+)$")


# --- 小工具 ---------------------------------------------------------------


def _now() -> str:
    return models.now_iso()


def _as_float(value: Any, fallback: float) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return float(fallback)
    if result != result or result in (float("inf"), float("-inf")):
        return float(fallback)
    return result


def _as_text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _dedupe(values: Iterable[Any]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        key = str(value)
        if key in seen:
            continue
        seen.add(key)
        result.append(key)
    return result


def _is_default_name(name: str) -> bool:
    return bool(_DEFAULT_NAME_RE.match(name))


def _next_default_name(taken: Iterable[str]) -> str:
    """下一个没人用过的默认组名「组 N」；N 只增不减，避免重名。"""
    highest = 0
    for name in taken:
        match = _DEFAULT_NAME_RE.match(str(name or ""))
        if match:
            highest = max(highest, int(match.group(1)))
    return models.default_group_name(highest + 1)


def _clamp_index(index: Any, size: int) -> int:
    if index is None:
        return size
    try:
        value = int(index)
    except (TypeError, ValueError):
        return size
    return max(0, min(size, value))


def _rect_overlap(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> bool:
    """两个矩形是否有面积重叠（只碰到边不算重叠）。"""
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    return min(ax + aw, bx + bw) - max(ax, bx) > 0 and min(ay + ah, by + bh) - max(ay, by) > 0


def _point_in_rect(x: float, y: float, rect: tuple[float, float, float, float]) -> bool:
    rx, ry, rw, rh = rect
    return rx <= x <= rx + rw and ry <= y <= ry + rh


def card_rect(card: dict) -> tuple[float, float, float, float]:
    return (
        _as_float(card.get("x"), 0.0),
        _as_float(card.get("y"), 0.0),
        max(1.0, _as_float(card.get("w"), DEFAULT_CARD_W)),
        max(1.0, _as_float(card.get("h"), DEFAULT_CARD_H)),
    )


def group_frame(group: dict) -> tuple[float, float, float, float]:
    return (
        _as_float(group.get("x"), 0.0),
        _as_float(group.get("y"), 0.0),
        max(1.0, _as_float(group.get("w"), 1.0)),
        max(1.0, _as_float(group.get("h"), 1.0)),
    )


def group_by_id(state: dict, group_id: str) -> dict | None:
    for group in state.get("groups", []) or []:
        if group.get("id") == group_id and not group.get("deleted"):
            return group
    return None


def group_of_card(state: dict, card_id: str) -> dict | None:
    """卡片所属的组（G1 保证最多一个）。"""
    return models.group_of(state, card_id)


# --- 归一化：契约 §1.2 的 G1..G8 -------------------------------------------


def _normalize_card(raw: dict, stamp: str) -> dict:
    card = dict(raw)
    kind = card.get("kind")
    if kind not in models.CARD_KINDS:
        kind = "text"
    card["kind"] = kind
    card["id"] = str(card.get("id") or models.new_id("c"))
    card["content"] = _as_text(card.get("content"))
    meta = card.get("meta")
    card["meta"] = dict(meta) if isinstance(meta, dict) else {}
    for key, fallback in (("x", 0.0), ("y", 0.0), ("w", DEFAULT_CARD_W), ("h", DEFAULT_CARD_H)):
        card[key] = _as_float(card.get(key), fallback)
    card["w"] = max(1.0, card["w"])
    card["h"] = max(1.0, card["h"])
    for key in ("checked", "hidden", "folded", "bookmarked", "deleted"):
        card[key] = bool(card.get(key, False))
    card["createdAt"] = _as_text(card.get("createdAt")) or stamp
    card["updatedAt"] = _as_text(card.get("updatedAt")) or card["createdAt"]
    # G6 明确隐藏与勾选互斥；G7 reply 卡片不参与注释勾选
    if card["hidden"]:
        card["checked"] = False
    if kind == models.REPLY_KIND:
        card["checked"] = False
    return card


def _reflow_group(group: dict, cards: dict[str, dict]) -> None:
    """组框跟着成员走：位置 / 大小只影响显示，不是意图依据。"""
    members = [cards[cid] for cid in group["members"] if cid in cards]
    if not members:
        return
    left = min(card["x"] for card in members) - GROUP_PAD_X
    top = min(card["y"] for card in members) - GROUP_PAD_TOP
    right = max(card["x"] + card["w"] for card in members) + GROUP_PAD_X
    bottom = max(card["y"] + card["h"] for card in members) + GROUP_PAD_BOTTOM
    group["x"] = left
    group["y"] = top
    group["w"] = max(1.0, right - left)
    group["h"] = max(1.0, bottom - top)


def _normalize_group(
    raw: dict,
    *,
    claimed: set[str],
    cards: dict[str, dict],
    taken_names: set[str],
    stamp: str,
) -> dict | None:
    if raw.get("deleted"):
        return None
    members: list[str] = []
    for card_id in raw.get("members") or []:
        key = str(card_id)
        card = cards.get(key)
        # G1 一张卡只属于一个组；G5 已删除的卡片不进组
        if key in claimed or card is None or card["deleted"]:
            continue
        claimed.add(key)
        members.append(key)
    # G3 成员全被移除或删除的组自动消失（不存在空组）
    if not members:
        return None
    group = dict(raw)
    group["id"] = str(group.get("id") or models.new_id("g"))
    name = _as_text(group.get("name")).strip()
    if not name:
        name = _next_default_name(taken_names)
        group["defaultName"] = True
    group["name"] = name
    if not isinstance(group.get("defaultName"), bool):
        group["defaultName"] = _is_default_name(name)
    group["ordered"] = bool(group.get("ordered", False))
    group["members"] = members
    group["deleted"] = False
    group["createdAt"] = _as_text(group.get("createdAt")) or stamp
    group["updatedAt"] = _as_text(group.get("updatedAt")) or group["createdAt"]
    _reflow_group(group, cards)
    return group


def _normalize_link(raw: dict, live: set[str], stamp: str) -> dict | None:
    if raw.get("deleted"):
        return None
    src = str(raw.get("src") or "")
    dst = str(raw.get("dst") or "")
    # G4 端点必须存在且未删除（G5 已删除卡片不进链接）
    if src not in live or dst not in live:
        return None
    link = dict(raw)
    link["id"] = str(link.get("id") or models.new_id("l"))
    link["src"] = src
    link["dst"] = dst
    link["direction"] = bool(link.get("direction", False))
    link["meaning"] = _as_text(link.get("meaning"))
    link["deleted"] = False
    link["createdAt"] = _as_text(link.get("createdAt")) or stamp
    link["updatedAt"] = _as_text(link.get("updatedAt")) or link["createdAt"]
    return link


def normalize_state(state: dict) -> dict:
    """落实契约 §1.2 的全部不变式（G1..G8），返回**新**状态（不原地改）。"""
    source = state if isinstance(state, dict) else {}
    stamp = _now()

    # --- 卡片：补默认值 + G6 / G7，并按 id 去重（同 id 只保留第一张） ---
    cards: list[dict] = []
    by_id: dict[str, dict] = {}
    for raw in source.get("cards") or []:
        if not isinstance(raw, dict):
            continue
        card = _normalize_card(raw, stamp)
        if card["id"] in by_id:
            continue
        by_id[card["id"]] = card
        cards.append(card)
    live = {cid for cid, card in by_id.items() if not card["deleted"]}

    # --- 组：G1 / G2 / G3 / G5，组名与组框 ---
    groups: list[dict] = []
    claimed: set[str] = set()
    taken_names: set[str] = set()
    for raw in source.get("groups") or []:
        if not isinstance(raw, dict):
            continue
        group = _normalize_group(raw, claimed=claimed, cards=by_id, taken_names=taken_names, stamp=stamp)
        if group is None:
            continue
        taken_names.add(group["name"])
        groups.append(group)

    # --- 链接：G4 / G5，同一对端点只保留一条 ---
    links: list[dict] = []
    seen_pairs: set[frozenset[str]] = set()
    for raw in source.get("links") or []:
        if not isinstance(raw, dict):
            continue
        link = _normalize_link(raw, live, stamp)
        if link is None:
            continue
        pair = frozenset((link["src"], link["dst"]))
        if pair in seen_pairs:
            continue
        seen_pairs.add(pair)
        links.append(link)

    # --- G8 选择只含存在且未删除的卡片 ---
    selection = [cid for cid in _dedupe(source.get("selection") or []) if cid in live]

    return {
        "boardId": _as_text(source.get("boardId")) or models.DEFAULT_BOARD_ID,
        "seq": int(_as_float(source.get("seq"), 0)),
        "updatedAt": _as_text(source.get("updatedAt")) or stamp,
        "cards": cards,
        "groups": groups,
        "links": links,
        "selection": selection,
    }


# --- 卡片：添加 / 编辑 / 删除 / 复制 ---------------------------------------


def empty_state(board_id: str) -> dict:
    return models.empty_state(board_id)


def add_card(state: dict, kind: str, content: str = "", **fields: Any) -> dict:
    """添加一张卡片。位置默认错开摆放（位置只影响显示，不构成意图依据）。"""
    if kind not in models.CARD_KINDS:
        return normalize_state(state)
    work = normalize_state(state)
    live_count = len([card for card in work["cards"] if not card["deleted"]])
    default_x = DEFAULT_CARD_ORIGIN + DEFAULT_CARD_STEP * (live_count % 8)
    default_y = DEFAULT_CARD_ORIGIN + DEFAULT_CARD_STEP * (live_count % 8)
    card = models.new_card(kind, content)
    card["x"] = _as_float(fields.get("x"), default_x)
    card["y"] = _as_float(fields.get("y"), default_y)
    card["w"] = max(1.0, _as_float(fields.get("w"), DEFAULT_CARD_W))
    card["h"] = max(1.0, _as_float(fields.get("h"), DEFAULT_CARD_H))
    for key in _CARD_PATCH_KEYS:
        if key in fields and key not in ("x", "y", "w", "h"):
            card[key] = fields[key]
    work["cards"].append(card)
    work["updatedAt"] = _now()
    return normalize_state(work)


def update_card(state: dict, card_id: str, patch: dict) -> dict:
    work = normalize_state(state)
    card = models.card_by_id(work, card_id)
    if card is None:
        return work
    changed = False
    for key in _CARD_PATCH_KEYS:
        if key not in patch:
            continue
        value = patch[key]
        if key in ("x", "y", "w", "h"):
            value = _as_float(value, card[key])
            if key in ("w", "h"):
                value = max(1.0, value)
        elif key == "meta":
            value = dict(value) if isinstance(value, dict) else {}
        elif key in ("checked", "hidden", "folded", "bookmarked", "deleted"):
            value = bool(value)
        elif key == "content":
            value = _as_text(value)
        if card.get(key) == value:
            continue
        card[key] = value
        changed = True
    if changed:
        card["updatedAt"] = _now()
        work["updatedAt"] = _now()
    return normalize_state(work)


def remove_card(state: dict, card_id: str) -> dict:
    """删除 = 撤回当前材料或关系（卡片保留 deleted 标记，可被撤销恢复）。"""
    return update_card(state, card_id, {"deleted": True})


def duplicate_card(state: dict, card_id: str) -> dict:
    """复制一张卡片：新 id、位置错开、勾选重置（副本是新材料，要重新确认）。"""
    work = normalize_state(state)
    source_card = models.card_by_id(work, card_id)
    if source_card is None or source_card["deleted"]:
        return work
    stamp = _now()
    copy_card = dict(source_card)
    copy_card["meta"] = copy.deepcopy(source_card["meta"])
    copy_card["id"] = models.new_id("c")
    copy_card["x"] = source_card["x"] + DUPLICATE_OFFSET
    copy_card["y"] = source_card["y"] + DUPLICATE_OFFSET
    copy_card["checked"] = False
    copy_card["hidden"] = False
    copy_card["deleted"] = False
    copy_card["createdAt"] = stamp
    copy_card["updatedAt"] = stamp
    if copy_card["kind"] == models.REPLY_KIND:
        copy_card["checked"] = False
    work["cards"].append(copy_card)
    # 副本留在原来的组里，紧跟原卡片
    group = models.group_of(work, card_id)
    if group is not None:
        group["members"].insert(group["members"].index(card_id) + 1, copy_card["id"])
        group["updatedAt"] = stamp
    work["updatedAt"] = stamp
    return normalize_state(work)


# --- 分组与顺序 -----------------------------------------------------------


def join_group(state: dict, card_id: str, group_id: str, index: int | None = None) -> dict:
    """把卡片加入组（会先离开原来的组）。有序组里 index 决定序号；组名不变。"""
    work = normalize_state(state)
    card = models.card_by_id(work, card_id)
    target = group_by_id(work, group_id)
    if card is None or card["deleted"] or target is None:
        return work
    stamp = _now()
    for group in work["groups"]:
        if group["id"] != group_id and card_id in group["members"]:
            group["members"].remove(card_id)
            group["updatedAt"] = stamp
    members = target["members"]
    if card_id in members:
        members.remove(card_id)
    members.insert(_clamp_index(index, len(members)), card_id)
    target["updatedAt"] = stamp
    work["updatedAt"] = stamp
    return normalize_state(work)


def remove_from_group(state: dict, card_id: str) -> dict:
    """把卡片移出组（自由摆放，位置不变）。组空了会自动消失（G3）。"""
    work = normalize_state(state)
    stamp = _now()
    changed = False
    for group in work["groups"]:
        if card_id in group["members"]:
            group["members"].remove(card_id)
            group["updatedAt"] = stamp
            changed = True
    if changed:
        work["updatedAt"] = stamp
    return normalize_state(work)


def dissolve_group(state: dict, group_id: str) -> dict:
    """解除组：成员全部恢复自由摆放，卡片本身不动。"""
    work = normalize_state(state)
    group = group_by_id(work, group_id)
    if group is None:
        return work
    group["members"] = []
    group["deleted"] = True
    work["updatedAt"] = _now()
    return normalize_state(work)


def rename_group(state: dict, group_id: str, name: str) -> dict:
    """改组名。留空不生效；名字是否还是默认名由名字本身决定（保留默认名也能提交）。"""
    work = normalize_state(state)
    group = group_by_id(work, group_id)
    if group is None:
        return work
    cleaned = _as_text(name).strip()
    if not cleaned or cleaned == group["name"]:
        return work
    group["name"] = cleaned
    group["defaultName"] = _is_default_name(cleaned)
    group["updatedAt"] = _now()
    work["updatedAt"] = _now()
    return normalize_state(work)


def set_group_ordered(state: dict, group_id: str, ordered: bool) -> dict:
    """有序 / 普通切换。普通组的自由摆放不表示先后；设为有往后列表顺序即序号。"""
    work = normalize_state(state)
    group = group_by_id(work, group_id)
    if group is None:
        return work
    if group["ordered"] == bool(ordered):
        return work
    group["ordered"] = bool(ordered)
    group["updatedAt"] = _now()
    work["updatedAt"] = _now()
    return normalize_state(work)


def move_within_group(state: dict, group_id: str, card_id: str, index: int) -> dict:
    """组内顺序调整：把成员移到 index（越界收敛到两端）。"""
    work = normalize_state(state)
    group = group_by_id(work, group_id)
    if group is None or card_id not in group["members"]:
        return work
    members = group["members"]
    members.remove(card_id)
    members.insert(_clamp_index(index, len(members)), card_id)
    group["updatedAt"] = _now()
    work["updatedAt"] = _now()
    return normalize_state(work)


def merge_groups(state: dict, source_group_id: str, target_group_id: str, insert_index: int | None = None) -> dict:
    """合并两个组：目标组保留 id 但换成默认名（原组不再独立保留）。

    - 被拖入组的成员**连续插入**目标位置；
    - 两个有序组 → 仍是有序，各自内部顺序保留，统一编号；
    - 有序组与普通组合并 → 普通组（取消序号），用户可重新设为有序。
    """
    work = normalize_state(state)
    source = group_by_id(work, source_group_id)
    target = group_by_id(work, target_group_id)
    if source is None or target is None or source["id"] == target["id"]:
        return work
    moved = [cid for cid in source["members"]]
    if not moved:
        return work
    index = _clamp_index(insert_index, len(target["members"]))
    target["members"][index:index] = moved
    target["ordered"] = bool(source["ordered"]) and bool(target["ordered"])
    target["name"] = _next_default_name([group["name"] for group in work["groups"] if group["id"] != source["id"]])
    target["defaultName"] = True
    target["updatedAt"] = _now()
    source["deleted"] = True
    work["updatedAt"] = _now()
    return normalize_state(work)


# --- 关系链接 -------------------------------------------------------------


def add_link(state: dict, src: str, dst: str, direction: bool = False, meaning: str = "") -> dict:
    """建立关系：方向与含义都由用户写明，系统不解释成因果 / 支持 / 执行顺序。

    同一对端点只保留一条（G4）：重复建立时更新已有那条的方向与含义。
    """
    work = normalize_state(state)
    if src == dst:
        return work
    live = models.live_card_ids(work)
    if src not in live or dst not in live:
        return work
    stamp = _now()
    pair = frozenset((src, dst))
    existing = next((link for link in work["links"] if frozenset((link["src"], link["dst"])) == pair), None)
    if existing is not None:
        existing["direction"] = bool(direction)
        existing["meaning"] = _as_text(meaning)
        existing["updatedAt"] = stamp
    else:
        work["links"].append(models.new_link(src, dst, direction=bool(direction), meaning=_as_text(meaning)))
    work["updatedAt"] = stamp
    return normalize_state(work)


def update_link(state: dict, link_id: str, patch: dict) -> dict:
    work = normalize_state(state)
    link = next((item for item in work["links"] if item["id"] == link_id), None)
    if link is None:
        return work
    changed = False
    for key in _LINK_PATCH_KEYS:
        if key not in patch:
            continue
        value = patch[key]
        if key in ("direction", "deleted"):
            value = bool(value)
        elif key == "meaning":
            value = _as_text(value)
        elif key in ("src", "dst"):
            value = str(value)
        if link.get(key) == value:
            continue
        link[key] = value
        changed = True
    if changed:
        link["updatedAt"] = _now()
        work["updatedAt"] = _now()
    return normalize_state(work)


def remove_link(state: dict, link_id: str) -> dict:
    """删除关系 = 撤回这条关联，不表示否定两端内容。"""
    return update_link(state, link_id, {"deleted": True})


# --- 选择 / 勾选 / 隐藏 / 折叠 / 书签 / 搜索 --------------------------------


def set_selection(state: dict, card_ids: Iterable[str]) -> dict:
    work = normalize_state(state)
    work["selection"] = _dedupe(card_ids or [])
    work["updatedAt"] = _now()
    return normalize_state(work)


def select_in_rect(state: dict, rect: dict, additive: bool = False) -> dict:
    """区域选择：与矩形有面积重叠的卡片。additive=true 时并入当前选择。"""
    work = normalize_state(state)
    x = _as_float((rect or {}).get("x"), 0.0)
    y = _as_float((rect or {}).get("y"), 0.0)
    w = _as_float((rect or {}).get("w"), 0.0)
    h = _as_float((rect or {}).get("h"), 0.0)
    area = (min(x, x + w), min(y, y + h), abs(w), abs(h))
    hit = [card["id"] for card in work["cards"] if not card["deleted"] and _rect_overlap(card_rect(card), area)]
    work["selection"] = _dedupe((work["selection"] if additive else []) + hit)
    work["updatedAt"] = _now()
    return normalize_state(work)


def set_checked(state: dict, card_id: str, checked: bool) -> dict:
    """勾选只对文字注释有意义（材料没有勾选框，reply 不参与）。

    勾选与明确隐藏互斥：勾上等于回到讨论范围，所以会取消隐藏。
    """
    work = normalize_state(state)
    card = models.card_by_id(work, card_id)
    if card is None or card["deleted"] or card["kind"] not in models.CHECKABLE_KINDS:
        return work
    value = bool(checked)
    if card["checked"] == value and not (value and card["hidden"]):
        return work
    card["checked"] = value
    if value:
        card["hidden"] = False
    card["updatedAt"] = _now()
    work["updatedAt"] = _now()
    return normalize_state(work)


def set_hidden(state: dict, card_id: str, hidden: bool) -> dict:
    """明确隐藏 = 退出讨论范围（与勾选互斥）；取消隐藏不等于重新勾选。"""
    work = normalize_state(state)
    card = models.card_by_id(work, card_id)
    if card is None or card["deleted"] or card["hidden"] == bool(hidden):
        return work
    card["hidden"] = bool(hidden)
    if card["hidden"]:
        card["checked"] = False
    card["updatedAt"] = _now()
    work["updatedAt"] = _now()
    return normalize_state(work)


def set_folded(state: dict, card_id: str, folded: bool) -> dict:
    """折叠只改变显示，不影响任何含义。"""
    work = normalize_state(state)
    card = models.card_by_id(work, card_id)
    if card is None or card["deleted"] or card["folded"] == bool(folded):
        return work
    card["folded"] = bool(folded)
    card["updatedAt"] = _now()
    work["updatedAt"] = _now()
    return normalize_state(work)


def set_bookmark(state: dict, card_id: str, bookmarked: bool) -> dict:
    """书签只提高查找优先级，不产生意图。"""
    work = normalize_state(state)
    card = models.card_by_id(work, card_id)
    if card is None or card["deleted"] or card["bookmarked"] == bool(bookmarked):
        return work
    card["bookmarked"] = bool(bookmarked)
    card["updatedAt"] = _now()
    work["updatedAt"] = _now()
    return normalize_state(work)


def search_cards(state: dict, query: str) -> list[str]:
    """板内局部搜索：只查用户自己的板面（**不是交给 QIO，也不受勾选限制**）。

    命中的卡片 id 按「书签优先，其次板面顺序」返回。
    """
    work = normalize_state(state)
    text = _as_text(query).strip().lower()
    if not text:
        return []
    hits: list[tuple[int, int, str]] = []
    for index, card in enumerate(work["cards"]):
        if card["deleted"]:
            continue
        haystack: list[str] = [card["content"]]
        for value in (card.get("meta") or {}).values():
            if isinstance(value, str):
                haystack.append(value)
            elif isinstance(value, (int, float, bool)):
                haystack.append(str(value))
            elif isinstance(value, (list, tuple)):
                haystack.extend(str(item) for item in value)
        if any(text in part.lower() for part in haystack):
            hits.append((0 if card["bookmarked"] else 1, index, card["id"]))
    hits.sort()
    return [card_id for _, _, card_id in hits]


# --- 拖动：预演与放下 ------------------------------------------------------


def _group_at_point(state: dict, x: float, y: float) -> dict | None:
    """落点所在的组；多个组框重叠时取面积最小的那个（最具体）。"""
    candidates: list[tuple[float, int, dict]] = []
    for index, group in enumerate(state.get("groups", []) or []):
        if group.get("deleted") or not group.get("members"):
            continue
        if _point_in_rect(x, y, group_frame(group)):
            rect = group_frame(group)
            candidates.append((rect[2] * rect[3], index, group))
    if not candidates:
        return None
    candidates.sort(key=lambda item: (item[0], item[1]))
    return candidates[0][2]


def _insert_index(state: dict, group: dict, exclude_card_id: str | None, x: float, y: float) -> int:
    """按落点算插入位置：数一数有几个成员的**中心**排在落点之前（阅读顺序 y,x）。

    排除正在拖动的卡片自己，这样「未移动 → 同一位置」不会把自己算进去。
    """
    others = [cid for cid in group["members"] if cid != exclude_card_id]
    centers: list[tuple[float, float]] = []
    for card_id in others:
        card = models.card_by_id(state, card_id)
        if card is None:
            continue
        centers.append((card["y"] + card["h"] / 2.0, card["x"] + card["w"] / 2.0))
    centers.sort()
    point = (y, x)
    return sum(1 for center in centers if center <= point)


def _overlapping_free_cards(state: dict, card_id: str, x: float, y: float) -> list[dict]:
    """与卡片矩形重叠的、未分组的活卡片（自动成组的对象）。

    矩形按**落点**算：拖动预演时卡片还没有真的移动过去。
    """
    card = models.card_by_id(state, card_id)
    if card is None:
        return []
    rect = (x, y, max(1.0, _as_float(card.get("w"), DEFAULT_CARD_W)), max(1.0, _as_float(card.get("h"), DEFAULT_CARD_H)))
    result: list[dict] = []
    for other in state.get("cards", []) or []:
        if other["id"] == card_id or other.get("deleted"):
            continue
        if models.group_of(state, other["id"]) is not None:
            continue
        if _rect_overlap(rect, card_rect(other)):
            result.append(other)
    return result


def _resolve_drop(state: dict, card_id: str, x: float, y: float) -> dict:
    target = _group_at_point(state, x, y)
    current = models.group_of(state, card_id)
    if target is not None:
        index = _insert_index(state, target, card_id, x, y)
        merges = current["id"] if current is not None and current["id"] != target["id"] else None
        return {"target": target, "index": index, "mergesWith": merges, "partners": []}
    partners = _overlapping_free_cards(state, card_id, x, y)
    return {
        "target": None,
        "index": None,
        "mergesWith": partners[0]["id"] if partners else None,
        "partners": partners,
    }


def preview_drop(state: dict, card_id: str, x: float, y: float) -> dict:
    """拖动期间显示将要加入的组或插入位置 —— **不改状态**。"""
    work = normalize_state(state)
    card = models.card_by_id(work, card_id)
    if card is None or card["deleted"]:
        return {"groupId": None, "index": None, "mergesWith": None}
    resolved = _resolve_drop(work, card_id, _as_float(x, 0.0), _as_float(y, 0.0))
    target = resolved["target"]
    return {
        "groupId": target["id"] if target is not None else None,
        "index": resolved["index"],
        "mergesWith": resolved["mergesWith"],
    }


def drop_card(state: dict, card_id: str, x: float, y: float) -> dict:
    """放下后才完成操作：重叠成组 / 加入已有组 / 合并两组 / 有序组按落点插入。"""
    px = _as_float(x, 0.0)
    py = _as_float(y, 0.0)
    work = normalize_state(state)
    card = models.card_by_id(work, card_id)
    if card is None or card["deleted"]:
        return {"state": work, "groupId": None, "merged": False, "index": None}

    resolved = _resolve_drop(work, card_id, px, py)
    stamp = _now()
    card["x"] = px
    card["y"] = py
    card["updatedAt"] = stamp
    work["updatedAt"] = stamp

    merged = False
    target = resolved["target"]
    if target is not None:
        current = models.group_of(work, card_id)
        if current is not None and current["id"] != target["id"]:
            # 两个已有组重叠 → 合并（新组用默认名，被拖入组的成员连续插入）
            work = merge_groups(work, current["id"], target["id"], resolved["index"])
            merged = True
        else:
            work = join_group(work, card_id, target["id"], resolved["index"])
    else:
        if models.group_of(work, card_id) is not None:
            # 拖出组：自由摆放，组空了会自动消失
            work = remove_from_group(work, card_id)
        partners = _overlapping_free_cards(work, card_id, px, py)
        if partners:
            # 两张（或多张）未分组卡片重叠 → 自动成组，默认组名
            group = models.new_group(_next_default_name(group["name"] for group in work["groups"]))
            group["members"] = [item["id"] for item in partners] + [card_id]
            work["groups"].append(group)
            work["updatedAt"] = stamp

    final = normalize_state(work)
    group = models.group_of(final, card_id)
    return {
        "state": final,
        "groupId": group["id"] if group is not None else None,
        "merged": merged,
        "index": group["members"].index(card_id) if group is not None else None,
    }
