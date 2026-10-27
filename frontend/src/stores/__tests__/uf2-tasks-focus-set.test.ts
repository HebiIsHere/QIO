/**
 * uf2 · F02：一组任务的定位（公共约定 §3.3）。
 *
 * 缺陷复现：通知里合并了多个失败任务时，旧实现只能 focusTask 单个；TopNotice 循环
 * 调用还会让定位互相覆盖（最后只剩一条可找）。这里按集合契约写失败用例：
 * 全部目标展开/高亮、滚动第一个；找不到时 focusError 必须说明，不静默成功。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
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
import { useSessionStore } from "../session";
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

function item(id: string, overrides: Partial<TaskFeedItem> = {}): TaskFeedItem {
  const colon = id.indexOf(":");
  return {
    id,
    source: (id.slice(0, colon) as TaskFeedItem["source"]) || "tool",
    sourceId: id.slice(colon + 1),
    title: "任务 " + id,
    status: "failed",
    rawStatus: "failed",
    origin: "topic",
    topicId: "t1",
    topicTitle: "布局讨论",
    executor: null,
    summary: "",
    queuedAt: null,
    startedAt: "2026-10-27T09:00:00.000Z",
    finishedAt: "2026-10-27T09:30:00.000Z",
    durationMs: 1_800_000,
    requiresAttention: true,
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

beforeEach(() => {
  setActivePinia(createPinia());
  vi.clearAllMocks();
  api.fetchTaskDetail.mockResolvedValue(null);
});

afterEach(() => {
  store?.dispose();
  store = null;
  useConversationShellStore().dispose();
});

describe("uf2 focusTasks 集合定位", () => {
  it("uf2- 全部目标都展开并定位，不截断、不只剩最后一个", async () => {
    feed(page([1, 2, 3, 4, 5].map((n) => item("tool:t" + n))));
    const tasks = makeStore();
    await tasks.refresh("current");

    const ids = ["tool:t1", "tool:t3", "tool:t5"];
    await tasks.focusTasks(ids);

    expect(tasks.focusError).toBe("");
    expect(tasks.focusTargets).toEqual(ids);
    expect(tasks.focusTarget?.id).toBe("tool:t1");
    for (const id of ids) expect(tasks.isExpanded("current", id)).toBe(true);
    expect(useConversationShellStore().prefs.taskTab).toBe("current");
  });

  it("uf2- 任务列表已加载但目标缺失：已找到的照常定位，focusError 说明缺几项", async () => {
    feed(page([item("tool:found")]));
    const tasks = makeStore();
    await tasks.refresh("current");

    await tasks.focusTasks(["tool:found", "tool:gone"]);

    expect(tasks.isExpanded("current", "tool:found")).toBe(true);
    expect(tasks.focusTarget?.id).toBe("tool:found");
    expect(tasks.focusTargets).toEqual(["tool:found"]);
    expect(tasks.focusError).toContain("1 项任务没有找到");
  });

  it("uf2- 全部目标都找不到：刷新快照后仍然没有 → focusError 说明，不静默成功", async () => {
    feed(page([]));
    const tasks = makeStore();
    await tasks.refresh("current");

    await tasks.focusTasks(["tool:none"]);
    expect(tasks.focusTarget).toBeNull();
    expect(tasks.focusTargets).toEqual([]);
    expect(tasks.focusError).toContain("没有找到");
  });

  it("uf2- 任务列表接口不可用：focusError 说明「暂不可用」，不假装定位成功", async () => {
    const notFound = Object.assign(new Error("tasks -> 404"), { status: 404 });
    // 当前/历史都读不到，才算任务列表整体不可用。
    feed(notFound, notFound);
    const tasks = makeStore();

    await tasks.focusTasks(["tool:1"]);
    expect(tasks.focusTarget).toBeNull();
    expect(tasks.focusError).toContain("任务列表暂不可用");
  });

  it("uf2- 目标在历史视图：切到历史并把历史里的全部目标展开", async () => {
    feed(page([]), page([item("tool:h1"), item("tool:h2")]));
    const tasks = makeStore();
    await tasks.refresh("history");

    await tasks.focusTasks(["tool:h1", "tool:h2"]);
    expect(useConversationShellStore().prefs.taskTab).toBe("history");
    expect(tasks.isExpanded("history", "tool:h1")).toBe(true);
    expect(tasks.isExpanded("history", "tool:h2")).toBe(true);
    expect(tasks.focusTargets).toEqual(["tool:h1", "tool:h2"]);
    expect(tasks.focusTarget?.id).toBe("tool:h1");
    expect(tasks.focusError).toBe("");
  });

  it("uf2- 目标同时分布在当前与历史：当前视图可见并优先定位，历史目标也保持展开", async () => {
    feed(page([item("tool:c1")]), page([item("tool:h1")]));
    const tasks = makeStore();
    await tasks.refresh("current");
    await tasks.refresh("history");

    await tasks.focusTasks(["tool:h1", "tool:c1"]);
    expect(useConversationShellStore().prefs.taskTab).toBe("current");
    expect(tasks.isExpanded("current", "tool:c1")).toBe(true);
    expect(tasks.isExpanded("history", "tool:h1")).toBe(true);
    expect(tasks.focusTargets).toEqual(["tool:c1"]);
    expect(tasks.focusError).toBe("");
  });

  it("uf2- focusTask(id) 等价 focusTasks([id])（单目标旧行为保留）", async () => {
    feed(page([item("tool:single")]));
    const tasks = makeStore();
    await tasks.refresh("current");

    await tasks.focusTask("tool:single");
    expect(tasks.focusTargets).toEqual(["tool:single"]);
    expect(tasks.isExpanded("current", "tool:single")).toBe(true);
    expect(tasks.focusError).toBe("");
  });

  it("uf2- 恢复事项按 record_id 定位到真实行身份（recovery:<id>）", async () => {
    const session = useSessionStore();
    session.recoveryRecords = [
      {
        record_id: "rec-1",
        kind: "user_turn",
        state_class: "ready",
        status: "interrupted",
        message: "把这条消息重新发出去",
        topic_id: "t1",
        reason: "上一次没有执行完",
        created_at: "2026-10-27T08:00:00Z",
        updated_at: null,
        owner_instance_id: null,
        owner_state: "dead",
        owner_note: "",
        claim_generation: null,
        attempts: 1,
        last_error: null,
        actions: [{ id: "continue", label: "继续", enabled: true, reason: "" }],
      } as never,
    ];
    feed(page([]));
    const tasks = makeStore();
    await tasks.refresh("current");

    await tasks.focusTasks(["rec-1"]);
    expect(tasks.focusTargets).toEqual(["recovery:rec-1"]);
    expect(tasks.focusTarget?.id).toBe("recovery:rec-1");
    expect(tasks.isExpanded("current", "recovery:rec-1")).toBe(true);
    expect(tasks.focusError).toBe("");
  });

  it("uf2- 空列表/空 id 不改变现有定位状态", async () => {
    feed(page([item("tool:1")]));
    const tasks = makeStore();
    await tasks.refresh("current");
    await tasks.focusTasks(["tool:1"]);
    const seq = tasks.focusTarget?.seq;

    await tasks.focusTasks([]);
    await tasks.focusTasks(["  "]);
    expect(tasks.focusTarget?.seq).toBe(seq);
  });
});
