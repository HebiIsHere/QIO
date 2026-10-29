import { describe, expect, it, vi, beforeEach } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { nextTick } from "vue";
import type { IntentExecutionRecord } from "../../services/api";

/**
 * R2 返修二（前端）：意图执行记录
 * 1. 确认 / 恢复 / 手动重试成功后，**无需手动刷新**当前话题就能看到记录（走既有历史读取）；
 * 2. 相关 Turn 结束后刷新真实终态；服务端自动重试至少在同话题 TURN_END 后刷新一次；
 * 3. 刷新失败按既有 error 口径（不伪造记录、不弹窗），并留下「可能过期」提示位供页面重激活再读。
 *
 * 全部用 mock 的历史接口，不依赖真实后端，也不新增轮询。
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

/** 互动页确认 / 恢复动作会先刷新一批互动事实；本文件只关心它是否接上「既有历史读取」。 */
vi.mock("../../services/interactive", () => ({
  fetchBoards: vi.fn(async () => ({ boards: [{ id: "board_default", title: "研究板面" }] })),
  fetchBoardState: vi.fn(async () => {
    throw new Error("板面接口不在本用例范围");
  }),
  fetchVisibleRange: vi.fn(async () => ({ visibleRange: [] })),
  fetchIntents: vi.fn(async () => ({
    intents: [],
    conflicts: [],
    batchAvailable: false,
    recovery: { paused: [] },
  })),
  fetchIntentViews: vi.fn(async () => ({
    available: false,
    intents: [],
    reason: "本用例不读意图视图",
  })),
  fetchExecutions: vi.fn(async () => ({ executions: [] })),
}));

/** jsdom 没有布局：虚拟列表换成「按 count 全量渲染」，只验证消息流的渲染分支。 */
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

import { api } from "../../services/api";
import { fetchBoards } from "../../services/interactive";
import MessageStream from "../MessageStream.vue";
import { useSessionStore } from "../../stores/session";
import { useEventStore } from "../../stores/events";
import { useInteractiveStore } from "../../stores/interactive";

const RUNNING_RECORD: IntentExecutionRecord = {
  record_key: "ie:sub_1:int_a",
  record_head: true,
  title: "整理这组资料",
  status: "running",
  status_label: "正在处理",
  intents: [],
  attempts: [],
  verified: true,
};

const COMPLETED_RECORD: IntentExecutionRecord = {
  ...RUNNING_RECORD,
  status: "completed",
  status_label: "已完成",
  verified: true,
};

const FAILED_RECORD: IntentExecutionRecord = {
  ...RUNNING_RECORD,
  status: "failed",
  status_label: "处理失败",
  verified: true,
};

const INTERNAL_TEXT = "【互动意图执行】内部工作文字，不应出现在对话流里。";

function setup() {
  const pinia = createPinia();
  setActivePinia(pinia);
  return { pinia, session: useSessionStore(), events: useEventStore() };
}

function ev(type: string, data: Record<string, unknown>) {
  return { type, id: type + "_" + Math.random().toString(36).slice(2, 10), ts: "", data };
}

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
    turn_id: "turn_x",
    intent_execution: record,
  };
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe("确认成功后无需手动刷新即可出现记录", () => {
  it("既有历史读取返回记录后，消息流里立即出现卡片（不显示内部文字）", async () => {
    const { pinia, session } = setup();
    vi.mocked(api.getSessionContext).mockResolvedValueOnce(historyPage([]) as never);
    await session.loadHistory();

    const w = mount(MessageStream, { global: { plugins: [pinia] } });
    vi.mocked(api.getSessionContext).mockResolvedValueOnce(
      historyPage([recordRow("m1", "2026-10-29T10:00:00+08:00", RUNNING_RECORD)]) as never,
    );

    // 互动页确认 / 恢复之后：互动 store 会标记「历史可能过期」并立刻走一次既有历史读取
    session.markIntentRecordsStale();
    const ok = await session.refreshIntentExecutionRecords("intent-action");
    await nextTick();

    expect(ok).toBe(true);
    expect(api.getSessionContext).toHaveBeenCalledTimes(2);
    expect(w.find('[data-test="intent-execution"]').exists()).toBe(true);
    expect(w.text()).toContain("整理这组资料");
    expect(w.text()).toContain("正在处理");
    expect(w.text()).not.toContain(INTERNAL_TEXT);
    expect(session.intentRecordsStale).toBe(false);
    w.unmount();
  });

  it("互动 store 的确认 / 恢复刷新入口会调用既有历史读取", async () => {
    const { session } = setup();
    const interactive = useInteractiveStore();
    const refresh = vi.spyOn(session, "refreshIntentExecutionRecords").mockResolvedValue(true);

    await interactive.refreshAfterIntentAction();

    expect(session.intentRecordsStale).toBe(true);
    expect(refresh).toHaveBeenCalledWith("intent-action");
  });
});

