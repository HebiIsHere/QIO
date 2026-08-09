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
});
