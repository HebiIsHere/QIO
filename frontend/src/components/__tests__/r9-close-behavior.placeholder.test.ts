/**
 * 关闭行为占位测试（R9 契约 §11；行为实现归 W5）。
 *
 * 为什么是「占位」：关闭/托盘/退出语义由 W5 在 WindowChrome.vue + src-tauri 实现，
 * 本分支（W4）不修改 WindowChrome.vue。这里只守住两件事，避免集成时窗口操作入口
 * 被悄悄删掉却没人发现：
 * 1) 标题条容器与拖拽面仍在（窗口可拖拽的前提）；
 * 2) 源码里三个窗口按钮（最小化 / 最大化 / 关闭）与统一的 run('close') 入口仍在
 *    —— W5 会把这个入口改成 request_close 并决定 ask / tray / quit 语义。
 * 真正的关闭策略行为断言由 W5 的测试覆盖；这里不伪造那部分覆盖。
 */
import { describe, expect, it } from "vitest";
import { mount } from "@vue/test-utils";
import WindowChrome from "../WindowChrome.vue";
import source from "../WindowChrome.vue?raw";

describe("关闭行为占位（实现归 W5）", () => {
  it("标题条容器与拖拽面仍渲染（浏览器环境不画窗口按钮，不假装能点）", () => {
    const w = mount(WindowChrome);
    expect(w.find(".window-chrome").exists()).toBe(true);
    expect(w.find(".drag-surface").exists()).toBe(true);
    // 浏览器里没有 Tauri 桥：不渲染按钮是诚实行为（画了也点不动）
    expect(w.find(".wc-close").exists()).toBe(false);
    w.unmount();
  });

  it("源码保留三个窗口按钮与统一 run('close') 入口（W5 在此接入关闭策略）", () => {
    expect(source).toContain('aria-label="关闭"');
    expect(source).toContain('title="关闭窗口"');
    expect(source).toContain("run('close')");
    expect(source).toContain('aria-label="最小化"');
    expect(source).toContain("run('toggleMaximize')");
  });
});
