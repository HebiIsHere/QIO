import { describe, expect, it } from "vitest";
import { mount } from "@vue/test-utils";
import { nextTick } from "vue";
import QSelect from "../QSelect.vue";
const opts = [{ value: "a", label: "A" }, { value: "b", label: "B" }];
describe("QSelect", () => {
  it("默认显示选中项", () => {
    const w = mount(QSelect, { props: { options: opts, modelValue: "b" } });
    expect(w.find(".qio-select-val").text()).toBe("B");
  });
  it("点击展开，点选项后发事件并关闭", async () => {
    const w = mount(QSelect, { props: { options: opts, modelValue: "a" } });
    await w.find(".qio-select").trigger("click");
    expect(w.find(".qio-select").classes()).toContain("open");
    const b = w.findAll(".opt").find(o => o.text() === "B")!;
    await b.trigger("click");
    expect(w.emitted("update:modelValue")?.[0]).toEqual(["b"]);
    expect(w.find(".qio-select").classes()).not.toContain("open");
    w.unmount();
  });
  it("点击组件外部关闭浮层", async () => {
    const w = mount(QSelect, { props: { options: opts, modelValue: "a" } });
    await w.find(".qio-select").trigger("click");
    expect(w.find(".qio-select").classes()).toContain("open");
    document.body.dispatchEvent(new PointerEvent("pointerdown", { bubbles: true }));
    await nextTick();
    expect(w.find(".qio-select").classes()).not.toContain("open");
    w.unmount();
  });
  it("ArrowDown 移动高亮，Enter 选中高亮项并关闭", async () => {
    const w = mount(QSelect, { props: { options: opts, modelValue: "a" } });
    await w.find(".qio-select").trigger("click");
    await w.find(".qio-select").trigger("keydown", { key: "ArrowDown" });
    await w.find(".qio-select").trigger("keydown", { key: "Enter" });
    expect(w.emitted("update:modelValue")?.[0]).toEqual(["b"]);
    expect(w.find(".qio-select").classes()).not.toContain("open");
    w.unmount();
  });
  it("暴露 combobox/listbox/option 角色与展开状态", () => {
    const w = mount(QSelect, { props: { options: opts, modelValue: "a" } });
    const root = w.find(".qio-select");
    expect(root.attributes("role")).toBe("combobox");
    expect(root.attributes("aria-expanded")).toBe("false");
    expect(root.attributes("aria-haspopup")).toBe("listbox");
    expect(w.find(".qio-select-menu").attributes("role")).toBe("listbox");
    expect(w.findAll(".opt")[0].attributes("role")).toBe("option");
    w.unmount();
  });
  it("disabled 时点击不展开并挂 disabled 类", async () => {
    const w = mount(QSelect, { props: { options: opts, modelValue: "a", disabled: true } });
    await w.find(".qio-select").trigger("click");
    expect(w.find(".qio-select").classes()).toContain("disabled");
    expect(w.find(".qio-select").classes()).not.toContain("open");
    w.unmount();
  });
  it("空 options 点击不崩溃且不展开", async () => {
    const w = mount(QSelect, { props: { options: [], modelValue: "" } });
    await w.find(".qio-select").trigger("click");
    expect(w.find(".qio-select").classes()).not.toContain("open");
    expect(w.findAll(".opt").length).toBe(0);
    await w.find(".qio-select").trigger("keydown", { key: "ArrowDown" });
    expect(w.find(".qio-select").classes()).not.toContain("open");
    w.unmount();
  });
  it("aria-selected 反映真实选中项", () => {
    const w = mount(QSelect, { props: { options: opts, modelValue: "a" } });
    const els = w.findAll(".opt");
    expect(els[0].attributes("aria-selected")).toBe("true");
    expect(els[1].attributes("aria-selected")).toBe("false");
    w.unmount();
  });
});

/**
 * 问题 13：从可操作变为 busy / disabled 后，已展开的菜单必须没收，
 * 且选项提交入口本身要拒绝迟到的鼠标 / 键盘事件（不能只把触发按钮变灰）。
 */
describe("QSelect · 处理中 / 禁用状态变化（问题 13）", () => {
  it("展开后变 busy：菜单收起，已渲染选项的点击与键盘都不能提交", async () => {
    const w = mount(QSelect, { props: { options: opts, modelValue: "a" } });
    await w.find(".qio-select").trigger("click");
    expect(w.find(".qio-select").classes()).toContain("open");

    await w.setProps({ busy: true });
    expect(w.find(".qio-select").classes()).not.toContain("open");
    expect(w.find(".qio-select").attributes("aria-expanded")).toBe("false");

    // 菜单始终在 DOM（由 CSS 控制显隐）：状态变化前就已渲染的选项，此刻点击也不能提交
    await w.findAll(".opt")[1].trigger("click");
    await w.find(".qio-select").trigger("keydown", { key: "Enter" });
    await w.find(".qio-select").trigger("keydown", { key: "ArrowDown" });
    await w.find(".qio-select").trigger("keydown", { key: " " });
    expect(w.emitted("update:modelValue")).toBeUndefined();
    expect(w.find(".qio-select").classes()).not.toContain("open");
    w.unmount();
  });

  it("快速点击：点击到达前状态变为 disabled，选项提交入口自己挡住", async () => {
    const w = mount(QSelect, { props: { options: opts, modelValue: "a" } });
    await w.find(".qio-select").trigger("click");
    const staleOption = w.findAll(".opt")[0];
    await w.setProps({ disabled: true });
    await staleOption.trigger("click");
    expect(w.emitted("update:modelValue")).toBeUndefined();
    expect(w.find(".qio-select").classes()).not.toContain("open");
    w.unmount();
  });

  it("busy 期间点击触发框不展开，也不响应方向键", async () => {
    const w = mount(QSelect, { props: { options: opts, modelValue: "a", busy: true } });
    await w.find(".qio-select").trigger("click");
    expect(w.find(".qio-select").classes()).not.toContain("open");
    await w.find(".qio-select").trigger("keydown", { key: "ArrowDown" });
    await w.find(".qio-select").trigger("keydown", { key: "Enter" });
    expect(w.emitted("update:modelValue")).toBeUndefined();
    expect(w.find(".qio-select").classes()).not.toContain("open");
    w.unmount();
  });

  it("恢复可操作后正常选择：值更新且菜单关闭", async () => {
    const w = mount(QSelect, { props: { options: opts, modelValue: "a", busy: true } });
    await w.setProps({ busy: false });
    await w.find(".qio-select").trigger("click");
    expect(w.find(".qio-select").classes()).toContain("open");
    await w.find(".qio-select").trigger("keydown", { key: "ArrowDown" });
    await w.find(".qio-select").trigger("keydown", { key: "Enter" });
    expect(w.emitted("update:modelValue")?.[0]).toEqual(["b"]);
    expect(w.find(".qio-select").classes()).not.toContain("open");
    w.unmount();
  });

  it("展开时变 busy 不会误发值，恢复后 Esc 仍只收自己的菜单", async () => {
    const w = mount(QSelect, { props: { options: opts, modelValue: "a" } });
    await w.find(".qio-select").trigger("click");
    await w.setProps({ busy: true });
    await w.setProps({ busy: false });
    await w.find(".qio-select").trigger("click");
    expect(w.find(".qio-select").classes()).toContain("open");
    document.body.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true, cancelable: true }));
    await nextTick();
    expect(w.find(".qio-select").classes()).not.toContain("open");
    expect(w.emitted("update:modelValue")).toBeUndefined();
    w.unmount();
  });
});
