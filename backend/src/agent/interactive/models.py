"""互动模式的共享数据含义（契约实现，Lead 维护）。

这里只放**跨模块共用的东西**：常量、状态构造函数、可见范围判定。
各模块自己的实现逻辑不放这里（board / board_store / submission / intents 各归其主）。

契约：docs/interactive-mode-contract.md
"""

from __future__ import annotations

import json
import math
import uuid
from datetime import datetime, timezone
from typing import Any, Iterable

# --- 常量 ---------------------------------------------------------------

DEFAULT_BOARD_ID = "board_default"
DEFAULT_BOARD_TITLE = "互动板面"

CARD_KINDS = ("text", "file", "code", "url", "reply")
#: 材料类卡片：添加材料本身不等于要求总结 / 比较 / 修改 / 执行。
#: 统一文字卡（契约 §2.2）：code / url **不再**是材料通道 —— 卡内文字、链接与代码
#: 一律通过**整卡许可**的文字卡交付；遗留 code / url 卡在迁移完成前整体不可见。
#: 图片不再有独立 kind：它是**文件卡的附件**（meta.file），交付口径见 materials.resolve_images。
MATERIAL_KINDS = ("file",)
#: 遗留字符类卡片（迁移前的历史 kind）：仍可读，但绝不回退成可见 / 可交付材料。
LEGACY_TEXT_CARD_KINDS = ("code", "url")
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
    # F4：QIO 自己产出的结果卡片进入可见范围后，它的新增 / 正文变化也是板面变化
    # （不属于用户提出的工作，见 NON_INTENT_EXPRESSIONS）。
    "result_added",
    "result_updated",
)

#: 这些改动不属于「用户提出的工作」，不作为意图依据（但仍然属于板面状态与变动记录）：
#: 位置 / 大小只影响显示；添加或删除材料本身不等于要求总结、比较、修改、执行；
#: 删除是撤回材料或关系，不表示否定其内容，也不自动取消任务。
NON_INTENT_EXPRESSIONS = (
    "layout_only",
    "material_added",
    "material_removed",
    "note_deleted",
    "link_removed",
    # F4：可见结果卡的新增 / 正文变化交给 QIO 了解，但不是用户提出的新工作。
    "result_added",
    "result_updated",
)

#: 系统默认组名（契约 §9.1，覆盖 §1.3 / §6.1 的「组 N」）：
#: 需要系统给名字的所有路径统一用它。它只表示「这个名字是系统给的」，
#: 不替用户表达任何关系判断，也不承诺名字唯一（组身份按 id 区分）。
DEFAULT_GROUP_NAME = "默认组名"
#: 历史默认名「组 N」的前缀：只用于**识别**旧数据，不再用于生成新名字。
LEGACY_GROUP_NAME_PREFIX = "组"

# --- 参与讨论（Phase 2 冻结） ----------------------------------------------
#: 参与范围的唯一事实源：卡片进入 / 退出交给 QIO 的讨论范围。
#: checked / hidden 保留为历史兼容字段，迁移完成后不再决定可见性（契约 §5.1）。
PARTICIPATION_INCLUDED = "included"
PARTICIPATION_EXCLUDED = "excluded"
PARTICIPATION_VALUES = (PARTICIPATION_INCLUDED, PARTICIPATION_EXCLUDED)
#: 板面状态里的迁移标记：1 表示已经按新规则算过 participation（只用于审计，
#: 不新增用户可见的历史页；缺失或非法时按 1 处理并写入，不猜更高版本）。
PARTICIPATION_VERSION = 1

# --- 连线端点（Phase 2 冻结） ----------------------------------------------
#: 卡片端点仍为**裸 card id**（旧数据零迁移）；组端点写成 group:<groupId>。
#: card:<id> 是兼容写法，归一化后仍落成裸 card id。
CARD_ENDPOINT_PREFIX = "card:"
GROUP_ENDPOINT_PREFIX = "group:"
MAX_ENDPOINT_CHARS = 200

