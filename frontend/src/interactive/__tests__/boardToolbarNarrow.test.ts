/**
 * 工具栏在窄窗口不许被压塌（2026-10-14：用户报「详情压住查看过程」）。
 *
 * 根因：P47 把工具栏上限改成「扣掉几何计划里的聊天宽度（420px）」，窄窗口下 480 - 420 - 48 ≈ 12px，
 * 工具栏塌成 18px、提交区宽度变成 0，按钮互相压住。
 * 修法：上限用 --im-toolbar-chat-reserve —— 宽窗口按聊天宽度扣，窄窗口（≤1024）换成 --im-chat-reserve。
 */
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

const CSS = readFileSync(resolve(process.cwd(), "src/styles/interactive-shell.css"), "utf8");

function block(selector: string): string {
  const start = CSS.indexOf(selector);
  if (start < 0) return "";
  const open = CSS.indexOf("{", start);
  const close = CSS.indexOf("}", open);
  return open < 0 || close < 0 ? "" : CSS.slice(open + 1, close);
}

describe("工具栏宽度上限", () => {
  it("用 --im-toolbar-chat-reserve，不再直接扣聊天宽度", () => {
    const rule = block('[data-im="board-toolbar"][data-im="board-toolbar"][data-im="board-toolbar"]');
    expect(rule, "缺少工具栏规则").not.toBe("");
    expect(rule).toContain("--im-toolbar-chat-reserve");
    expect(rule, "不许直接扣 --im-geo-chat-max-w：窄窗口会塌成 0").not.toContain("--im-geo-chat-max-w");
  });

  it("窄窗口换成 chat-reserve", () => {
    expect(CSS).toContain("--im-toolbar-chat-reserve: var(--im-chat-reserve)");
  });
});
