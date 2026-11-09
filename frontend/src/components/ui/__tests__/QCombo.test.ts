/**
 * QCombo：可搜索下拉的键盘 / 输入法 / 空结果行为（问题 10、11、12 回归）。
 *
 * 守住的边界：
 * - 输入框持焦点时一次方向键只移动一项、回车只选择一次并保持关闭（之前输入框和
 *   外层各挂一份 keydown，一次按键被执行两遍：跳两项、选中后又被重新展开）；
 * - 中文输入法组合输入（选字）阶段的回车 / 方向键不当作提交与导航；组合结束后
 *   普通键盘选择仍然可用；
 * - 有结果 / 无结果 / 清空查询三种状态，Tab 都收起菜单且不阻止默认焦点移动，
 *   Shift+Tab 同样；Esc 收菜单。
 */
import { afterEach, describe, expect, it } from "vitest";
import { flushPromises, mount, type VueWrapper } from "@vue/test-utils";
import { defineComponent, h, nextTick, ref } from "vue";
import QCombo from "../QCombo.vue";

const OPTIONS = [
  { value: "deepseek", label: "DeepSeek", hint: "官方服务" },
  { value: "anthropic", label: "Anthropic", hint: "官方服务" },
  { value: "custom", label: "其他 / 自定义服务", hint: "自定义服务" },
];

const mounted: VueWrapper[] = [];
afterEach(() => {
  for (const wrapper of mounted) wrapper.unmount();
  mounted.length = 0;
  document.body.innerHTML = "";
});

function mountCombo(props: Record<string, unknown> = {}) {
  const wrapper = mount(QCombo, {
    props: { options: OPTIONS, modelValue: "", ...props },
    attachTo: document.body,
  });
  mounted.push(wrapper);
  return wrapper;
}

async function openMenu(wrapper: VueWrapper) {
  await wrapper.find(".qio-combo-trigger").trigger("click");
  await nextTick();
  await nextTick();
}

function search(wrapper: VueWrapper) {
  return wrapper.find(".qio-combo-search");
}

function highlightedIndex(wrapper: VueWrapper): number {
  return wrapper.findAll(".qio-combo-opt").findIndex((option) => option.classes().includes("hl"));
}

function pressKey(target: Element, key: string, init: KeyboardEventInit = {}): KeyboardEvent {
  const event = new KeyboardEvent("keydown", { key, bubbles: true, cancelable: true, ...init });
  target.dispatchEvent(event);
  return event;
}

function pressEscape(target: EventTarget = window, init: KeyboardEventInit = {}): KeyboardEvent {
  const event = new KeyboardEvent("keydown", { key: "Escape", bubbles: true, cancelable: true, ...init });
  target.dispatchEvent(event);
  return event;
}

