/**
 * 板面 chrome 不许启动浏览器原生文字选择（2026-10-14 第一个问题的修复，方案 A）。
 *
 * 现象：Ctrl+拖拽时画布向下滚。根因不是 Ctrl，而是**原生选择拖拽**：
 * 在非画布表面的板面 chrome（提交区 / 徽章 / 工具栏文字）上按下并拖动会启动浏览器选择，
 * Chromium 顺带**自动滚动最近的滚动容器** —— 实测从提交区拖到窗口底，
 * .board-viewport 的 scrollTop 从 0 跳到 811。
 *
 * 修法：互动舞台默认 user-select: none；需要选文字的地方显式开回来（卡片正文 / 输入框 / 可编辑区）。
 *
 * 2026-10-26（合并后清理 · C03）：规则从 styles/interactive-shell.css 迁到唯一公共来源
 * styles/layers.css，并且舞台钩子从旧布局的 main.im-stage 改成 v2 布局真实渲染的
 * [data-im="interactive-stage"]（InteractiveView.vue 的 .im-board-stage）。
 * 旧钩子在 1b03541e 上没有任何生产者 —— 这不是新功能，是把同一条约束挂回真实元素。
 */
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

// jsdom 环境下 import.meta.url 不是 file: 协议，按 vitest 的工作目录（frontend/）取文件
const RAW = readFileSync(resolve(process.cwd(), "src/styles/layers.css"), "utf8");
/** 注释里会提到旧实现与旧钩子名：断言只看真实声明。 */
const CSS = RAW.replace(/\/\*[\s\S]*?\*\//g, "");

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
    const stage = block('[data-im="interactive-stage"]');
    expect(stage, '缺少 [data-im="interactive-stage"] 规则').not.toBe("");
    expect(stage).toContain("user-select: none");
  });

  it("不再依赖已无生产者的旧舞台钩子", () => {
    expect(CSS, "旧布局的 main.im-stage 钩子已无生产者，不应再出现在样式声明里").not.toContain("main.im-stage");
  });

  it("卡片正文与输入区的文字仍然可以选中", () => {
    for (const sel of [
      '[data-im="interactive-stage"] [data-im="card-body"]',
      '[data-im="interactive-stage"] textarea',
    ]) {
      const css = block(sel);
      expect(css, "缺少规则：" + sel).not.toBe("");
      expect(css, sel + " 必须把选择开回来").toContain("user-select: text");
    }
  });
});