# --- 图片比例（Phase 2 冻结） ----------------------------------------------
#: 浏览器解码后的真实显示像素：只接受有限正数，超出上限按非法处理（不猜）。
MAX_IMAGE_DISPLAY_EDGE = 100_000.0
#: 宽高比上限（1:1000 / 1000:1）；超出说明这份 display 不可信，按非法处理。
MAX_ASPECT_RATIO = 1000.0
#: 只有位图扩展名会被当作「图片文件卡」；svg 等带脚本格式不算（与前端同一口径）。
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".avif", ".ico")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def default_group_name(index: int = 1) -> str:
    """系统默认组名：固定返回「默认组名」，不再编号（契约 §9.1）。

    组身份按 id 区分，不依赖名称唯一性，默认名可以重复；
    index 只保留参数位，避免调用方（intents 等）改动，且不影响任何行为。
    """
    return DEFAULT_GROUP_NAME


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def loads(text: str | None, fallback: Any) -> Any:
    if not text:
        return fallback
    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        return fallback
    # 板面状态在读取层就做一次性清洗：旧数据（无 participation / 旧图片比例）
    # 读出来即符合 Phase 2 口径，API 不必各自再迁移一次；随后通知观察者
    # （board.py 用它维护「上一版语义指纹」，保存时权威推进对象版本）。
    if is_board_state_payload(value):
        value = sanitize_board_state(value)
        observer = _BOARD_STATE_OBSERVER
        if observer is not None:
            observer(value)
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
        # Phase 2：新板面从第一天就是新参与规则，不需要再做历史迁移。
        "participationVersion": PARTICIPATION_VERSION,
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
    # F4：结果卡的默认勾选就是上面的 False（QIO 自己生成 ≠ 获得许可）；
    # 调用方明确传入 checked 时**尊重它**（用户可以许可查看一张结果卡）。
    #
    # Phase 2：新对象在构造时就带上 participation（唯一事实源）。
    # 文字卡暂不在这里写死：旧格式 / 旧测试仍用 checked 作为历史意图，
    # 由迁移函数按 checked 推导；前端新建文字卡会显式传 participation=included。
    if "participation" not in fields:
        if kind == "file":
            card["participation"] = PARTICIPATION_INCLUDED
        elif kind in LEGACY_TEXT_CARD_KINDS:
            card["participation"] = PARTICIPATION_EXCLUDED
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
        "src": str(src),
        "dst": str(dst),
        "direction": bool(direction),
        "meaning": meaning,
        "curve": {"x": 0.0, "y": 0.0},
        "deleted": False,
        "createdAt": stamp,
        "updatedAt": stamp,
    }



#: 关系曲线的弯折偏移上限（板面坐标像素）。只影响显示，不改变端点身份。
MAX_LINK_CURVE = 240.0


def normalize_curve(value: object) -> dict:
    """把曲线偏移归一为 {x, y}，非法值回到默认直线。"""
    if not isinstance(value, dict):
        return {"x": 0.0, "y": 0.0}
    out = {}
    for key in ("x", "y"):
        raw = value.get(key, 0)
        if isinstance(raw, bool):
            raw = 0
        try:
            number = float(raw)
        except (TypeError, ValueError):
            number = 0.0
        if not math.isfinite(number):
            number = 0.0
        out[key] = max(-MAX_LINK_CURVE, min(MAX_LINK_CURVE, number))
    return out



# --- 可见范围判定（权限边界，唯一实现） -----------------------------------


def is_live(card: dict) -> bool:
    return not card.get("deleted", False)


#: 需要「勾选才允许查看」的卡片：注释（文字卡片）与 QIO 结果卡片（reply）。
#: 结果卡默认 **未**勾选（QIO 自己生成不等于获得许可，F4）；勾选后许可持续有效，
#: 取消勾选 / 明确隐藏 / 删除立即退出范围。
CHECKABLE_KINDS = ANNOTATION_KINDS + (REPLY_KIND,)


