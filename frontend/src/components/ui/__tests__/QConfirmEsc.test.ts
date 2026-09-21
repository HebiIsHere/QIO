/**
 * 就地 / 浮层两档的确认框，按 Esc 必须能关掉。
 *
 * 实测问题（上一轮穷举截图时 8/8 复现）：点开确认框之后触发按钮被移除、
 * 焦点落到 body，此时 Esc 无反应 —— 因为 Esc 只挂在确认层元素上，
 * 只有 layer 档才注册了全局监听。现在三档一律全局生效。
 */
import { describe, expect, it } from "vitest";
import { mount } from "@vue/test-utils";
import { nextTick } from "vue";
import QConfirm from "../QConfirm.vue";

function pressEscape(): void {
  window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
}

for (const variant of ["inline", "popover", "layer"] as const) {
  it(`${variant} 档：焦点不在框内时按 Esc 也能取消`, async () => {
    const wrapper = mount(QConfirm, {
      props: { open: true, title: "归档这条知识？", variant },
      attachTo: document.body,
    });
    await nextTick();

    // 模拟「触发按钮已被移除」：焦点落到 body
    (document.activeElement as HTMLElement | null)?.blur?.();

    pressEscape();
    await nextTick();

    expect(wrapper.emitted("cancel")).toHaveLength(1);
    expect(wrapper.emitted("confirm")).toBeUndefined();
    wrapper.unmount();
  });
}

it("一次按键只发一次 cancel（不会因为监听重复而发两次）", async () => {
  const wrapper = mount(QConfirm, {
    props: { open: true, title: "确认", variant: "inline" },
    attachTo: document.body,
  });
  await nextTick();

  pressEscape();
  await nextTick();

  expect(wrapper.emitted("cancel")).toHaveLength(1);
  wrapper.unmount();
});

it("关闭后再打开，Esc 依然有效（监听不会漏挂）", async () => {
  const wrapper = mount(QConfirm, {
    props: { open: true, title: "确认", variant: "inline" },
    attachTo: document.body,
  });
  await nextTick();
  pressEscape();
  await nextTick();
  expect(wrapper.emitted("cancel")).toHaveLength(1);

  await wrapper.setProps({ open: false });
  await nextTick();
  await wrapper.setProps({ open: true });
  await nextTick();
  pressEscape();
  await nextTick();

  expect(wrapper.emitted("cancel")).toHaveLength(2);
  wrapper.unmount();
});
