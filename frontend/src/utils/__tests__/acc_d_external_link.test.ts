/**
 * F03 反例（成员 D · 共用外链入口）：openExternal 不能用 noopener 下 window.open 的返回值判失败。
 *
 * 基线行为（红）：`window.open(target, "_blank", "noopener,noreferrer")` 在现代浏览器里
 *   成功打开**也返回 null** → openExternal 返回 false → MarkdownContent 给链接打上
 *   `link-failed` 并提示「打开失败」，用户看到错误提示，但外链其实已经打开。
 * 期望（绿）：返回值只表示「是否成功发起打开尝试」；null 不算失败，
 *   只有可验证的失败（没有 window.open / 调用抛错）才返回 false。
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { openExternal } from "../externalLink";

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("F03 外链入口：noopener 返回值不作成败判据", () => {
  it("返回 null（现代浏览器 noopener 的正常返回）仍视为发起成功", async () => {
    const open = vi.fn(() => null);
    vi.stubGlobal("open", open);

    await expect(openExternal("https://example.com/a")).resolves.toBe(true);
    expect(open).toHaveBeenCalledWith("https://example.com/a", "_blank", "noopener,noreferrer");
  });

  it("可验证的失败：window.open 抛错 → false", async () => {
    vi.stubGlobal(
      "open",
      vi.fn(() => {
        throw new Error("not allowed");
      }),
    );
    await expect(openExternal("https://example.com")).resolves.toBe(false);
  });

  it("可验证的失败：环境没有 window.open → false", async () => {
    vi.stubGlobal("open", undefined);
    await expect(openExternal("https://example.com")).resolves.toBe(false);
  });

  it("危险 scheme 依旧不开（安全边界不因本次修复放宽）", async () => {
    const open = vi.fn(() => null);
    vi.stubGlobal("open", open);
    await expect(openExternal("javascript:alert(1)")).resolves.toBe(false);
    expect(open).not.toHaveBeenCalled();
  });
});
