/**
 * R5 渲染级（fb-c）：历史恢复后，系统事实区域独立、注记 DOM 恰好一次、正文恰好一次。
 *
 * 契约 K2.3 / K2.4：字段优先、仅字段缺失才用内联拆分、并存不重复渲染；
 * 渲染层把注记放进独立「系统事实」区域，正文只出现一次。
 *
 * 运行：cd frontend; npx vitest run src/components/__tests__/fb_c_r5_annotation_dom.test.ts
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { nextTick } from "vue";

import MessageStream from "../MessageStream.vue";
import { useEventStore } from "../../stores/events";
import { useSessionStore, SYSTEM_ANNOTATION_HEADER } from "../../stores/session";

const { getSessionContext, sendTurn } = vi.hoisted(() => ({
  getSessionContext: vi.fn(),
  sendTurn: vi.fn(async () => ({
    ok: true,
    accepted: true,
    turn_id: "turn_new",
    status: "accepted",
    topic_id: "topic_1",
  })),
}));

vi.mock("../../services/api", () => ({
  api: {
    getSessionContext,
    sendTurn,
    getRuntimeState: vi.fn(async () => ({
      instance_id: "inst_r5",
      revision: 1,
      turn_queue: { instance_id: "inst_r5", revision: 1, running: null, queued: [], cancelled: [] },
      approvals: [],
      tasks: [],
      tools: [],
      narratives: [],
    })),
  },
  ApiError: class ApiError extends Error {},
}));

vi.mock("@tanstack/vue-virtual", async () => {
  const { computed } = await import("vue");
  return {
    useVirtualizer: (options: { value: { count: number } }) =>
      computed(() => ({
        getTotalSize: () => options.value.count * 400,
        getVirtualItems: () =>
          Array.from({ length: options.value.count }, (_, index) => ({
            index,
            key: index,
            start: index * 400,
          })),
        measureElement: () => undefined,
      })),
  };
});

const BODY = "这是这一轮唯一的正式回答，正文只应该出现一次。";
const NOTE =
  SYSTEM_ANNOTATION_HEADER +
  "\n· 任务 ws_x 还没有测试证据\n这几项没有通过验证，不能当作「已完成 / 可使用」。";

async function settle(): Promise<void> {
  await flushPromises();
  await nextTick();
  await nextTick();
  await new Promise((resolve) => setTimeout(resolve, 0));
  await nextTick();
}

function historyRow(content: string, raw?: string) {
  return {
    id: "m_assist",
    role: "assistant",
    content,
    content_type: "text",
    created_at: "2026-10-10T05:00:01+00:00",
    turn_id: "turn_i",
    ...(raw === undefined ? {} : { raw }),
  };
}

const USER_ROW = {
  id: "m_user",
  role: "user",
  content: "帮我核对这一轮",
  content_type: "text",
  created_at: "2026-10-10T05:00:00+00:00",
  turn_id: "turn_i",
};

async function mountAfterHistory(content: string, raw?: string) {
  const pinia = createPinia();
  setActivePinia(pinia);
  const events = useEventStore();
  const session = useSessionStore();
  // 实时先来过一遍（真实链路的形态），再模拟刷新：历史接口带回同一轮的持久化记录
  events.route({ type: "TURN_START", id: "e1", ts: "", data: { turn_id: "turn_i", revision: 1 } });
  events.route({
    type: "ASSISTANT",
    id: "e2",
    ts: "",
    data: { turn_id: "turn_i", content: BODY, interim: false, streaming: true, delta_id: "dl_i", seq: 1 },
  });
  events.route({
    type: "TURN_END",
    id: "e3",
    ts: "",
    data: {
      turn_id: "turn_i",
      status: "completed",
      final_content: BODY,
      revision: 2,
    },
  });

  getSessionContext.mockResolvedValueOnce({
    topic_id: "topic_1",
    topic_name: "默认话题",
    anchor_fragment: null,
    messages: [USER_ROW, historyRow(content, raw)],
    tool_records: [],
    turn_facts: [],
    has_more: false,
    next_before: null,
  });
  await session.loadHistory();

  const wrapper = mount(MessageStream, { global: { plugins: [pinia] } });
  await settle();
  const stream = wrapper.find(".stream").element as HTMLElement;
  Object.defineProperty(stream, "scrollHeight", { value: 1200, configurable: true });
  Object.defineProperty(stream, "clientHeight", { value: 400, configurable: true });
  await settle();
  return { wrapper, session };
}

beforeEach(() => {
  localStorage.clear();
  getSessionContext.mockReset();
  sendTurn.mockClear();
});

describe("R5 渲染：注记恰好一次、正文恰好一次、区域独立", () => {
  it("新记录（raw.annotation）：刷新后系统事实区域仍在", async () => {
    const { wrapper } = await mountAfterHistory(BODY, JSON.stringify({ annotation: NOTE }));
    const notes = wrapper.findAll("[data-test='answer-system-note']");
    expect(notes, "注记 DOM 恰好一个").toHaveLength(1);
    expect(notes[0]!.text()).toContain("任务 ws_x");
    // 正文气泡里不得混进注记
    expect(wrapper.find(".assist-bubble").text()).not.toContain("系统核对");
    const text = wrapper.text();
    expect(text.split(BODY).length - 1, "正文恰好一次").toBe(1);
    expect(text.split("—— 系统核对").length - 1, "注记恰好一次").toBe(1);
    wrapper.unmount();
  });

  it("旧内联注记：刷新后仍然只渲染一次，正文只出现一次", async () => {
    const { wrapper } = await mountAfterHistory(BODY + "\n\n" + NOTE);
    expect(wrapper.findAll("[data-test='answer-system-note']")).toHaveLength(1);
    const text = wrapper.text();
    expect(text.split(BODY).length - 1).toBe(1);
    expect(text.split("—— 系统核对").length - 1).toBe(1);
    wrapper.unmount();
  });

  it("raw 异常：正常显示正文，不制造失败提醒，也不出现系统事实块", async () => {
    const { wrapper, session } = await mountAfterHistory(BODY, "{坏掉的 raw");
    expect(wrapper.findAll("[data-test='answer-system-note']")).toHaveLength(0);
    expect(wrapper.find(".assist-bubble").text()).toContain(BODY);
    expect(session.lastError).toBeNull();
    wrapper.unmount();
  });

  it("并存且等价：刷新后注记恰好一次（优先字段）", async () => {
    const { wrapper } = await mountAfterHistory(
      BODY + "\n\n" + NOTE,
      JSON.stringify({ annotation: NOTE }),
    );
    expect(wrapper.findAll("[data-test='answer-system-note']")).toHaveLength(1);
    const text = wrapper.text();
    expect(text.split("—— 系统核对").length - 1).toBe(1);
    expect(text.split(BODY).length - 1).toBe(1);
    wrapper.unmount();
  });
});
