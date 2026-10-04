/**
 * 底部让位：输入区有多高，底部那几块就往上让多少 —— 但**空块不让位**。
 *
 * 这条测试对应两个真实问题：
 * 1. 输入区是固定悬浮层，底部三块（知识候选卡 / 兼容模式提示 / 话题切换提示）排在它下面，
 *    按钮中点命中的是输入框而不是按钮 → 有内容时必须让位；
 * 2. 这一块**没有任何内容**时以前也留出「输入区高度 + 16px」，消息区底边因此被抬高
 *    （「回到最新消息」按钮靠 sticky 贴底，于是飘到输入框上方很远）→ 空块必须不写内边距。
 */
import { describe, expect, it, afterEach } from "vitest";
import { mount } from "@vue/test-utils";
import { defineComponent, h, ref } from "vue";
import { useComposerClearance } from "../useComposerClearance";

/** 宿主：默认空块；用 `withCard` 模拟「候选卡已在里面」 */
const Host = defineComponent({
  props: { withCard: { type: Boolean, default: false } },
  setup(props) {
    const target = ref<HTMLElement | null>(null);
    useComposerClearance(target);
    return () =>
      h("div", { ref: target, class: "bottom-cluster" }, props.withCard ? [h("div", { class: "card" })] : []);
  },
});

function fakeComposer(height: number): HTMLElement {
  const el = document.createElement("div");
  el.className = "composer";
  el.getBoundingClientRect = () =>
    ({ height, width: 860, top: 0, left: 0, right: 860, bottom: height, x: 0, y: 0, toJSON: () => ({}) }) as DOMRect;
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

describe("useComposerClearance", () => {
  it("块里有内容时：写输入区高度 + 间隙", async () => {
    const composer = fakeComposer(124);
    const wrapper = mount(Host, { props: { withCard: true }, attachTo: document.body });

    await wrapper.vm.$nextTick();
    await settle();

    const cluster = wrapper.element as HTMLElement;
    expect(cluster.style.paddingBottom).toBe("140px");

    composer.remove();
    wrapper.unmount();
  });

  it("空块不写内边距（不再把消息区底边抬高）", async () => {
    const composer = fakeComposer(124);
    const wrapper = mount(Host, { attachTo: document.body });

    await wrapper.vm.$nextTick();
    await settle();

    expect((wrapper.element as HTMLElement).style.paddingBottom).toBe("");

    composer.remove();
    wrapper.unmount();
  });

  it("候选卡出现后才开始让位（子节点变化要能被观察到）", async () => {
    const composer = fakeComposer(100);
    const wrapper = mount(Host, { attachTo: document.body });
    await wrapper.vm.$nextTick();
    await settle();
    expect((wrapper.element as HTMLElement).style.paddingBottom).toBe("");

    await wrapper.setProps({ withCard: true });
    await wrapper.vm.$nextTick();
    await settle();
    expect((wrapper.element as HTMLElement).style.paddingBottom).toBe("116px");

    composer.remove();
    wrapper.unmount();
  });

  it("输入区不存在时不写内边距（优雅降级，不凭空留白）", async () => {
    const wrapper = mount(Host, { props: { withCard: true }, attachTo: document.body });

    await wrapper.vm.$nextTick();
    await settle();

    expect((wrapper.element as HTMLElement).style.paddingBottom).toBe("");
    wrapper.unmount();
  });
});
