/**
 * 0.3.2 审计 · 页面接线（shell-conversation 主责）：
 * #38 星球覆盖时隔离背景 + 退出归还焦点；
 * #51 全窗口预览隔离背景 + 收起归还触发焦点；普通分栏双方可操作；
 * #50 大窗手动全窗口回分栏；小窗自动全窗口时「返回对话」/Esc 收起预览并保留标签、搜索、阅读位置；
 * #78/#79 打开恢复窗口并聚焦记录、Esc 分层（先收详情再收弹窗）、背景限制、关闭归还焦点；
 * 文案 5/6：恢复窗口不再有页面级重复标题与通用导语。
 */
import { nextTick, defineComponent, h } from "vue";
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
import type { TaskCounts, TaskFeedPage } from "../../conversation/contracts";
import type { PreviewTabState } from "../../conversation/previewTypes";

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

function page(): TaskFeedPage {
  return {
    items: [],
    nextCursor: null,
    counts: ZERO_COUNTS,
    serverTime: "2026-10-22T10:00:00.000Z",
    instanceId: "inst-1",
  };
}

function seedTab(position: Record<string, unknown> = {}): PreviewTabState {
  return {
    id: "attachment:a1",
    target: {
      id: "attachment:a1",
      kind: "text",
      title: "方案.md",
      source: "attachment",
      attachmentId: "a1",
    },
    position,
  } as PreviewTabState;
}

const handleEscape = vi.fn(() => false);

const ChatColumnStub = defineComponent({
  name: "ChatColumn",
  setup(_, { slots }) {
    return () =>
      h("div", { class: "stub-chat" }, [
        h("button", { class: "chat-btn", type: "button" }, "背景输入"),
        // PlanetDock 由页面通过 #planet 槽塞进对话列；替身必须透传槽，
        // 否则「星球入口 → 覆盖层 → 焦点归还」整条链路测不到。
        slots.planet ? slots.planet() : null,
      ]);
  },
});

const PreviewPanelStub = defineComponent({
  name: "PreviewPanel",
  props: { full: { type: Boolean, default: false } },
  emits: ["close", "toggleFull"],
  setup(props, { emit }) {
    return () =>
      h(
        "div",
        {
          class: "stub-preview",
          "data-preview-panel": "",
          "data-full": props.full ? "true" : "false",
        },
        [
          h(
            "button",
            { class: "preview-toggle", type: "button", onClick: () => emit("toggleFull") },
            props.full ? "返回对话" : "全窗口",
          ),
          h("button", { class: "preview-tab", role: "tab", "aria-selected": "true", type: "button" }, "方案.md"),
          h("button", { class: "preview-close", type: "button", onClick: () => emit("close") }, "收起"),
        ],
      );
  },
});

const PlanetDockStub = defineComponent({
  name: "PlanetDock",
  emits: ["open"],
  setup(_, { emit }) {
    return () =>
      h(
        "button",
        {
          class: "stub-dock",
          type: "button",
          "data-planet-entry": "",
          onClick: () => emit("open"),
        },
        "星球",
      );
  },
});

const PlanetViewStub = defineComponent({
  name: "PlanetView",
  props: {
    seq: { type: Number, default: 0 },
    open: { type: Boolean, default: false },
    initialTab: { type: String, default: "topic" },
    focusPendingId: { type: String, default: null },
  },
  emits: ["close"],
  setup(props, { emit }) {
    return () =>
      h(
        "div",
        {
          class: "stub-planet",
          "data-planet-overlay": "",
          "data-open": String(props.open),
          "data-seq": String(props.seq),
        },
        [
          h(
            "button",
            {
              class: "planet-close",
              type: "button",
              onClick: () => emit("close", props.seq),
            },
            "关闭星球",
          ),
        ],
      );
  },
});

const RecoveryInboxStub = defineComponent({
  name: "RecoveryInbox",
  props: { focusRecordId: { type: String, default: null } },
  setup(props, { expose }) {
    expose({ handleEscape });
    return () =>
      h("div", { class: "stub-recovery", "data-focus-id": props.focusRecordId ?? "" }, [
        h("h2", "未完成事项 · 1"),
      ]);
  },
});

function stubs() {
  return {
    ChatColumn: ChatColumnStub,
    PreviewPanel: PreviewPanelStub,
    PlanetDock: PlanetDockStub,
    PlanetView: PlanetViewStub,
    UnifiedApprovalLayer: { template: "<div />" },
    RecoveryInbox: RecoveryInboxStub,
  };
}

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
  return mount(ConversationView, { attachTo: document.body, global: { plugins: [pinia], stubs: stubs() } });
}

