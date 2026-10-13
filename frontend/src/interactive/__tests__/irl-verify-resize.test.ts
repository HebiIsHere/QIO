/**
 * IRL-E 独立复核（第一阶段）· resize.ts 对抗式用例。
 *
 * 与 Lead 的 resize.test.ts 刻意换角度：不重述同样的样例，而是
 * 1) 跨「8 方向 × 7×7 位移」的属性式断言（对边固定 / 夹紧 / 单调 / 幂等）；
 * 2) 夹紧边界的精确值；
 * 3) 坏数据（NaN / ±Infinity / 零面积）的传播路径；
 * 4) findFreeSlot 的可见性不变量、顺序落点、已知的窄缝漏找。
 *
 * it.fails = 「按 _IRL-COMMON.md §2 契约应当成立、当前实现不成立」的最小复现；
 * 这样文件整体保持绿色，缺陷本身写在 _IRL-REVIEW-E-1.md 并回报 Lead。
 */
import { describe, expect, it } from "vitest";
import {
  RESIZE_HANDLES,
  boxEquals,
  boxesOverlap,
  clampBox,
  clampNumber,
  convergeBox,
  findFreeSlot,
  limitsForArea,
  overlapArea,
  resizeBox,
  sizeLimits,
  stepBox,
  usableArea,
  type ResizeBox,
  type ResizeHandle,
  type SizeLimits,
} from "../resize";

