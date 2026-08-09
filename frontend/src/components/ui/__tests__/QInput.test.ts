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
});
