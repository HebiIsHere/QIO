/**
 * 刷新之后：失败轮的过程区与「重试」入口必须还在（组件级）。
 *
 * 修复前红：历史恢复只有用户消息（失败轮没有 assistant 正文、facts 也没恢复），
 * turn.showProcess 为 false → 过程区消失、「重试」入口随之消失。
 *
 * 装置：api 打桩 + 虚拟滚动全部渲染（jsdom 没有布局）。
 * 运行：cd frontend; npx vitest run src/components/__tests__/FailedTurnRestore.test.ts
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { nextTick } from "vue";

import MessageStream from "../MessageStream.vue";
import { useEventStore } from "../../stores/events";
import { useSessionStore } from "../../stores/session";
import type { AgentEvent } from "../../services/events";

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

vi.mock("../../services/api", () => {
  const payload = () => ({
    tasks: [],
    messages: [],
    approvals: [],
    tools: [],
    narratives: [],
    instance_id: "inst_restore",
    turn_queue: { running: null, queued: [], cancelled: [], revision: 0, instance_id: "inst_restore" },
  });
  return {
    api: {
      getSessionContext,
      sendTurn,
      getRuntimeState: vi.fn(async () => payload()),
      getTurnQueue: vi.fn(async () => ({ running: null, queued: [], cancelled: [], revision: 0 })),
    },
    ApiError: class ApiError extends Error {},
  };
});

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

let seq = 0;
function ev(type: string, data: Record<string, unknown>): AgentEvent {
  seq += 1;
  return { type, id: "fr_" + seq, ts: new Date().toISOString(), data };
}

async function settle(): Promise<void> {
  await flushPromises();
  await nextTick();
  await nextTick();
  await new Promise((resolve) => setTimeout(resolve, 0));
  await nextTick();
}

const USER_ROW = {
  id: "msg_user_1",
  role: "user",
  content: "帮我改这个文件",
  content_type: "text",
  created_at: "2026-10-07T05:00:00+00:00",
  turn_id: "turn_failed",
};

async function mountAfterRefresh() {
  const pinia = createPinia();
  setActivePinia(pinia);
  const events = useEventStore();
  const session = useSessionStore();
  getSessionContext.mockResolvedValueOnce({
    topic_id: "topic_1",
    topic_name: "默认话题",
    anchor_fragment: null,
    messages: [USER_ROW],
    tool_records: [],
    has_more: false,
    next_before: null,
  } as never);
  await session.loadHistory();
  const wrapper = mount(MessageStream, { global: { plugins: [pinia] } });
  await settle();
  const stream = wrapper.find(".stream").element as HTMLElement;
  Object.defineProperty(stream, "scrollHeight", { value: 1200, configurable: true });
  Object.defineProperty(stream, "clientHeight", { value: 400, configurable: true });
  await settle();
  return { wrapper, session, events };
}

beforeEach(() => {
  seq = 0;
  localStorage.clear();
  getSessionContext.mockClear();
  sendTurn.mockClear();
});

describe("刷新后失败轮的入口", () => {
  it("本机记录过真实 TURN_END 事实：刷新后过程区在、原因在、「重试」可点且真的重发这一轮", async () => {
    // 刷新前：这一轮真的失败过（事实是本机从 TURN_END 记下的，不是猜的）
    const first = createPinia();
    setActivePinia(first);
    const firstEvents = useEventStore();
    firstEvents.route(ev("TURN_START", { turn_id: "turn_failed", revision: 1 }));
    firstEvents.route(
      ev("TURN_END", {
        turn_id: "turn_failed",
        status: "failed",
        reason_code: "provider_error",
        reason: "模型服务没有响应（连续 2 次）",
        stopped_by: "system",
        actions: ["retry"],
        error: "upstream 502",
      }),
    );

    const { wrapper } = await mountAfterRefresh();

    const region = wrapper.find('[data-test="turn-process"]');
    expect(region.exists(), "失败轮的过程区必须从恢复后仍然存在").toBe(true);
    expect(region.text(), "结束原因要如实显示").toContain("模型服务没有响应");
    const retry = wrapper.find('[data-test="turn-process-action-retry"]');
    expect(retry.exists(), "刷新后「重试」入口必须还在").toBe(true);

    await retry.trigger("click");
    await flushPromises();
    // 重发的是这一轮的用户消息，并带上 retry_of_turn_id（后端据此复用原轮附件）
    expect(sendTurn).toHaveBeenCalledWith("帮我改这个文件", "topic_1", [], "turn_failed");
    wrapper.unmount();
  });

  it("旧记录（没有任何事实）：过程区不硬造，也不出现「重试」按钮", async () => {
    const { wrapper } = await mountAfterRefresh();

    expect(wrapper.find('[data-test="turn-process"]').exists()).toBe(false);
    expect(wrapper.find('[data-test="turn-process-action-retry"]').exists()).toBe(false);
    // 用户消息本身仍在
    expect(wrapper.text()).toContain("帮我改这个文件");
    wrapper.unmount();
  });
});
