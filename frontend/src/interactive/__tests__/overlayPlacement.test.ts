/**
 * 卡片局部工具栏的落点决策（纯函数）。
 *
 * 契约 §8.2 只要求「选中后在卡片附近出现」，没有规定上下；这里定的是**不许压住卡片**：
 * 竖向能放到卡片外面就放外面（先上后下，且整个工具栏都在可视区内），
 * 竖向两边都放不下（卡片比可视区还高）时，至少挪到卡片**横向之外**，而不是整块压在卡片上。
 *
 * 2026-10-14 修的依据（真实浏览器复现）：
 * - 1200×500 窗口：卡片 104–508、可视区 111–500、工具栏 277×35 → 旧实现把工具栏放在 442–478，
 *   整块落在卡片内部（用户报告：「他不应该在下面的时候挡住卡片」）；
 * - 旧实现的根因：下方位置被 maxTopClient = viewport.bottom − 30 夹回卡片里，而当时的
 *   「碰到卡片顶边」判定看不见这种「落在卡片下半部分」的情况，且 30px 也没算工具栏自身高度。
 */
import { describe, expect, it } from "vitest";
import { placeCardOverlay } from "../overlayPlacement";

const GAP = 6;
const TOOLBAR = { w: 277, h: 35 };
const VIEWPORT = { top: 111, bottom: 800, left: 0, right: 1200 };

function card(top: number, bottom: number, left = 407, right = 684) {
  return { top, bottom, left, right };
}

const overlaps = (placed: { left: number; top: number }, c: { top: number; bottom: number; left: number; right: number }) =>
  placed.top + TOOLBAR.h > c.top + 2 &&
  placed.top < c.bottom - 2 &&
  placed.left + TOOLBAR.w > c.left + 2 &&
  placed.left < c.right - 2;

describe("placeCardOverlay：工具栏不许压住卡片", () => {
  it("卡片上方放得下：放上方，且整个工具栏都在可视区内", () => {
    const c = card(254, 658);
    const placed = placeCardOverlay({ card: c, toolbar: TOOLBAR, viewport: VIEWPORT, gap: GAP });
    expect(placed.top).toBe(254 - TOOLBAR.h - GAP);
    expect(placed.top).toBeGreaterThanOrEqual(VIEWPORT.top);
    expect(overlaps(placed, c)).toBe(false);
  });

  it("卡片顶到可视区上沿、下方还有空间：放卡片下方", () => {
    const c = card(111, 400);
    const placed = placeCardOverlay({ card: c, toolbar: TOOLBAR, viewport: VIEWPORT, gap: GAP });
    expect(placed.top).toBe(400 + GAP);
    expect(placed.top + TOOLBAR.h).toBeLessThanOrEqual(VIEWPORT.bottom);
    expect(overlaps(placed, c)).toBe(false);
  });

  it("卡片底边贴近可视区下缘：不许被夹回卡片里（用户报告的场景）", () => {
    // 卡片 404 高、底边距下缘 20px：下方放不下（要 41px），但上方放得下 → 应该回到上方
    const c = card(376, 780);
    const placed = placeCardOverlay({ card: c, toolbar: TOOLBAR, viewport: VIEWPORT, gap: GAP });
    expect(overlaps(placed, c), "工具栏压住了卡片").toBe(false);
    expect(placed.top + TOOLBAR.h).toBeLessThanOrEqual(c.top);
  });

  it("卡片比可视区还高（竖向两边都放不下）：挪到卡片横向之外，而不是压在卡片上", () => {
    // 2026-10-14 实测几何：1200×500 窗口
    const short = { top: 111, bottom: 500, left: 0, right: 1200 };
    const c = card(104, 508);
    const placed = placeCardOverlay({ card: c, toolbar: TOOLBAR, viewport: short, gap: GAP });
    expect(overlaps(placed, c), "工具栏压住了卡片").toBe(false);
    expect(placed.left).toBeGreaterThanOrEqual(c.right);
    expect(placed.top).toBeGreaterThanOrEqual(short.top);
    expect(placed.top + TOOLBAR.h).toBeLessThanOrEqual(short.bottom);
  });

  it("横向也没地方（卡片占满宽度）：保底夹进可视区，仍然可见", () => {
    const short = { top: 111, bottom: 500, left: 0, right: 1200 };
    const c = card(104, 508, 0, 1200);
    const placed = placeCardOverlay({ card: c, toolbar: TOOLBAR, viewport: short, gap: GAP });
    expect(placed.left).toBeGreaterThanOrEqual(short.left);
    expect(placed.left + TOOLBAR.w).toBeLessThanOrEqual(short.right);
    expect(placed.top).toBeGreaterThanOrEqual(short.top);
    expect(placed.top + TOOLBAR.h).toBeLessThanOrEqual(short.bottom);
  });
});
