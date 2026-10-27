/**
 * uf2 · F02：通知点击的任务落点（公共约定 §3.3）。
 *
 * 缺陷复现：旧 TopNotice 只展开左栏 + 循环 focusTask，没有保证用户此刻真的能看到
 * 任务列表（可能在设置页 / 星球覆盖层 / 恢复收件箱 / 窄窗收起），也可能在有中央弹窗时
 * 直接导航并把通知关掉。这里按契约写失败用例：blocked 不导航、跨页先让列表可见、
 * 再 focusTasks 整组目标；任何一步失败都必须返回可展示说明。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { flushPromises } from "@vue/test-utils";

const routerState = vi.hoisted(() => ({
  currentRoute: { value: { name: "settings" as string | null, path: "/settings" } },
  push: vi.fn(async (_target: unknown) => {
    routerState.currentRoute.value = { name: "conversation", path: "/" };
  }),
}));

vi.mock("../../router", () => ({ default: routerState }));

const api = vi.hoisted(() => ({
  fetchTaskFeed: vi.fn(),
  fetchTaskDetail: vi.fn(),
  acknowledgeTask: vi.fn(),
  requestTaskCancel: vi.fn(),
}));

vi.mock("../taskFeedApi", () => api);

import {
  currentNoticeNavigationHost,
  openTaskNoticeTargets,
  registerNoticeNavigationHost,
  unregisterNoticeNavigationHost,
} from "../noticeNavigation";
import { useConversationTasksStore } from "../../stores/conversationTasks";
import { useConversationShellStore } from "../../stores/conversationShell";
import { useModalArbiterStore } from "../../stores/modalArbiter";
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

function page(items: TaskFeedItem[], overrides: Partial<TaskFeedPage> = {}): TaskFeedPage {
  return {
    items,
    nextCursor: null,
    counts: { ...ZERO_COUNTS },
    serverTime: "2026-10-27T10:00:00.000Z",
    instanceId: "inst-1",
    ...overrides,
  };
}

function item(id: string): TaskFeedItem {
  const colon = id.indexOf(":");
  return {
    id,
    source: id.slice(0, colon) as TaskFeedItem["source"],
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
  };
}

let hostVisible = true;
const host = vi.fn(async () => hostVisible);

function feed(current: TaskFeedPage | Error, history?: TaskFeedPage | Error) {
  api.fetchTaskFeed.mockImplementation((opts: { view: TaskView }) => {
    const target = opts.view === "current" ? current : (history ?? page([]));
    if (target instanceof Error) return Promise.reject(target);
    return Promise.resolve(target);
  });
}

function tasksStore() {
  return useConversationTasksStore();
}

beforeEach(() => {
  setActivePinia(createPinia());
  vi.clearAllMocks();
  routerState.currentRoute.value = { name: "settings", path: "/settings" };
  hostVisible = true;
  host.mockClear();
  registerNoticeNavigationHost(null);
  api.fetchTaskDetail.mockResolvedValue(null);
  api.fetchTaskFeed.mockResolvedValue(page([]));
});

afterEach(() => {
  registerNoticeNavigationHost(null);
  tasksStore().dispose();
  useConversationShellStore().dispose();
});

describe("uf2 通知落点：前置守卫", () => {
  it("uf2- 没有任务目标：返回 no-targets，不导航也不动任何界面", async () => {
    registerNoticeNavigationHost({ ensureTaskListVisible: host });
    const result = await openTaskNoticeTargets(["", "   "]);

    expect(result.ok).toBe(false);
    expect(result.reason).toBe("no-targets");
    expect(result.taskIds).toEqual([]);
    expect(result.message).toContain("没有可定位的任务");
    expect(routerState.push).not.toHaveBeenCalled();
    expect(host).not.toHaveBeenCalled();
  });

  it("uf2- 有中央弹窗：返回 blocked-by-modal，不导航、不关弹窗、不丢通知", async () => {
    registerNoticeNavigationHost({ ensureTaskListVisible: host });
    useModalArbiterStore().open({ id: "approval-1", kind: "approval" });
    feed(page([item("tool:fail")]));

    const result = await openTaskNoticeTargets(["tool:fail"]);

    expect(result.ok).toBe(false);
    expect(result.reason).toBe("blocked-by-modal");
    expect(result.taskIds).toEqual(["tool:fail"]);
    expect(result.message).toContain("弹窗");
    expect(routerState.push).not.toHaveBeenCalled();
    expect(host).not.toHaveBeenCalled();
    // 弹窗仍在、任务也没有被定位跳转
    expect(useModalArbiterStore().hasModal).toBe(true);
    expect(tasksStore().focusTargets).toEqual([]);
  });
});

describe("uf2 通知落点：跨页到可见任务列表", () => {
  it("uf2- 从设置页点击：切对话路由 → 页面宿主保证列表可见 → focusTasks 定位整组目标", async () => {
    registerNoticeNavigationHost({ ensureTaskListVisible: host });
    feed(page([item("tool:a"), item("tool:b")]));

    const result = await openTaskNoticeTargets([" tool:a ", "tool:b", "tool:a"]);

    expect(routerState.push).toHaveBeenCalledTimes(1);
    expect(routerState.push.mock.calls[0][0]).toMatchObject({ name: "conversation" });
    expect(host).toHaveBeenCalledTimes(1);
    expect(result.ok).toBe(true);
    expect(result.reason).toBe("ok");
    expect(result.taskIds).toEqual(["tool:a", "tool:b"]);

    const tasks = tasksStore();
    await flushPromises();
    expect(tasks.focusTargets).toEqual(["tool:a", "tool:b"]);
    expect(tasks.isExpanded("current", "tool:a")).toBe(true);
    expect(tasks.isExpanded("current", "tool:b")).toBe(true);
  });

  it("uf2- 已经在对话页：不重复 push，但仍调用页面宿主并定位", async () => {
    routerState.currentRoute.value = { name: "conversation", path: "/" };
    registerNoticeNavigationHost({ ensureTaskListVisible: host });
    feed(page([item("tool:fail")]));

    const result = await openTaskNoticeTargets(["tool:fail"]);
    expect(routerState.push).not.toHaveBeenCalled();
    expect(host).toHaveBeenCalledTimes(1);
    expect(result.ok).toBe(true);
    expect(tasksStore().focusTargets).toEqual(["tool:fail"]);
  });

  it("uf2- 对话页懒加载、宿主稍后才注册：等服务期间仍能成功定位", async () => {
    routerState.push.mockImplementationOnce(async () => {
      routerState.currentRoute.value = { name: "conversation", path: "/" };
      setTimeout(() => registerNoticeNavigationHost({ ensureTaskListVisible: host }), 30);
    });
    feed(page([item("tool:late")]));

    const result = await openTaskNoticeTargets(["tool:late"]);
    expect(result.ok).toBe(true);
    expect(host).toHaveBeenCalledTimes(1);
    expect(tasksStore().focusTargets).toEqual(["tool:late"]);
  });
});

describe("uf2 通知落点：失败必须说出来，不静默成功", () => {
  it("uf2- 路由切换失败：navigation-failed，不继续定位", async () => {
    registerNoticeNavigationHost({ ensureTaskListVisible: host });
    routerState.push.mockRejectedValueOnce(new Error("router boom"));

    const result = await openTaskNoticeTargets(["tool:fail"]);
    expect(result.ok).toBe(false);
    expect(result.reason).toBe("navigation-failed");
    expect(result.message).toContain("没能切换");
    expect(host).not.toHaveBeenCalled();
    expect(tasksStore().focusTargets).toEqual([]);
  });

  it("uf2- 页面宿主判断列表不可见：navigation-failed，不假装定位成功", async () => {
    hostVisible = false;
    registerNoticeNavigationHost({ ensureTaskListVisible: host });
    feed(page([item("tool:fail")]));

    const result = await openTaskNoticeTargets(["tool:fail"]);
    expect(result.ok).toBe(false);
    expect(result.reason).toBe("navigation-failed");
    expect(result.message).toContain("任务列表");
    expect(tasksStore().focusTargets).toEqual([]);
  });

  it("uf2- 页面宿主抛错：navigation-failed，保留任务身份", async () => {
    registerNoticeNavigationHost({
      ensureTaskListVisible: async () => {
        throw new Error("drawer boom");
      },
    });

    const result = await openTaskNoticeTargets(["tool:fail"]);
    expect(result.ok).toBe(false);
    expect(result.reason).toBe("navigation-failed");
    expect(result.taskIds).toEqual(["tool:fail"]);
    expect(result.message).toContain("打开任务列表时出错");
  });

  it("uf2- 目标都没有找到：把 focusError 原样作为可展示说明返回", async () => {
    registerNoticeNavigationHost({ ensureTaskListVisible: host });
    feed(page([]), page([]));

    const result = await openTaskNoticeTargets(["tool:gone"]);
    expect(result.ok).toBe(false);
    expect(result.reason).toBe("navigation-failed");
    expect(result.message).toContain("没有找到");
    expect(result.message).toContain("任务列表");
  });

  it("uf2- 没有页面宿主时兜底检查左栏：宽窗可见照常定位；窄窗收起则如实失败", async () => {
    routerState.currentRoute.value = { name: "conversation", path: "/" };
    feed(page([item("tool:ok")]));

    // 宽窗、左栏可见（默认）→ 不需要宿主也能给出真实落点
    const ok = await openTaskNoticeTargets(["tool:ok"]);
    expect(ok.ok).toBe(true);

    // 窄窗：左栏临时收起，没有页面宿主打开抽屉 → 不能声称列表可见
    const shell = useConversationShellStore();
    shell.viewportWidth = 1000;
    const narrow = await openTaskNoticeTargets(["tool:ok"]);
    expect(narrow.ok).toBe(false);
    expect(narrow.reason).toBe("navigation-failed");
    expect(narrow.message).toContain("任务列表");
  });
});

describe("uf2 通知落点：宿主注册表", () => {
  it("uf2- 卸载只移除自己注册的宿主，不会误清新注册的", () => {
    const first = { ensureTaskListVisible: host };
    const second = { ensureTaskListVisible: host };
    registerNoticeNavigationHost(first);
    registerNoticeNavigationHost(second);
    expect(currentNoticeNavigationHost()).toBe(second);

    unregisterNoticeNavigationHost(first);
    expect(currentNoticeNavigationHost()).toBe(second);

    unregisterNoticeNavigationHost(second);
    expect(currentNoticeNavigationHost()).toBeNull();
  });
});
