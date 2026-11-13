/**
 * 文案 6 / #85（PM-QIO032）：首次手势指引只写一句可读文案，颜色用次级文字色。
 *
 * 文案 6 原文：「拖动空白处平移，按住空格拖动框选，滚轮缩放」。
 * #85：板面操作指引必须可读 —— 用 --text-secondary（净白 / 暗紫晶两套主题都定义了它），
 * 不能用更浅的 --text-faint（暗色下几乎看不清）。
 */
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { mount, type VueWrapper } from "@vue/test-utils";
import BoardCanvas from "../BoardCanvas.vue";
import source from "../BoardCanvas.vue?raw";


const HINT = "拖动空白处平移，按住空格拖动框选，滚轮缩放";

let wrapper: VueWrapper | null = null;

beforeEach(() => {
  localStorage.clear();
  setActivePinia(createPinia());
});

afterEach(() => {
  try {
    wrapper?.unmount();
  } catch {
    /* 已卸载 */
  }
  wrapper = null;
  document.body.innerHTML = "";
});

describe("文案 6：首次手势指引", () => {
  it("未用过手势时只显示原文这一句（不含旧的长解释与百分比）", () => {
    wrapper = mount(BoardCanvas, { attachTo: document.body });
    const hint = wrapper.get('[data-im="board-view-hint"]');
    expect(hint.text().replace(/\s+/g, "")).toBe(HINT);
    expect(hint.text()).not.toContain("%");
  });
});

describe("#85：板面操作指引使用可读次级文字色", () => {
  it("提示条用 --text-secondary，而不是更浅的 --text-faint", () => {
    expect(source).toMatch(/\.banner\.view-hint \{ color: var\(--text-secondary\); \}/);
    expect(source).not.toMatch(/\.banner\.view-hint \{ color: var\(--text-faint\); \}/);
  });

});