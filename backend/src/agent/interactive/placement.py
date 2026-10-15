"""新建落点与相对摆放（F7，唯一实现）。

服务端与审批预览共用同一份：默认尺寸、尺寸夹取、x/y 校验、冲突时的确定性空位、
`place`（相对锚点摆放）。预览与执行调用同一个函数、同一份冻结 view；
依据变化（板面新增 / 移动）按既有版本校验处理。

坐标口径：板面坐标（board units），原点在板面左上，x 向右、y 向下。
"""

from __future__ import annotations

import math
from typing import Any, Callable, Iterable, Sequence

from agent.interactive import models

#: 尺寸口径（与前端 F2 geometry.CARD_MIN_SIZE / CARD_MAX_SIZE 同口径）。
CARD_MIN_W = 180.0
CARD_MIN_H = 96.0
CARD_MAX_W = 1600.0
CARD_MAX_H = 1200.0

#: 内容类型默认尺寸（F7）：结果卡 320×240、文字卡 260×170。
DEFAULT_SIZES: dict[str, tuple[float, float]] = {
    models.REPLY_KIND: (320.0, 240.0),
    "text": (260.0, 170.0),
}

#: 摆放间距与原点留白（不得默认落到 (0,0)）。
PLACEMENT_GAP = 24.0
ORIGIN_MARGIN = 60.0

#: 确定性找空位时最多向外扩多少圈（够用即止，绝不随机）。
MAX_SEARCH_RINGS = 64

SIDES = ("left", "right", "above", "below")


def _num(value: Any, fallback: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return fallback
    if not math.isfinite(number):
        return fallback
    return number


def _finite(value: Any) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(float(value))
    )


def default_size(kind: str) -> tuple[float, float]:
    """内容类型的默认尺寸（reply 320×240 / text 260×170，未知类型按文字卡口径）。"""
    return DEFAULT_SIZES.get(str(kind), DEFAULT_SIZES["text"])


def clamp_size(w: Any, h: Any) -> tuple[float, float]:
    """把尺寸夹进 [180×96, 1600×1200]（视野大小永远不是卡片上限）。"""
    width = _num(w, CARD_MIN_W)
    height = _num(h, CARD_MIN_H)
    return (
        min(CARD_MAX_W, max(CARD_MIN_W, width)),
        min(CARD_MAX_H, max(CARD_MIN_H, height)),
    )


def size_for(kind: str, w: Any = None, h: Any = None) -> tuple[float, float, bool]:
    """这次落地要用的尺寸：给了就夹取，省略就按内容类型取默认。

    返回 (w, h, adjusted)：adjusted=true 表示给了尺寸但被夹取过（调用方要如实说明）。
    """
    if w is None and h is None:
        dw, dh = default_size(kind)
        return dw, dh, False
    dw, dh = default_size(kind)
    base_w = dw if w is None else _num(w, dw)
    base_h = dh if h is None else _num(h, dh)
    cw, ch = clamp_size(base_w, base_h)
    return cw, ch, (cw != base_w or ch != base_h)


def rect_of(card: dict) -> dict:
    return {
        "id": str(card.get("id") or ""),
        "x": _num(card.get("x"), 0.0),
        "y": _num(card.get("y"), 0.0),
        "w": max(1.0, _num(card.get("w"), 1.0)),
        "h": max(1.0, _num(card.get("h"), 1.0)),
    }


def overlaps(a: dict, b: dict, *, gap: float = 0.0) -> bool:
    """矩形是否重叠（gap>0 时要求至少留出 gap 的间隙，边界相接不算重叠）。"""
    if a["x"] + a["w"] + gap <= b["x"]:
        return False
    if b["x"] + b["w"] + gap <= a["x"]:
        return False
    if a["y"] + a["h"] + gap <= b["y"]:
        return False
    if b["y"] + b["h"] + gap <= a["y"]:
        return False
    return True


def place_relative(anchor: dict, side: str, size: dict, gap: float) -> dict:
    """由程序按锚点确定性算出相对矩形（F7 的 placeRelative 同口径）。"""
    width = max(1.0, _num(size.get("w"), 1.0))
    height = max(1.0, _num(size.get("h"), 1.0))
    side = str(side or "right")
    if side not in SIDES:
        side = "right"
    if side == "left":
        x = anchor["x"] - gap - width
        y = anchor["y"]
    elif side == "above":
        x = anchor["x"]
        y = anchor["y"] - gap - height
    elif side == "below":
        x = anchor["x"]
        y = anchor["y"] + anchor["h"] + gap
    else:
        x = anchor["x"] + anchor["w"] + gap
        y = anchor["y"]
    return {"x": x, "y": y, "w": width, "h": height}


def find_free_slot(rect: dict, others: Iterable[dict], *, gap: float = PLACEMENT_GAP) -> dict:
    """从 rect 出发，按**确定性**顺序向外找附近空位（绝不随机挤动其他卡片）。

    顺序：圈号 → 行偏移 dy → 列偏移 dx（同一个起点永远得到同一个结果）。
    左边与上边以 0 为界（不允许负坐标）。
    """
    base = {
        "x": _num(rect.get("x"), 0.0),
        "y": _num(rect.get("y"), 0.0),
        "w": max(1.0, _num(rect.get("w"), 1.0)),
        "h": max(1.0, _num(rect.get("h"), 1.0)),
    }
    live = [rect_of(card) for card in others if isinstance(card, dict)]
    step_x = base["w"] + gap
    step_y = base["h"] + gap
    for ring in range(0, MAX_SEARCH_RINGS + 1):
        for dy in range(-ring, ring + 1):
            for dx in range(-ring, ring + 1):
                if max(abs(dx), abs(dy)) != ring:
                    continue
                x = base["x"] + dx * step_x
                y = base["y"] + dy * step_y
                if x < 0 or y < 0:
                    continue
                candidate = {"x": x, "y": y, "w": base["w"], "h": base["h"]}
                if not any(overlaps(candidate, other) for other in live):
                    return candidate
    # 理论不可达：如实返回原矩形（不假装找到了空位）。
    return base


