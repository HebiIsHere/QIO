import { describe, expect, it } from "vitest";
import { mount } from "@vue/test-utils";
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
  });
});