def _legacy_participation(card: dict) -> str:
    """旧数据的参与意图（只在 cards[].participation 缺失 / 非法时使用）。

    契约 §5.1 的一次性迁移规则：

    - hidden=true -> excluded；
    - text / reply -> 保留旧 checked 意图（checked=true 才 included）；
    - file -> included（旧规则里材料默认在范围内，即使 checked=false）；
    - 遗留 code / url -> excluded（仍不可交付）。

    另外：旧 code / url 卡会被 text_migration 迁成文字卡（checked=true）；
    为了保持「旧 code / url 仍不可交付」，这里认得它们保留的 meta 标记
    （meta.language / meta.url），把它们继续算作 excluded。
    """
    if card.get("hidden", False):
        return PARTICIPATION_EXCLUDED
    kind = card.get("kind")
    if kind in LEGACY_TEXT_CARD_KINDS:
        return PARTICIPATION_EXCLUDED
    meta = card.get("meta") if isinstance(card.get("meta"), dict) else {}
    if kind == "text" and ("language" in meta or isinstance(meta.get("url"), dict)):
        # 由旧 code / url 卡迁移来的文字卡：旧规则下它们默认可见，但 Phase 2
        # 明确要求「旧 code/url -> excluded」（不能借迁移把不可交付内容带进范围）。
        return PARTICIPATION_EXCLUDED
    if kind == "file":
        return PARTICIPATION_INCLUDED
    if kind in CHECKABLE_KINDS:
        return PARTICIPATION_INCLUDED if card.get("checked", False) else PARTICIPATION_EXCLUDED
    return PARTICIPATION_EXCLUDED


def card_participation(card: dict) -> str:
    """一张卡当前的参与状态（唯一事实源；缺失 / 非法时按旧意图推导，不抛错）。

    显式值永远优先：一旦迁移写入 participation，checked / hidden 不再改变可见性。
    """
    # hidden 是「明确退出讨论范围」，优先级高于 participation（绝不让隐藏内容泄露）。
    if card.get("hidden", False):
        return PARTICIPATION_EXCLUDED
    # 遗留 code / url 卡无论显式值如何都不可交付（迁移完成前）。
    if card.get("kind") in LEGACY_TEXT_CARD_KINDS:
        return PARTICIPATION_EXCLUDED
    value = card.get("participation")
    if value in PARTICIPATION_VALUES:
        return str(value)
    return _legacy_participation(card)


def card_is_included(card: dict) -> bool:
    return card_participation(card) == PARTICIPATION_INCLUDED


def visible_except_deleted(card: dict) -> bool:
    """可见性判断，但**不看 deleted**（participation 是唯一事实源）。

    两种情况要用它：

    - 正常投影（`selectable_cards`）：先看它，再看 deleted；
    - 「删除 = 撤回」的表达：一张卡被删掉时，只有它在**当前**可见范围内，
      才允许把「撤回」这件事本身写进提交载荷 —— 否则未参与的注释会
      借着「删除」重新把文字与链接带进 before（实测过的漏洞）。
    """
    return card_is_included(card)


def selectable_cards(state: dict) -> list[dict]:
    """本次允许 QIO 查看的范围（唯一实现，前后端与提交载荷都用它）。

    规则（Phase 2）：

    - 只看 `participation`（唯一事实源）；缺失 / 非法时按旧数据规则推导一次；
    - file 新材料默认 included；text / reply 沿用旧 checked 意图，旧 code / url 恒 excluded；
    - hidden / checked 不再决定可见性（迁移后）；已删除的卡片不进范围。
    """
    return [
        card
        for card in state.get("cards", [])
        if isinstance(card, dict) and is_live(card) and visible_except_deleted(card)
    ]


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



