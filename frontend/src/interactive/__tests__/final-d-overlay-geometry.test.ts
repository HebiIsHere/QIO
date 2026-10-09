/**
 * 收尾轮 · D 的反例测试（19：定位与浮条离屏）——纯几何部分。
 *
 * 契约：docs/interactive-final-closure-contract.md M8。
 * 反例 19 要求：
 * - 预览四方向（右 / 下 / 左 / 上）离屏时，定位都要把它移进实际可用区域（不只左 / 上）；
 * - 完全离屏的预览不作为浮条的可见锚点；
 * - 操作浮条始终留在视口内（优先视口约束，其次才轮到浮层互避让）；
 * - 几何数值先确认有效（NaN / Infinity 不产生假结论），再断言边界。
 *
 * 这些几何事实统一落在 interactive/overlayLayout.ts 的纯函数里（无 DOM，可离线验证）。
 */
import { describe, expect, it } from "vitest";
import {
  panDeltaToReveal,
  planDockStrip,
  rectVisibleIn,
  type BoxRect,
} from "../overlayLayout";

const VIEW = { left: 40, top: 100, right: 3040, bottom: 2100 };

function box(left: number, top: number, right: number, bottom: number): BoxRect {
  return { left, top, right, bottom };
}

describe("panDeltaToReveal（19a：四方向定位）", () => {
  it("目标在右侧视口外 → 位移把右缘拉回可用区（应用位移后不再越界）", () => {
    const delta = panDeltaToReveal({ viewport: VIEW, target: box(3200, 300, 3500, 400), margin: 60 });
    expect(delta.dx).toBeLessThan(0);
    expect(3500 + delta.dx).toBeLessThanOrEqual(VIEW.right - 60 + 0.5);
    expect(delta.dy).toBe(0);
  });

  it("目标在下侧视口外 → 位移把底缘拉回可用区", () => {
    const delta = panDeltaToReveal({ viewport: VIEW, target: box(100, 2300, 400, 2400), margin: 60 });
    expect(delta.dy).toBeLessThan(0);
    expect(2400 + delta.dy).toBeLessThanOrEqual(VIEW.bottom - 60 + 0.5);
    expect(delta.dx).toBe(0);
  });

  it("目标在左 / 上视口外 → 反向位移（既有行为保持，不回退）", () => {
    const left = panDeltaToReveal({ viewport: VIEW, target: box(-300, 300, -50, 400), margin: 60 });
    expect(left.dx).toBeGreaterThan(0);
    expect(-300 + left.dx).toBeGreaterThanOrEqual(VIEW.left + 60 - 0.5);
    const top = panDeltaToReveal({ viewport: VIEW, target: box(100, -200, 400, -20), margin: 60 });
    expect(top.dy).toBeGreaterThan(0);
  });

  it("目标部分可见但越出右 / 下边缘 → 仍按越出方向位移（不只处理左 / 上）", () => {
    const delta = panDeltaToReveal({ viewport: VIEW, target: box(3000, 200, 3300, 300), margin: 60 });
    expect(delta.dx).toBeLessThan(0);
    expect(3300 + delta.dx).toBeLessThanOrEqual(VIEW.right - 60 + 0.5);
    const down = panDeltaToReveal({ viewport: VIEW, target: box(100, 2050, 400, 2200), margin: 60 });
    expect(down.dy).toBeLessThan(0);
    expect(2200 + down.dy).toBeLessThanOrEqual(VIEW.bottom - 60 + 0.5);
  });

  it("目标完全在视口内 → 不平移", () => {
    const delta = panDeltaToReveal({ viewport: VIEW, target: box(500, 500, 800, 600), margin: 60 });
    expect(delta).toEqual({ dx: 0, dy: 0 });
  });

  it("极远坐标（1e9 量级）结果仍是有限数，不产生 NaN", () => {
    const delta = panDeltaToReveal({ viewport: VIEW, target: box(1e9, -1e9, 1e9 + 300, -1e9 + 100), margin: 60 });
    expect(Number.isFinite(delta.dx)).toBe(true);
    expect(Number.isFinite(delta.dy)).toBe(true);
  });

  it("几何数值无效（NaN）→ 不产生位移，也不抛错", () => {
    const delta = panDeltaToReveal({
      viewport: VIEW,
      target: box(Number.NaN, 300, Number.NaN, 400),
      margin: 60,
    });
    expect(delta).toEqual({ dx: 0, dy: 0 });
  });
});

