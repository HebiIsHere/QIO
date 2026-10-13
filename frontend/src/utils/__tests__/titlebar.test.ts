/**
 * 标题条高度的 JS 侧读取。
 *
 * 它存在的理由只有一个：CSS 令牌 `--titlebar-height` 必须是唯一事实来源，
 * 而浮动组件的默认位置在 JS 里算 —— 两边读同一个值，才不会出现
 * 「改高度只改一半，某个控件被标题条压住」。
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { titlebarHeight } from "../titlebar";

afterEach(() => {
  vi.restoreAllMocks();
});

function stubToken(value: string) {
  vi.spyOn(window, "getComputedStyle").mockReturnValue({
    getPropertyValue: () => value,
  } as unknown as CSSStyleDeclaration);
}

describe("titlebarHeight", () => {
  it("读得到令牌时按 px 解析", () => {
    stubToken(" 40px ");
    expect(titlebarHeight()).toBe(40);
  });

  it("令牌缺失（jsdom / 未加载样式）时用兜底值，不返回 NaN", () => {
    stubToken("");
    expect(titlebarHeight()).toBe(32);
  });

  it("令牌不是 px 时宁可兜底，也不猜一个错的像素数", () => {
    stubToken("2rem");
    expect(titlebarHeight()).toBe(32);
  });

  it("读令牌抛异常也不炸", () => {
    vi.spyOn(window, "getComputedStyle").mockImplementation(() => {
      throw new Error("no style");
    });
    expect(titlebarHeight()).toBe(32);
  });
});
