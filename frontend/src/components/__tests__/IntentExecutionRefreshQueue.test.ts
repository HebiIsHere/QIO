import { describe, expect, it, vi, beforeEach } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { nextTick } from "vue";
import type { IntentExecutionRecord } from "../../services/api";

/**
 * 执行记录更新（第三轮返修）：过程状态读取尚未返回时收到「执行结束」通知，
 * 不能直接丢弃这次刷新要求；当前读取结束后应补读一次最新状态。
 *
 * 触发顺序：续作轮 STAGE 事件触发过程刷新（历史读取在途）→ TURN_END 到达。
 * 旧实现：TURN_END 的刷新请求因为「已经有一次读取在跑」被直接丢弃 → 卡片停在
 * 「正在处理」，直到用户手动刷新。修复：把要求记下来，当前读取结束后补读一次
 * （同一话题、无轮询）；话题已经换了就丢弃这次要求，避免旧响应覆盖新话题。
 */

vi.mock("../../services/api", () => ({
  api: {
    getSessionContext: vi.fn(),
    getSessionMessagesBefore: vi.fn(async () => ({ messages: [], has_more: false, next_before: null })),
    sendTurn: vi.fn(async () => ({ ok: true })),
    getDevTasks: vi.fn(async () => ({ tasks: [] })),
    listDevAuthorizations: vi.fn(async () => ({ authorizations: [] })),
  },
}));

vi.mock("../../services/interactive", () => ({
  fetchBoards: vi.fn(async () => ({ boards: [] })),
  fetchBoardState: vi.fn(async () => {
    throw new Error("板面接口不在本用例范围");
  }),
  fetchVisibleRange: vi.fn(async () => ({ visibleRange: [] })),
  fetchIntents: vi.fn(async () => ({ intents: [], conflicts: [], batchAvailable: false, recovery: { paused: [] } })),
  fetchIntentViews: vi.fn(async () => ({ available: false, intents: [], reason: "本用例不读意图视图" })),
  fetchExecutions: vi.fn(async () => ({ executions: [] })),
}));

vi.mock("@tanstack/vue-virtual", async () => {
  const { computed } = await import("vue");
  return {
    useVirtualizer: (options: { value?: { count?: number } }) =>
      computed(() => {
        const count = Number(options.value?.count ?? 0);
        return {
          getTotalSize: () => count * 180,
          getVirtualItems: () =>
            Array.from({ length: count }, (_, index) => ({ index, key: index, start: index * 180, size: 180 })),
          measureElement: () => undefined,
        };
      }),
  };
});

import { api } from "../../services/api";
import MessageStream from "../MessageStream.vue";
import { useSessionStore } from "../../stores/session";
import { useEventStore } from "../../stores/events";

const RUNNING: IntentExecutionRecord = {
  record_key: "ie:sub_1:int_a",
  record_head: true,
  title: "整理这组资料",
  status: "running",
  status_label: "正在处理",
  intents: [],
  attempts: [],
  verified: true,
};

const COMPLETED: IntentExecutionRecord = {
  ...RUNNING,
  status: "completed",
  status_label: "已完成",
};

const INTERNAL_TEXT = "【互动意图执行】内部工作文字，不应出现在对话流里。";

function historyPage(messages: unknown[], extra: Record<string, unknown> = {}) {
  return {
    topic_id: "t1",
    topic_name: "话题",
    anchor_fragment: null,
    messages,
    has_more: false,
    next_before: null,
    turn_facts: {},
    ...extra,
  };
}

function recordRow(id: string, at: string, record: IntentExecutionRecord) {
  return {
    id,
    role: "user",
    content: INTERNAL_TEXT,
    content_type: "intent_execution",
    created_at: at,
    turn_id: "turn_1",
    intent_execution: record,
  };
}

function ev(type: string, data: Record<string, unknown>) {
  return { type, id: type + "_" + Math.random().toString(36).slice(2, 10), ts: "", data };
}

interface Deferred<T> {
  promise: Promise<T>;
  resolve: (value: T) => void;
  reject: (error: unknown) => void;
}