/** 确定性伪随机：同一 seed 复现同一组样本，报告里的数字可以被别人重跑。 */
function makeRandom(seed: number): () => number {
  let state = seed >>> 0;
  return () => {
    state = (state + 0x6d2b79f5) >>> 0;
    let t = Math.imul(state ^ (state >>> 15), 1 | state);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

/** 属性式用例只留前几条违规，避免失败输出淹没有效信息。 */
function note(list: string[], message: string): void {
  if (list.length < 6) list.push(message);
}

const HANDLES: readonly ResizeHandle[] = RESIZE_HANDLES;
const LIMITS: SizeLimits = sizeLimits(120, 80, 640, 480);
const START: ResizeBox = { x: 200, y: 150, w: 320, h: 240 };
const DELTAS = [-4000, -321, -7, 0, 7, 321, 4000];
const show = (box: ResizeBox): string =>
  "x=" + box.x + ",y=" + box.y + ",w=" + box.w + ",h=" + box.h;
const has = (handle: ResizeHandle, edge: string): boolean => handle.indexOf(edge) >= 0;

describe("八向拖动：跨方向 × 位移的属性式不变量", () => {
  it("尺寸永远落在限制内；没被拉的轴位置与尺寸都不动", () => {
    const bad: string[] = [];
    for (const handle of HANDLES) {
      for (const dx of DELTAS) {
        for (const dy of DELTAS) {
          const next = resizeBox(START, handle, dx, dy, LIMITS);
          const tag = handle + "(" + dx + "," + dy + ") -> " + show(next);
          if (next.w < LIMITS.minW || next.w > LIMITS.maxW) note(bad, "宽越界 " + tag);
          if (next.h < LIMITS.minH || next.h > LIMITS.maxH) note(bad, "高越界 " + tag);
          const vertical = has(handle, "n") || has(handle, "s");
          const horizontal = has(handle, "e") || has(handle, "w");
          if (!horizontal && (next.x !== START.x || next.w !== START.w)) note(bad, "水平轴被带动 " + tag);
          if (!vertical && (next.y !== START.y || next.h !== START.h)) note(bad, "垂直轴被带动 " + tag);
        }
      }
    }
    expect(bad).toEqual([]);
  });

  it("对边固定：拉西 / 拉北时右 / 下边界分毫不动，拉东 / 拉南时左 / 上边界分毫不动", () => {
    const bad: string[] = [];
    const right = START.x + START.w;
    const bottom = START.y + START.h;
    for (const handle of HANDLES) {
      for (const dx of DELTAS) {
        for (const dy of DELTAS) {
          const next = resizeBox(START, handle, dx, dy, LIMITS);
          const tag = handle + "(" + dx + "," + dy + ") -> " + show(next);
          if (has(handle, "w") && next.x + next.w !== right) note(bad, "右边界漂了 " + tag);
          if (has(handle, "e") && !has(handle, "w") && next.x !== START.x) note(bad, "左边界漂了 " + tag);
          if (has(handle, "n") && next.y + next.h !== bottom) note(bad, "下边界漂了 " + tag);
          if (has(handle, "s") && !has(handle, "n") && next.y !== START.y) note(bad, "上边界漂了 " + tag);
        }
      }
    }
    expect(bad).toEqual([]);
  });

  it("单调：拉伸方向只增不减，反方向只减不增", () => {
    const bad: string[] = [];
    for (const handle of HANDLES) {
      let east = -Infinity;
      let west = Infinity;
      let south = -Infinity;
      let north = Infinity;
      for (const delta of DELTAS) {
        const onX = resizeBox(START, handle, delta, 0, LIMITS);
        const onY = resizeBox(START, handle, 0, delta, LIMITS);
        const tag = handle + "@" + delta;
        if (has(handle, "e")) {
          if (onX.w < east) note(bad, "拉东时宽回落 " + tag);
          east = onX.w;
        }
        if (has(handle, "w")) {
          if (onX.w > west) note(bad, "拉西时宽回升 " + tag);
          west = onX.w;
        }
        if (has(handle, "s")) {
          if (onY.h < south) note(bad, "拉南时高回落 " + tag);
          south = onY.h;
        }
        if (has(handle, "n")) {
          if (onY.h > north) note(bad, "拉北时高回升 " + tag);
          north = onY.h;
        }
      }
    }
    expect(bad).toEqual([]);
  });

  it("拖到夹紧边界后重复同一操作幂等（不会继续漂移）", () => {
    const bad: string[] = [];
    for (const handle of HANDLES) {
      for (const dx of [-9999, 0, 9999]) {
        for (const dy of [-9999, 0, 9999]) {
          const once = resizeBox(START, handle, dx, dy, LIMITS);
          const twice = resizeBox(once, handle, dx, dy, LIMITS);
          if (!boxEquals(once, twice, 0)) {
            note(bad, handle + "(" + dx + "," + dy + "): " + show(once) + " -> " + show(twice));
          }
        }
      }
    }
    expect(bad).toEqual([]);
  });
});

describe("夹紧边界：精确值", () => {
  it("恰好拉到最小宽度：宽 = minW，右边界分毫不动", () => {
    const next = resizeBox(START, "w", START.w - LIMITS.minW, 0, LIMITS);
    expect(next.w).toBe(LIMITS.minW);
    expect(next.x).toBe(START.x + START.w - LIMITS.minW);
  });

  it("比最小值再多拉 1：宽仍是 minW，位置不再漂移", () => {
    const once = resizeBox(START, "w", START.w - LIMITS.minW, 0, LIMITS);
    const more = resizeBox(START, "w", START.w - LIMITS.minW + 1, 0, LIMITS);
    expect(more).toEqual(once);
  });

  it("恰好拉到最大宽度：宽 = maxW，左边不动", () => {
    const next = resizeBox(START, "e", LIMITS.maxW - START.w, 0, LIMITS);
    expect(next.w).toBe(LIMITS.maxW);
    expect(next.x).toBe(START.x);
  });

  it("东南角同时到上限：宽高只停在上限，左上角不动", () => {
    const next = resizeBox(START, "se", LIMITS.maxW - START.w, LIMITS.maxH - START.h, LIMITS);
    expect(next).toEqual({ x: START.x, y: START.y, w: LIMITS.maxW, h: LIMITS.maxH });
  });

  it("西北角同时到下限：右下角不动", () => {
    const next = resizeBox(START, "nw", START.w - LIMITS.minW, START.h - LIMITS.minH, LIMITS);
    expect(next.w).toBe(LIMITS.minW);
    expect(next.h).toBe(LIMITS.minH);
    expect(next.x + next.w).toBe(START.x + START.w);
    expect(next.y + next.h).toBe(START.y + START.h);
  });
});

describe("键盘步进", () => {
  it("只动一条轴：另一轴的 x / y / w / h 全部保持", () => {
    expect(stepBox(START, "w", 1, LIMITS)).toEqual({ x: START.x, y: START.y, w: START.w + 24, h: START.h });
    expect(stepBox(START, "h", -1, LIMITS)).toEqual({ x: START.x, y: START.y, w: START.w, h: START.h - 24 });
  });

  it("步长取绝对值：负步长不会把 ＋ 变成变小", () => {
    expect(stepBox(START, "w", 1, LIMITS, -24).w).toBe(START.w + 24);
    expect(stepBox(START, "w", -1, LIMITS, -24).w).toBe(START.w - 24);
  });

  it("步长 0 不改变任何字段（仍会照常回调，调用方要自己防重复提交）", () => {
    expect(stepBox(START, "h", 1, LIMITS, 0)).toEqual(START);
  });

  it("非整数步长按原样累加，不产生 NaN", () => {
    const next = stepBox(START, "w", 1, LIMITS, 12.5);
    expect(next.w).toBe(START.w + 12.5);
    expect(Number.isFinite(next.w)).toBe(true);
  });
});

describe("限制生成：可用区域不足时向内收敛", () => {
  it("可用区域不小于最小值：下限就是最小值，上限就是可用区域", () => {
    expect(limitsForArea({ w: 1000, h: 600 }, { w: 300, h: 220 })).toEqual({
      minW: 300,
      minH: 220,
      maxW: 1000,
      maxH: 600,
    });
  });

  it("内边距只从总量里扣一次（不是左右各扣一次），调用方自己乘 2", () => {
    const limits = limitsForArea({ w: 1000, h: 600 }, { w: 300, h: 220 }, 40);
    expect(limits.maxW).toBe(960);
    expect(limits.maxH).toBe(560);
  });

  it("内边距大于可用区域：收敛到 1，而不是负数", () => {
    const limits = limitsForArea({ w: 1000, h: 600 }, { w: 300, h: 220 }, 5000);
    expect(limits).toEqual({ minW: 1, minH: 1, maxW: 1, maxH: 1 });
  });

  it("负内边距视作 0", () => {
    expect(limitsForArea({ w: 1000, h: 600 }, { w: 300, h: 220 }, -50).maxW).toBe(1000);
  });

  it("修复后（W4）：零面积算「还没量到」，限制是「无上界」而不是 1", () => {
    expect(limitsForArea({ w: 0, h: 0 }, { w: 300, h: 220 })).toEqual({
      minW: 300,
      minH: 220,
      maxW: Number.POSITIVE_INFINITY,
      maxH: Number.POSITIVE_INFINITY,
    });
  });

  it("修复后（W4）：0 / 负数 / NaN / Infinity 面积都走同一条「无上界」路径", () => {
    const unusable: { w: number; h: number }[] = [
      { w: 0, h: 800 },
      { w: 800, h: 0 },
      { w: Number.NaN, h: 600 },
      { w: -10, h: -10 },
      { w: Number.POSITIVE_INFINITY, h: 600 },
    ];
    for (const area of unusable) {
      const limits = limitsForArea(area, { w: 300, h: 220 });
      expect(limits.minW).toBe(300);
      expect(limits.minH).toBe(220);
      expect(limits.maxW).toBe(Number.POSITIVE_INFINITY);
      expect(limits.maxH).toBe(Number.POSITIVE_INFINITY);
      expect(usableArea(area)).toBe(false);
    }
  });
});

describe("坏数据", () => {
  it("修复后：NaN 位移按「没动」处理，八个方向都保持原框", () => {
    const bad: string[] = [];
    for (const handle of HANDLES) {
      const next = resizeBox(START, handle, Number.NaN, Number.NaN, LIMITS);
      if (!boxEquals(next, START, 0)) note(bad, handle + " -> " + show(next));
    }
    expect(bad).toEqual([]);
  });

  it("修复后：±Infinity 位移也按「没动」处理（不再塌到最小值，也不超出上下界）", () => {
    expect(resizeBox(START, "se", Number.POSITIVE_INFINITY, Number.NEGATIVE_INFINITY, LIMITS)).toEqual(START);
    expect(resizeBox(START, "nw", Number.NEGATIVE_INFINITY, Number.POSITIVE_INFINITY, LIMITS)).toEqual(START);
  });

  it.fails("修复说明声称「±Infinity 仍按上下界饱和」，实测是按「没动」处理（代码与说明要二选一）", () => {
    const grown = resizeBox(START, "se", Number.POSITIVE_INFINITY, 0, LIMITS);
    expect(grown.w).toBe(LIMITS.maxW);
  });

  it("clampNumber 自身仍是「非有限值取低界」（+Infinity 也不例外）；resizeBox 已不再把 ±Infinity 交给它", () => {
    expect(clampNumber(Number.NaN, 10, 20)).toBe(10);
    expect(clampNumber(Number.POSITIVE_INFINITY, 10, 20)).toBe(10);
    expect(clampNumber(Number.NEGATIVE_INFINITY, 10, 20)).toBe(10);
    expect(clampNumber(15, 10, 20)).toBe(15);
  });

  it("stepBox 的步长非有限时退回默认 24（新增的防御）", () => {
    expect(stepBox(START, "w", 1, LIMITS, Number.NaN).w).toBe(START.w + 24);
    expect(stepBox(START, "w", 1, LIMITS, Number.POSITIVE_INFINITY).w).toBe(START.w + 24);
  });

  it("convergeBox 收到 NaN 位置时按 origin 落位，不产生 NaN", () => {
    const next = convergeBox({ x: Number.NaN, y: Number.NaN, w: 300, h: 200 }, { w: 800, h: 600 }, {
      x: 0,
      y: 44,
    });
    expect(next.x).toBe(0);
    expect(next.y).toBe(44);
    expect(Number.isFinite(next.x + next.y + next.w + next.h)).toBe(true);
  });

  it("clampBox 只负责宽高：位置原样带出（NaN 位置不会被它修好）", () => {
    const next = clampBox({ x: Number.NaN, y: 5, w: 10, h: 10 }, LIMITS);
    expect(Number.isNaN(next.x)).toBe(true);
    expect(next.y).toBe(5);
    expect(next.w).toBe(LIMITS.minW);
    expect(next.h).toBe(LIMITS.minH);
  });
});

describe("convergeBox：可见性与幂等（200 组随机）", () => {
  it("结果落在 [origin, origin + area] 内、宽度不超过 area，且二次收敛不变", () => {
    const rand = makeRandom(20261013);
    const bad: string[] = [];
    for (let i = 0; i < 200; i += 1) {
      const origin = { x: Math.round((rand() - 0.5) * 400), y: Math.round((rand() - 0.5) * 200) };
      const area = { w: 1 + Math.round(rand() * 1600), h: 1 + Math.round(rand() * 900) };
      const box: ResizeBox = {
        x: Math.round((rand() - 0.5) * 4000),
        y: Math.round((rand() - 0.5) * 4000),
        w: Math.round(rand() * 3000),
        h: Math.round(rand() * 3000),
      };
      const next = convergeBox(box, area, origin);
      const tag = show(box) + " in " + area.w + "x" + area.h + " @" + origin.x + "," + origin.y + " -> " + show(next);
      if (next.x < origin.x || next.y < origin.y) note(bad, "位置跑到 origin 之外 " + tag);
      if (next.x + next.w > origin.x + area.w) note(bad, "右边超出可见区 " + tag);
      if (next.y + next.h > origin.y + area.h) note(bad, "下边超出可见区 " + tag);
      if (!Number.isFinite(next.x + next.y + next.w + next.h)) note(bad, "出现非有限数 " + tag);
      if (!boxEquals(next, convergeBox(next, area, origin), 0)) note(bad, "二次收敛还在动 " + tag);
    }
    expect(bad).toEqual([]);
  });
});

describe("convergeBox：面积未知（W4 修复后）", () => {
  it("不再把用户的框压成 1×1：原尺寸保留，位置只保证不小于 origin", () => {
    expect(convergeBox({ x: 5000, y: 5000, w: 400, h: 500 }, { w: 0, h: 0 }, { x: 0, y: 44 })).toEqual({
      x: 5000,
      y: 5000,
      w: 400,
      h: 500,
    });
  });

  it("坏尺寸（0 / NaN）仍然被修成有效值 1，坏位置落到 origin", () => {
    expect(convergeBox({ x: Number.NaN, y: Number.NaN, w: 0, h: Number.NaN }, { w: 0, h: 0 })).toEqual({
      x: 0,
      y: 0,
      w: 1,
      h: 1,
    });
  });

  it("负位置被抬到 origin 之上，NaN 面积也走同一条路", () => {
    const next = convergeBox({ x: -50, y: -50, w: 400, h: 500 }, { w: Number.NaN, h: Number.NaN }, {
      x: 0,
      y: 44,
    });
    expect(next.x).toBe(0);
    expect(next.y).toBe(44);
    expect(next.w).toBe(400);
    expect(next.h).toBe(500);
  });
});

describe("findFreeSlot：可见性不变量（200 组随机）", () => {
  it("落点始终在传入的可见范围内，尺寸不超过可见范围，且全是有限数", () => {
    const rand = makeRandom(5171013);
    const bad: string[] = [];
    for (let i = 0; i < 200; i += 1) {
      const area: ResizeBox = {
        x: Math.round((rand() - 0.5) * 3000),
        y: Math.round((rand() - 0.5) * 2000),
        w: 200 + Math.round(rand() * 1600),
        h: 160 + Math.round(rand() * 1000),
      };
      const size = { w: 40 + Math.round(rand() * 400), h: 32 + Math.round(rand() * 300) };
      const obstacles: ResizeBox[] = [];
      for (let j = 0; j < 4; j += 1) {
        obstacles.push({
          x: area.x + Math.round((rand() - 0.3) * area.w),
          y: area.y + Math.round((rand() - 0.3) * area.h),
          w: 40 + Math.round(rand() * 300),
          h: 40 + Math.round(rand() * 200),
        });
      }
      const slot = findFreeSlot({ size, area, obstacles });
      const tag = show(slot) + " in " + show(area);
      if (!Number.isFinite(slot.x + slot.y + slot.w + slot.h)) note(bad, "非有限数 " + tag);
      if (slot.x < area.x || slot.y < area.y) note(bad, "落点跑到可见起点之前 " + tag);
      if (slot.x + slot.w > area.x + area.w) note(bad, "右边超出可见范围 " + tag);
      if (slot.y + slot.h > area.y + area.h) note(bad, "下边超出可见范围 " + tag);
    }
    expect(bad).toEqual([]);
  });
});

describe("findFreeSlot：顺序落点与窄缝", () => {
  it("连落 10 张 200×140：两两不重叠，且没有两张落点相同", () => {
    const area: ResizeBox = { x: 0, y: 0, w: 1600, h: 1000 };
    const placed: ResizeBox[] = [];
    for (let i = 0; i < 10; i += 1) {
      const slot = findFreeSlot({ size: { w: 200, h: 140 }, area, obstacles: placed });
      for (const other of placed) expect(boxesOverlap(slot, other)).toBe(false);
      expect(placed.some((item) => item.x === slot.x && item.y === slot.y)).toBe(false);
      placed.push(slot);
    }
    expect(placed).toHaveLength(10);
  });

  it("确定性：同一输入两次调用给出同一落点", () => {
    const area: ResizeBox = { x: 320, y: 180, w: 1200, h: 800 };
    const obstacles: ResizeBox[] = [{ x: 320, y: 180, w: 400, h: 300 }];
    const first = findFreeSlot({ size: { w: 240, h: 160 }, area, obstacles });
    const second = findFreeSlot({ size: { w: 240, h: 160 }, area, obstacles });
    expect(first).toEqual(second);
  });

  it("障碍在可见范围之外时不把落点推出可见范围", () => {
    const area: ResizeBox = { x: 3200, y: 1800, w: 1200, h: 800 };
    const far: ResizeBox[] = [{ x: -9000, y: -9000, w: 400, h: 300 }];
    const slot = findFreeSlot({ size: { w: 240, h: 160 }, area, obstacles: far });
    expect(slot).toEqual({ x: 3200, y: 1800, w: 240, h: 160 });
  });

  it("修复后（W9）：三种宽度的窄空档都能命中，不再回落到重叠位置", () => {
    const area: ResizeBox = { x: 0, y: 0, w: 1000, h: 400 };
    // 三种空档宽度（100 / 60 / 140）：都窄于或错开默认网格步长（= max(24, 卡宽/2)）。
    const cases: { left: number; gapWidth: number; card: number }[] = [
      { left: 405, gapWidth: 100, card: 80 },
      { left: 407, gapWidth: 60, card: 56 },
      { left: 333, gapWidth: 140, card: 132 },
    ];
    for (const item of cases) {
      const obstacles: ResizeBox[] = [
        { x: 0, y: 0, w: item.left, h: 400 },
        { x: item.left + item.gapWidth, y: 0, w: 1000 - item.left - item.gapWidth, h: 400 },
      ];
      const slot = findFreeSlot({ size: { w: item.card, h: 200 }, area, obstacles, gap: 0 });
      expect(overlapArea(slot, obstacles[0]) + overlapArea(slot, obstacles[1])).toBe(0);
    }
  });

  it("仍然：实在放不下时退化为重叠最小，且落点仍在可见范围内", () => {
    const area: ResizeBox = { x: 0, y: 0, w: 300, h: 200 };
    const obstacles: ResizeBox[] = [{ x: 0, y: 0, w: 300, h: 200 }];
    const slot = findFreeSlot({ size: { w: 120, h: 90 }, area, obstacles, gap: 0 });
    expect(slot.x).toBeGreaterThanOrEqual(0);
    expect(slot.y).toBeGreaterThanOrEqual(0);
    expect(slot.x + slot.w).toBeLessThanOrEqual(300);
    expect(slot.y + slot.h).toBeLessThanOrEqual(200);
    expect(overlapArea(slot, obstacles[0])).toBeGreaterThan(0);
  });

  it("性质（W9）：只要范围内存在零重叠落点，findFreeSlot 必须返回零重叠（40 组随机几何，2px 暴力扫描对照）", () => {
    const rand = makeRandom(880011);
    const bad: string[] = [];
    for (let i = 0; i < 40; i += 1) {
      const area: ResizeBox = {
        x: 0,
        y: 0,
        w: 240 + Math.round(rand() * 160),
        h: 180 + Math.round(rand() * 120),
      };
      const size = { w: 40 + Math.round(rand() * 80), h: 32 + Math.round(rand() * 60) };
      const gap = 8;
      const obstacles: ResizeBox[] = [];
      const count = 1 + Math.round(rand() * 3);
      for (let j = 0; j < count; j += 1) {
        obstacles.push({
          x: Math.round(rand() * (area.w - 40)),
          y: Math.round(rand() * (area.h - 40)),
          w: 20 + Math.round(rand() * 70),
          h: 20 + Math.round(rand() * 60),
        });
      }
      const maxX = area.x + Math.max(0, area.w - size.w);
      const maxY = area.y + Math.max(0, area.h - size.h);
      let freeExists = false;
      for (let y = area.y; y <= maxY && !freeExists; y += 2) {
        for (let x = area.x; x <= maxX; x += 2) {
          const probe: ResizeBox = { x: x - gap, y: y - gap, w: size.w + gap * 2, h: size.h + gap * 2 };
          let overlap = 0;
          for (const obstacle of obstacles) overlap += overlapArea(probe, obstacle);
          if (overlap === 0) {
            freeExists = true;
            break;
          }
        }
      }
      const slot = findFreeSlot({ size, area, obstacles, gap });
      const probe: ResizeBox = { x: slot.x - gap, y: slot.y - gap, w: slot.w + gap * 2, h: slot.h + gap * 2 };
      let overlap = 0;
      for (const obstacle of obstacles) overlap += overlapArea(probe, obstacle);
      if (freeExists && overlap !== 0) note(bad, "有空位却没找到 " + show(slot) + " in " + show(area));
    }
    expect(bad).toEqual([]);
  });

  it("提醒（W4 未覆盖的同类路径）：面积是 0 时 findFreeSlot 会把尺寸夹成 1×1，C 必须先量到面积", () => {
    const slot = findFreeSlot({ size: { w: 240, h: 160 }, area: { x: 0, y: 0, w: 0, h: 0 }, obstacles: [] });
    expect(slot).toEqual({ x: 0, y: 0, w: 1, h: 1 });
  });

  it("可见范围比卡片还小：落在范围左上角，尺寸被夹进范围", () => {
    const area: ResizeBox = { x: 40, y: 60, w: 100, h: 80 };
    const slot = findFreeSlot({ size: { w: 400, h: 300 }, area, obstacles: [] });
    expect(slot).toEqual({ x: 40, y: 60, w: 100, h: 80 });
  });
});
