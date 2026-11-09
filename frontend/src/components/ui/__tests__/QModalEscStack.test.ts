/**
 * 弹层内 Esc 层级（问题 6）：一次 Esc 只处理最内层可关闭界面。
 *
 * 之前的根因：QModal 在 window 捕获阶段监听 Esc，先于下拉框自己的冒泡处理执行，
 * 于是「收起服务商列表」和「关闭 Key 表单」被同一次 Esc 一起做掉；内层再
 * stopPropagation 也来不及。现在 QModal / QCombo / QSelect 都注册到统一层级栈
 * （composables/useEscapeLayer.ts），后打开的内层先处理。
 */
import { afterEach, describe, expect, it } from "vitest";
import { mount, type VueWrapper } from "@vue/test-utils";
import { defineComponent, h, nextTick } from "vue";
import QModal from "../QModal.vue";
import QCombo from "../QCombo.vue";
import QSelect from "../QSelect.vue";


const COMBO_OPTIONS = [
  { value: "deepseek", label: "DeepSeek", hint: "官方服务" },
  { value: "anthropic", label: "Anthropic", hint: "官方服务" },
];
const SELECT_OPTIONS = [
  { value: "a", label: "A" },
  { value: "b", label: "B" },
];

const mounted: VueWrapper[] = [];
afterEach(() => {
  for (const wrapper of mounted) wrapper.unmount();
  mounted.length = 0;
  document.body.innerHTML = "";
});

/** 模拟真实结构：中央弹窗（Key 表单）里放一个服务商可搜索下拉和一个短选择器。 */
function mountLayered() {
  const Harness = defineComponent({
    name: "LayeredHarness",
    setup() {
      return () =>
        h(
          QModal,
          { open: true, title: "添加 Key", modalId: "qio-test-modal" },
          {
            default: () => [
              h(QCombo, { options: COMBO_OPTIONS, modelValue: "" }),
              h(QSelect, { options: SELECT_OPTIONS, modelValue: "" }),
            ],
          },
        );
    },
  });
  const wrapper = mount(Harness, { attachTo: document.body });
  mounted.push(wrapper);
  return wrapper;
}

function escapeEvent(init: KeyboardEventInit = {}): KeyboardEvent {
  return new KeyboardEvent("keydown", { key: "Escape", bubbles: true, cancelable: true, ...init });
}

async function settle() {
  await nextTick();
  await nextTick();
}

function dialogEl(): HTMLElement | null {
  return document.querySelector('[role="dialog"]');
}

describe("问题 6：内层下拉先收，外层表单后关", () => {
  it("打开服务商列表时第一次 Esc 只收列表，第二次才关闭表单", async () => {
    const wrapper = mountLayered();
    await settle();
    const modal = wrapper.findComponent(QModal);
    const combo = wrapper.findComponent(QCombo);
    expect(dialogEl()).not.toBeNull();

    await combo.find(".qio-combo-trigger").trigger("click");
    await settle();
    expect(combo.find(".qio-combo-menu").exists()).toBe(true);
    expect(modal.emitted("close")).toBeUndefined();

    window.dispatchEvent(escapeEvent());
    await settle();
    // 列表收起、表单保留、弹窗没有收到 close
    expect(combo.find(".qio-combo-menu").exists()).toBe(false);
    expect(dialogEl()).not.toBeNull();
    expect(wrapper.findComponent(QCombo).exists()).toBe(true);
    expect(modal.emitted("close")).toBeUndefined();

    // 列表已经收起：这次 Esc 才轮到外层弹窗
    window.dispatchEvent(escapeEvent());
    await settle();
    expect(modal.emitted("close")).toHaveLength(1);
  });

  it("焦点在搜索框里按 Esc 同样只收列表（事件从输入框冒泡/捕获）", async () => {
    const wrapper = mountLayered();
    await settle();
    const modal = wrapper.findComponent(QModal);
    const combo = wrapper.findComponent(QCombo);
    await combo.find(".qio-combo-trigger").trigger("click");
    await settle();
    const input = combo.find(".qio-combo-search").element;
    expect(document.activeElement).toBe(input);

    input.dispatchEvent(escapeEvent());
    await settle();
    expect(combo.find(".qio-combo-menu").exists()).toBe(false);
    expect(modal.emitted("close")).toBeUndefined();
    expect(dialogEl()).not.toBeNull();
  });

  it("短选择器展开时 Esc 也只收它的菜单", async () => {
    const wrapper = mountLayered();
    await settle();
    const modal = wrapper.findComponent(QModal);
    const select = wrapper.findComponent(QSelect);
    await select.find(".qio-select").trigger("click");
    await settle();
    expect(select.classes()).toContain("open");

    window.dispatchEvent(escapeEvent());
    await settle();
    expect(select.classes()).not.toContain("open");
    expect(modal.emitted("close")).toBeUndefined();
    expect(dialogEl()).not.toBeNull();
  });

  it("组合输入（选字）中的 Esc 不收列表也不关表单", async () => {
    const wrapper = mountLayered();
    await settle();
    const modal = wrapper.findComponent(QModal);
    const combo = wrapper.findComponent(QCombo);
    await combo.find(".qio-combo-trigger").trigger("click");
    await settle();
    const input = combo.find(".qio-combo-search").element;
    input.dispatchEvent(new CompositionEvent("compositionstart", { bubbles: true }));
    await settle();

    input.dispatchEvent(escapeEvent({ isComposing: true }));
    await settle();
    expect(combo.find(".qio-combo-menu").exists()).toBe(true);
    expect(modal.emitted("close")).toBeUndefined();

    input.dispatchEvent(new CompositionEvent("compositionend", { bubbles: true }));
    await settle();
    window.dispatchEvent(escapeEvent());
    await settle();
    expect(combo.find(".qio-combo-menu").exists()).toBe(false);
    expect(modal.emitted("close")).toBeUndefined();
  });
});

describe("问题 7 的弹层机制：busy 时 Esc 不关闭，解除后恢复", () => {
  it("处理中消费 Esc（不关闭、不落到后面的界面）；恢复后 Esc 正常关闭", async () => {
    const wrapper = mount(QModal, {
      props: { open: true, title: "保存中", modalId: "qio-busy-modal" },
      attachTo: document.body,
    });
    mounted.push(wrapper);
    await settle();

    await wrapper.setProps({ busy: true });
    await settle();
    const closeButton = document.querySelector(".qio-modal-head__slot--end button") as HTMLButtonElement;
    expect(closeButton.disabled).toBe(true);

    window.dispatchEvent(escapeEvent());
    await settle();
    expect(wrapper.emitted("close")).toBeUndefined();
    expect(dialogEl()).not.toBeNull();

    await wrapper.setProps({ busy: false });
    await settle();
    window.dispatchEvent(escapeEvent());
    await settle();
    expect(wrapper.emitted("close")).toHaveLength(1);
  });
});
