/**
 * 板面 chrome 不许启动浏览器原生文字选择（2026-10-14 第一个问题的修复，方案 A）。
 *
 * 现象：Ctrl+拖拽时画布向下滚。根因不是 Ctrl，而是**原生选择拖拽**：
 * 在非画布表面的板面 chrome（提交区 / 徽章 / 工具栏文字）上按下并拖动会启动浏览器选择，
 * Chromium 顺带**自动滚动最近的滚动容器** —— 实测从提交区拖到窗口底，
 * .board-viewport 的 scrollTop 从 0 跳到 811。
 *
 * 修法：互动舞台 main.im-stage 默认 user-select: none；需要选文字的地方显式开回来
 * （卡片正文 / 对话面板 / 输入框 / 可编辑区）。这条用例守住这个契约。
 */
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

// jsdom 环境下 import.meta.url 不是 file: 协议，按 vitest 的工作目录（frontend/）取文件
const CSS = readFileSync(resolve(process.cwd(), "src/styles/interactive-shell.css"), "utf8");

/** 取选择器对应的声明块（找不到返回空串）。 */
function block(selector: string): string {
  const start = CSS.indexOf(selector);
  if (start < 0) return "";
  const open = CSS.indexOf("{", start);
  const close = CSS.indexOf("}", open);
  return open < 0 || close < 0 ? "" : CSS.slice(open + 1, close);
}

describe("板面 chrome 不启动原生选择", () => {
  it("舞台默认不可选中", () => {
    const stage = block("main.im-stage");
    expect(stage, "缺少 main.im-stage 规则").not.toBe("");
    expect(stage).toContain("user-select: none");
  });

  it("卡片正文与对话面板里的文字仍然可以选中", () => {
    for (const sel of ['main.im-stage [data-im="card-body"]', 'main.im-stage [data-im="chat-panel"]']) {
      const css = block(sel);
      expect(css, "缺少规则：" + sel).not.toBe("");
      expect(css, sel + " 必须把选择开回来").toContain("user-select: text");
    }
  });

  it("输入框与可编辑区不受影响", () => {
    const css = block("main.im-stage textarea");
    expect(css, "缺少 textarea 规则").not.toBe("");
    expect(css).toContain("user-select: text");
  });
});
