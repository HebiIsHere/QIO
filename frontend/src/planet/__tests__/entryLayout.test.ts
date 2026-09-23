import { describe, expect, it } from "vitest";
import { BALL_SIZE_FALLBACK, ballLayoutReady } from "../entryLayout";

describe("球态入口对齐的布局判据", () => {
  it("画布已经落到球尺寸：允许对齐", () => {
    expect(ballLayoutReady(108)).toBe(true);
    expect(ballLayoutReady(108, 108)).toBe(true);
  });

  it("画布还是整窗尺寸：绝不能用它算入口尺度", () => {
    // 实测：在 280px 的过渡画布上算出来的 k≈0.34，正常球态是 0.88
    expect(ballLayoutReady(280)).toBe(false);
    expect(ballLayoutReady(1513)).toBe(false);
  });

  it("量不到尺寸时不阻塞（降级：先按现状对齐）", () => {
    expect(ballLayoutReady(0)).toBe(true);
    expect(ballLayoutReady(Number.NaN)).toBe(true);
  });

  it("默认球尺寸与 CSS 令牌一致（108px）", () => {
    expect(BALL_SIZE_FALLBACK).toBe(108);
  });
});
