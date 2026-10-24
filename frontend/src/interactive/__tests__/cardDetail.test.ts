import { describe, expect, it } from "vitest";
import {
  DETAIL_ENTER_HEIGHT,
  DETAIL_ENTER_WIDTH,
  DETAIL_EXIT_HEIGHT,
  DETAIL_EXIT_WIDTH,
  shouldAutoDetail,
  shouldShowImageDetailFallback,
} from "../cardDetail";

describe("卡片详细状态：普通卡片", () => {
  it("宽度和高度同时达到进入阈值时进入详细", () => {
    expect(
      shouldAutoDetail({
        width: DETAIL_ENTER_WIDTH,
        height: DETAIL_ENTER_HEIGHT,
      }),
    ).toBe(true);
  });

  it("只差 1px 就不进入详细", () => {
    expect(
      shouldAutoDetail({
        width: DETAIL_ENTER_WIDTH - 1,
        height: DETAIL_ENTER_HEIGHT,
      }),
    ).toBe(false);
    expect(
      shouldAutoDetail({
        width: DETAIL_ENTER_WIDTH,
        height: DETAIL_ENTER_HEIGHT - 1,
      }),
    ).toBe(false);
  });

  it("已详细时在缓冲区里保持详细，不来回抖动", () => {
    expect(
      shouldAutoDetail({
        width: 380,
        height: 250,
        previousDetailed: true,
      }),
    ).toBe(true);
  });

  it("已详细时低于退出阈值就回到简洁", () => {
    expect(
      shouldAutoDetail({
        width: DETAIL_EXIT_WIDTH - 1,
        height: 300,
        previousDetailed: true,
      }),
    ).toBe(false);
    expect(
      shouldAutoDetail({
        width: 380,
        height: DETAIL_EXIT_HEIGHT - 1,
        previousDetailed: true,
      }),
    ).toBe(false);
  });

  it("尺寸非法时不进入详细", () => {
    expect(shouldAutoDetail({ width: 0, height: 0 })).toBe(false);
    expect(shouldAutoDetail({ width: -1, height: 999 })).toBe(false);
  });
});

describe("卡片详细状态：图片", () => {
  it("竖图按短边和面积进入详细，不被普通双阈值卡住", () => {
    expect(
      shouldAutoDetail({ width: 300, height: 900, aspectRatio: 1 / 3 }),
    ).toBe(true);
  });

  it("横长图短边达到 200 且面积足够时进入详细", () => {
    expect(
      shouldAutoDetail({ width: 900, height: 200, aspectRatio: 4.5 }),
    ).toBe(true);
  });

  it("极端长图当前达不到自动阈值时提供兜底入口", () => {
    const input = { width: 700, height: 175, aspectRatio: 4 };
    expect(shouldAutoDetail(input)).toBe(false);
    expect(shouldShowImageDetailFallback(input)).toBe(true);
  });

  it("正常图片自动详细时不显示兜底入口", () => {
    const input = { width: 420, height: 280, aspectRatio: 1.5 };
    expect(shouldAutoDetail(input)).toBe(true);
    expect(shouldShowImageDetailFallback(input)).toBe(false);
  });

  it("普通卡片没有图片兜底入口", () => {
    expect(
      shouldShowImageDetailFallback({ width: 200, height: 200, aspectRatio: null }),
    ).toBe(false);
  });

  it("图片缩小到退出阈值以下时退出详细", () => {
    expect(
      shouldAutoDetail({
        width: 700,
        height: 175,
        aspectRatio: 4,
        previousDetailed: true,
      }),
    ).toBe(false);
  });
});
