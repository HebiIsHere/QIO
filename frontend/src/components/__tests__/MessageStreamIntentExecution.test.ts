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

/**
 * jsdom 没有布局：把虚拟列表换成「按 count 全量渲染」的确定性实现，
 * 只为了验证消息流里的渲染分支（滚动、测量等行为由既有 MessageStream.test.ts 覆盖）。
 */
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
import { api } from "../../services/api";

const RECORD: IntentExecutionRecord = {
  record_key: "ie:sub_123:int_a",
  record_head: true,
  title: "整理这组资料",
  status: "running",
  status_label: "正在处理",
  scope: { board_name: "研究板面", intent_titles: ["整理这组资料"], source: "confirm" },
  intents: [],
  attempts: [],
  verified: true,
};

const INTERNAL_TEXT = "【互动意图执行】内部工作文字，不应出现在对话流里。";

async function mountStream() {
  const pinia = createPinia();
  setActivePinia(pinia);
  const w = mount(MessageStream, { global: { plugins: [pinia] } });
  await nextTick();
  return { w, session: useSessionStore() };
}

describe("MessageStream 意图执行记录（历史与实时共用）", () => {
  it("记录首次进入消息流时立即渲染成卡片，内部文字不显示", async () => {
    const { w, session } = await mountStream();
    session.pushMessage({
      role: "user",
      content: INTERNAL_TEXT,
      contentType: "intent_execution",
      intentExecution: RECORD,
    });
    await nextTick();
    expect(w.find('[data-test="intent-execution"]').exists()).toBe(true);
    expect(w.find(".user-bubble").exists()).toBe(false);
    expect(w.text()).not.toContain(INTERNAL_TEXT);
    w.unmount();
  });

  it("同一 record 的非头部消息（自动重试）不刷屏，只渲染头部一条", async () => {
    const { w, session } = await mountStream();
    session.pushMessage({
      role: "user",
      content: INTERNAL_TEXT + " 重试 2",
      contentType: "intent_execution",
      intentExecution: { ...RECORD, record_head: false, status: "failed", status_label: "处理失败" },
    });
    session.pushMessage({
      role: "user",
      content: "用户自己输入的问题",
      contentType: "text",
    });
    await nextTick();
    expect(w.findAll('[data-test="intent-execution"]')).toHaveLength(0);
    expect(w.find(".user-bubble").text()).toContain("用户自己输入的问题");
    expect(w.text()).not.toContain("内部工作文字");
    w.unmount();
  });

  it("普通用户代码消息保持既有展示，不被记录渲染影响", async () => {
    const { w, session } = await mountStream();
    session.pushMessage({
      role: "user",
      content: "def add(a, b):\n    return a + b",
      contentType: "text",
    });
    await nextTick();
    expect(w.find(".user-bubble").text()).toContain("def add(a, b):");
    expect(w.find('[data-test="intent-execution"]').exists()).toBe(false);
    w.unmount();
  });

  it("分页后同一 record_key 在两页都带 head：只渲染一张（保留顺序最早的那条）", async () => {
    const { w, session } = await mountStream();
    const recordRow = (id: string, at: string, title: string) => ({
      id,
      role: "user",
      content: INTERNAL_TEXT,
      content_type: "intent_execution",
      created_at: at,
      turn_id: "turn_" + id,
      intent_execution: { ...RECORD, title, record_head: true },
    });
    vi.mocked(api.getSessionContext).mockResolvedValueOnce({
      topic_id: "t1",
      topic_name: "话题",
      anchor_fragment: null,
      messages: [recordRow("m2", "2026-10-29T10:05:00+08:00", "第二次确认")],
      has_more: true,
      next_before: "c2|m2",
    } as never);
    vi.mocked(api.getSessionMessagesBefore).mockResolvedValueOnce({
      messages: [recordRow("m1", "2026-10-29T10:00:00+08:00", "第一次确认")],
      has_more: false,
      next_before: null,
    } as never);

    await session.loadHistory();
    await nextTick();
    // 最新一页先读到（它自己就是这一页的 head）
    expect(w.findAll('[data-test="intent-execution"]')).toHaveLength(1);

    // 向上翻页拿到更早的 head：整条消息流里仍然只有一张卡，且是顺序最早的那条
    await session.loadOlderHistory();
    await nextTick();
    const cards = w.findAll('[data-test="intent-execution"]');
    expect(cards).toHaveLength(1);
    expect(cards[0].text()).toContain("第一次确认");
    expect(w.text()).not.toContain("第二次确认");
    w.unmount();
  });
});
