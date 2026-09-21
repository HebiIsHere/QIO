/**
 * 底部让位：输入区有多高，底部那几块就往上让多少。
 *
 * 这条测试对应实测到的问题：输入区是固定悬浮层，底部三块（知识候选卡 /
 * 兼容模式提示 / 话题切换提示）排在它下面，按钮中点命中的是输入框而不是按钮。
 */
import { describe, expect, it, afterEach } from "vitest";
import { mount } from "@vue/test-utils";
import { defineComponent, h, ref } from "vue";
import { useComposerClearance } from "../useComposerClearance";

const Host = defineComponent({
  setup() {
    const target = ref<HTMLElement | null>(null);
    useComposerClearance(target);
    return () => h("div", { ref: target, class: "bottom-cluster" });
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

afterEach(() => {
  document.body.innerHTML = "";
});

describe("useComposerClearance", () => {
  it("把输入区高度 + 间隙写成底部容器的下内边距", async () => {
    const composer = fakeComposer(124);
    const wrapper = mount(Host, { attachTo: document.body });

    await wrapper.vm.$nextTick();
    await new Promise((r) => requestAnimationFrame(() => r(null)));

    const cluster = wrapper.element as HTMLElement;
    expect(cluster.style.paddingBottom).toBe("140px");

    composer.remove();
    wrapper.unmount();
  });

  it("输入区不存在时不写内边距（优雅降级，不凭空留白）", async () => {
    const wrapper = mount(Host, { attachTo: document.body });

    await wrapper.vm.$nextTick();
    await new Promise((r) => requestAnimationFrame(() => r(null)));

    expect((wrapper.element as HTMLElement).style.paddingBottom).toBe("");
    wrapper.unmount();
  });
});
