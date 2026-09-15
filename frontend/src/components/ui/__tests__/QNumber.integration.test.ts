import { describe, expect, it } from "vitest";
import { mount } from "@vue/test-utils";
import { defineComponent, nextTick, ref } from "vue";
import QNumber from "../QNumber.vue";

/**
 * 集成测试：用「真实父组件绑定」而不是只传 props。
 * 只传 props 时 `update:modelValue` 不会回流，父级回填掩盖了 bug：
 * 真实绑定下父级会把输入值同步回 props，QNumber 若把这次回显当成
 * 「已提交」，失焦时就不会再 emit change，设置页也就不会保存。
 */
function mountBound(initial: number | null) {
  const changes: (number | null)[] = [];
  const values: (number | null)[] = [];
  const Parent = defineComponent({
    components: { QNumber },
    setup() {
      const value = ref<number | null>(initial);
      const onChange = () => changes.push(value.value);
      return { value, onChange, values };
    },
    template: `<QNumber v-model="value" :min="1" :max="1000" @change="onChange" />`,
  });
  const w = mount(Parent);
  const input = w.find("input");
  return { w, input, changes, values };
}

describe("QNumber 与真实父组件绑定（P0：手工输入必须提交）", () => {
  it("手输范围内新值 + 失焦：emit change 一次，父级拿到新值", async () => {
    const { w, input, changes } = mountBound(128);
    // 只派发 input（真实浏览器的逐字输入），不派发 change：
    // change 在真实浏览器里是 blur 时才发生的
    (input.element as HTMLInputElement).value = "200";
    await input.trigger("input");
    await nextTick();
    await input.trigger("blur");
    expect(changes).toEqual([200]);
    expect((input.element as HTMLInputElement).value).toBe("200");
    w.unmount();
  });

  it("手输超范围值 + 失焦：clamp 后 emit change 一次", async () => {
    const { w, input, changes } = mountBound(128);
    await input.setValue("9999");
    await nextTick();
    await input.trigger("blur");
    expect(changes).toEqual([1000]);
    expect((input.element as HTMLInputElement).value).toBe("1000");
    w.unmount();
  });

  it("值没变时失焦不触发保存", async () => {
    const { w, input, changes } = mountBound(128);
    await input.trigger("blur");
    expect(changes).toEqual([]);
    w.unmount();
  });

  it("手输之后按 +/- 仍以输入框显示值为基准累加", async () => {
    const { w, input, changes } = mountBound(128);
    (input.element as HTMLInputElement).value = "200";
    await input.trigger("input");
    await nextTick();
    await w.find(".step.up").trigger("click");
    expect(changes).toEqual([201]);
    w.unmount();
  });
});