function dispatchEscape() {
  const event = new KeyboardEvent("keydown", { key: "Escape", bubbles: true, cancelable: true });
  window.dispatchEvent(event);
  return event;
}

function workspaceRow(wrapper: ReturnType<typeof mountView>) {
  return wrapper.find(".workspace-row").element as HTMLElement;
}

function inertSubtree(wrapper: ReturnType<typeof mountView>): HTMLElement[] {
  const row = workspaceRow(wrapper);
  return [row, ...Array.from(row.querySelectorAll<HTMLElement>("*"))];
}

beforeEach(() => {
  vi.clearAllMocks();
  handleEscape.mockReset();
  handleEscape.mockReturnValue(false);
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
  document.body.innerHTML = "";
  setViewport(1440);
});

describe("#38 星球覆盖对话时隔离下层并归还焦点", () => {
  it("打开星球：背景对话进入 inert；星球内部控件不受影响", async () => {
    const wrapper = mountView();
    await flushPromises();

    const dock = wrapper.find("[data-planet-entry]");
    await dock.trigger("click");
    await flushPromises();

    expect(wrapper.find(".stub-planet").exists()).toBe(true);
    expect(workspaceRow(wrapper).hasAttribute("inert")).toBe(true);
    // 下层对话控件在 inert 子树里（Tab / Enter / 鼠标都不可达）
    expect(wrapper.find(".stub-chat").element.closest("[inert]")).toBe(workspaceRow(wrapper));
    // 星球内的关闭按钮不在 inert 子树里，仍然可用
    expect(wrapper.find(".planet-close").element.closest("[inert]")).toBeNull();

    wrapper.unmount();
  });

  it("关闭星球：焦点归还给星球入口（[data-planet-entry]），背景解除隔离", async () => {
    const wrapper = mountView();
    await flushPromises();

    const dock = wrapper.find("[data-planet-entry]");
    (dock.element as HTMLElement).focus();
    expect(document.activeElement).toBe(dock.element);

    await dock.trigger("click");
    await flushPromises();
    expect(workspaceRow(wrapper).hasAttribute("inert")).toBe(true);

    // 星球页自己发 close（带自己的 seq）
    wrapper.findComponent({ name: "PlanetView" }).vm.$emit("close", Number(
      wrapper.find(".stub-planet").attributes("data-seq") ?? 1,
    ));
    await flushPromises();
    await nextTick();

    expect(workspaceRow(wrapper).hasAttribute("inert")).toBe(false);
    expect(document.activeElement, "退出后焦点回到星球入口").toBe(dock.element);
    wrapper.unmount();
  });
});

describe("#51 全窗口预览隔离背景；普通分栏双方可操作", () => {
  it("普通分栏不隔离：背景对话与预览都能操作", async () => {
    const wrapper = mountView();
    await flushPromises();
    const shell = useConversationShellStore();
    shell.prefs.previewTabs = [seedTab()];
    shell.prefs.rightOpen = true;
    await flushPromises();

    expect(wrapper.find('.stub-preview[data-full="false"]').exists()).toBe(true);
    expect(workspaceRow(wrapper).hasAttribute("inert")).toBe(false);
    expect(wrapper.find(".stub-preview").element.closest("[inert]")).toBeNull();

    wrapper.unmount();
  });

  it("打开全窗口预览：背景对话进入 inert，预览层自身可操作", async () => {
    const wrapper = mountView();
    await flushPromises();
    const shell = useConversationShellStore();
    shell.prefs.previewTabs = [seedTab()];
    shell.prefs.rightOpen = true;
    await flushPromises();

    await wrapper.find(".preview-toggle").trigger("click");
    await flushPromises();

    expect(shell.prefs.previewFull).toBe(true);
    expect(wrapper.find('.stub-preview[data-full="true"]').exists()).toBe(true);
    expect(workspaceRow(wrapper).hasAttribute("inert")).toBe(true);
    expect(wrapper.find(".stub-chat").element.closest("[inert]")).toBe(workspaceRow(wrapper));
    expect(wrapper.find(".stub-preview").element.closest("[inert]")).toBeNull();

    wrapper.unmount();
  });

  it("收起全窗口后焦点归还触发的标签/按钮（分栏恢复后落在当前预览标签上）", async () => {
    const wrapper = mountView();
    await flushPromises();
    const shell = useConversationShellStore();
    shell.prefs.previewTabs = [seedTab()];
    shell.prefs.rightOpen = true;
    await flushPromises();

    const toggle = wrapper.find(".preview-toggle");
    (toggle.element as HTMLElement).focus();
    await toggle.trigger("click");
    await flushPromises();
    expect(wrapper.find('.stub-preview[data-full="true"]').exists()).toBe(true);

    await wrapper.find(".stub-preview .preview-toggle").trigger("click");
    await flushPromises();
    await nextTick();

    expect(shell.prefs.previewFull).toBe(false);
    expect(wrapper.find('.stub-preview[data-full="false"]').exists()).toBe(true);
    expect(document.activeElement, "焦点回到触发全窗口的标签/按钮").toBe(
      wrapper.find(".stub-preview .preview-tab").element,
    );
    wrapper.unmount();
  });
});