def _blank_in_view(view: dict, size: dict, others: Iterable[dict], *, gap: float) -> dict | None:
    """在冻结 view 内找一块**完全落在视野里**的空白落点；找不到返回 None。"""
    x0 = _num(view.get("x"), 0.0)
    y0 = _num(view.get("y"), 0.0)
    vw = _num(view.get("w"), 0.0)
    vh = _num(view.get("h"), 0.0)
    if vw <= 0 or vh <= 0:
        return None
    width = max(1.0, _num(size.get("w"), 1.0))
    height = max(1.0, _num(size.get("h"), 1.0))
    live = [rect_of(card) for card in others if isinstance(card, dict)]
    step_x = min(width + gap, max(width, vw))
    step_y = min(height + gap, max(height, vh))
    # 横向先铺满，再换行；每一格都要求整块在 view 内。
    rows = max(1, int((vh - height) // step_y) + 1) if vh >= height else 0
    cols = max(1, int((vw - width) // step_x) + 1) if vw >= width else 0
    if rows == 0 or cols == 0:
        return None
    for row in range(rows):
        for col in range(cols):
            candidate = {
                "x": x0 + col * step_x,
                "y": y0 + row * step_y,
                "w": width,
                "h": height,
            }
            if not any(overlaps(candidate, other) for other in live):
                return candidate
    return None


def _anchor_rect(relative_to: Any, others: Sequence[dict], pending: Sequence[dict]) -> dict | None:
    key = str(relative_to or "").strip()
    if not key:
        return None
    for card in list(others) + list(pending):
        if not isinstance(card, dict):
            continue
        if str(card.get("id") or "") == key:
            return rect_of(card)
    return None


def plan_card_placement(
    op: dict,
    *,
    kind: str,
    others: Iterable[dict] = (),
    pending: Sequence[dict] = (),
    view: dict | None = None,
    resolve_card_id: Callable[[str], str] | None = None,
    gap: float = PLACEMENT_GAP,
) -> dict:
    """这次新建卡片的落点与尺寸（预览与执行共用）。

    - 给了 w/h：夹进 [180×96, 1600×1200]；
    - 给了 x/y：校验为有限数并**真实生效**；与既有卡片冲突时确定性找附近空位；
    - 省略 x/y：有 `place` 就按相对摆放；否则在冻结 view 内找空白落点；
      再不行从原点留白处找空位 —— **绝不落到 (0,0)**。
    """
    others = [card for card in others if isinstance(card, dict)]
    pending = [card for card in pending if isinstance(card, dict)]
    w, h, size_adjusted = size_for(kind, op.get("w"), op.get("h"))
    size = {"w": w, "h": h}
    given_x, given_y = op.get("x"), op.get("y")
    has_xy = _finite(given_x) and _finite(given_y)
    base: dict | None = None
    reason = "explicit"
    if has_xy:
        base = {"x": float(given_x), "y": float(given_y), "w": w, "h": h}
    else:
        place = op.get("place")
        if isinstance(place, dict) and str(place.get("relativeTo") or "").strip():
            relative_to = str(place.get("relativeTo")).strip()
            if resolve_card_id is not None:
                relative_to = str(resolve_card_id(relative_to) or relative_to)
            anchor = _anchor_rect(relative_to, others, pending)
            if anchor is not None:
                base = place_relative(
                    anchor,
                    str(place.get("side") or "right"),
                    size,
                    _num(place.get("gap"), gap),
                )
                reason = "relative"
        if base is None and isinstance(view, dict):
            blank = _blank_in_view(
                view, size, list(others) + list(pending), gap=PLACEMENT_GAP
            )
            if blank is not None:
                base = blank
                reason = "view"
        if base is None:
            base = {
                "x": ORIGIN_MARGIN,
                "y": ORIGIN_MARGIN,
                "w": w,
                "h": h,
            }
            reason = "free"
    # 冲突（含刚放下的其他新卡片）→ 确定性找空位；不动其他卡片。
    conflict = [rect_of(card) for card in list(others) + list(pending)]
    if base["x"] < 0:
        base["x"] = 0.0
    if base["y"] < 0:
        base["y"] = 0.0
    if any(overlaps(base, other) for other in conflict):
        base = find_free_slot(base, conflict)
        reason = "resolved"
    if base["x"] == 0.0 and base["y"] == 0.0 and not has_xy:
        # 兜底：默认落点绝不允许是 (0,0)。
        base = find_free_slot({**base, "x": ORIGIN_MARGIN, "y": ORIGIN_MARGIN}, conflict)
    return {
        "x": float(base["x"]),
        "y": float(base["y"]),
        "w": float(w),
        "h": float(h),
        "sizeAdjusted": bool(size_adjusted),
        "reason": reason,
    }
