/**
 * uf2 · F02：任务数据的应用级生命周期（公共约定 §3.2）。
 *
 * 缺陷复现：任务快照的事件订阅与轮询由 ConversationView 启动、也由它 dispose，
 * 于是对话页卸载后全局任务数据停更；设置/互动/调试页没有地方接续该职责。
 * 这里先写成应用级引用计数契约：首次 acquire 才建立订阅/轮询/首份快照，
 * 重复 acquire 不重复订阅，最后一个 release 才真正停。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { nextTick } from "vue";
import { flushPromises } from "@vue/test-utils";

const api = vi.hoisted(() => ({
  fetchTaskFeed: vi.fn(),
  fetchTaskDetail: vi.fn(),
  acknowledgeTask: vi.fn(),
  requestTaskCancel: vi.fn(),
}));

vi.mock("../../services/taskFeedApi", () => api);

import { useConversationTasksStore } from "../conversationTasks";
import { useConversationShellStore } from "../conversationShell";
import { useEventStore } from "../events";
import type { TaskCounts, TaskFeedItem, TaskFeedPage, TaskView } from "../../conversation/contracts";

const ZERO_COUNTS: TaskCounts = {
  queued: 0,
  running: 0,
  waitingApproval: 0,
  waitingUser: 0,
  completed: 0,
  partial: 0,
  failed: 0,
  cancelled: 0,
  budgetExhausted: 0,
  unknown: 0,
};

function counts(overrides: Partial<TaskCounts> = {}): TaskCounts {
  return { ...ZERO_COUNTS, ...overrides };
}

function page(items: TaskFeedItem[], overrides: Partial<TaskFeedPage> = {}): TaskFeedPage {
  return {
    items,
    nextCursor: null,
    counts: counts(),
    serverTime: "2026-10-27T10:00:00.000Z",
    instanceId: "inst-1",
    ...overrides,
  };
}

function item(overrides: Partial<TaskFeedItem> = {}): TaskFeedItem {
  const id = String(overrides.id ?? "logged_after_cleanup");
  const typedId = id.includes(":") ? id : "tool:" + id;
  const colon = typedId.indexOf(":");
  return {
    id: typedId,
    source: (typedId.slice(0, colon) as TaskFeedItem["source"]) || "tool",
    sourceId: typedId.slice(colon + 1),
    title: "一次工具执行",
    status: "running",
    rawStatus: "running",
    origin: "topic",
    topicId: "t1",
    topicTitle: "布局讨论",
    executor: null,
    summary: "",
    queuedAt: null,
    startedAt: "2026-10-27T09:00:00.000Z",
    finishedAt: null,
    durationMs: null,
    requiresAttention: false,
    acknowledged: false,
    output: { truncated: false, preview: "", hint: "", readMore: false },
    actions: [],
    ...overrides,
  };
}

let store: ReturnType<typeof useConversationTasksStore> | null = null;

function feed(current: TaskFeedPage | Error, history?: TaskFeedPage | Error) {
  api.fetchTaskFeed.mockImplementation((opts: { view: TaskView }) => {
    const target = opts.view === "current" ? current : (history ?? page([]));
    if (target instanceof Error) return Promise.reject(target);
    return Promise.resolve(target);
  });
}

function makeStore() {
  store = useConversationTasksStore();
  return store;
}

function currentCalls(): number {
  return api.fetchTaskFeed.mock.calls.filter(
    (call) => (call[0] as { view: TaskView }).view === "current",
  ).length;
}

beforeEach(() => {
  setActivePinia(createPinia());
  vi.clearAllMocks();
  api.fetchTaskDetail.mockResolvedValue(null);
});

afterEach(() => {
  store?.dispose();
  store = null;
  useConversationShellStore().dispose();
  vi.useRealTimers();
});

describe("uf2 应用级引用计数", () => {
  it("uf2- 未 acquire 时不建立订阅/轮询；首次 acquire 才 bind + startPolling + ensureLoaded", async () => {
    feed(page([item({ id: "tool:run" })]));
    const tasks = makeStore();
    expect(tasks.globalRefCount).toBe(0);
    expect(tasks._bound).toBe(false);
    expect(tasks._pollTimer).toBe(0);
    expect(currentCalls()).toBe(0);

    tasks.acquireGlobal();
    expect(tasks.globalRefCount).toBe(1);
    expect(tasks._bound).toBe(true);
    expect(tasks._pollTimer).not.toBe(0);
    await flushPromises();
    expect(currentCalls()).toBe(1);
    expect(tasks.current.loaded).toBe(true);
  });

  it("uf2- 重复 acquire 不重复订阅/轮询，只加计数；重复 release 到 0 才 dispose", () => {
    feed(page([item({ id: "tool:run" })]));
    const tasks = makeStore();
    tasks.acquireGlobal();
    const firstTimer = tasks._pollTimer;
    const firstWatchers = tasks._stops.length;
    expect(firstWatchers).toBeGreaterThan(0);

    tasks.acquireGlobal();
    tasks.acquireGlobal();
    expect(tasks.globalRefCount).toBe(3);
    expect(tasks._pollTimer).toBe(firstTimer);
    expect(tasks._stops.length).toBe(firstWatchers);
    expect(tasks._bound).toBe(true);

    tasks.releaseGlobal();
    tasks.releaseGlobal();
    expect(tasks.globalRefCount).toBe(1);
    expect(tasks._bound).toBe(true);
    expect(tasks._pollTimer).toBe(firstTimer);

    tasks.releaseGlobal();
    expect(tasks.globalRefCount).toBe(0);
    expect(tasks._bound).toBe(false);
    expect(tasks._pollTimer).toBe(0);
    expect(tasks._stops.length).toBe(0);
  });

  it("uf2- release 到 0 后可以再次 acquire：重新建立订阅与轮询", () => {
    feed(page([]));
    const tasks = makeStore();
    tasks.acquireGlobal();
    tasks.releaseGlobal();
    expect(tasks._bound).toBe(false);

    tasks.acquireGlobal();
    expect(tasks.globalRefCount).toBe(1);
    expect(tasks._bound).toBe(true);
    expect(tasks._pollTimer).not.toBe(0);
    expect(tasks._stops.length).toBeGreaterThan(0);
  });

  it("uf2- 计数为 0 时多余的 release 不误停、不把计数减成负数", () => {
    feed(page([]));
    const tasks = makeStore();
    tasks.releaseGlobal();
    expect(tasks.globalRefCount).toBe(0);
    expect(tasks._bound).toBe(false);

    tasks.acquireGlobal();
    tasks.releaseGlobal();
    tasks.releaseGlobal();
    expect(tasks.globalRefCount).toBe(0);
    expect(tasks._bound).toBe(false);
  });

  it("uf2- 直接进入非对话页也能建立任务基线（acquire 即加载权威快照）", async () => {
    feed(page([item({ id: "tool:failed", status: "failed", requiresAttention: true })]));
    const tasks = makeStore();
    // 没有挂载任何对话页组件，只有应用级 acquire。
    tasks.acquireGlobal();
    await flushPromises();
    expect(tasks.current.loaded).toBe(true);
    expect(tasks.current.items.map((row) => row.id)).toEqual(["tool:failed"]);
  });
});

describe("uf2 订阅/轮询数量稳定（切页不累加、最后一个 release 才停）", () => {
  it("uf2- 重复 acquire 期间事件与重连只触发一次刷新；最后一个 release 后不再刷新也不再轮询", async () => {
    vi.useFakeTimers();
    feed(page([item({ id: "tool:run" })]));
    const tasks = makeStore();
    const events = useEventStore();

    tasks.acquireGlobal();
    await vi.advanceTimersByTimeAsync(0);
    await flushPromises();
    const afterFirst = currentCalls();
    expect(afterFirst).toBe(1);

    // 重复 acquire（模拟页面级重复挂载）不增加订阅：一次重连只刷新一次
    tasks.acquireGlobal();
    events.connected = false;
    await nextTick();
    events.connected = true;
    await nextTick();
    await flushPromises();
    expect(currentCalls()).toBe(afterFirst + 1);

    // 有运行中任务时兜底轮询会刷新（每周期一次，不叠加）
    const beforePoll = currentCalls();
    await vi.advanceTimersByTimeAsync(12_000);
    await flushPromises();
    expect(currentCalls()).toBe(beforePoll + 1);

    // 两次 acquire 之后逐个 release：最后一个 release 才收口
    tasks.releaseGlobal();
    expect(tasks.globalRefCount).toBe(1);
    tasks.releaseGlobal();
    expect(tasks.globalRefCount).toBe(0);
    expect(tasks._bound).toBe(false);
    const afterRelease = currentCalls();
    events.connected = false;
    await nextTick();
    events.connected = true;
    await nextTick();
    await flushPromises();
    await vi.advanceTimersByTimeAsync(12_000);
    await flushPromises();
    expect(currentCalls()).toBe(afterRelease);
  });
});