# --- 组参与（由成员派生，没有独立字段） -----------------------------------


def live_group_ids(state: dict) -> set[str]:
    return {
        str(group["id"])
        for group in state.get("groups", []) or []
        if isinstance(group, dict) and group.get("id") and not group.get("deleted")
    }


def group_by_id(state: dict, group_id: str) -> dict | None:
    for group in state.get("groups", []) or []:
        if isinstance(group, dict) and str(group.get("id")) == str(group_id) and not group.get("deleted"):
            return group
    return None


def group_live_members(state: dict, group: dict) -> list[str]:
    """组里仍然存在的成员（保持成员顺序；删除的卡片不算）。"""
    cards = {
        str(card.get("id")): card
        for card in state.get("cards", []) or []
        if isinstance(card, dict) and card.get("id")
    }
    members: list[str] = []
    for raw in group.get("members", []) or []:
        card_id = str(raw)
        card = cards.get(card_id)
        if card is not None and is_live(card):
            members.append(card_id)
    return members


def group_participation(state: dict, group: dict) -> str:
    """组没有独立 participation：由成员派生 all / none / partial。"""
    members = group_live_members(state, group)
    if not members:
        return "none"
    cards = {str(card.get("id")): card for card in state.get("cards", []) or [] if isinstance(card, dict)}
    included = [card_id for card_id in members if card_is_included(cards.get(card_id, {}))]
    if len(included) == len(members):
        return "all"
    if not included:
        return "none"
    return "partial"


def group_is_visible(state: dict, group: dict) -> bool:
    """组进入模型可见范围的条件：未删除，且至少一名成员 included。

    只列出 included 成员；组名沿用既有规则（≥1 可见成员时才出现）。
    """
    if not isinstance(group, dict) or group.get("deleted"):
        return False
    cards = {str(card.get("id")): card for card in state.get("cards", []) or [] if isinstance(card, dict)}
    return any(card_is_included(cards.get(card_id, {})) for card_id in group.get("members", []) or [] if str(card_id) in cards)


def visible_group_ids(state: dict) -> set[str]:
    return {
        str(group["id"])
        for group in state.get("groups", []) or []
        if isinstance(group, dict) and group.get("id") and group_is_visible(state, group)
    }


# --- 连线端点（Phase 2） ---------------------------------------------------


def parse_link_endpoint(value: Any) -> tuple[str, str] | None:
    """解析端点：返回 ("card"|"group", id)；非法返回 None（不猜）。

    卡片端点：裸 id 或 card:<id>；组端点：group:<id>。空串 / 超长 / 含空白一律非法。
    """
    text = str(value if value is not None else "").strip()
    if not text or len(text) > MAX_ENDPOINT_CHARS:
        return None
    kind = "card"
    endpoint_id = text
    if text.startswith(GROUP_ENDPOINT_PREFIX):
        kind = "group"
        endpoint_id = text[len(GROUP_ENDPOINT_PREFIX):].strip()
    elif text.startswith(CARD_ENDPOINT_PREFIX):
        endpoint_id = text[len(CARD_ENDPOINT_PREFIX):].strip()
    if not endpoint_id or len(endpoint_id) > MAX_ENDPOINT_CHARS:
        return None
    if any(ch.isspace() for ch in endpoint_id):
        return None
    return kind, endpoint_id


def normalize_link_endpoint(value: Any) -> str:
    """归一化端点字符串：卡片落成裸 id（旧数据不变）；组落成 group:<id>；非法为空串。"""
    parsed = parse_link_endpoint(value)
    if parsed is None:
        return ""
    kind, endpoint_id = parsed
    return endpoint_id if kind == "card" else GROUP_ENDPOINT_PREFIX + endpoint_id


def is_group_endpoint(value: Any) -> bool:
    parsed = parse_link_endpoint(value)
    return parsed is not None and parsed[0] == "group"


