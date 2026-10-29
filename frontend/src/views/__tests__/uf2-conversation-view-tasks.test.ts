/**
 * uf2 · F02：对话页不再拥有全局任务生命周期；通知点击时提供页面内可见性宿主。
 *
 * 缺陷复现：ConversationView.onMounted 启动任务事件订阅/轮询，onBeforeUnmount 调
 * tasks.dispose() —— 对话页一卸载全局任务数据就停更；设置/互动页没有接续者。
 * 这里断言：页面不再建立/销毁全局生命周期；但保留自己的幂等刷新与定位监听。
 * 另覆盖通知落点的页面宿主：关闭星球/恢复区/全窗口预览、展开左栏、窄窗抽屉不抢焦点。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount, type VueWrapper } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { nextTick } from "vue";

const feedApi = vi.hoisted(() => ({
  fetchTaskFeed: vi.fn(),
  fetchTaskDetail: vi.fn(),
  acknowledgeTask: vi.fn(),
  requestTaskCancel: vi.fn(),
}));

vi.mock("../../services/taskFeedApi", () => feedApi);
vi.mock("../../services/api", () => ({
  api: {
    getUISettings: vi.fn(async () => ({ typewriter_cps: 50 })),
    saveConversationLayout: vi.fn(async () => ({ ok: true })),
    getSessionContext: vi.fn(async () => ({
      topic_id: "t1",
      topic_name: "布局讨论",
      anchor_fragment: null,
      messages: [],
      has_more: false,
      next_before: null,
      tool_records: [],
      turn_facts: [],
    })),
  },
}));

import ConversationView from "../ConversationView.vue";
import {
  currentNoticeNavigationHost,
  registerNoticeNavigationHost,
} from "../../services/noticeNavigation";
import { useConversationTasksStore } from "../../stores/conversationTasks";
import { useConversationShellStore } from "../../stores/conversationShell";
import { FOCUS_TASK_EVENT } from "../../services/conversationTasks";
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

function page(items: TaskFeedItem[] = [], overrides: Partial<TaskFeedPage> = {}): TaskFeedPage {
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

const stubs = {
  ChatColumn: {
    name: "ChatColumn",
    template: '<div class="stub-chat"><slot name="planet" /><slot /></div>',
  },
  PreviewPanel: {
    name: "PreviewPanel",
    props: { full: { type: Boolean, default: false } },
    emits: ["close", "toggleFull"],
    template: '<div class="stub-preview" />',
  },
  UnifiedApprovalLayer: { name: "UnifiedApprovalLayer", template: "<div />" },
  RecoveryInbox: { name: "RecoveryInbox", template: '<div class="stub-recovery-inbox" />' },
  PlanetDock: {
    name: "PlanetDock",
    emits: ["open"],
    template: '<button class="stub-dock" type="button" @click="$emit(\'open\')">dock</button>',
  },
  PlanetView: {
    name: "PlanetView",
    props: { seq: { type: Number, default: 0 }, open: { type: Boolean, default: true } },
    emits: ["close"],
    template: '<div class="stub-planet" :data-seq="seq" :data-open="String(open)" />',
  },
};

function setViewport(width: number) {
  Object.defineProperty(window, "innerWidth", {
    configurable: true,
    writable: true,
    value: width,
  });
  window.dispatchEvent(new Event("resize"));
}

let wrapper: VueWrapper | null = null;

function mountView(attachTo?: HTMLElement) {
  const pinia = createPinia();
  setActivePinia(pinia);
  wrapper = mount(ConversationView, {
    global: { plugins: [pinia], stubs },
    ...(attachTo ? { attachTo } : {}),
  });
  return wrapper;
}

function tasks() {
  return useConversationTasksStore();
}

beforeEach(() => {
  feedApi.fetchTaskFeed.mockResolvedValue(page());
  feedApi.fetchTaskDetail.mockResolvedValue(null);
  setViewport(1440);
});

afterEach(() => {
  wrapper?.unmount();
  wrapper = null;
  tasks().dispose();
  useConversationShellStore().dispose();
  registerNoticeNavigationHost(null);
  setViewport(1440);
  document.body.innerHTML = "";
});

describe("uf2 对话页不拥有全局任务生命周期", () => {
  it("uf2- 对话页挂载不再自行 bindEvents/startPolling（全局订阅只由应用级 acquire 建立）", async () => {
    const w = mountView();
    await flushPromises();
    await nextTick();

    const store = tasks();
    expect(store.globalRefCount).toBe(0);
    expect(store._bound).toBe(false);
    expect(store._pollTimer).toBe(0);
    // 页面自己的幂等首读仍然发生（不是把任务数据也一起停掉）
    expect(feedApi.fetchTaskFeed).toHaveBeenCalled();
    await flushPromises();
    expect(store.current.loaded).toBe(true);
    w.unmount();
    wrapper = null;
  });

  it("uf2- 卸载对话页不会停掉应用级订阅/轮询（最后一个 release 才 dispose）", async () => {
    const store = tasks();
    store.acquireGlobal();
    const polling = store._pollTimer;
    expect(polling).not.toBe(0);
    expect(store._stops.length).toBeGreaterThan(0);

    const w = mountView();
    await flushPromises();
    w.unmount();
    wrapper = null;
    await flushPromises();

    expect(store.globalRefCount).toBe(1);
    expect(store._bound).toBe(true);
    expect(store._pollTimer).toBe(polling);
    expect(store._stops.length).toBeGreaterThan(0);
  });

  it("uf2- 页面仍保留 qio:focus-task 定位监听", async () => {
    feedApi.fetchTaskFeed.mockResolvedValue(page([item("tool:focus")]));
    const w = mountView();
    await flushPromises();
    await nextTick();

    window.dispatchEvent(new CustomEvent(FOCUS_TASK_EVENT, { detail: "tool:focus" }));
    await flushPromises();
    await nextTick();

    expect(tasks().isExpanded("current", "tool:focus")).toBe(true);
    expect(tasks().focusTargets).toEqual(["tool:focus"]);
    w.unmount();
    wrapper = null;
  });
});

describe("uf2 对话页注册通知落点宿主", () => {
  it("uf2- 挂载后宿主可被通知导航取用；卸载后注销", async () => {
    const w = mountView();
    await flushPromises();
    const host = currentNoticeNavigationHost();
    expect(host).not.toBeNull();
    w.unmount();
    wrapper = null;
    expect(currentNoticeNavigationHost()).toBeNull();
  });

  it("uf2- 页内覆盖层（星球/恢复区/全窗口预览）会先关闭，任务列表可见", async () => {
    const w = mountView();
    await flushPromises();
    const shell = useConversationShellStore();

    // 打开星球覆盖层与恢复收件箱
    await w.find(".stub-dock").trigger("click");
    await flushPromises();
    expect(w.find(".stub-planet").exists()).toBe(true);
    window.dispatchEvent(new CustomEvent("qio:open-recovery"));
    await flushPromises();
    expect(w.find(".recovery-layer").exists()).toBe(true);

    // 打开全窗口预览
    shell.prefs.previewTabs = [
      {
        id: "attachment:a1",
        target: {
          id: "attachment:a1",
          kind: "text",
          title: "方案.md",
          source: "attachment",
          attachmentId: "a1",
        },
        position: {},
      } as never,
    ];
    shell.prefs.rightOpen = true;
    shell.prefs.previewFull = true;
    await nextTick();
    expect(w.find(".preview-overlay").exists()).toBe(true);

    const visible = await currentNoticeNavigationHost()!.ensureTaskListVisible();
    await flushPromises();
    await nextTick();

    expect(visible).toBe(true);
    expect(w.find(".recovery-layer").exists()).toBe(false);
    expect(w.find(".stub-planet").attributes("data-open")).toBe("false");
    expect(w.find(".preview-overlay").exists()).toBe(false);
    expect(shell.prefs.previewFull).toBe(false);
    expect(w.find(".task-rail").exists()).toBe(true);
    w.unmount();
    wrapper = null;
  });

  it("uf2- 手动收起左栏时宿主按现有开关展开左栏（不重置用户宽度）", async () => {
    const w = mountView();
    await flushPromises();
    const shell = useConversationShellStore();
    shell.prefs.leftWidth = 360;
    shell.prefs.leftCollapsed = true;
    await nextTick();
    expect(w.find(".task-rail").exists()).toBe(false);

    const visible = await currentNoticeNavigationHost()!.ensureTaskListVisible();
    await nextTick();
    expect(visible).toBe(true);
    expect(w.find(".task-rail").exists()).toBe(true);
    expect(shell.prefs.leftWidth).toBe(360);
    w.unmount();
    wrapper = null;
  });

  it("uf2- 窄窗用现有任务抽屉保证可见落点，且不抢走当前焦点", async () => {
    setViewport(1000);
    const anchor = document.createElement("button");
    anchor.textContent = "页面上的当前焦点";
    document.body.appendChild(anchor);
    anchor.focus();
    expect(document.activeElement).toBe(anchor);

    const w = mountView(document.body);
    await flushPromises();
    await nextTick();
    expect(w.find(".rail-drawer").exists()).toBe(false);

    const visible = await currentNoticeNavigationHost()!.ensureTaskListVisible();
    await flushPromises();
    await nextTick();

    expect(visible).toBe(true);
    expect(w.find(".rail-drawer").exists()).toBe(true);
    // 打开抽屉是程序化落点，不是用户点击入口：焦点必须留在原处（通知不抢焦点）
    expect(document.activeElement).toBe(anchor);
    w.unmount();
    wrapper = null;
  });
});
