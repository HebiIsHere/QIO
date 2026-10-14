import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";

// 路径放进变量：字面量会被 Vite 当成资源 URL 重写，读不到真实文件
const scrollCssPath = "./scroll.css";
const tokensCssPath = "./tokens.css";
const scrollCss = readFileSync(new URL(scrollCssPath, import.meta.url), "utf8");
const tokensCss = readFileSync(new URL(tokensCssPath, import.meta.url), "utf8");

/**
 * 统一滚动规则的**底线守卫**。
 *
 * 这些断言只保证「共享规则确实存在、没有用被禁止的捷径」；
 * 真实像素级布局由浏览器验收阶段测量（scrollWidth/clientWidth、表格边界、手势），
 * 不把这里的源码断言当成布局证据。
 */
describe("styles/scroll.css 统一滚动规则", () => {
  it("全局消除原生滚动条外观，且不设 overflow:hidden、不禁用滚轮", () => {
    expect(scrollCss).toContain("*::-webkit-scrollbar");
    expect(scrollCss).toContain("scrollbar-width: none");
    for (const part of [
      "*::-webkit-scrollbar-button",
      "*::-webkit-scrollbar-track",
      "*::-webkit-scrollbar-thumb",
      "*::-webkit-scrollbar-corner",
    ]) {
      expect(scrollCss).toContain(part);
    }
    // 禁止的整体捷径：全局隐藏溢出 / 禁用滚轮
    expect(scrollCss).not.toMatch(/html\s*,\s*body\s*\{[^}]*overflow:\s*hidden/);
    expect(scrollCss).not.toContain("overscroll-behavior: none");
    expect(scrollCss).not.toMatch(/pointer-events:\s*none;\s*\}\s*\/\*\s*global/);
  });

  it("内容宽度约束用 clip 而不是 hidden（clip 不建立滚动容器，藏不住横向滚动）", () => {
    expect(scrollCss).toMatch(/\[data-qio-fit\]\s*\{[^}]*overflow-x:\s*clip/);
    expect(scrollCss).toMatch(/\[data-qio-scope~="chat"\]/);
    expect(scrollCss).toContain("overflow-wrap: anywhere");
    // 对话作用域里不允许出现任何横向滚动容器
    expect(scrollCss).not.toMatch(/\[data-qio-scope~="chat"\][^{]*\{[^}]*overflow-x:\s*(auto|scroll)/);
  });

  it("表格两态规则齐全，且记录模式保留表头语义", () => {
    expect(scrollCss).toContain(".qio-table-wrap");
    expect(scrollCss).toContain('[data-mode="records"]');
    expect(scrollCss).toContain("attr(data-label)");
    // 记录模式：表头行只是视觉隐藏，仍在无障碍树里
    expect(scrollCss).toMatch(/\[data-mode="records"\]\s*>\s*table\s*>\s*thead\s*\{[^}]*clip-path:\s*inset\(50%\)/);
    // 表格本身不得有横向滚动
    expect(scrollCss).not.toMatch(/\.qio-table-wrap[^{]*\{[^}]*overflow-x:\s*(auto|scroll)/);
  });

  it("指示条浮在边缘、不可见时不吞点击（轨道 pointer-events:none）", () => {
    expect(scrollCss).toContain(".qio-scroll-rail");
    expect(scrollCss).toMatch(/\.qio-scroll-rail\s*\{[^}]*pointer-events:\s*none/);
    expect(scrollCss).toMatch(/\.qio-scroll-rail\.is-visible\s+\.qio-scroll-thumb\s*\{[^}]*pointer-events:\s*auto/);
    expect(scrollCss).toContain("position: absolute");
  });

  it("滚动视觉全部来自 --scroll-* 令牌（组件不得自造一套）", () => {
    for (const token of [
      "--scroll-indicator-w",
      "--scroll-indicator-hit",
      "--scroll-rail-inset",
      "--scroll-indicator-bg",
      "--scroll-indicator-bg-active",
      "--scroll-fade",
    ]) {
      expect(tokensCss).toContain(token);
      expect(scrollCss).toContain(token.replace("--", "var(--")) ;
    }
    // 视觉宽度 3–5px（规范区间）
    const width = tokensCss.match(/--scroll-indicator-w:\s*(\d+)px/);
    expect(width).not.toBeNull();
    expect(Number(width![1])).toBeGreaterThanOrEqual(3);
    expect(Number(width![1])).toBeLessThanOrEqual(5);
  });
});
