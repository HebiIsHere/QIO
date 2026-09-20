import { describe, expect, it } from "vitest";
import { CSS_SETTLE_FALLBACK, easePointsFromCssValue, evalCubicBezier } from "../easing";

/**
 * 参考值来自**独立实现**（密集采样参数式定义 + 线性插值），不是这套代码算出来的 ——
 * 否则「用同一套求解器验自己」就成了自证。
 */
describe("缓动曲线：把设计令牌里的 cubic-bezier 拿到 JS 里求值", () => {
  it("端点与线性曲线（cubic-bezier(0,0,1,1)）必须等于输入", () => {
    const linear: [number, number, number, number] = [0, 0, 1, 1];
    expect(evalCubicBezier(linear, 0)).toBeCloseTo(0, 6);
    expect(evalCubicBezier(linear, 1)).toBeCloseTo(1, 6);
    for (const x of [0.1, 0.25, 0.5, 0.75, 0.9]) {
      expect(evalCubicBezier(linear, x)).toBeCloseTo(x, 4);
    }
  });

  it("CSS 的 ease（cubic-bezier(.25,.1,.25,1)）给出公认形状", () => {
    const ease: [number, number, number, number] = [0.25, 0.1, 0.25, 1];
    expect(evalCubicBezier(ease, 0.1)).toBeCloseTo(0.0948, 3);
    expect(evalCubicBezier(ease, 0.25)).toBeCloseTo(0.4085, 3);
    expect(evalCubicBezier(ease, 0.5)).toBeCloseTo(0.8024, 3);
    expect(evalCubicBezier(ease, 0.9)).toBeCloseTo(0.9943, 3);
  });

  it("低频档的柔性收束（--ease-3-settle）前段极快、末段几乎不动", () => {
    // 这条曲线是收缩尾段「继续用同一条曲线」的依据：0.25 处已走完 78.6%，
    // 0.5 处 97.4%，之后基本停住（最大值 1.00016，几乎不过冲）。
    expect(evalCubicBezier(CSS_SETTLE_FALLBACK, 0.1)).toBeCloseTo(0.3997, 3);
    expect(evalCubicBezier(CSS_SETTLE_FALLBACK, 0.25)).toBeCloseTo(0.7858, 3);
    expect(evalCubicBezier(CSS_SETTLE_FALLBACK, 0.5)).toBeCloseTo(0.974, 3);
    expect(evalCubicBezier(CSS_SETTLE_FALLBACK, 0.9)).toBeCloseTo(1.0002, 3);
  });

  it("曲线只允许「柔性收束」那一次轻微过冲，不能有可感知回弹", () => {
    let prev = 0;
    let peak = 0;
    for (let i = 0; i <= 200; i++) {
      const y = evalCubicBezier(CSS_SETTLE_FALLBACK, i / 200);
      // 允许收尾回落（过冲后回到 1），但不能超过千分之五 —— 这就是「柔性收束」的预算
      expect(y).toBeGreaterThanOrEqual(prev - 0.005);
      peak = Math.max(peak, y);
      prev = y;
    }
    expect(peak).toBeGreaterThan(1); // 确实有一次过冲，而不是一条普通缓出
    expect(peak - 1).toBeLessThan(0.01);
    expect(evalCubicBezier(CSS_SETTLE_FALLBACK, 1)).toBe(1);
  });

  it("越界输入按端点夹住（rAF 迟到的时间戳不能让尺寸飞出画面）", () => {
    expect(evalCubicBezier(CSS_SETTLE_FALLBACK, -1)).toBe(0);
    expect(evalCubicBezier(CSS_SETTLE_FALLBACK, 2)).toBe(1);
  });

  it("能从 CSS 值里解析出控制点：容忍空格/省略的 0.、以及非曲线值退回默认", () => {
    expect(easePointsFromCssValue("cubic-bezier(.24,1.04,.32,1)", CSS_SETTLE_FALLBACK)).toEqual([
      0.24, 1.04, 0.32, 1,
    ]);
    expect(easePointsFromCssValue("cubic-bezier(0.5, 0, 0.24, 1)", CSS_SETTLE_FALLBACK)).toEqual([
      0.5, 0, 0.24, 1,
    ]);
    expect(easePointsFromCssValue("", CSS_SETTLE_FALLBACK)).toEqual(CSS_SETTLE_FALLBACK);
    expect(easePointsFromCssValue("ease-out", CSS_SETTLE_FALLBACK)).toEqual(CSS_SETTLE_FALLBACK);
  });
});
