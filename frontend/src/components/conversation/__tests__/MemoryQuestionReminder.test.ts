/** 记忆问题提醒组件：真实数量 / 失败不消失 / 点击带定位目标 / 0 不显示。 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";

const mocks = vi.hoisted(() => ({ request: vi.fn() }));
vi.mock("../../../services/api", () => ({ request: mocks.request }));

import MemoryQuestionReminder from "../MemoryQuestionReminder.vue";

function mountReminder() {
  setActivePinia(createPinia());
  return mount(MemoryQuestionReminder);
}

beforeEach(() => {
  mocks.request.mockReset();
});

describe("记忆问题提醒", () => {
  it("有真实待回答问题时显示数量，点击带上星球 tab 与问题 id", async () => {
    mocks.request.mockResolvedValue({
      items: [
        {
          id: "mq_1",
          kind: "maintenance_question",
          status: "awaiting_answer",
          handler: "maintenance",
          title: "问题",
          question: "问题",
          knowledge: [],
          before: [],
          after: [],
          evidence: [],
          actions: ["answer", "defer"],
          created_at: "t",
          updated_at: "t",
        },
      ],
      total: 1,
      truncated: false,
      maintenance_available: true,
    });
    const w = mountReminder();
    await flushPromises();

    expect(w.text()).toContain("有 1 个问题待回答");
    await w.find('[data-test="memory-question-reminder"]').trigger("click");
    expect(w.emitted("open")?.[0]).toEqual([{ tab: "memory", itemId: "mq_1" }]);
    w.unmount();
  });

  it("读取失败时不显示成「没有问题」，点击重试而不是假装打开", async () => {
    mocks.request.mockRejectedValueOnce(new Error("后端 503"));
    const w = mountReminder();
    await flushPromises();

    expect(w.text()).toContain("无法读取");
    expect(w.text()).not.toContain("0 个");
    await w.find('[data-test="memory-question-reminder"]').trigger("click");
    expect(w.emitted("open")).toBeUndefined();
    expect(mocks.request).toHaveBeenCalledTimes(2); // 点击触发了重试
    w.unmount();
  });

  it("没有待回答问题时整条提醒不出现", async () => {
    mocks.request.mockResolvedValue({ items: [], total: 0, truncated: false, maintenance_available: true });
    const w = mountReminder();
    await flushPromises();

    expect(w.find('[data-test="memory-question-reminder"]').exists()).toBe(false);
    w.unmount();
  });
});