describe("rectVisibleIn（19c：完全离屏不作为锚点）", () => {
  it("完全在视口右 / 下外 → 不可见", () => {
    expect(rectVisibleIn(box(3100, 300, 3400, 400), VIEW)).toBe(false);
    expect(rectVisibleIn(box(100, 2200, 400, 2400), VIEW)).toBe(false);
  });

  it("完全在视口左 / 上外 → 不可见", () => {
    expect(rectVisibleIn(box(-400, 300, -100, 400), VIEW)).toBe(false);
    expect(rectVisibleIn(box(100, -300, 400, -100), VIEW)).toBe(false);
  });

  it("部分可见（压线）→ 可见", () => {
    expect(rectVisibleIn(box(3000, 300, 3200, 400), VIEW)).toBe(true);
    expect(rectVisibleIn(box(100, 2050, 400, 2150), VIEW)).toBe(true);
  });

  it("零面积 / 数值无效 → 不可见（不是真元素）", () => {
    expect(rectVisibleIn(box(500, 500, 500, 600), VIEW)).toBe(false);
    expect(rectVisibleIn(box(Number.NaN, 500, 800, 600), VIEW)).toBe(false);
  });
});

describe("planDockStrip（19c：浮条留在视口内）", () => {
  const STRIP = { width: 360, height: 150 };
  const base = {
    viewport: { width: 1440, height: 900 },
    strip: STRIP,
    bottomLimit: 900 - 96,
    topLimit: 64,
    edge: 16,
    gap: 10,
    avoid: [] as BoxRect[],
  };

  it("锚点可见时贴在锚点下方，且整体在视口与底部边界之内", () => {
    const plan = planDockStrip({ ...base, anchor: box(500, 200, 900, 400) });
    expect(plan.mode).toBe("anchored-below");
    expect(plan.top).toBeGreaterThanOrEqual(base.topLimit);
    expect(plan.top + STRIP.height).toBeLessThanOrEqual(base.bottomLimit);
    expect(plan.left).toBeGreaterThanOrEqual(base.edge);
    expect(plan.left + STRIP.width).toBeLessThanOrEqual(base.viewport.width - base.edge);
  });

  it("锚点贴着底部边界、下方放不下 → 换到锚点上方，仍不出视口", () => {
    const plan = planDockStrip({ ...base, anchor: box(500, 500, 900, 780) });
    expect(plan.mode).toBe("anchored-above");
    expect(plan.top + STRIP.height).toBeLessThanOrEqual(base.bottomLimit);
    expect(plan.top).toBeLessThan(500);
  });

  it("完全离屏的预览不作为锚点：退回板面下沿，且退回位置本身在视口内", () => {
    for (const anchor of [
      box(2000, 300, 2400, 400),
      box(100, 860, 500, 1000),
      box(-500, 300, -100, 400),
      box(100, -300, 500, -100),
    ]) {
      const plan = planDockStrip({ ...base, anchor });
      expect(plan.mode).toBe("fallback");
      expect(plan.top).toBeGreaterThanOrEqual(base.topLimit);
      expect(plan.top + STRIP.height).toBeLessThanOrEqual(base.bottomLimit);
      expect(plan.left).toBeGreaterThanOrEqual(base.edge);
      expect(plan.left + STRIP.width).toBeLessThanOrEqual(base.viewport.width - base.edge);
    }
  });

  it("优先视口约束，其次才避让浮层：候选与必须避开的浮层相交时换不相交的方向", () => {
    const chat = box(500, 410, 900, 780);
    const plan = planDockStrip({ ...base, anchor: box(500, 200, 900, 400), avoid: [chat] });
    expect(plan.mode).not.toBe("anchored-below");
    const placed: BoxRect = { left: plan.left, top: plan.top, right: plan.left + STRIP.width, bottom: plan.top + STRIP.height };
    const width = Math.min(placed.right, chat.right) - Math.max(placed.left, chat.left);
    const height = Math.min(placed.bottom, chat.bottom) - Math.max(placed.top, chat.top);
    expect(width <= 0 || height <= 0).toBe(true);
  });

  it("窄窗口：浮条按可用宽度夹取，left 不会出视口", () => {
    const plan = planDockStrip({
      ...base,
      viewport: { width: 480, height: 600 },
      bottomLimit: 600 - 96,
      anchor: box(600, 200, 1000, 400),
    });
    const stripW = Math.min(STRIP.width, 480 - 2 * base.edge);
    expect(plan.left).toBeGreaterThanOrEqual(base.edge);
    expect(plan.left + stripW).toBeLessThanOrEqual(480 - base.edge);
    expect(Number.isFinite(plan.left)).toBe(true);
    expect(Number.isFinite(plan.top)).toBe(true);
  });

  it("几何数值无效（NaN 锚点）→ 走 fallback 且结果有限", () => {
    const plan = planDockStrip({ ...base, anchor: box(Number.NaN, 200, 900, 400) });
    expect(plan.mode).toBe("fallback");
    expect(Number.isFinite(plan.left)).toBe(true);
    expect(Number.isFinite(plan.top)).toBe(true);
  });
});
