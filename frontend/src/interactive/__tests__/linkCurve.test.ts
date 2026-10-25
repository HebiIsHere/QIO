import { describe, expect, it } from "vitest";
import {
  CURVE_OFFSET_LIMIT,
  DRAG_THRESHOLD_PX,
  clampCurveOffset,
  curveFromPointerDrag,
  curveGeometry,
  curvePath,
  normalizeCurveScale,
} from "../linkCurve";

describe("关系曲线几何", () => {
  it("默认按主方向出线，曲线不改变端点", () => {
    const geometry = curveGeometry({ x1: 0, y1: 0, x2: 400, y2: 0 });
    expect(geometry.c1x).toBeGreaterThan(0);
    expect(geometry.c2x).toBeLessThan(400);
    expect(geometry.labelX).toBeGreaterThan(100);
    expect(geometry.labelX).toBeLessThan(300);
    expect(curvePath({ x1: 0, y1: 0, x2: 400, y2: 0 })).toMatch(/^M 0 0 C /);
  });

  it("用户拖出的偏移作用在两个控制点上并改变标签位置", () => {
    const straight = curveGeometry({ x1: 0, y1: 0, x2: 400, y2: 0 });
    const bent = curveGeometry({ x1: 0, y1: 0, x2: 400, y2: 0, curve: { x: 20, y: -30 } });
    expect(bent.c1x).toBe(straight.c1x + 20);
    expect(bent.c1y).toBe(straight.c1y - 30);
    expect(bent.c2x).toBe(straight.c2x + 20);
    expect(bent.c2y).toBe(straight.c2y - 30);
    expect(bent.labelY).not.toBe(straight.labelY);
  });

  it("竖向端点按竖向出线", () => {
    const geometry = curveGeometry({ x1: 0, y1: 0, x2: 0, y2: 400 });
    expect(geometry.c1y).toBeGreaterThan(0);
    expect(geometry.c2y).toBeLessThan(400);
  });
});
describe("拖动曲线的缩放换算（板面位移 = 屏幕位移 / scale）", () => {
  it.each([
    { scale: 0.5, expected: { x: 200, y: -100 } },
    { scale: 1, expected: { x: 100, y: -50 } },
    { scale: 2, expected: { x: 50, y: -25 } },
  ])("scale=$scale：相同屏幕位移 (100,-50) → 板面 $expected", ({ scale, expected }) => {
    const result = curveFromPointerDrag({
      origin: { x: 0, y: 0 },
      startClientX: 300,
      startClientY: 200,
      clientX: 400,
      clientY: 150,
      scale,
    });
    expect(result.moved).toBe(true);
    expect(result.curve).toEqual(expected);
    expect(result.screenDx).toBe(100);
    expect(result.screenDy).toBe(-50);
  });

  it("缩放非正 / 非有限时按 1 换算，不产生 0 / NaN / 反向位移", () => {
    for (const scale of [0, -3, Number.NaN, Number.POSITIVE_INFINITY, undefined]) {
      const result = curveFromPointerDrag({
        origin: { x: 5, y: 5 },
        startClientX: 0,
        startClientY: 0,
        clientX: 100,
        clientY: -50,
        scale,
      });
      expect(result.curve).toEqual({ x: 105, y: -45 });
    }
  });

  it("在起始偏移上累加，并截断到 ±240 板面像素（按缩放折算后再截断）", () => {
    const result = curveFromPointerDrag({
      origin: { x: 230, y: -230 },
      startClientX: 0,
      startClientY: 0,
      clientX: 300,
      clientY: -300,
      scale: 0.5,
    });
    // 300 / 0.5 = 600 板面像素：230 + 600 → 超出上限被截断
    expect(result.curve).toEqual({ x: CURVE_OFFSET_LIMIT, y: -CURVE_OFFSET_LIMIT });
  });

  it("阈值按屏幕像素判定：低于阈值不算拖动，达到阈值才算", () => {
    const still = curveFromPointerDrag({
      origin: { x: 5, y: 6 },
      startClientX: 0,
      startClientY: 0,
      clientX: 1,
      clientY: 1,
      scale: 1,
    });
    expect(still.moved).toBe(false);
    expect(still.curve).toEqual({ x: 6, y: 7 });

    const moved = curveFromPointerDrag({
      origin: { x: 5, y: 6 },
      startClientX: 0,
      startClientY: 0,
      clientX: DRAG_THRESHOLD_PX,
      clientY: 0,
      scale: 1,
    });
    expect(moved.moved).toBe(true);
    expect(moved.curve).toEqual({ x: 5 + DRAG_THRESHOLD_PX, y: 6 });
  });

  it("非法坐标 / 缺失起始偏移都有有限值兜底，且不改输入对象", () => {
    const origin = { x: Number.NaN, y: undefined as unknown as number };
    const result = curveFromPointerDrag({
      origin,
      startClientX: Number.NaN,
      startClientY: Number.POSITIVE_INFINITY,
      clientX: 50,
      clientY: 80,
      scale: 1,
    });
    expect(Number.isFinite(result.curve.x)).toBe(true);
    expect(Number.isFinite(result.curve.y)).toBe(true);
    expect(origin.x).toBeNaN(); // 不原地修改调用方数据
  });
});

describe("缩放与偏移的安全值", () => {
  it("normalizeCurveScale：正常值原样返回，非正 / 非有限按 1", () => {
    expect(normalizeCurveScale(1)).toBe(1);
    expect(normalizeCurveScale(0.5)).toBe(0.5);
    expect(normalizeCurveScale(2)).toBe(2);
    for (const invalid of [0, -1, Number.NaN, Number.POSITIVE_INFINITY, Number.NEGATIVE_INFINITY, undefined, null, "abc"]) {
      expect(normalizeCurveScale(invalid)).toBe(1);
    }
  });

  it("clampCurveOffset：默认 ±240，支持自定义上限，非法输入按 0", () => {
    expect(clampCurveOffset(100)).toBe(100);
    expect(clampCurveOffset(1000)).toBe(CURVE_OFFSET_LIMIT);
    expect(clampCurveOffset(-1000)).toBe(-CURVE_OFFSET_LIMIT);
    expect(clampCurveOffset(50, 40)).toBe(40);
    expect(clampCurveOffset("60")).toBe(60);
    expect(clampCurveOffset(Number.NaN)).toBe(0);
  });
});
