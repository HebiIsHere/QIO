import { describe, expect, it } from "vitest";
import { mount } from "@vue/test-utils";
import QSlider from "../QSlider.vue";

function lastVal(w: ReturnType<typeof mount>) {
  const ev = w.emitted("update:modelValue");
  return ev?.[ev.length - 1];
}

describe("QSlider", () => {
  it("渲染 range 并带 min/max/step/aria-label", () => {
    const w = mount(QSlider, { props: { modelValue: 0.5, min: 0, max: 1, step: 0.01, label: "记忆强度" } });
    const input = w.find("input");
    expect(input.attributes("type")).toBe("range");
    expect(input.attributes("min")).toBe("0");
    expect(input.attributes("max")).toBe("1");
    expect(input.attributes("step")).toBe("0.01");
    expect(input.attributes("aria-label")).toBe("记忆强度");
    w.unmount();
  });

  it("按值计算填充比例 --qslider-fill", () => {
    const w = mount(QSlider, { props: { modelValue: 0.25, min: 0, max: 1 } });
    const el = w.find("input").element as HTMLElement;
    expect(el.style.getPropertyValue("--qslider-fill")).toBe("25%");
    const w2 = mount(QSlider, { props: { modelValue: 1, min: 0, max: 1 } });
    expect((w2.find("input").element as HTMLElement).style.getPropertyValue("--qslider-fill")).toBe("100%");
    w.unmount();
    w2.unmount();
  });

  it("input 时 emit update:modelValue（数值）", async () => {
    const w = mount(QSlider, { props: { modelValue: 0.3, min: 0, max: 1 } });
    await w.find("input").setValue("0.6");
    expect(lastVal(w)).toEqual([0.6]);
    w.unmount();
  });
});