describe("#50 全窗口预览的返回对话与 Esc（标签 / 搜索 / 阅读位置保留）", () => {
  it("大窗手动全窗口：点「返回对话」回到分栏，标签、搜索词与阅读位置都在", async () => {
    const wrapper = mountView();
    await flushPromises();
    const shell = useConversationShellStore();
    const tab = seedTab({ scrollTop: 320, page: 2, search: "捕获率" });
    shell.prefs.previewTabs = [tab];
    shell.prefs.activePreviewTabId = tab.id;
    shell.prefs.rightOpen = true;
    await flushPromises();

    await wrapper.find(".preview-toggle").trigger("click");
    await flushPromises();
    expect(shell.effectivePreviewFull).toBe(true);

    await wrapper.find(".stub-preview .preview-toggle").trigger("click");
    await flushPromises();

    expect(shell.effectivePreviewFull).toBe(false);
    expect(shell.prefs.previewFull).toBe(false);
    expect(wrapper.find('.stub-preview[data-full="false"]').exists()).toBe(true);
    expect(shell.prefs.previewTabs).toHaveLength(1);
    expect(shell.prefs.previewTabs[0].position.search).toBe("捕获率");
    expect(shell.prefs.previewTabs[0].position.scrollTop).toBe(320);
    wrapper.unmount();
  });

  it("小窗自动全窗口：「返回对话」收起预览（不假装回到分栏），标签/搜索/位置保留", async () => {
    setViewport(800);
    const wrapper = mountView();
    await flushPromises();
    const shell = useConversationShellStore();
    const tab = seedTab({ scrollTop: 88, search: "第三章" });
    shell.prefs.previewTabs = [tab];
    shell.prefs.activePreviewTabId = tab.id;
    shell.prefs.rightOpen = true;
    await flushPromises();

    // 临时全窗口由实际可用宽度推导，不写回用户偏好
    expect(shell.effectivePreviewFull).toBe(true);
    expect(shell.prefs.previewFull).toBe(false);
    expect(wrapper.find('.stub-preview[data-full="true"]').exists()).toBe(true);

    await wrapper.find(".stub-preview .preview-toggle").trigger("click");
    await flushPromises();
    await nextTick();

    expect(shell.prefs.rightOpen).toBe(false);
    expect(shell.prefs.previewFull).toBe(false);
    expect(wrapper.find(".preview-overlay").exists()).toBe(false);
    expect(wrapper.find(".preview-reopen").exists()).toBe(true);
    expect(shell.prefs.previewTabs).toHaveLength(1);
    expect(shell.prefs.previewTabs[0].position.search).toBe("第三章");
    expect(shell.prefs.previewTabs[0].position.scrollTop).toBe(88);
    // 收起后焦点给预览恢复入口（不是丢到 body）
    expect(document.activeElement).toBe(wrapper.find(".preview-reopen").element);
    wrapper.unmount();
  });

  it("小窗自动全窗口：Esc 收起预览并保留标签，事件被消费", async () => {
    setViewport(800);
    const wrapper = mountView();
    await flushPromises();
    const shell = useConversationShellStore();
    const tab = seedTab({ search: "结果表" });
    shell.prefs.previewTabs = [tab];
    shell.prefs.rightOpen = true;
    await flushPromises();
    expect(wrapper.find(".preview-overlay").exists()).toBe(true);

    const event = dispatchEscape();
    await flushPromises();
    await nextTick();

    expect(event.defaultPrevented).toBe(true);
    expect(shell.prefs.rightOpen).toBe(false);
    expect(shell.prefs.previewTabs).toHaveLength(1);
    expect(shell.prefs.previewTabs[0].position.search).toBe("结果表");
    expect(wrapper.find(".preview-reopen").exists()).toBe(true);
    wrapper.unmount();
  });
});

