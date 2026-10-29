import { describe, expect, it, vi } from "vitest";
import { mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { nextTick } from "vue";
import type { IntentExecutionRecord } from "../../services/api";

vi.mock("../../services/api", () => ({
  api: {
    sendTurn: vi.fn(async () => ({ ok: true })),
    getSessionMessagesBefore: vi.fn(async () => ({ messages: [], has_more: false, next_before: null })),
    getSessionContext: vi.fn(async () => ({
      topic_id: "t1",
      topic_name: "话题",
      anchor_fragment: null,
      messages: [],
    })),
  },
}));

/** jsdom 没有布局：与既有 MessageStream 用例同一套确定性虚拟列表替身。 */
vi.mock("@tanstack/vue-virtual", async () => {
  const { computed } = await import("vue");
  return {
    useVirtualizer: (options: { value?: { count?: number } }) =>
      computed(() => {
        const count = Number(options.value?.count ?? 0);
        return {
          getTotalSize: () => count * 180,
          getVirtualItems: () =>
            Array.from({ length: count }, (_, index) => ({
              index,
              key: index,
              start: index * 180,
              size: 180,
            })),
          measureElement: () => undefined,
        };
      }),
  };
});

import MessageStream from "../MessageStream.vue";
import { useSessionStore } from "../../stores/session";

const RECORD: IntentExecutionRecord = {
  record_key: "ie:sub_1:int_a",
  record_head: true,
  title: "整理这组资料",
  status: "completed",
  status_label: "已完成",
  scope: { board_name: "互动板面", intent_titles: ["整理这组资料"], source: "confirm" },
  intents: [],
  attempts: [],
  verified: true,
};

const INTERNAL_SUBMISSION_TEXT =
  "【互动板面提交】board_id=board_default submission_id=sub_r5 这是程序生成的提交消息。";

async function mountStream() {
  const pinia = createPinia();
  setActivePinia(pinia);
  const w = mount(MessageStream, { global: { plugins: [pinia] } });
  await nextTick();
  return { w, session: useSessionStore() };
}

describe("R5 提交轮程序消息（board_submission）不作为普通用户消息渲染", () => {
  it("board_submission 整条不渲染，内部绑定文字不出现", async () => {
    const { w, session } = await mountStream();
    session.pushMessage({
      role: "user",
      content: INTERNAL_SUBMISSION_TEXT,
      contentType: "board_submission",
    });
    await nextTick();
    expect(w.find(".message").exists()).toBe(false);
    expect(w.find(".user-bubble").exists()).toBe(false);
    expect(w.text()).not.toContain("【互动板面提交】");
    expect(w.text()).not.toContain("sub_r5");
    w.unmount();
  });

  it("历史旧消息（content_type=text）保持原样渲染，不回溯改写", async () => {
    const { w, session } = await mountStream();
    session.pushMessage({
      role: "user",
      content: INTERNAL_SUBMISSION_TEXT,
      contentType: "text",
    });
    await nextTick();
    expect(w.find(".user-bubble").text()).toContain("【互动板面提交】");
    w.unmount();
  });

  it("普通用户消息与意图执行记录仍按原样渲染（回归）", async () => {
    const { w, session } = await mountStream();
    session.pushMessage({ role: "user", content: "用户自己输入的问题", contentType: "text" });
    session.pushMessage({
      role: "user",
      content: "【互动意图执行】内部工作文字",
      contentType: "intent_execution",
      intentExecution: RECORD,
    });
    await nextTick();
    expect(w.find('[data-test="intent-execution"]').exists()).toBe(true);
    expect(w.text()).toContain("用户自己输入的问题");
    expect(w.text()).not.toContain("【互动意图执行】");
    w.unmount();
  });
});
