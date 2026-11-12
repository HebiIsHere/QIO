import { describe, expect, it } from "vitest";
import { mount } from "@vue/test-utils";
import QNumber from "../QNumber.vue";

function lastVal(w: ReturnType<typeof mount>) {
  const ev = w.emitted("update:modelValue");
  return ev?.[ev.length - 1];
}

describe("QNumber", () => {
  it("单位显示在数值旁边（如「24 小时」），不传就不渲染", () => {
    const withUnit = mount(QNumber, { props: { modelValue: 24, label: "维护间隔（小时）", unit: "小时" } });
    expect(withUnit.find(".q-number-unit").exists()).toBe(true);
    expect(withUnit.find(".q-number-unit").text()).toBe("小时");
    withUnit.unmount();

    const withoutUnit = mount(QNumber, { props: { modelValue: 24 } });
    expect(withoutUnit.find(".q-number-unit").exists()).toBe(false);
    withoutUnit.unmount();
  });

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
    // 以输入框当前显示值（50）为基准回退一步
    expect(lastVal(w)).toEqual([49]);
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
    // 以输入框当前显示值为基准（prop 回填是异步的）：6 → 5
    expect(lastVal(w)).toEqual([5]);
    w.unmount();
  });
});

describe("QNumber commit 语义（问题4）", () => {
  it("+ 同时触发 update:modelValue 与 change（设置页依赖 change 保存）", async () => {
    const w = mount(QNumber, { props: { modelValue: 5, min: 1, max: 10 } });
    await w.find(".step.up").trigger("click");
    expect(lastVal(w)).toEqual([6]);
    expect(w.emitted("change")?.length).toBe(1);
    w.unmount();
  });

  it("- 同时触发 update:modelValue 与 change", async () => {
    const w = mount(QNumber, { props: { modelValue: 5, min: 1, max: 10 } });
    await w.find(".step.down").trigger("click");
    expect(lastVal(w)).toEqual([4]);
    expect(w.emitted("change")?.length).toBe(1);
    w.unmount();
  });

  it("+ 之后失焦不重复 change（避免一次点击发两次保存请求）", async () => {
    const w = mount(QNumber, { props: { modelValue: 5, min: 1, max: 10 } });
    await w.find(".step.up").trigger("click");
    await w.find("input").trigger("blur");
    expect(w.emitted("change")?.length).toBe(1);
    w.unmount();
  });

  it("+ 两次再失焦：每次步进各一次 change，共两次", async () => {
    const w = mount(QNumber, { props: { modelValue: 5, min: 1, max: 10 } });
    await w.find(".step.up").trigger("click");
    await w.find(".step.up").trigger("click");
    await w.find("input").trigger("blur");
    expect(w.emitted("change")?.length).toBe(2);
    w.unmount();
  });

  it("值未变化时失焦/回车不 emit change（纯聚焦点击不触发保存）", async () => {
    const w = mount(QNumber, { props: { modelValue: 5, min: 1, max: 10 } });
    await w.find("input").trigger("focus");
    await w.find("input").trigger("blur");
    await w.find("input").trigger("keydown", { key: "Enter" });
    expect(w.emitted("change")).toBeUndefined();
    w.unmount();
  });

  it("手输新值：失焦 clamp 后 emit change 一次；再次失焦不重复", async () => {
    const w = mount(QNumber, { props: { modelValue: 5, min: 1, max: 10 } });
    const input = w.find("input");
    await input.setValue("99");
    await input.trigger("blur");
    expect(lastVal(w)).toEqual([10]);
    expect(w.emitted("change")?.length).toBe(1);
    await input.trigger("blur");
    expect(w.emitted("change")?.length).toBe(1);
    w.unmount();
  });
});
describe("QNumber 必填模式（设置页数字项：#63 / #64）", () => {
  /**
   * 真实浏览器的「打字」只有 input 事件；VTU 的 setValue 会顺带触发 change
   * （提交），会把「编辑过程中」和「离开输入框」两步揉成一步。这里显式分开。
   */
  async function typeOnly(input: ReturnType<ReturnType<typeof mount>["find"]>, value: string) {
    (input.element as HTMLInputElement).value = value;
    await input.trigger("input");
  }

  it("清空后失焦：不 emit / 不 emit null，提示必填并回显当前值", async () => {
    const w = mount(QNumber, { props: { modelValue: 90, min: 0, max: 3650, required: true, label: "天数" } });
    const input = w.find("input");
    await typeOnly(input, "");
    expect((input.element as HTMLInputElement).value).toBe("");
    // 编辑过程中不 emit null（父级因此不会把空值当 0）
    expect(w.emitted("update:modelValue")).toBeUndefined();
    await input.trigger("blur");
    expect(w.emitted("update:modelValue")).toBeUndefined();
    expect(w.emitted("change")).toBeUndefined();
    expect((input.element as HTMLInputElement).value).toBe("90");
    expect(w.find(".q-number-hint").text()).toContain("必填");
    expect(input.attributes("aria-invalid")).toBe("true");
    w.unmount();
  });

  it("非法文本 abc / 1e：不提交、提示可读、回显当前值", async () => {
    for (const bad of ["abc", "1e"]) {
      const w = mount(QNumber, { props: { modelValue: 90, min: 0, max: 3650, required: true } });
      const input = w.find("input");
      await typeOnly(input, bad);
      await input.trigger("blur");
      expect(w.emitted("update:modelValue"), bad).toBeUndefined();
      expect(w.emitted("change"), bad).toBeUndefined();
      expect((input.element as HTMLInputElement).value, bad).toBe("90");
      expect(w.find(".q-number-hint").text(), bad).toContain("只能填写数字");
      w.unmount();
    }
  });

  it("低于下限（-1）不按收口提交：提示范围并回显当前值", async () => {
    const w = mount(QNumber, { props: { modelValue: 90, min: 0, max: 3650, required: true } });
    const input = w.find("input");
    await typeOnly(input, "-1");
    await input.trigger("blur");
    expect(w.emitted("update:modelValue")).toBeUndefined();
    expect(w.emitted("change")).toBeUndefined();
    expect((input.element as HTMLInputElement).value).toBe("90");
    expect(w.find(".q-number-hint").text()).toContain("不能小于 0");
    w.unmount();
  });

  it("显式 0 是合法值：emit 0 并触发一次 change", async () => {
    const w = mount(QNumber, { props: { modelValue: 90, min: 0, max: 3650, required: true } });
    const input = w.find("input");
    await input.setValue("0");
    await input.trigger("blur");
    const ev = w.emitted("update:modelValue");
    expect(ev?.[ev.length - 1]).toEqual([0]);
    expect(w.emitted("change")?.length).toBe(1);
    expect((input.element as HTMLInputElement).value).toBe("0");
    expect(w.find(".q-number-hint").exists()).toBe(false);
    w.unmount();
  });

  it("高于上限仍按既有步进器契约收口后提交（不是静默丢弃）", async () => {
    const w = mount(QNumber, { props: { modelValue: 5, min: 0, max: 30, required: true } });
    const input = w.find("input");
    await input.setValue("99");
    await input.trigger("blur");
    const ev = w.emitted("update:modelValue");
    expect(ev?.[ev.length - 1]).toEqual([30]);
    expect(w.emitted("change")?.length).toBe(1);
    expect((input.element as HTMLInputElement).value).toBe("30");
    w.unmount();
  });
});