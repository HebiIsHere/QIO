/**
 * 「回到最新消息」按钮的锚定测量：从真实元素边界算出它该站的位置。
 */
import { describe, expect, it, afterEach } from "vitest";
import { mount } from "@vue/test-utils";
import { defineComponent, h } from "vue";
import { useLatestButtonAnchor } from "../useLatestButtonAnchor";

const Host = defineComponent({
  setup() {
    const { bottomPx } = useLatestButtonAnchor();
    return () => h("div", { class: "host", "data-bottom": String(bottomPx.value) });
  },
});

function rect(el: HTMLElement, box: { top: number; height: number }): void {
  el.getBoundingClientRect = () =>
    ({
      top: box.top,
      height: box.height,
      bottom: box.top + box.height,
      left: 0,
      right: 860,
      width: 860,
      x: 0,
      y: box.top,
      toJSON: () => ({}),
    }) as DOMRect;
}

function fakeComposer(top: number, height: number, inputTop = top): HTMLElement {
  const el = document.createElement("div");
  el.className = "composer";
  const input = document.createElement("textarea");
  input.id = "composer-input";
  input.getBoundingClientRect = () =>
    ({ top: inputTop, height: 64, bottom: inputTop + 64, left: 0, right: 700, width: 700, x: 0, y: inputTop, toJSON: () => ({}) }) as DOMRect;
  el.appendChild(input);
  rect(el, { top, height });
  document.body.appendChild(el);
  return el;
}

function fakeCluster(top: number, height: number): HTMLElement {
  const el = document.createElement("div");
  el.className = "bottom-cluster";
  rect(el, { top, height });
  document.body.appendChild(el);
  return el;
}

async function settle(): Promise<void> {
  await new Promise((r) => requestAnimationFrame(() => r(null)));
  await new Promise((r) => requestAnimationFrame(() => r(null)));
}

afterEach(() => {
  document.body.innerHTML = "";
});

describe("useLatestButtonAnchor", () => {
  it("空底部块：按钮下缘 = 输入框上缘往上 10px", async () => {
    Object.defineProperty(window, "innerHeight", { value: 900, configurable: true });
    fakeComposer(800, 88);
    fakeCluster(800, 0);
    const wrapper = mount(Host, { attachTo: document.body });
    await settle();

    expect(wrapper.element.getAttribute("data-bottom")).toBe("110");
    wrapper.unmount();
  });

  it("有候选卡时抬到卡片上缘上方", async () => {
    Object.defineProperty(window, "innerHeight", { value: 900, configurable: true });
    fakeComposer(800, 88);
    fakeCluster(640, 160);
    const wrapper = mount(Host, { attachTo: document.body });
    await settle();

    expect(wrapper.element.getAttribute("data-bottom")).toBe("270");
    wrapper.unmount();
  });

  it("面板顶部还有话题行时：锚在面板上缘（贴输入框本体会被话题行盖住）", async () => {
    // 面板上缘 761、输入框本体上缘 805（中间是话题名 / 键盘提示那一条 .topicbar）。
    // 实测：锚到 805 会让按钮落进 .topicbar 里，elementFromPoint 命中 topicbar、按钮点不到。
    // 所以锚线取面板上缘，按钮下缘 = 761 - 10 = 751 → bottom = 900 - 751 = 149。
    Object.defineProperty(window, "innerHeight", { value: 900, configurable: true });
    fakeComposer(761, 124, 805);
    const wrapper = mount(Host, { attachTo: document.body });
    await settle();

    expect(wrapper.element.getAttribute("data-bottom")).toBe("149");
    wrapper.unmount();
  });

  it("输入框不存在时不编造位置（null 交给 CSS 兜底）", async () => {
    const wrapper = mount(Host, { attachTo: document.body });
    await settle();

    expect(wrapper.element.getAttribute("data-bottom")).toBe("null");
    wrapper.unmount();
  });

  it("页面被 CSS zoom 缩放时：把视口像素换算回局部像素", async () => {
    // zoom=0.8：面板 rect 高 99.2（视口像素）但布局高 124（局部像素）→ scale=0.8。
    // 换算前实测过 zoom=0.8 时按钮跑到输入区里面去（间距 -14.4px）。
    Object.defineProperty(window, "innerHeight", { value: 900, configurable: true });
    const el = document.createElement("div");
    el.className = "composer";
    Object.defineProperty(el, "offsetHeight", { value: 124, configurable: true });
    rect(el, { top: 761, height: 99.2 });
    document.body.appendChild(el);
    const wrapper = mount(Host, { attachTo: document.body });
    await settle();

    // 视口像素：900 - 761 + 10 = 149 → 局部像素 149 / 0.8 = 186.25
    expect(wrapper.element.getAttribute("data-bottom")).toBe("186.25");
    wrapper.unmount();
  });

  it("卸载后窗口缩放不再改动位置（回调已清理）", async () => {
    Object.defineProperty(window, "innerHeight", { value: 900, configurable: true });
    fakeComposer(800, 88);
    const wrapper = mount(Host, { attachTo: document.body });
    await settle();
    expect(wrapper.element.getAttribute("data-bottom")).toBe("110");

    const el = wrapper.element as HTMLElement;
    wrapper.unmount();
    Object.defineProperty(window, "innerHeight", { value: 500, configurable: true });
    window.dispatchEvent(new Event("resize"));
    await settle();

    // 卸载后不再更新（元素已脱离文档，值保持不变即可）
    expect(el.getAttribute("data-bottom")).toBe("110");
  });
});