describe("QCombo · 一次按键只处理一次（问题 10）", () => {
  it("输入框持焦点时：一次方向键只移动一项，回车只选择一次并保持关闭", async () => {
    const wrapper = mountCombo();
    await openMenu(wrapper);
    const input = search(wrapper);
    expect(document.activeElement).toBe(input.element);
    // modelValue 为空 → 打开时高亮第一项
    expect(highlightedIndex(wrapper)).toBe(0);

    const down = pressKey(input.element, "ArrowDown");
    await nextTick();
    expect(down.defaultPrevented).toBe(true);
    expect(highlightedIndex(wrapper)).toBe(1);

    pressKey(input.element, "ArrowDown");
    await nextTick();
    expect(highlightedIndex(wrapper)).toBe(2);

    pressKey(input.element, "ArrowUp");
    await nextTick();
    expect(highlightedIndex(wrapper)).toBe(1);

    pressKey(input.element, "Enter");
    await flushPromises();
    const emissions = wrapper.emitted("update:modelValue") ?? [];
    expect(emissions).toHaveLength(1);
    expect(emissions[0]).toEqual(["anthropic"]);
    expect(wrapper.find(".qio-combo-menu").exists()).toBe(false);
    expect(wrapper.classes()).not.toContain("open");
    expect(wrapper.find(".qio-combo-trigger").attributes("aria-expanded")).toBe("false");
    // 选中后焦点回到触发按钮，菜单保持关闭（不会被这次回车重新展开）
    expect(document.activeElement).toBe(wrapper.find(".qio-combo-trigger").element);
    await flushPromises();
    expect(wrapper.find(".qio-combo-menu").exists()).toBe(false);
  });

  it("鼠标选择只提交一次；重新打开时选中值与高亮一致", async () => {
    const wrapper = mountCombo();
    await openMenu(wrapper);
    const option = wrapper.findAll(".qio-combo-opt").find((item) => item.text().includes("Anthropic"));
    expect(option).toBeTruthy();
    await option!.trigger("click");
    await nextTick();

    const emissions = wrapper.emitted("update:modelValue") ?? [];
    expect(emissions).toHaveLength(1);
    expect(emissions[0]).toEqual(["anthropic"]);
    expect(wrapper.find(".qio-combo-menu").exists()).toBe(false);

    await wrapper.setProps({ modelValue: "anthropic" });
    expect(wrapper.find(".qio-combo-value").text()).toBe("Anthropic");
    await openMenu(wrapper);
    expect(highlightedIndex(wrapper)).toBe(1);
    expect(wrapper.findAll(".qio-combo-opt")[1].attributes("aria-selected")).toBe("true");
  });

  it("菜单关闭时触发按钮上的回车只负责展开，不会同时选中", async () => {
    const wrapper = mountCombo();
    const trigger = wrapper.find(".qio-combo-trigger");
    trigger.element.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true, cancelable: true }));
    await flushPromises();
    expect(wrapper.find(".qio-combo-menu").exists()).toBe(true);
    expect(wrapper.emitted("update:modelValue")).toBeUndefined();
  });
});

describe("QCombo · 中文输入法组合输入（问题 11）", () => {
  it("组合输入阶段的回车与方向键不提交 / 不导航；组合结束后键盘选择恢复", async () => {
    const wrapper = mountCombo();
    await openMenu(wrapper);
    const input = search(wrapper);

    input.element.dispatchEvent(new CompositionEvent("compositionstart", { bubbles: true }));
    await nextTick();

    const enterDuring = pressKey(input.element, "Enter", { isComposing: true });
    await nextTick();
    // 确认候选字的回车不能被当成「选中当前项」，也不能被 preventDefault 掉
    expect(enterDuring.defaultPrevented).toBe(false);
    expect(wrapper.emitted("update:modelValue")).toBeUndefined();
    expect(wrapper.find(".qio-combo-menu").exists()).toBe(true);

    pressKey(input.element, "ArrowDown", { isComposing: true });
    await nextTick();
    expect(highlightedIndex(wrapper)).toBe(0); // 选字导航没有移动下拉高亮

    // 部分浏览器/输入法在组合输入期间用 keyCode 229 标记
    pressKey(input.element, "Enter", { keyCode: 229 });
    await nextTick();
    expect(wrapper.emitted("update:modelValue")).toBeUndefined();

    input.element.dispatchEvent(new CompositionEvent("compositionend", { bubbles: true, data: "服" }));
    await nextTick();

    const enterAfter = pressKey(input.element, "Enter");
    await nextTick();
    expect(enterAfter.defaultPrevented).toBe(true);
    expect(wrapper.emitted("update:modelValue")).toHaveLength(1);
    expect(wrapper.emitted("update:modelValue")![0]).toEqual(["deepseek"]);
    expect(wrapper.find(".qio-combo-menu").exists()).toBe(false);
  });

  it("组合输入中按 Esc 不收起菜单（Esc 属于输入法取消选字）", async () => {
    const wrapper = mountCombo();
    await openMenu(wrapper);
    const input = search(wrapper);
    input.element.dispatchEvent(new CompositionEvent("compositionstart", { bubbles: true }));
    await nextTick();

    // 真实输入法在组合输入期间产生的 keydown 带 isComposing（部分浏览器/keyCode 229 由工具兜底）
    pressEscape(input.element, { isComposing: true });
    await nextTick();
    expect(wrapper.find(".qio-combo-menu").exists()).toBe(true);

    input.element.dispatchEvent(new CompositionEvent("compositionend", { bubbles: true }));
    await nextTick();
    pressEscape(input.element);
    await nextTick();
    expect(wrapper.find(".qio-combo-menu").exists()).toBe(false);
  });
});