describe("确认后记录还没落库：提示位保留，续作轮开始后再读出来", () => {
  it("确认成功立刻读一次（记录未出现）→ 续作轮首个落库后事件把记录读出来", async () => {
    const { pinia, session, events } = setup();
    vi.mocked(api.getSessionContext).mockResolvedValueOnce(historyPage([]) as never);
    await session.loadHistory();
    const w = mount(MessageStream, { global: { plugins: [pinia] } });

    // 互动 store 的动作刷新：记录可能还没落库（续作轮还在建上下文）
    vi.mocked(api.getSessionContext).mockResolvedValueOnce(historyPage([]) as never);
    session.markIntentRecordsStale();
    const ok = await session.refreshIntentExecutionRecords("intent-action");
    expect(ok).toBe(true);
    expect(w.find('[data-test="intent-execution"]').exists()).toBe(false);
    // 读不到记录不能把「可能过期」清掉，否则后面不会再自动读
    expect(session.intentRecordsStale).toBe(true);

    // 续作轮真正开跑（服务端发起，TURN_START 没有本机 request_id）
    events.route(ev("TURN_START", { turn_id: "turn_confirm", revision: 1 }));
    vi.mocked(api.getSessionContext).mockResolvedValueOnce(
      historyPage([recordRow("m1", "2026-10-29T10:00:00+08:00", RUNNING_RECORD)]) as never,
    );
    events.route(
      ev("STAGE", {
        turn_id: "turn_confirm",
        stage_id: "stage_1",
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
    await nextTick();

    expect(w.find('[data-test="intent-execution"]').exists()).toBe(true);
    expect(w.text()).toContain("正在处理");
    expect(session.intentRecordsStale).toBe(false);
    w.unmount();
  });
});

describe("Turn 结束后刷新真实状态", () => {
  it("登记过的续作轮 TURN_END：卡片状态更新为服务端终态", async () => {
    const { pinia, session, events } = setup();
    vi.mocked(api.getSessionContext).mockResolvedValueOnce(
      historyPage([recordRow("m1", "2026-10-29T10:00:00+08:00", RUNNING_RECORD)]) as never,
    );
    await session.loadHistory();
    const w = mount(MessageStream, { global: { plugins: [pinia] } });
    expect(w.text()).toContain("正在处理");

    // 服务端发起的续作轮：TURN_START 不带本机的 request_id
    events.route(ev("TURN_START", { turn_id: "turn_x", revision: 1 }));
    vi.mocked(api.getSessionContext).mockResolvedValueOnce(
      historyPage([recordRow("m1", "2026-10-29T10:00:00+08:00", COMPLETED_RECORD)]) as never,
    );
    events.route(ev("TURN_END", { turn_id: "turn_x", status: "completed", final_content: null }));
    await flushPromises();
    await nextTick();

    expect(w.text()).toContain("已完成");
    expect(w.text()).not.toContain("正在处理");
    // 只有一张卡：同一 record 的重复读取不会追加第二张
    expect(w.findAll('[data-test="intent-execution"]')).toHaveLength(1);
    w.unmount();
  });

  it("手动重试 / 自动重试轮：首个落库后的事件就把记录刷新出来（不轮询）", async () => {
    const { pinia, session, events } = setup();
    vi.mocked(api.getSessionContext).mockResolvedValueOnce(
      historyPage([recordRow("m1", "2026-10-29T10:00:00+08:00", FAILED_RECORD)]) as never,
    );
    await session.loadHistory();
    const w = mount(MessageStream, { global: { plugins: [pinia] } });
    expect(w.text()).toContain("处理失败");

    // 手动重试 = 服务端发起新一轮（本机没有发送动作）
    events.route(ev("TURN_START", { turn_id: "turn_retry", revision: 2 }));
    vi.mocked(api.getSessionContext).mockResolvedValueOnce(
      historyPage([recordRow("m1", "2026-10-29T10:00:00+08:00", RUNNING_RECORD)]) as never,
    );
    events.route(
      ev("STAGE", {
        turn_id: "turn_retry",
        stage_id: "stage_1",
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
    await nextTick();

    expect(api.getSessionContext).toHaveBeenCalledTimes(2);
    expect(w.text()).toContain("正在处理");
    w.unmount();
  });

  it("服务端自动重试轮（本机不知道 turnId）：当前话题有记录时，TURN_END 刷新真实状态", async () => {
    const { pinia, session, events } = setup();
    vi.mocked(api.getSessionContext).mockResolvedValueOnce(
      historyPage([recordRow("m1", "2026-10-29T10:00:00+08:00", FAILED_RECORD)]) as never,
    );
    await session.loadHistory();
    const w = mount(MessageStream, { global: { plugins: [pinia] } });
    expect(w.text()).toContain("处理失败");

    // 没有 TURN_START（本机不知道这轮自动重试的 id）：只凭「当前页有意图执行记录」就要刷一次
    vi.mocked(api.getSessionContext).mockResolvedValueOnce(
      historyPage([recordRow("m1", "2026-10-29T10:00:00+08:00", COMPLETED_RECORD)]) as never,
    );
    events.route(ev("TURN_END", { turn_id: "turn_reconnected", status: "completed", final_content: null }));
    await flushPromises();
    await nextTick();

    expect(w.text()).toContain("已完成");
    w.unmount();
  });

  it("本机用户在输入区发出的聊天轮：不会被当成续作轮去重读历史", async () => {
    const { session, events } = setup();
    vi.mocked(api.getSessionContext).mockResolvedValueOnce(historyPage([]) as never);
    await session.loadHistory();

    events.route(ev("TURN_START", { turn_id: "turn_chat", revision: 1, request_id: "req_1" }));
    events.route(
      ev("STAGE", {
        turn_id: "turn_chat",
        stage_id: "stage_1",
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

    expect(api.getSessionContext).toHaveBeenCalledTimes(1);
  });
});

describe("刷新失败按既有错误口径", () => {
  it("读不到就如实标错、保留旧消息、不伪造记录，也不清掉「可能过期」提示位", async () => {
    const { session } = setup();
    vi.mocked(api.getSessionContext).mockResolvedValueOnce(historyPage([]) as never);
    await session.loadHistory();

    vi.mocked(api.getSessionContext).mockRejectedValueOnce(new Error("网络断了"));
    session.markIntentRecordsStale();
    const ok = await session.refreshIntentExecutionRecords("intent-action");

    expect(ok).toBe(false);
    expect(session.history.status).toBe("error");
    expect(session.history.error).toContain("网络断了");
    // 没有本地伪造记录；提示位保留，页面重新激活时还会再读一次（不假装已更新）
    expect(session.messages).toHaveLength(0);
    expect(session.intentRecordsStale).toBe(true);
  });

  it("消息流空态把「读不到」说成读不到，而不是「没有消息」", async () => {
    const { pinia, session } = setup();
    vi.mocked(api.getSessionContext).mockResolvedValueOnce(historyPage([]) as never);
    await session.loadHistory();
    vi.mocked(api.getSessionContext).mockRejectedValueOnce(new Error("后端没起来"));
    session.markIntentRecordsStale();
    await session.refreshIntentExecutionRecords();

    const w = mount(MessageStream, { global: { plugins: [pinia] } });
    await nextTick();
    expect(w.text()).toContain("历史没有读取出来");
    expect(w.text()).not.toContain("今天想聊点什么");
    w.unmount();
  });
});

describe("板面结果入口：定位失败有一句实话（store 层）", () => {
  it("板面清单里没有这块板面：如实说不存在，不切换当前板面", async () => {
    const { session } = setup();
    void session;
    const interactive = useInteractiveStore();
    vi.mocked(fetchBoards).mockResolvedValueOnce({
      boards: [{ id: "board_other", title: "别的板面" }],
    } as never);

    const result = await interactive.selectBoard("board_gone");

    expect(result.ok).toBe(false);
    expect(result.reason).toContain("没有找到这块板面");
    expect(interactive.boardId).toBe("board_default");
  });

  it("板面清单读取失败：如实说读不到清单（不假装板面被删）", async () => {
    setup();
    const interactive = useInteractiveStore();
    vi.mocked(fetchBoards).mockRejectedValueOnce(new Error("后端没起来"));

    const result = await interactive.selectBoard("board_default");

    expect(result.ok).toBe(false);
    expect(result.reason).toContain("读取板面清单失败");
    expect(result.reason).toContain("后端没起来");
  });

  it("板面存在但加载失败：选中目标板面并如实报告加载失败", async () => {
    setup();
    const interactive = useInteractiveStore();

    const result = await interactive.selectBoard("board_default");

    expect(result.ok).toBe(false);
    expect(interactive.boardId).toBe("board_default");
    expect(result.reason).toContain("板面接口不在本用例范围");
  });
});
