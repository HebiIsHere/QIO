/**
 * 页面工具行的搜索入口（#30 文案 / #32 焦点返回）。
 *
 * 两件事：
 * 1. 悬停只说「全局搜索」——搜索范围只留在搜索输入框 placeholder，不在入口重复；
 * 2. 暴露 focusSearch：Esc 退出搜索后由 PlanetView 把焦点交回**真实的**入口按钮。
 */
import { afterEach, describe, expect, it } from "vitest";
import { mount } from "@vue/test-utils";
import PageTools from "../PageTools.vue";
import QIconButton from "../../ui/QIconButton.vue";

afterEach(() => {
  document.body.innerHTML = "";
});

describe("页面工具行：搜索入口", () => {
  it("悬停只写「全局搜索」：不在入口重复搜索范围", () => {
    const w = mount(PageTools);
    const search = w.findComponent(QIconButton);
    expect(search.props("label")).toBe("全局搜索");
    expect(search.props("description")).toBe("");
    expect(w.find("[data-mp-search-open]").exists()).toBe(true);
    expect(w.text()).not.toContain("话题");
    w.unmount();
  });

  it("focusSearch 把焦点放到真实搜索入口按钮（Esc 退出搜索后用）", () => {
    const w = mount(PageTools, { attachTo: document.body });
    const button = w.find("[data-mp-search-open]").element as HTMLButtonElement;
    (w.vm as unknown as { focusSearch: () => void }).focusSearch();
    expect(document.activeElement).toBe(button);
    w.unmount();
  });

  it("点搜索入口只上报 search 事件（展开与落点由 PlanetView 决定）", async () => {
    const w = mount(PageTools);
    await w.find("[data-mp-search-open]").trigger("click");
    expect(w.emitted("search")).toHaveLength(1);
    w.unmount();
  });
});
