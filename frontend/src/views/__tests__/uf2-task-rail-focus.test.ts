/**
 * uf2 · F02：任务栏对一组 focus 目标的展开/高亮/滚动。
 *
 * 缺陷复现：旧 TaskRail 只有单目标 highlightId，通知合并多个失败任务时只能高亮最后一个；
 * 这里断言集合行为，同时保留单目标既有表现。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount, type VueWrapper } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";

const api = vi.hoisted(() => ({
  fetchTaskFeed: vi.fn(),
  fetchTaskDetail: vi.fn(),
  acknowledgeTask: vi.fn(),
  requestTaskCancel: vi.fn(),
}));

vi.mock("../../services/taskFeedApi", () => api);

import TaskRail from "../../components/conversation/TaskRail.vue";
import { useConversationTasksStore } from "../../stores/conversationTasks";
import { useConversationShellStore } from "../../stores/conversationShell";
import type { TaskCounts, TaskFeedItem, TaskFeedPage } from "../../conversation/contracts";

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

function page(items: TaskFeedItem[]): TaskFeedPage {
  return {
    items,
    nextCursor: null,
    counts: { ...ZERO_COUNTS },
    serverTime: "2026-10-27T10:00:00.000Z",
    instanceId: "inst-1",
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

const scrolled: HTMLElement[] = [];
const originalScrollIntoView = Element.prototype.scrollIntoView;

function installScrollSpy() {
  scrolled.length = 0;
  Element.prototype.scrollIntoView = function scrollIntoView(this: Element) {
    scrolled.push(this as HTMLElement);
  } as typeof Element.prototype.scrollIntoView;
}

let wrapper: VueWrapper | null = null;

async function mountRail(items: TaskFeedItem[]) {
  api.fetchTaskFeed.mockResolvedValue(page(items));
  const pinia = createPinia();
  setActivePinia(pinia);
  wrapper = mount(TaskRail, { global: { plugins: [pinia] } });
  await flushPromises();
  await wrapper.vm.$nextTick();
  return wrapper;
}

function highlightedIds(w: VueWrapper): string[] {
  return w
    .findAll(".task-item.highlighted")
    .map((row) => row.element.closest("[data-task-id]")?.getAttribute("data-task-id") ?? "")
    .filter(Boolean);
}

beforeEach(() => {
  vi.clearAllMocks();
  api.fetchTaskDetail.mockResolvedValue(null);
  installScrollSpy();
});

afterEach(() => {
  wrapper?.unmount();
  wrapper = null;
  useConversationTasksStore().dispose();
  useConversationShellStore().dispose();
  Element.prototype.scrollIntoView = originalScrollIntoView;
});

describe("uf2 TaskRail 集合定位", () => {
  it("uf2- 一组目标全部展开并高亮，只滚动第一个", async () => {
    const w = await mountRail([item("tool:1"), item("tool:2"), item("tool:3")]);
    const ids = ["tool:1", "tool:3"];
    await useConversationTasksStore().focusTasks(ids);
    await flushPromises();
    await w.vm.$nextTick();

    expect(w.findAll(".task-detail")).toHaveLength(2);
    expect(highlightedIds(w).sort()).toEqual(ids.sort());
    expect(scrolled).toHaveLength(1);
    expect(scrolled[0].closest("[data-task-id]")?.getAttribute("data-task-id")).toBe("tool:1");
  });

  it("uf2- 单目标定位保持既有表现（只高亮一条、滚动它）", async () => {
    const w = await mountRail([item("tool:1"), item("tool:2")]);
    await useConversationTasksStore().focusTasks(["tool:2"]);
    await flushPromises();
    await w.vm.$nextTick();

    expect(highlightedIds(w)).toEqual(["tool:2"]);
    expect(scrolled).toHaveLength(1);
    expect(scrolled[0].closest("[data-task-id]")?.getAttribute("data-task-id")).toBe("tool:2");
  });

  it("uf2- 目标找不到时任务栏显示 focusError 说明（不是静默无反应）", async () => {
    const w = await mountRail([item("tool:1")]);
    await useConversationTasksStore().focusTasks(["tool:missing"]);
    await flushPromises();
    await w.vm.$nextTick();

    const alert = w.find('.rail-warn[role="alert"]');
    expect(alert.exists()).toBe(true);
    expect(alert.text()).toContain("没有找到");
  });
});
