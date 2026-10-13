/**
 * 尺寸原语用例（互动模式契约 2026-10-13 · IRL §2）。
 *
 * 只测纯函数：不挂组件、不读 DOM、不碰 store / 网络 / localStorage。
 * 逐条对应任务书的硬规则：
 * 1. 八向拖动，拉西 / 北边时对边固定；
 * 2. 最小 / 最大尺寸夹紧，且最小值不得强行超过可用区域；
 * 3. 键盘 −/＋ 一次一步、只改一条边；
 * 4. 恢复时按当前可用空间收敛，绝不把窗口恢复到屏幕外；
 * 5. 新卡片优先放可见空白位置，不再按固定列数取模。
 */
import { describe, expect, it } from "vitest";
import {
  RESIZE_HANDLES,
  boxesOverlap,
  clampBox,
  convergeBox,
  findFreeSlot,
  limitsForArea,
  overlapArea,
  resizeBox,
  sizeLimits,
  stepBox,
  type ResizeBox,
} from "../resize";

const LIMITS = sizeLimits(100, 60, 800, 600);
const BASE: ResizeBox = { x: 100, y: 100, w: 400, h: 300 };

describe("resize：八个方向", () => {
  it("四个角 + 四条边一共 8 个把手，角在最后（渲染顺序）", () => {
    expect(RESIZE_HANDLES).toHaveLength(8);
    expect(RESIZE_HANDLES.slice(-4)).toEqual(["ne", "nw", "se", "sw"]);
  });

  it("东边：只改宽，位置不动", () => {
    const next = resizeBox(BASE, "e", 50, 999, LIMITS);
    expect(next).toEqual({ x: 100, y: 100, w: 450, h: 300 });
  });

  it("南边：只改高，位置不动", () => {
    const next = resizeBox(BASE, "s", 999, -40, LIMITS);
    expect(next).toEqual({ x: 100, y: 100, w: 400, h: 260 });
  });

  it("西边：对边固定（右边界不动）", () => {
    const next = resizeBox(BASE, "w", 60, 0, LIMITS);
    expect(next.x + next.w).toBe(BASE.x + BASE.w);
    expect(next.w).toBe(340);
    expect(next.x).toBe(160);
  });

  it("北边：对边固定（下边界不动）", () => {
    const next = resizeBox(BASE, "n", 0, 70, LIMITS);
    expect(next.y + next.h).toBe(BASE.y + BASE.h);
    expect(next.h).toBe(230);
    expect(next.y).toBe(170);
  });

  it("右下角：宽高同时变", () => {
    const next = resizeBox(BASE, "se", 20, 30, LIMITS);
    expect(next).toEqual({ x: 100, y: 100, w: 420, h: 330 });
  });

  it("拉到最小值以下时停住，且不产生「到界了还在漂」的位移", () => {
    const once = resizeBox(BASE, "w", 5000, 0, LIMITS);
    expect(once.w).toBe(LIMITS.minW);
    const twice = resizeBox(once, "w", 5000, 0, LIMITS);
    expect(twice).toEqual(once);
  });

  it("拉到最大值以上时停住", () => {
    const next = resizeBox(BASE, "se", 5000, 5000, LIMITS);
    expect(next.w).toBe(LIMITS.maxW);
    expect(next.h).toBe(LIMITS.maxH);
  });
});

describe("resize：上限来自可用区域", () => {
  it("可用区域比最小值还小时，最小值向内收敛（不强行套用超过屏幕宽度的最小值）", () => {
    const limits = limitsForArea({ w: 240, h: 180 }, { w: 300, h: 220 });
    expect(limits.minW).toBe(240);
    expect(limits.minH).toBe(180);
    expect(limits.maxW).toBe(240);
  });

  it("sizeLimits 不允许 min 超过 max", () => {
    const limits = sizeLimits(900, 700, 300, 200);
    expect(limits.minW).toBe(300);
    expect(limits.minH).toBe(200);
  });

  it("无限 / NaN 输入不产生 NaN 输出", () => {
    const next = resizeBox(BASE, "se", Number.NaN, Number.POSITIVE_INFINITY, LIMITS);
    expect(Number.isFinite(next.w)).toBe(true);
    expect(Number.isFinite(next.h)).toBe(true);
  });
});

