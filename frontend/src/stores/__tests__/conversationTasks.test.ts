/**
 * 左侧任务栏 store（A）：权威快照、去重、置顶、完成短暂停留、ack、定位。
 *
 * 这些测试都在守「界面不会替后端说假话」：失败/未知原样保留；完成只在拿到历史
 * 最终事实时才显示；旧快照不会覆盖新状态；分页条数不会冒充总数。
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
import { useApprovalsStore } from "../approvals";
import { useSessionStore } from "../session";
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
    serverTime: "2026-10-22T10:00:00.000Z",
    instanceId: "inst-1",
    ...overrides,
  };
}

function item(overrides: Partial<TaskFeedItem> = {}): TaskFeedItem {
  return {
    id: "tool:tr_1",
    source: "tool",
    sourceId: "tr_1",
    title: "一次工具执行",
    status: "running",
    rawStatus: "running",
    origin: "topic",
    topicId: "t1",
    topicTitle: "布局讨论",
    executor: null,
    summary: "",
    queuedAt: null,
    startedAt: "2026-10-22T09:00:00.000Z",
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

beforeEach(() => {
  setActivePinia(createPinia());
  vi.clearAllMocks();
  api.acknowledgeTask.mockResolvedValue(undefined);
  api.requestTaskCancel.mockResolvedValue({ accepted: false, detail: "只有独立任务支持取消" });
  api.fetchTaskDetail.mockResolvedValue(null);
});

afterEach(() => {
  store?.dispose();
  store = null;
  vi.useRealTimers();
});

describe("取数与错误表达", () => {
  it("接口不可用时如实给出「任务列表暂不可用」，而不是空列表冒充没有任务", async () => {
    const notFound = Object.assign(new Error("tasks -> 404: not found"), { status: 404 });
    feed(notFound);
    const tasks = makeStore();
    await tasks.refresh("current");

    expect(tasks.current.loaded).toBe(false);
    expect(tasks.current.error).toContain("任务列表暂不可用");
    expect(tasks.displayItems("current")).toHaveLength(0);
  });

  it("成功后去重：同一 id 不会出现两行", async () => {
    feed(page([item({ id: "tool:1" }), item({ id: "tool:1", title: "重复行" })]));
    const tasks = makeStore();
    await tasks.refresh("current");

    expect(tasks.current.loaded).toBe(true);
    expect(tasks.current.error).toBe("");
    expect(tasks.displayItems("current")).toHaveLength(1);
  });

  it("同一实例的旧快照整份丢弃：迟到响应不会覆盖更新的状态", async () => {
    feed(page([item({ id: "tool:new" })], { serverTime: "2026-10-22T10:00:00.000Z" }));
    const tasks = makeStore();
    await tasks.refresh("current");

    feed(page([item({ id: "tool:old" })], { serverTime: "2026-10-22T09:00:00.000Z" }));
    await tasks.refresh("current");

    expect(tasks.current.items.map((row) => row.id)).toEqual(["tool:new"]);
  });

  it("后端换实例时接受新快照并清掉旧实例的展开/停留", async () => {
    feed(page([item({ id: "tool:1", status: "running" })], { instanceId: "inst-1" }));
    const tasks = makeStore();
    await tasks.refresh("current");
    tasks.expand("current", "tool:1");

    feed(
      page([item({ id: "tool:2", status: "completed", finishedAt: "2026-10-22T10:05:00.000Z" })], {
        instanceId: "inst-2",
        serverTime: "2026-10-22T09:00:00.000Z",
      }),
    );
    await tasks.refresh("current");

    expect(tasks.current.items.map((row) => row.id)).toEqual(["tool:2"]);
    expect(tasks.isExpanded("current", "tool:1")).toBe(false);
  });
});

describe("完成项短暂停留（用历史里的最终事实，不伪造完成）", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  it("运行中的任务在快照里消失、历史里已完成：先显示完成，随后自动归档", async () => {
    feed(page([item({ id: "tool:1", status: "running" })]));
    const tasks = makeStore();
    await tasks.refresh("current");

    feed(
      page([], { serverTime: "2026-10-22T10:00:01.000Z" }),
      page([item({ id: "tool:1", status: "completed", finishedAt: "2026-10-22T10:00:01.000Z" })], {
        serverTime: "2026-10-22T10:00:01.000Z",
      }),
    );
    await tasks.refresh("current");

    const shown = tasks.displayItems("current");
    expect(shown.map((row) => row.id)).toEqual(["tool:1"]);
    expect(shown[0].status).toBe("completed");

    vi.advanceTimersByTime(6000);
    expect(tasks.displayItems("current")).toHaveLength(0);
  });

  it("用户正在展开阅读的任务不会突然消失，收起后才离开当前视图", async () => {
    feed(page([item({ id: "tool:1", status: "running" })]));
    const tasks = makeStore();
    await tasks.refresh("current");
    tasks.expand("current", "tool:1");

    feed(
      page([], { serverTime: "2026-10-22T10:00:01.000Z" }),
      page([item({ id: "tool:1", status: "completed", finishedAt: "2026-10-22T10:00:01.000Z" })], {
        serverTime: "2026-10-22T10:00:01.000Z",
      }),
    );
    await tasks.refresh("current");

    vi.advanceTimersByTime(30_000);
    expect(tasks.displayItems("current").map((row) => row.id)).toContain("tool:1");

    tasks.collapse("current", "tool:1");
    expect(tasks.displayItems("current")).toHaveLength(0);
  });

  it("历史里也没有结局但用户正展开：保留最后一次后端状态并标注未确认", async () => {
    feed(page([item({ id: "tool:1", status: "running" })]));
    const tasks = makeStore();
    await tasks.refresh("current");
    tasks.expand("current", "tool:1");

    feed(
      page([], { serverTime: "2026-10-22T10:00:01.000Z" }),
      page([], { serverTime: "2026-10-22T10:00:01.000Z" }),
    );
    await tasks.refresh("current");

    const shown = tasks.displayItems("current");
    expect(shown.map((row) => row.id)).toContain("tool:1");
    // 原状态没有被改写成「完成」：只是保留了最后一次已知状态
    expect(shown[0].status).toBe("running");
    expect(tasks.retainedNoteFor("tool:1")).toContain("最终结果未确认");
  });
});

describe("动作：只有后端支持且本端接线的才有实际行为", () => {
  it("不支持的动作用户点不到（store 也拒绝执行）", async () => {
    const tasks = makeStore();
    const row = item({
      id: "tool:9",
      actions: [{ kind: "cancel", label: "申请取消", supported: false }],
    });
    await tasks.runAction(row, "cancel");
    expect(api.requestTaskCancel).not.toHaveBeenCalled();
  });

  it("「知道了」把失败项移入历史（失败原因与部分结果继续保留在后端）", async () => {
    const failed = item({
      id: "tool:f",
      status: "failed",
      rawStatus: "failed",
      requiresAttention: true,
      actions: [{ kind: "ack", label: "知道了", supported: true }],
    });
    feed(page([failed]), page([failed]));
    const tasks = makeStore();
    await tasks.refresh("current");

    feed(page([], { serverTime: "2026-10-22T10:00:01.000Z" }), page([failed]));
    await tasks.runAction(failed, "ack");

    expect(api.acknowledgeTask).toHaveBeenCalledWith("tool:f");
    expect(tasks.displayItems("current").map((row) => row.id)).not.toContain("tool:f");
    expect(tasks.feedback).toContain("历史");
    const historyCall = api.fetchTaskFeed.mock.calls.some(
      (call) => (call[0] as { view: TaskView }).view === "history",
    );
    expect(historyCall).toBe(true);
  });

  it("取消只表达「请求已提交」：不假装底层已经停止", async () => {
    const row = item({
      id: "subagent:task_1",
      source: "subagent",
      sourceId: "task_1",
      status: "running",
      actions: [{ kind: "cancel", label: "申请取消", supported: true }],
    });
    api.requestTaskCancel.mockResolvedValueOnce({ accepted: true, detail: "已提交取消请求" });
    feed(page([row]));
    const tasks = makeStore();
    await tasks.refresh("current");

    feed(page([], { serverTime: "2026-10-22T10:00:01.000Z" }), page([]));
    await tasks.runAction(row, "cancel");

    expect(api.requestTaskCancel).toHaveBeenCalledWith("subagent:task_1");
    expect(tasks.noticeFor("subagent:task_1")).toContain("可能仍在停止");
  });
});

describe("定位、计数与偏好", () => {
  it("历史里的任务：切到历史 tab 并展开定位（不打开空详情）", async () => {
    feed(page([]), page([item({ id: "tool:h", status: "completed" })]));
    const tasks = makeStore();
    await tasks.refresh("history");

    await tasks.focusTask("tool:h");
    const shell = useConversationShellStore();
    expect(shell.prefs.taskTab).toBe("history");
    expect(tasks.isExpanded("history", "tool:h")).toBe(true);
    expect(tasks.focusTarget?.id).toBe("tool:h");
  });

  it("恢复事项按 record_id 也能定位（稳定身份 recovery:<id>）", async () => {
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
        created_at: "2026-10-22T08:00:00Z",
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

    await tasks.focusTask("rec-1");
    expect(useConversationShellStore().prefs.taskTab).toBe("current");
    expect(tasks.isExpanded("current", "recovery:rec-1")).toBe(true);
  });

  it("收起入口的数量 = 需要处理任务（含恢复事项）+ 待审批；没有就 0", async () => {
    feed(page([], { counts: counts({ waitingUser: 2, running: 1 }) }));
    const tasks = makeStore();
    await tasks.refresh("current");
    expect(tasks.taskAttentionCount).toBe(2);
    expect(tasks.attentionCount).toBe(2);
    expect(tasks.activeCount).toBe(1);
    expect(tasks.badgeCount).toBe(2);
    expect(tasks.badgeTone).toBe("attention");

    useApprovalsStore().enqueue("approval-1", "tool", {}, { autoOpen: true });
    expect(tasks.attentionCount).toBe(3);

    const session = useSessionStore();
    session.recoveryRecords = [
      {
        record_id: "rec-9",
        kind: "user_turn",
        state_class: "ready",
        status: "interrupted",
        message: "待处理",
        topic_id: null,
        reason: null,
        created_at: "2026-10-22T08:00:00Z",
        updated_at: null,
        owner_instance_id: null,
        owner_state: "dead",
        owner_note: "",
        claim_generation: null,
        attempts: null,
        last_error: null,
        actions: [],
      } as never,
    ];
    expect(tasks.taskAttentionCount).toBe(3);
    expect(tasks.attentionCount).toBe(4);
  });

  it("没有需要处理但有运行中的任务：收起入口显示运行/排队数量", async () => {
    feed(page([], { counts: counts({ running: 2, queued: 1 }) }));
    const tasks = makeStore();
    await tasks.refresh("current");
    expect(tasks.badgeCount).toBe(3);
    expect(tasks.badgeTone).toBe("active");
  });
});

describe("事件驱动刷新与断线重连", () => {
  it("真实任务事件只触发一次防抖刷新（不按事件条数重复打后端）", async () => {
    vi.useFakeTimers();
    feed(page([]));
    const tasks = makeStore();
    tasks.bindEvents();
    const events = useEventStore();

    events.events.push({ type: "SUBAGENT_STATUS", id: "ev-1", ts: "", data: {} } as never);
    events.events.push({ type: "SUBAGENT_STATUS", id: "ev-2", ts: "", data: {} } as never);
    await nextTick();
    await vi.advanceTimersByTimeAsync(500);
    await flushPromises();

    const currentCalls = api.fetchTaskFeed.mock.calls.filter(
      (call) => (call[0] as { view: TaskView }).view === "current",
    );
    expect(currentCalls).toHaveLength(1);
    tasks.unbindEvents();
  });

  it("断线时提示可能不是最新；重连后清掉旧快照时间并整份重新对齐", async () => {
    feed(page([item({ id: "tool:1", status: "running" })], { serverTime: "2026-10-22T10:00:00.000Z" }));
    const tasks = makeStore();
    tasks.bindEvents();
    const events = useEventStore();
    await tasks.refresh("current");
    expect(tasks.current.items[0].status).toBe("running");

    events.connected = true;
    await nextTick();
    events.connected = false;
    await nextTick();
    expect(tasks.connection).toBe("offline");
    expect(tasks.connectionHint).toContain("可能已经变化");

    // 后端重启：新实例给出的历史终态不能被旧快照时间挡掉，也不许被重新标成 running
    feed(
      page([item({ id: "tool:1", status: "completed", finishedAt: "2026-10-22T10:05:00.000Z" })], {
        serverTime: "2026-10-22T08:00:00.000Z",
        instanceId: "inst-2",
      }),
    );
    events.connected = true;
    await nextTick();
    await flushPromises();
    expect(tasks.current.items[0].status).toBe("completed");
    tasks.unbindEvents();
  });
});