def link_endpoint_visible(state: dict, value: Any) -> bool:
    """一个端点是否对模型可见：卡片看 participation；组看 ≥1 名 included 成员。"""
    parsed = parse_link_endpoint(value)
    if parsed is None:
        return False
    kind, endpoint_id = parsed
    if kind == "card":
        return endpoint_id in selectable_ids(state)
    group = group_by_id(state, endpoint_id)
    return group is not None and group_is_visible(state, group)


def link_visible(state: dict, link: dict) -> bool:
    """链接进入模型可见范围的条件：未删除，且两端都可见。"""
    if not isinstance(link, dict) or link.get("deleted"):
        return False
    return link_endpoint_visible(state, link.get("src")) and link_endpoint_visible(state, link.get("dst"))


def visible_links(state: dict) -> list[dict]:
    """提交投影 / 上下文共用的链接过滤（组端点也走同一规则）。

    submission.project_snapshot / project_baseline 目前仍按卡片 id 过滤链接；
    接入 Phase 2 时改用这个函数即可，避免前端与模型看到不同的关系集合。
    """
    return [link for link in state.get("links", []) or [] if link_visible(state, link)]


# --- 图片比例（meta.display） ----------------------------------------------


def _positive_number(value: Any) -> float | None:
    """有限正数才算事实；0 / 负数 / NaN / inf / 非数字一律 None（不猜）。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if number != number or number in (float("inf"), float("-inf")) or number <= 0:
        return None
    return number


def card_attachment_name(card: dict) -> str:
    meta = card.get("meta") if isinstance(card.get("meta"), dict) else {}
    block = meta.get("file") if isinstance(meta.get("file"), dict) else {}
    name = block.get("name") or meta.get("name") or ""
    return str(name or "").strip()


def card_attachment_id(card: dict) -> str:
    meta = card.get("meta") if isinstance(card.get("meta"), dict) else {}
    block = meta.get("file") if isinstance(meta.get("file"), dict) else {}
    raw = block.get("attachmentId") or block.get("attachment_id") or meta.get("attachmentId")
    return str(raw or "").strip()


def is_image_attachment_name(name: Any) -> bool:
    text = str(name or "").strip().lower()
    return bool(text) and text.endswith(IMAGE_SUFFIXES)


def is_image_file_card(card: dict) -> bool:
    """图片仍是 file 卡附件；这里只判断「像不像图片」，不新增 kind。"""
    if str(card.get("kind") or "") != "file":
        return False
    name = card_attachment_name(card)
    if name:
        return is_image_attachment_name(name)
    # 名字未知但有已保存附件：浏览器给了 display 才可能是图片；没有 display 就不猜。
    return bool(card_attachment_id(card))


def clean_card_display(card: dict) -> dict | None:
    """校验 meta.display；非法 / 未知 / 非图片文件卡一律 None（不写 0/NaN/猜比例）。

    输出只保留 {width?, height?, aspectRatio}；宽高同时存在时比例由真实像素算出，
    不信任调用方临时算的 aspectRatio（避免出现 0 / NaN / 不一致的比例）。
    """
    if str(card.get("kind") or "") != "file" or not is_image_file_card(card):
        return None
    meta = card.get("meta") if isinstance(card.get("meta"), dict) else {}
    raw = meta.get("display")
    if not isinstance(raw, dict):
        return None
    width = _positive_number(raw.get("width"))
    height = _positive_number(raw.get("height"))
    ratio = _positive_number(raw.get("aspectRatio"))
    if width is not None or height is not None:
        if width is None or height is None:
            return None
        if width > MAX_IMAGE_DISPLAY_EDGE or height > MAX_IMAGE_DISPLAY_EDGE:
            return None
        px_w, px_h = int(round(width)), int(round(height))
        if px_w < 1 or px_h < 1 or px_w > int(MAX_IMAGE_DISPLAY_EDGE) or px_h > int(MAX_IMAGE_DISPLAY_EDGE):
            return None
        computed = px_w / px_h
        if computed <= 0 or computed > MAX_ASPECT_RATIO or (1.0 / computed) > MAX_ASPECT_RATIO:
            return None
        return {"width": px_w, "height": px_h, "aspectRatio": round(computed, 6)}
    if ratio is not None and 0 < ratio <= MAX_ASPECT_RATIO and (1.0 / ratio) <= MAX_ASPECT_RATIO:
        return {"aspectRatio": round(ratio, 6)}
    return None


def apply_card_display(card: dict) -> None:
    """就地收敛一张卡片的 meta.display 与 w/h 比例（纯布局约束，不改语义版本）。"""
    meta = card.get("meta") if isinstance(card.get("meta"), dict) else {}
    display = clean_card_display(card)
    if display is None:
        if "display" in meta:
            meta = {key: value for key, value in meta.items() if key != "display"}
            card["meta"] = meta
        return
    meta = dict(meta)
    meta["display"] = display
    card["meta"] = meta
    ratio = display.get("aspectRatio")
    try:
        width = float(card.get("w"))
    except (TypeError, ValueError):
        width = 1.0
    if width != width or width in (float("inf"), float("-inf")) or width < 1.0:
        width = 1.0
    if not isinstance(ratio, (int, float)) or ratio <= 0:
        return
    height = width / float(ratio)
    if height < 1.0:
        height = 1.0
        width = float(ratio)
        if width < 1.0:
            width = 1.0
            height = width / float(ratio)
    card["w"] = round(width, 3)
    card["h"] = round(height, 3)


# --- 一次性迁移 / 读取清洗 ------------------------------------------------


def is_board_state_payload(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and isinstance(value.get("boardId"), str)
        and all(isinstance(value.get(key), list) for key in ("cards", "groups", "links"))
    )


def migrate_participation_state(state: dict) -> dict:
    """按契约 §5.1 做一次性 participation 迁移（幂等；显式值永远优先）。

    返回新的 state（cards / meta 为浅拷贝），并写入 participationVersion=1。
    不修改 groups / links / selection 的其它字段。
    """
    if not isinstance(state, dict):
        return state
    source = dict(state)
    cards: list[Any] = []
    for raw in state.get("cards") or []:
        if not isinstance(raw, dict):
            cards.append(raw)
            continue
        card = dict(raw)
        meta = card.get("meta")
        card["meta"] = dict(meta) if isinstance(meta, dict) else {}
        if card.get("hidden", False):
            # 迁移规则：hidden=true -> excluded（即使旧数据里还带着 included）。
            card["participation"] = PARTICIPATION_EXCLUDED
        elif card.get("participation") not in PARTICIPATION_VALUES:
            card["participation"] = _legacy_participation(card)
        cards.append(card)
    source["cards"] = cards
    marker = state.get("participationVersion")
    if isinstance(marker, bool) or not isinstance(marker, int) or marker < 1:
        marker = PARTICIPATION_VERSION
    source["participationVersion"] = marker
    return source


def sanitize_board_state(state: dict) -> dict:
    """读取 / 保存共用的轻量清洗：participation 迁移 + meta.display 与比例约束。

    这里不做完整 G1..G8 归一化（那是 board.normalize_state 的职责），只保证
    「旧数据读出来就带 participation」与「图片比例不会写出 0/NaN/猜测值」。
    """
    source = migrate_participation_state(state)
    cards = source.get("cards")
    if isinstance(cards, list):
        cleaned: list[Any] = []
        for card in cards:
            if isinstance(card, dict):
                apply_card_display(card)
            cleaned.append(card)
        source["cards"] = cleaned
    return source


#: 读取板面状态时的观察者（board.py 注册）：用于后端权威推进对象版本。
#: 传 None 只是不记录，不影响读取结果。
_BOARD_STATE_OBSERVER: Any = None


def set_board_state_observer(observer: Any) -> None:
    global _BOARD_STATE_OBSERVER
    _BOARD_STATE_OBSERVER = observer


# --- 真实模型理解与真实执行（第 9 轮，2026-10-11）------------------------
# 契约：docs/interactive-mode-contract.md §14（本轮冻结接口）

#: 一次提交的理解过程状态（受理与理解分开，取代「succeeded 同时代表本地成功与模型完成」）
UNDERSTANDING_STATUSES = (
    "accepted",           # 提交已可靠受理，理解请求已落库（模型还没开始）
    "understanding",      # 正在读取材料 / 调用模型
    "understood",         # 理解完成，产出了意图（等待审批）
    "needs_clarification",# 理解完成，但需要用户补充说明
    "no_action",          # 理解完成，当前无需开展任务
    "failed",             # 理解失败（可重试；重试从保存的快照恢复）
)
UNDERSTANDING_ACTIVE_STATUSES = ("accepted", "understanding")
UNDERSTANDING_SETTLED_STATUSES = ("understood", "needs_clarification", "no_action", "failed")

#: 模型输出的三种判定（不允许「每次提交都强制生成任务」）
UNDERSTANDING_KINDS = ("intent", "clarification", "no_action")

#: 材料在本次理解中的来源（诚实口径：拿到地址 / 拿到文件名 都不等于读到正文）
MATERIAL_SOURCES = (
    "inline",            # 板面卡片自带正文（注释 / 代码 / 用户直接输入的文字）
    "attachment_text",   # 真实读取了已保存附件的正文
    "url_fetched",       # 实际抓取并读到网页正文
    "url_address_only",  # 只有地址，没有读到正文
    "image_delivered",   # 图片：本次**真的把字节交给了模型**（多模态），由模型自己看
    "image_unsupported", # 图片：本次没有把字节交给模型（没有视觉能力 / 读不到 / 超上限），如实标注
    # 多媒体（契约 §4.4）：音频 / 视频的诚实事实与图片同一套口径，绝不混为一谈
    "audio_delivered",   # 音频字节已交给模型
    "audio_transcribed", # 音频经派生转写得到文字（带 transcript / 时段 / 提供方）
    "audio_unsupported", # 没有音频能力也没有转写通道（带原因）
    "video_delivered",   # 视频字节已交给模型
    "video_sampled",     # 视频按时间点抽样帧交付（不是逐帧）
    "video_unsupported", # 没有视频能力也没有可用的抽样通道（带原因）
    "unreadable",        # 附件当前不可读（格式未实现 / 读取失败），带原因
    "unsupported",       # 其他明确不支持的材料
)

#: 互动执行允许的板面操作：**用户在互动模式中能做的子集**。
#: 不允许删除、隐藏、勾选用户内容；不允许绕过审批直接写库。
ALLOWED_INTENT_OPS = (
    "add_reply_card",   # 生成 QIO 结果卡片
    "add_text_card",    # 生成文字卡片（说明 / 清单）
    "move_card",        # 只移动位置（语义不变）
    "group_cards",      # 成组 / 加入组
    "order_group",      # 有序组内排序
    "add_link",         # 建立关系链接（含用户写明的含义）
)

#: 一次真实执行记录的状态（意图行的 status 是任务状态，这里是一次执行尝试的状态）
EXECUTION_STATUSES = ("queued", "running", "paused", "done", "failed", "cancelled")
EXECUTION_TERMINAL_STATUSES = ("done", "failed", "cancelled")

#: 理解请求 / 执行记录的 id 前缀
UNDERSTANDING_ID_PREFIX = "u"
EXECUTION_ID_PREFIX = "x"


def new_understanding_id() -> str:
    return new_id(UNDERSTANDING_ID_PREFIX)


def new_execution_id() -> str:
    return new_id(EXECUTION_ID_PREFIX)

