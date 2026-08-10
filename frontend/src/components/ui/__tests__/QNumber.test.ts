import { describe, expect, it } from "vitest";
import { mount } from "@vue/test-utils";
import QNumber from "../QNumber.vue";

function lastVal(w: ReturnType<typeof mount>) {
  const ev = w.emitted("update:modelValue");
  return ev?.[ev.length - 1];
}

describe("QNumber", () => {
  it("渲染输入框 + 增减按钮，初始值回填", () => {
    const w = mount(QNumber, { props: { modelValue: 5, min: 1, max: 50, label: "最大迭代" } });
    expect(w.find("input").exists()).toBe(true);
    expect(w.find(".step.up").exists()).toBe(true);
    expect(w.find(".step.down").exists()).toBe(true);
    expect((w.find("input").element as HTMLInputElement).value).toBe("5");
    expect(w.find("input").attributes("aria-label")).toBe("最大迭代");
    w.unmount();
  });

  it("+/- 按 step 步进并 emit；clamp 到 min/max", async () => {
    const w = mount(QNumber, { props: { modelValue: 49, min: 1, max: 50, step: 1 } });
    await w.find(".step.up").trigger("click");
    expect(lastVal(w)).toEqual([50]);
    await w.find(".step.down").trigger("click");
    expect(lastVal(w)).toEqual([48]);
    // 已在 max：再 + 保持 50
    const w2 = mount(QNumber, { props: { modelValue: 50, min: 1, max: 50 } });
    await w2.find(".step.up").trigger("click");
    expect(lastVal(w2)).toEqual([50]);
    // 已在 min：再 - 保持 1
    const w3 = mount(QNumber, { props: { modelValue: 1, min: 1, max: 50 } });
    await w3.find(".step.down").trigger("click");
    expect(lastVal(w3)).toEqual([1]);
    w.unmount(); w2.unmount(); w3.unmount();
  });

  it("失焦 commit 收口：clamp 到 max 并 emit change", async () => {
    const w = mount(QNumber, { props: { modelValue: 5, min: 1, max: 10 } });
    const input = w.find("input");
    await input.setValue("99");
    await input.trigger("blur");
    expect(lastVal(w)).toEqual([10]); // clamp 到 max
    expect(w.emitted("change")?.length).toBeGreaterThanOrEqual(1);
    w.unmount();
  });

  it("清空输入 → emit null", async () => {
    const w = mount(QNumber, { props: { modelValue: 5 } });
    await w.find("input").setValue("");
    expect(lastVal(w)).toEqual([null]);
    w.unmount();
  });

  it("方向键上下步进", async () => {
    const w = mount(QNumber, { props: { modelValue: 5, min: 1, max: 10 } });
    await w.find("input").trigger("keydown", { key: "ArrowUp" });
    expect(lastVal(w)).toEqual([6]);
    await w.find("input").trigger("keydown", { key: "ArrowDown" });
    expect(lastVal(w)).toEqual([4]); // 静态 prop 仍为 5：5-1
    w.unmount();
  });
});