function deferred<T>(): Deferred<T> {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

function setup() {
  const pinia = createPinia();
  setActivePinia(pinia);
  return { pinia, session: useSessionStore(), events: useEventStore() };
}

beforeEach(() => {
  vi.resetAllMocks();
  vi.mocked(api.getSessionMessagesBefore).mockResolvedValue({
    messages: [],
    has_more: false,
    next_before: null,
  } as never);
  vi.mocked(api.sendTurn).mockResolvedValue({ ok: true } as never);
  vi.mocked(api.getDevTasks).mockResolvedValue({ tasks: [] } as never);
  vi.mocked(api.listDevAuthorizations).mockResolvedValue({ authorizations: [] } as never);
});

describe("执行记录更新：读取在途时收到结束通知不丢", () => {
  it("过程读取未返回时 TURN_END 到达：当前读取结束后自动补读并更新真实终态", async () => {
    const { pinia, session, events } = setup();
    vi.mocked(api.getSessionContext).mockResolvedValueOnce(
      historyPage([recordRow("m1", "2026-10-29T10:00:00+08:00", RUNNING)]) as never,
    );
    await session.loadHistory();
    const w = mount(MessageStream, { global: { plugins: [pinia] } });
    expect(w.text()).toContain("正在处理");
    expect(api.getSessionContext).toHaveBeenCalledTimes(1);

    // 服务端发起的续作轮：TURN_START 不带本机 request_id
    events.route(ev("TURN_START", { turn_id: "turn_1", revision: 1 }));

    // 过程刷新：历史读取挂起（模拟服务端慢）
    const slow = deferred<ReturnType<typeof historyPage>>();
    vi.mocked(api.getSessionContext).mockImplementationOnce(() => slow.promise as never);
    events.route(
      ev("STAGE", {
        turn_id: "turn_1",
        stage_id: "s1",
        index: 0,
        status: "running",
        name: "准备",
        text: "",
        kind: "",
        op: "",
        call_ids: [],
      }),
    );
    await flushPromises();
    expect(api.getSessionContext).toHaveBeenCalledTimes(2);

    // 结束通知在过程读取返回之前到达：这次刷新要求不能被丢掉
    events.route(ev("TURN_END", { turn_id: "turn_1", status: "completed", final_content: null }));
    await flushPromises();
    // 仍在读：还没有发起第三次读取（不并发打接口）
    expect(api.getSessionContext).toHaveBeenCalledTimes(2);

    // 过程读取返回（仍是进行中）→ 结束后自动补读一次真实终态
    vi.mocked(api.getSessionContext).mockResolvedValueOnce(
      historyPage([recordRow("m1", "2026-10-29T10:00:00+08:00", COMPLETED)]) as never,
    );
    slow.resolve(historyPage([recordRow("m1", "2026-10-29T10:00:00+08:00", RUNNING)]));
    await flushPromises();
    await flushPromises();
    await nextTick();

    expect(api.getSessionContext).toHaveBeenCalledTimes(3);
    expect(w.text()).toContain("已完成");
    expect(w.text()).not.toContain("正在处理");
    expect(w.findAll('[data-test="intent-execution"]')).toHaveLength(1);

    // 没有轮询：补读完成后再等两拍也不会再打接口
    await flushPromises();
    await nextTick();
    expect(api.getSessionContext).toHaveBeenCalledTimes(3);
    w.unmount();
  });

  it("补读要求发起后话题已换：丢弃这次要求，不覆盖新话题", async () => {
    const { session, events } = setup();
    vi.mocked(api.getSessionContext).mockResolvedValueOnce(
      historyPage([recordRow("m1", "2026-10-29T10:00:00+08:00", RUNNING)]) as never,
    );
    await session.loadHistory();
    events.route(ev("TURN_START", { turn_id: "turn_1", revision: 1 }));

    const slow = deferred<ReturnType<typeof historyPage>>();
    vi.mocked(api.getSessionContext).mockImplementationOnce(() => slow.promise as never);
    events.route(
      ev("STAGE", {
        turn_id: "turn_1",
        stage_id: "s1",
        index: 0,
        status: "running",
        name: "准备",
        text: "",
        kind: "",
        op: "",
        call_ids: [],
      }),
    );
    await flushPromises();

    // 读取在途时 TURN_END 到达 → 记下「需要补读」，然后用户切到另一个话题
    events.route(ev("TURN_END", { turn_id: "turn_1", status: "completed", final_content: null }));
    await flushPromises();
    session.currentTopicId = "t2";

    slow.resolve(historyPage([recordRow("m1", "2026-10-29T10:00:00+08:00", RUNNING)]));
    await flushPromises();
    await flushPromises();

    // 话题已经变了：补读要求被丢弃（不额外打接口，也不把 t1 的内容写回来）
    expect(api.getSessionContext).toHaveBeenCalledTimes(2);
    expect(session.currentTopicId).toBe("t2");
  });
});