describe("#78/#79 恢复窗口接线：打开、聚焦记录、Esc 分层、背景限制、焦点归还", () => {
  it("qio:open-recovery 事件带 recordId：打开恢复窗口并把目标记录交给收件箱", async () => {
    const wrapper = mountView();
    await flushPromises();

    window.dispatchEvent(new CustomEvent("qio:open-recovery", { detail: { recordId: "rec-9" } }));
    await flushPromises();
    await nextTick();

    expect(wrapper.find(".recovery-layer").exists()).toBe(true);
    expect(wrapper.find(".stub-recovery").attributes("data-focus-id")).toBe("rec-9");
    wrapper.unmount();
  });

  it("恢复窗口打开时背景对话被隔离；关闭后解除并归还焦点", async () => {
    const wrapper = mountView();
    await flushPromises();
    const trigger = wrapper.find(".chat-btn");
    (trigger.element as HTMLElement).focus();

    window.dispatchEvent(new CustomEvent("qio:open-recovery", { detail: {} }));
    await flushPromises();
    expect(workspaceRow(wrapper).hasAttribute("inert")).toBe(true);
    expect(wrapper.find(".stub-chat").element.closest("[inert]")).toBe(workspaceRow(wrapper));

    // 收件箱没有内层详情可收：Esc 关窗口并把焦点还给触发元素
    handleEscape.mockReturnValue(false);
    dispatchEscape();
    await flushPromises();
    await nextTick();

    expect(wrapper.find(".recovery-layer").exists()).toBe(false);
    expect(workspaceRow(wrapper).hasAttribute("inert")).toBe(false);
    expect(document.activeElement).toBe(trigger.element);
    wrapper.unmount();
  });

  it("Esc 分层：收件箱先收详情（handleEscape=true）时弹窗不关", async () => {
    const wrapper = mountView();
    await flushPromises();
    window.dispatchEvent(new CustomEvent("qio:open-recovery", { detail: {} }));
    await flushPromises();

    handleEscape.mockReturnValue(true);
    const event = dispatchEscape();
    await flushPromises();
    await nextTick();

    expect(handleEscape).toHaveBeenCalledTimes(1);
    expect(wrapper.find(".recovery-layer").exists(), "详情被收掉，弹窗仍开着").toBe(true);
    expect(event.defaultPrevented).toBe(true);
    wrapper.unmount();
  });

  it("恢复窗口盖在全窗口预览之上时：Esc 先关恢复窗口，不误退预览", async () => {
    const wrapper = mountView();
    await flushPromises();
    const shell = useConversationShellStore();
    shell.prefs.previewTabs = [seedTab()];
    shell.prefs.rightOpen = true;
    shell.prefs.previewFull = true;
    await flushPromises();
    expect(wrapper.find(".preview-overlay").exists()).toBe(true);

    window.dispatchEvent(new CustomEvent("qio:open-recovery", { detail: {} }));
    await flushPromises();
    expect(wrapper.find(".recovery-layer").exists()).toBe(true);

    handleEscape.mockReturnValue(false);
    dispatchEscape();
    await flushPromises();
    await nextTick();

    expect(wrapper.find(".recovery-layer").exists()).toBe(false);
    expect(shell.prefs.previewFull, "预览全窗口状态不该被这次 Esc 改掉").toBe(true);
    wrapper.unmount();
  });

  it("页面不再渲染重复的恢复标题与通用导语（文案 5/6）", async () => {
    const wrapper = mountView();
    await flushPromises();
    window.dispatchEvent(new CustomEvent("qio:open-recovery", { detail: {} }));
    await flushPromises();

    const panel = wrapper.find(".recovery-panel");
    expect(panel.exists()).toBe(true);
    // 页面级 <h2>未完成事项</h2> 已移除：标题只由收件箱组件给（含数量）
    expect(panel.element.querySelector(".recovery-head h2")).toBeNull();
    expect(panel.element.textContent).not.toContain("这些是上次没有执行完");
    // 标题只出现一次，且带数量
    expect(wrapper.find(".stub-recovery h2").text()).toBe("未完成事项 · 1");
    expect(panel.attributes("aria-label")).toBe("未完成事项");
    wrapper.unmount();
  });
});
