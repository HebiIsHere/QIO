import { describe, expect, it } from "vitest";
import { mount } from "@vue/test-utils";
import QInput from "../QInput.vue";

describe("QInput", () => {
  it("渲染原生 input 并透传 placeholder", () => {
    const w = mount(QInput, { props: { placeholder: "test" } });
    expect(w.find("input").attributes("placeholder")).toBe("test");
  });
  it("error 时挂 err 类", () => {
    const w = mount(QInput, { props: { error: true } });
    expect(w.find("input").classes()).toContain("err");
  });
  it("mono 变体挂 mono 类", () => {
    const w = mount(QInput, { props: { mono: true } });
    expect(w.find("input").classes()).toContain("mono");
  });
  it("输入时触发 update:modelValue", async () => {
    const w = mount(QInput, { props: { modelValue: "" } });
    await w.find("input").setValue("hi");
    expect(w.emitted("update:modelValue")?.[0]).toEqual(["hi"]);
  });
  it("error 时设置 aria-invalid", () => {
    const w = mount(QInput, { props: { error: true } });
    expect(w.find("input").attributes("aria-invalid")).toBe("true");
  });
  it("change 事件透传", async () => {
    const w = mount(QInput, { props: { modelValue: "1" } });
    await w.find("input").trigger("change");
    expect(w.emitted("change")).toHaveLength(1);
  });
});