describe("QCombo · Tab / Esc 收起菜单（问题 12）", () => {
  it("有结果时按 Tab：收起菜单、不阻止默认焦点移动", async () => {
    const wrapper = mountCombo();
    await openMenu(wrapper);
    const input = search(wrapper);
    await input.setValue("an");
    expect(wrapper.find(".qio-combo-list").findAll(".qio-combo-opt").length).toBeGreaterThan(0);

    const tab = pressKey(input.element, "Tab");
    await nextTick();
    expect(tab.defaultPrevented).toBe(false);
    expect(wrapper.find(".qio-combo-menu").exists()).toBe(false);
  });

  it("无匹配结果时按 Tab：空下拉也收起，Shift+Tab 同样", async () => {
    const wrapper = mountCombo();
    await openMenu(wrapper);
    let input = search(wrapper);
    await input.setValue("zzz");
    expect(wrapper.find(".qio-combo-empty").exists()).toBe(true);

    const tab = pressKey(input.element, "Tab");
    await nextTick();
    expect(tab.defaultPrevented).toBe(false);
    expect(wrapper.find(".qio-combo-menu").exists()).toBe(false);

    await openMenu(wrapper);
    input = search(wrapper);
    await input.setValue("zzz");
    const shiftTab = pressKey(input.element, "Tab", { shiftKey: true });
    await nextTick();
    expect(shiftTab.defaultPrevented).toBe(false);
    expect(wrapper.find(".qio-combo-menu").exists()).toBe(false);
  });

  it("清空查询后按 Tab：同样收起菜单", async () => {
    const wrapper = mountCombo();
    await openMenu(wrapper);
    const input = search(wrapper);
    await input.setValue("an");
    await input.setValue("");
    const tab = pressKey(input.element, "Tab");
    await nextTick();
    expect(tab.defaultPrevented).toBe(false);
    expect(wrapper.find(".qio-combo-menu").exists()).toBe(false);
  });

  it("无结果时按 Esc 也收起菜单", async () => {
    const wrapper = mountCombo();
    await openMenu(wrapper);
    const input = search(wrapper);
    await input.setValue("zzz");
    pressEscape(input.element);
    await nextTick();
    expect(wrapper.find(".qio-combo-menu").exists()).toBe(false);
  });
});

describe("QCombo · 菜单打开时回车不提交周围表单", () => {
  it("无匹配结果时按回车只打开/保持菜单，不触发表单提交", async () => {
    const submitted = ref(0);
    const Harness = defineComponent({
      name: "ComboInForm",
      setup() {
        return () =>
          h(
            "form",
            { onSubmit: (event: Event) => { event.preventDefault(); submitted.value += 1; } },
            [h(QCombo, { options: OPTIONS, modelValue: "" })],
          );
      },
    });
    const wrapper = mount(Harness, { attachTo: document.body });
    mounted.push(wrapper);

    await wrapper.find(".qio-combo-trigger").trigger("click");
    await nextTick();
    await nextTick();
    const input = wrapper.find(".qio-combo-search");
    await input.setValue("zzz");
    expect(wrapper.find(".qio-combo-empty").exists()).toBe(true);

    const enter = new KeyboardEvent("keydown", { key: "Enter", bubbles: true, cancelable: true });
    input.element.dispatchEvent(enter);
    await nextTick();
    expect(enter.defaultPrevented).toBe(true);
    expect(submitted.value).toBe(0);
    expect(wrapper.findComponent(QCombo).emitted("update:modelValue")).toBeUndefined();
  });
});
