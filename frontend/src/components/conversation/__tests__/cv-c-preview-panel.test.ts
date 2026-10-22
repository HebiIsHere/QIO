/**
 * C 预览外壳（PreviewPanel）定向用例：
 *   * 多标签：切换、关闭只关一个、激活邻居；
 *   * 全窗口：返回按钮与 Esc 都发出 toggleFull（外壳据此切布局），标签与位置不丢；
 *   * 文件更新提示 / 更新失败提示按真实运行时状态显示；
 *   * 没有标签时给出诚实空态（不是一片空白）。
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

function replyTab(messageId: string, title: string): PreviewTabState {
  return {
    id: "replycode:" + messageId + ":0",
    target: {
      id: "replycode:" + messageId + ":0",
      kind: "code",
      title,
      source: "reply_code",
      messageId,
      blockIndex: 0,
    },
    position: { scrollTop: 12 },
  };
}

function seedTabs(): { first: PreviewTabState; second: PreviewTabState } {
  const first = replyTab("m1", "回复中的代码");
  const second = replyTab("m2", "另一段代码");
  const shell = useConversationShellStore();
  shell.prefs.previewTabs = [first, second];
  shell.prefs.activePreviewTabId = first.id;
  shell.prefs.rightOpen = true;
  const preview = useConversationPreviewStore();
  for (const tab of [first, second]) {
    preview.documents[tab.id] = { text: "a\nb" };
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
  return { first, second };
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
});

describe("多标签", () => {
  it("显示全部标签，点击切换激活项；关闭只关一个并激活邻居", async () => {
    const { first, second } = seedTabs();
    const wrapper = mount(PreviewPanel);
    await flushPromises();

    const tabs = wrapper.findAll('[role="tab"]');
    expect(tabs).toHaveLength(2);
    expect(tabs[0].attributes("aria-selected")).toBe("true");
    await tabs[1].trigger("click");
    const shell = useConversationShellStore();
    expect(shell.prefs.activePreviewTabId).toBe(second.id);
    expect(wrapper.findAll('[role="tab"]')[1].attributes("aria-selected")).toBe("true");

    const closeButtons = wrapper.findAll(".pv-tab-close");
    await closeButtons[0].trigger("click");
    expect(shell.prefs.previewTabs.map((tab) => tab.id)).toEqual([second.id]);
    expect(shell.prefs.activePreviewTabId).toBe(second.id);
    expect(useConversationPreviewStore().runtimeOf(first.id)).toBeNull();
    wrapper.unmount();
  });

  it("没有标签时显示可理解的空态", () => {
    const wrapper = mount(PreviewPanel);
    expect(wrapper.find("[data-preview-empty]").exists()).toBe(true);
    expect(wrapper.find("[data-preview-empty]").text()).toContain("打开文件");
    wrapper.unmount();
  });
});

describe("全窗口", () => {
  it("full 模式下有「返回对话」，点击与 Esc 都发出 toggleFull", async () => {
    const shell = useConversationShellStore();
    seedTabs();
    shell.prefs.previewFull = true;
    const wrapper = mount(PreviewPanel, { props: { full: true } });
    await flushPromises();
    const back = wrapper.findAll("button").find((button) => button.text() === "返回对话");
    expect(back).toBeTruthy();
    await back!.trigger("click");
    expect(wrapper.emitted("toggleFull")).toHaveLength(1);

    window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }));
    await flushPromises();
    expect(wrapper.emitted("toggleFull")).toHaveLength(2);
    wrapper.unmount();
  });
});

describe("文件更新与失败提示", () => {
  it("展示「文件已更新」与「当前显示的是上次内容」两类真实状态", async () => {
    const preview = useConversationPreviewStore();
    seedTabs();
    const activeId = useConversationShellStore().prefs.activePreviewTabId!;
    preview.runtime[activeId] = {
      ...preview.runtime[activeId],
      notice: "文件已更新，阅读位置已保留",
      updateError: "更新检查失败：当前显示的是上次读取的内容（连接中断）",
    };
    const wrapper = mount(PreviewPanel);
    await flushPromises();
    expect(wrapper.get("[data-pv-updated]").text()).toContain("文件已更新");
    expect(wrapper.get("[data-pv-update-error]").text()).toContain("当前显示的是上次读取的内容");
    wrapper.unmount();
  });
});
