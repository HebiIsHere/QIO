/**
 * 对话页外层布局（A）：三栏、窄窗口临时收起（不写偏好）、手动收起记忆、
 * 分隔条双击恢复默认、任务抽屉、预览贴边入口、全窗口 Esc 返回、页面切换不重复初始化。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";

const feedApi = vi.hoisted(() => ({
  fetchTaskFeed: vi.fn(),
  fetchTaskDetail: vi.fn(),
  acknowledgeTask: vi.fn(),
  requestTaskCancel: vi.fn(),
}));

vi.mock("../../services/taskFeedApi", () => feedApi);

import ConversationView from "../ConversationView.vue";
import { api } from "../../services/api";
import { useConversationShellStore } from "../../stores/conversationShell";
import { useConversationTasksStore } from "../../stores/conversationTasks";
import { LEFT_DEFAULT } from "../../conversation/layoutPrefs";
import type { TaskCounts, TaskFeedPage } from "../../conversation/contracts";

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

function page(counts: Partial<TaskCounts> = {}): TaskFeedPage {
  return {
    items: [],
    nextCursor: null,
    counts: { ...ZERO_COUNTS, ...counts },
    serverTime: "2026-10-22T10:00:00.000Z",
    instanceId: "inst-1",
  };
}

const stubs = {
  ChatColumn: { name: "ChatColumn", template: '<div class="stub-chat" />' },
  PreviewPanel: {
    name: "PreviewPanel",
    props: { full: { type: Boolean, default: false } },
    emits: ["close", "toggleFull"],
    template: '<div class="stub-preview" :data-full="full ? \'true\' : \'false\'" />',
  },
  UnifiedApprovalLayer: { name: "UnifiedApprovalLayer", template: "<div />" },
  SettingsFloat: { name: "SettingsFloat", template: "<div />" },
  PlanetDock: {
    name: "PlanetDock",
    emits: ["open"],
    template: '<button class="stub-dock" type="button" />',
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

function mountView() {
  const pinia = createPinia();
  setActivePinia(pinia);
  return mount(ConversationView, { global: { plugins: [pinia], stubs } });
}

function previewTab() {
  return {
    id: "attachment:a1",
    target: {
      id: "attachment:a1",
      kind: "text",
      title: "方案.md",
      source: "attachment",
      attachmentId: "a1",
    },
    position: {},
  } as never;
}

beforeEach(() => {
  vi.clearAllMocks();
  vi.spyOn(api, "getUISettings").mockResolvedValue({ typewriter_cps: 50 } as never);
  vi.spyOn(api, "saveConversationLayout").mockResolvedValue({ ok: true } as never);
  vi.spyOn(api, "getSessionContext").mockResolvedValue({
    topic_id: "t1",
    topic_name: "布局讨论",
    anchor_fragment: null,
    messages: [],
    has_more: false,
    next_before: null,
    tool_records: [],
    turn_facts: [],
  } as never);
  feedApi.fetchTaskFeed.mockResolvedValue(page());
  setViewport(1440);
});

afterEach(() => {
  useConversationTasksStore().dispose();
  useConversationShellStore().dispose();
  setViewport(1440);
});

describe("三栏与窄窗口规则", () => {
  it("窄窗口先临时收起左栏，变宽恢复；用户偏好没有被改写", async () => {
    feedApi.fetchTaskFeed.mockResolvedValue(page({ waitingUser: 2 }));
    setViewport(1000);
    const wrapper = mountView();
    await flushPromises();
    await wrapper.vm.$nextTick();

    const shell = useConversationShellStore();
    expect(shell.prefs.leftCollapsed).toBe(false);
    expect(wrapper.find(".task-rail").exists()).toBe(false);
    expect(wrapper.find(".collapsed-rail").exists()).toBe(true);
    // 收起入口显示真实需要处理数量
    expect(wrapper.find(".rail-badge").text()).toBe("2");

    setViewport(1440);
    await flushPromises();
    await wrapper.vm.$nextTick();

    expect(wrapper.find(".task-rail").exists()).toBe(true);
    expect(shell.prefs.leftCollapsed).toBe(false);
    wrapper.unmount();
  });

  it("手动收起后记住用户选择：窗口变宽也不会自动展开", async () => {
    const wrapper = mountView();
    await flushPromises();
    await wrapper.vm.$nextTick();

    const shell = useConversationShellStore();
    await wrapper.find('[aria-label="收起任务栏"]').trigger("click");
    await flushPromises();
    expect(shell.prefs.leftCollapsed).toBe(true);
    expect(wrapper.find(".collapsed-rail").exists()).toBe(true);

    setViewport(1000);
    await flushPromises();
    setViewport(1440);
    await flushPromises();
    await wrapper.vm.$nextTick();
    expect(shell.prefs.leftCollapsed).toBe(true);
    expect(wrapper.find(".collapsed-rail").exists()).toBe(true);
    wrapper.unmount();
  });

  it("双击分隔条恢复默认宽度", async () => {
    const wrapper = mountView();
    await flushPromises();
    await wrapper.vm.$nextTick();

    const shell = useConversationShellStore();
    shell.setLeftWidth(400);
    await wrapper.vm.$nextTick();
    expect(wrapper.find('[aria-label="调整左侧任务栏宽度"]').attributes("aria-valuenow")).toBe("400");

    await wrapper.find('[aria-label="调整左侧任务栏宽度"]').trigger("dblclick");
    expect(shell.prefs.leftWidth).toBe(LEFT_DEFAULT);
    wrapper.unmount();
  });

  it("窄窗口点收起入口打开任务抽屉；Esc 关闭且不改写偏好", async () => {
    setViewport(1000);
    const wrapper = mountView();
    await flushPromises();
    await wrapper.vm.$nextTick();

    await wrapper.find("[data-rail-entry]").trigger("click");
    await flushPromises();
    await wrapper.vm.$nextTick();
    expect(wrapper.find(".rail-drawer").exists()).toBe(true);
    expect(useConversationShellStore().prefs.leftCollapsed).toBe(false);

    window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }));
    await flushPromises();
    await wrapper.vm.$nextTick();
    expect(wrapper.find(".rail-drawer").exists()).toBe(false);
    wrapper.unmount();
  });
});

describe("预览栏容器", () => {
  it("预览收起但还有标签：贴边入口可恢复预览", async () => {
    const wrapper = mountView();
    await flushPromises();
    await wrapper.vm.$nextTick();

    const shell = useConversationShellStore();
    shell.prefs.previewTabs = [previewTab()];
    shell.prefs.rightOpen = false;
    await wrapper.vm.$nextTick();

    const reopen = wrapper.find(".preview-reopen");
    expect(reopen.exists()).toBe(true);
    expect(reopen.attributes("aria-label")).toContain("1 个标签");

    await reopen.trigger("click");
    await wrapper.vm.$nextTick();
    expect(shell.prefs.rightOpen).toBe(true);
    expect(wrapper.find(".stub-preview").exists()).toBe(true);
    wrapper.unmount();
  });

  it("全窗口预览按 Esc 返回对话布局", async () => {
    const wrapper = mountView();
    await flushPromises();
    await wrapper.vm.$nextTick();

    const shell = useConversationShellStore();
    shell.prefs.previewTabs = [previewTab()];
    shell.prefs.rightOpen = true;
    shell.prefs.previewFull = true;
    await wrapper.vm.$nextTick();
    expect(wrapper.find(".preview-overlay").exists()).toBe(true);

    window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }));
    await wrapper.vm.$nextTick();
    expect(shell.prefs.previewFull).toBe(false);
    wrapper.unmount();
  });
});

describe("页面切换不重复初始化状态", () => {
  it("从设置页返回：布局偏好与历史只初始化一次（不重置草稿等业务状态）", async () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const first = mount(ConversationView, { global: { plugins: [pinia], stubs } });
    await flushPromises();
    await first.vm.$nextTick();
    first.unmount();
    await flushPromises();

    const second = mount(ConversationView, { global: { plugins: [pinia], stubs } });
    await flushPromises();
    await second.vm.$nextTick();

    expect(vi.mocked(api.getUISettings)).toHaveBeenCalledTimes(1);
    expect(vi.mocked(api.getSessionContext)).toHaveBeenCalledTimes(1);
    second.unmount();
  });
});