describe("resize：键盘 −/＋", () => {
  it("一步 24px，只改一条边，左上角固定", () => {
    expect(stepBox(BASE, "w", 1, LIMITS)).toEqual({ x: 100, y: 100, w: 424, h: 300 });
    expect(stepBox(BASE, "h", -1, LIMITS)).toEqual({ x: 100, y: 100, w: 400, h: 276 });
  });

  it("步进同样被最小值夹住", () => {
    const small = { x: 0, y: 0, w: LIMITS.minW, h: LIMITS.minH };
    expect(stepBox(small, "w", -1, LIMITS).w).toBe(LIMITS.minW);
    expect(stepBox(small, "h", -1, LIMITS).h).toBe(LIMITS.minH);
  });

  it("clampBox 只夹宽高，不动位置", () => {
    const next = clampBox({ x: 12, y: 34, w: 1, h: 99999 }, LIMITS);
    expect(next.x).toBe(12);
    expect(next.y).toBe(34);
    expect(next.w).toBe(LIMITS.minW);
    expect(next.h).toBe(LIMITS.maxH);
  });
});

describe("resize：恢复时收敛回可见区", () => {
  it("保存的框比当前可用区域大：宽高收敛", () => {
    const next = convergeBox({ x: 0, y: 0, w: 2000, h: 1500 }, { w: 800, h: 600 });
    expect(next.w).toBe(800);
    expect(next.h).toBe(600);
  });

  it("保存的位置在屏幕外：挪回可见区（含负坐标）", () => {
    const right = convergeBox({ x: 5000, y: 5000, w: 400, h: 300 }, { w: 1000, h: 800 });
    expect(right.x).toBe(600);
    expect(right.y).toBe(500);
    const left = convergeBox({ x: -900, y: -900, w: 400, h: 300 }, { w: 1000, h: 800 });
    expect(left.x).toBe(0);
    expect(left.y).toBe(0);
  });

  it("origin 非零（标题栏之下）时不被拉回到 0", () => {
    const next = convergeBox({ x: 0, y: 0, w: 400, h: 300 }, { w: 1000, h: 700 }, { x: 0, y: 48 });
    expect(next.y).toBe(48);
  });
});

describe("resize：新卡片落点", () => {
  const area: ResizeBox = { x: 0, y: 0, w: 1200, h: 800 };

  it("空板面：落在可见范围左上角", () => {
    const slot = findFreeSlot({ size: { w: 240, h: 160 }, area, obstacles: [] });
    expect(slot.x).toBe(0);
    expect(slot.y).toBe(0);
  });

  it("避开已有卡片：结果与所有障碍都不重叠", () => {
    const obstacles: ResizeBox[] = [
      { x: 0, y: 0, w: 300, h: 200 },
      { x: 0, y: 216, w: 300, h: 200 },
      { x: 0, y: 432, w: 300, h: 200 },
    ];
    const slot = findFreeSlot({ size: { w: 240, h: 160 }, area, obstacles });
    for (const obstacle of obstacles) {
      expect(overlapArea({ x: slot.x - 16, y: slot.y - 16, w: slot.w + 32, h: slot.h + 32 }, obstacle)).toBe(0);
    }
    expect(slot.x).toBeGreaterThan(0);
  });

  it("平移之后仍落在**当前可见范围**里，不回到板面原点", () => {
    const scrolled = { x: 3200, y: 1800, w: 1200, h: 800 };
    const slot = findFreeSlot({ size: { w: 240, h: 160 }, area: scrolled, obstacles: [] });
    expect(slot.x).toBeGreaterThanOrEqual(scrolled.x);
    expect(slot.y).toBeGreaterThanOrEqual(scrolled.y);
  });

  it("张张不同：连续落点不重复（不再按固定列数取模）", () => {
    const obstacles: ResizeBox[] = [];
    const seen = new Set<string>();
    for (let i = 0; i < 8; i += 1) {
      const slot = findFreeSlot({ size: { w: 240, h: 160 }, area, obstacles });
      seen.add(slot.x + "," + slot.y);
      obstacles.push(slot);
    }
    expect(seen.size).toBe(8);
  });

  it("实在放不下时退化为重叠最小，而不是丢到不可见处", () => {
    const tiny: ResizeBox = { x: 0, y: 0, w: 100, h: 80 };
    const slot = findFreeSlot({ size: { w: 400, h: 300 }, area: tiny, obstacles: [tiny] });
    expect(boxesOverlap(slot, { x: -1000, y: -1000, w: 1, h: 1 })).toBe(false);
    expect(slot.w).toBeLessThanOrEqual(tiny.w);
    expect(slot.h).toBeLessThanOrEqual(tiny.h);
  });
});
