/**
 * shell-preview #52 定向用例：标签快捷键按「实际焦点所指标签」动作。
 *
 * 审计问题：乙选中但焦点在甲的关闭按钮上时，删除键不能误关乙。
 * 同时方向键/Home/End 也从焦点所在标签出发（选中态与焦点可以短暂不同步）。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";

const mocks = vi.hoisted(() => ({
  getUISettings: vi.fn(),
  saveConversationLayout: vi.fn(),
}));

vi.mock("../../../services/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../../services/api")>();
  return {
    ...actual,
    api: {
      ...actual.api,
      getUISettings: mocks.getUISettings,
      saveConversationLayout: mocks.saveConversationLayout,
    },
  };
});

import PreviewPanel from "../PreviewPanel.vue";
import { useConversationShellStore } from "../../../stores/conversationShell";
import { useConversationPreviewStore } from "../../../stores/conversationPreview";
import type { PreviewTabState } from "../../../conversation/previewTypes";

function codeTab(index: number): PreviewTabState {
  const id = "replycode:m" + index + ":0";
  return {
    id,
    target: {
      id,
      kind: "code",
      title: "文件 " + index,
      source: "reply_code",
      messageId: "m" + index,
      blockIndex: 0,
    },
    position: {},
  };
}

function seedTabs(count: number): PreviewTabState[] {
  const tabs = Array.from({ length: count }, (_, index) => codeTab(index + 1));
  const shell = useConversationShellStore();
  shell.prefs.previewTabs = tabs;
  shell.prefs.activePreviewTabId = tabs[tabs.length - 1].id;
  shell.prefs.rightOpen = true;
  shell.viewportWidth = 1400;
  const preview = useConversationPreviewStore();
  for (const tab of tabs) {
    preview.documents[tab.id] = { text: "alpha\nbeta" };
    preview.runtime[tab.id] = {
      phase: "ready",
      envelope: null,
      problem: null,
      version: "",
      notice: "",
      updateError: "",
      updateFailures: 0,
      loadedAt: 1,
    };
  }
  return tabs;
}

async function mountPanel() {
  const wrapper = mount(PreviewPanel, { attachTo: document.body });
  await flushPromises();
  return wrapper;
}

function press(element: Element, key: string): KeyboardEvent {
  const event = new KeyboardEvent("keydown", { key, bubbles: true, cancelable: true });
  element.dispatchEvent(event);
  return event;
}

function focusedTabId(): string | null {
  const active = document.activeElement;
  return active instanceof Element ? active.closest("[data-tab-id]")?.getAttribute("data-tab-id") ?? null : null;
}

beforeEach(() => {
  setActivePinia(createPinia());
  mocks.getUISettings.mockReset();
  mocks.getUISettings.mockResolvedValue({ conversation_layout: null });
  mocks.saveConversationLayout.mockReset();
  mocks.saveConversationLayout.mockImplementation(async (prefs: unknown) => ({
    conversation_layout: prefs,
  }));
});

afterEach(() => {
  useConversationPreviewStore().resetForTests();
  useConversationShellStore().dispose();
  vi.restoreAllMocks();
  document.body.innerHTML = "";
});

describe("#52 标签快捷键使用实际焦点所指标签", () => {
  it("焦点在甲的关闭按钮、激活的是乙：Delete 只关甲，不误关乙", async () => {
    const tabs = seedTabs(3);
    const wrapper = await mountPanel();
    const shell = useConversationShellStore();
    // 激活项是最后一个（丙）；焦点放在第一个标签（甲）的关闭按钮上。
    expect(shell.prefs.activePreviewTabId).toBe(tabs[2].id);
    const closeButtons = wrapper.findAll(".pv-tab-close");
    (closeButtons[0].element as HTMLElement).focus();
    expect(document.activeElement).toBe(closeButtons[0].element);

    const event = press(closeButtons[0].element, "Delete");
    await flushPromises();

    expect(event.defaultPrevented).toBe(true);
    expect(shell.prefs.previewTabs.map((tab) => tab.id)).toEqual([tabs[1].id, tabs[2].id]);
    // 激活项没被误关，仍是丙；焦点交给新的激活标签。
    expect(shell.prefs.activePreviewTabId).toBe(tabs[2].id);
    expect(focusedTabId()).toBe(tabs[2].id);
    wrapper.unmount();
  });

  it("方向键从焦点所指标签出发，而不是从碰巧激活的标签出发", async () => {
    const tabs = seedTabs(3);
    const wrapper = await mountPanel();
    const shell = useConversationShellStore();
    expect(shell.prefs.activePreviewTabId).toBe(tabs[2].id);

    const closeButtons = wrapper.findAll(".pv-tab-close");
    (closeButtons[0].element as HTMLElement).focus();
    // 焦点在甲：右移应到乙。若按激活项（丙）算，会绕回甲。
    press(closeButtons[0].element, "ArrowRight");
    await flushPromises();
    expect(shell.prefs.activePreviewTabId).toBe(tabs[1].id);
    expect(focusedTabId()).toBe(tabs[1].id);

    // 焦点已在乙的主体上：左移回到甲（真实按键事件的目标就是被聚焦的元素）。
    press(document.activeElement as Element, "ArrowLeft");
    await flushPromises();
    expect(shell.prefs.activePreviewTabId).toBe(tabs[0].id);
    expect(focusedTabId()).toBe(tabs[0].id);
    wrapper.unmount();
  });

  it("Home/End 仍是首尾，从焦点标签移动后焦点跟随", async () => {
    const tabs = seedTabs(3);
    const wrapper = await mountPanel();
    const shell = useConversationShellStore();
    const closeButtons = wrapper.findAll(".pv-tab-close");
    (closeButtons[1].element as HTMLElement).focus();

    press(closeButtons[1].element, "End");
    await flushPromises();
    expect(shell.prefs.activePreviewTabId).toBe(tabs[2].id);
    expect(focusedTabId()).toBe(tabs[2].id);

    const mainButtons = wrapper.findAll('[role="tab"]');
    press(mainButtons[2].element, "Home");
    await flushPromises();
    expect(shell.prefs.activePreviewTabId).toBe(tabs[0].id);
    expect(focusedTabId()).toBe(tabs[0].id);
    wrapper.unmount();
  });
});
