/**
 * 「回到最新消息」按钮的锚定计算。
 *
 * 需求（修复提示词 §6）：按钮下缘距输入框上缘 8–12px，位置由真实元素边界决定，
 * 不用固定的大 bottom 数值补偿；有候选卡时抬到卡片上方（不能被卡片盖住）。
 */
import { describe, expect, it } from "vitest";
import {
  LATEST_BUTTON_GAP_PX,
  latestButtonBottom,
  latestButtonLeftFromBase,
  latestButtonOffsetFromBase,
  latestButtonTargetBottom,
} from "../latestButton";

describe("latestButtonBottom", () => {
  it("空底部块：锚在输入框上缘往上 10px", () => {
    // 视口 900、输入框上缘 800 → 按钮下缘应在 790
    expect(latestButtonBottom({ composerTop: 800, clusterTop: null, viewportHeight: 900 })).toBe(110);
  });

  it("间距落在需求区间 8–12px", () => {
    expect(LATEST_BUTTON_GAP_PX).toBeGreaterThanOrEqual(8);
    expect(LATEST_BUTTON_GAP_PX).toBeLessThanOrEqual(12);
  });

  it("底部块有内容时：抬到卡片上缘上方（不会被卡片盖住）", () => {
    // 输入框上缘 800、卡片上缘 640 → 锚线取 640
    expect(latestButtonBottom({ composerTop: 800, clusterTop: 640, viewportHeight: 900 })).toBe(270);
  });

  it("底部块上缘比输入框低（异常情况）时不把按钮压到输入框里", () => {
    expect(latestButtonBottom({ composerTop: 700, clusterTop: 820, viewportHeight: 900 })).toBe(210);
  });

  it("两者都拿不到时返回 null（界面用 CSS 兜底，而不是算出一个假位置）", () => {
    expect(latestButtonBottom({ composerTop: null, clusterTop: null, viewportHeight: 900 })).toBeNull();
  });

  it("视口高度异常小也不会算出负值", () => {
    const value = latestButtonBottom({ composerTop: 1800, clusterTop: null, viewportHeight: 900 });
    expect(value).not.toBeNull();
    expect(value as number).toBeGreaterThanOrEqual(48);
  });
});

describe("按实测原点换算（不假设包含块 = 视口）", () => {
  it("目标下缘 = 锚线 - 间隙", () => {
    expect(latestButtonTargetBottom(761)).toBe(751);
    expect(latestButtonTargetBottom(761, 12)).toBe(749);
  });

  it("包含块底边比视口低 15px 时（CSS zoom 实测场景）也能算出正确偏移", () => {
    // 包含块底边 885、目标下缘 701.6、缩放 1.25 → 需要 146.72 局部像素
    const value = latestButtonOffsetFromBase({ baseBottom: 885, targetBottom: 701.625, scale: 1.25 });
    expect(value).toBeCloseTo(146.7, 1);
    // 校验：885 - 146.7 * 1.25 ≈ 701.6（按钮下缘正好落在目标上）
    expect(885 - value * 1.25).toBeCloseTo(701.625, 1);
  });

  it("没被缩放时就是两点之差", () => {
    expect(latestButtonOffsetFromBase({ baseBottom: 900, targetBottom: 750, scale: 1 })).toBe(150);
  });

  it("比例异常时按 1 处理，不产生 NaN", () => {
    expect(latestButtonOffsetFromBase({ baseBottom: 900, targetBottom: 750, scale: 0 })).toBe(150);
  });

  it("水平方向：把锚点中心对到内容列中心（补回半个宽度）", () => {
    const value = latestButtonLeftFromBase({
      baseLeft: 0,
      targetCenterX: 720,
      anchorWidth: 116,
      scale: 1,
    });
    expect(value).toBe(662);
  });
});
