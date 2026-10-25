/**
 * 工具栏档位的宽度测量：窄窗口不许被压塌（2026-10-14：用户报「详情压住查看过程」）。
 *
 * 根因：P47 把工具栏档位上限改成「直接扣几何计划里的聊天宽度（420px）」，窄窗口下
 * 480 - 420 - 48 ≈ 12px，工具栏塌成 18px、提交区宽度变成 0，按钮互相压住。
 * 修法：档位用 --im-toolbar-chat-reserve —— 宽窗口按对话卡宽度扣，窄窗口换成固定保留宽度。
 *
 * 2026-10-26（合并后清理 · C03）：变量从 styles/interactive-shell.css 迁到 styles/layers.css。
 * 旧的绝对定位规则随 v2 布局（工具栏进悬浮对话卡）删除，但 interactive/toolbarLayout.ts
 * 与 BoardToolbar 的隐形探测器仍然读这两个变量决定宽 / 中 / 窄档，所以守卫跟着变量走。
 */
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

const RAW = readFileSync(resolve(process.cwd(), "src/styles/layers.css"), "utf8");
/** 注释里会提到旧实现（含已删除的 --im-geo-* / 旧绝对定位）：断言只看真实声明。 */
const CSS = RAW.replace(/\/\*[\s\S]*?\*\//g, "");

function block(selector: string): string {
  const start = CSS.indexOf(selector);
  if (start < 0) return "";
  const open = CSS.indexOf("{", start);
  const close = CSS.indexOf("}", open);
  return open < 0 || close < 0 ? "" : CSS.slice(open + 1, close);
}

describe("工具栏档位的宽度测量", () => {
  it("宽窗口按对话卡宽度（420px）扣，不依赖已无生产者的 --im-geo-* 变量", () => {
    const rule = block(":root");
    expect(rule, "缺少 :root 规则").not.toBe("");
    expect(rule).toContain("--im-toolbar-chat-reserve: min(420px, 40vw)");
    expect(CSS).not.toContain("--im-geo-chat-max-w");
    expect(rule).toContain("--im-toolbar-max-width: 1180px");
  });

  it("窄窗口换成固定保留宽度（1024 / 800 两档）", () => {
    const narrow = block("@media (max-width: 1024px)");
    expect(narrow).toContain("--im-toolbar-chat-reserve: 104px");
    const narrower = block("@media (max-width: 800px)");
    expect(narrower).toContain("--im-toolbar-chat-reserve: 88px");
  });

  it("悬浮对话卡里的工具栏不吃自己的一份卡片外观", () => {
    const floating = block('[data-im="board-toolbar"][data-im="board-toolbar"][data-im="board-toolbar"].is-floating');
    expect(floating, "缺少悬浮工具栏规则").not.toBe("");
    expect(floating).toContain("background: transparent");
    expect(floating).toContain("border: 0");
  });
});
