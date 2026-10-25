/**
 * 卡片局部工具栏的落点决策（纯函数）。
 *
 * 契约（2026-10-14 第二轮，用户报告「工具栏与卡片有偏移、松手后还会漂」）：
 * - 左边缘 = 所属卡片实际显示外框的左边缘（0 偏差）；
 * - 底边 = 卡片顶边上方固定的 gap 屏幕像素；
 * - **没有**横向夹取、**没有**上下翻转、**没有**侧移：视口边缘不改变相对位置。
 *
 * 用例刻意把「旧实现会翻转 / 夹取 / 侧移」的几何都摆出来，断言新规则一动不动。
 */
import { describe, expect, it } from "vitest";
import { placeToolbarRelativeToCard, rectsIntersect, type OverlayRect } from "../overlayPlacement";

const GAP = 10;
const TOOLBAR_H = 34;

function rect(left: number, top: number, right: number, bottom: number): OverlayRect {
  return { left, top, right, bottom };
}

/** 宿主原点：真实浏览器里 .card-toolbar-host 与 .board-viewport 同一个盒子。 */
const HOST = rect(40, 100, 1240, 900);

describe("placeToolbarRelativeToCard：固定锚点，不做任何避让", () => {
  it("左边缘对齐卡片左边缘，底边在卡片顶边上方 gap", () => {
    const card = rect(407, 254, 684, 658);
    const placed = placeToolbarRelativeToCard({ cardRect: card, hostRect: HOST, toolbarHeight: TOOLBAR_H, gap: GAP });
    expect(placed.left).toBe(card.left - HOST.left);
    expect(placed.top).toBe(card.top - HOST.top - TOOLBAR_H - GAP);
  });

  it("工具栏高度实测：换行 / 文案变化时 top 跟着实测高度走", () => {
    const card = rect(407, 254, 684, 658);
    const oneLine = placeToolbarRelativeToCard({ cardRect: card, hostRect: HOST, toolbarHeight: 34, gap: GAP });
    const wrapped = placeToolbarRelativeToCard({ cardRect: card, hostRect: HOST, toolbarHeight: 76, gap: GAP });
    expect(oneLine.top - wrapped.top).toBe(42);
    expect(wrapped.left).toBe(oneLine.left);
  });

  it("卡片顶到可视区上沿（旧实现会翻到下方）：仍然在卡片上方，不翻转", () => {
    const card = rect(407, 102, 684, 400);
    const placed = placeToolbarRelativeToCard({ cardRect: card, hostRect: HOST, toolbarHeight: TOOLBAR_H, gap: GAP });
    expect(placed.top).toBe(102 - 100 - TOOLBAR_H - GAP);
    expect(placed.top).toBeLessThan(card.top - HOST.top);
  });

  it("卡片贴着可视区右边缘（旧实现会横向夹取）：left 仍是卡片左边缘", () => {
    const card = rect(1180, 300, 1420, 500);
    const placed = placeToolbarRelativeToCard({ cardRect: card, hostRect: HOST, toolbarHeight: TOOLBAR_H, gap: GAP });
    expect(placed.left).toBe(1140);
    expect(placed.left + 277).toBeGreaterThan(HOST.right - HOST.left);
  });

  it("卡片贴着可视区左边缘：left 可以为负，不夹取", () => {
    const card = rect(-120, 300, 60, 500);
    const placed = placeToolbarRelativeToCard({ cardRect: card, hostRect: HOST, toolbarHeight: TOOLBAR_H, gap: GAP });
    expect(placed.left).toBe(-160);
  });

  it("卡片比可视区还高（旧实现会侧移到卡片左右）：不侧移，left 不变", () => {
    const card = rect(40, 111, 1240, 1000);
    const placed = placeToolbarRelativeToCard({ cardRect: card, hostRect: HOST, toolbarHeight: TOOLBAR_H, gap: GAP });
    expect(placed.left).toBe(card.left - HOST.left);
    expect(placed.left).not.toBe(card.right - HOST.left + GAP);
  });

  it("竖卡：工具栏改贴左边，顶部对齐并避开左边中点", () => {
    const card = rect(407, 254, 627, 658); // 220 宽、404 高
    const placed = placeToolbarRelativeToCard({
      cardRect: card,
      hostRect: HOST,
      toolbarHeight: TOOLBAR_H,
      toolbarWidth: 210,
      gap: GAP,
    });
    expect(placed.left).toBe(407 - 210 - GAP - HOST.left);
    expect(placed.top).toBe(254 - HOST.top + 8);
    expect(placed.top).toBeLessThan(card.top + (card.bottom - card.top) / 2 - HOST.top);
  });

  it("宽卡：仍贴上边", () => {
    const card = rect(407, 254, 847, 514);
    const placed = placeToolbarRelativeToCard({
      cardRect: card,
      hostRect: HOST,
      toolbarHeight: TOOLBAR_H,
      toolbarWidth: 210,
      gap: GAP,
    });
    expect(placed.left).toBe(card.left - HOST.left);
    expect(placed.top).toBe(card.top - HOST.top - TOOLBAR_H - GAP);
  });

  it("宿主处在缩放祖先下：按实测比例逆变换（hostRect / offsetWidth）", () => {
    const card = rect(240, 300, 440, 500);
    const scaledHost = rect(40, 100, 640, 500); // 1200×800 的宿主被 2 倍祖先放大后只剩 600×400
    const placed = placeToolbarRelativeToCard({
      cardRect: card,
      hostRect: scaledHost,
      toolbarHeight: 68, // 屏幕像素是布局值的 2 倍
      gap: 10,
      hostScale: { x: 2, y: 2 },
    });
    expect(placed.left).toBe((240 - 40) / 2);
    expect(placed.top).toBe((300 - 100 - 68 - 10) / 2);
  });

  it("缩放比例非法（0 / 负数 / NaN / 缺省）一律按 1，不写经验补偿", () => {
    const card = rect(240, 300, 440, 500);
    const base = { cardRect: card, hostRect: HOST, toolbarHeight: TOOLBAR_H, gap: GAP };
    const expected = { left: 200, top: 300 - 100 - TOOLBAR_H - GAP };
    for (const hostScale of [{ x: 0, y: 0 }, { x: -2, y: -2 }, { x: Number.NaN, y: Number.NaN }]) {
      expect(placeToolbarRelativeToCard({ ...base, hostScale })).toEqual(expected);
    }
    expect(placeToolbarRelativeToCard(base)).toEqual(expected);
  });
});

describe("rectsIntersect：只决定隐藏，不参与定位", () => {
  const view = rect(0, 0, 1200, 800);

  it("部分相交 = 可见（照常按固定相对位置显示）", () => {
    expect(rectsIntersect(rect(-50, -50, 100, 100), view)).toBe(true);
    expect(rectsIntersect(rect(1100, 750, 1400, 1000), view)).toBe(true);
  });

  it("完全不相交 = 隐藏", () => {
    expect(rectsIntersect(rect(1300, 100, 1400, 200), view)).toBe(false);
    expect(rectsIntersect(rect(0, -300, 200, -1), view)).toBe(false);
  });

  it("边贴边不算相交", () => {
    expect(rectsIntersect(rect(1200, 0, 1400, 100), view)).toBe(false);
    expect(rectsIntersect(rect(0, 800, 100, 900), view)).toBe(false);
  });
});
