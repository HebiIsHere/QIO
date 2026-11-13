/**
 * shell-preview #50 / #53 / 文案 1 定向用例（外壳级）：
 *   * #50 大窗手动全窗口「返回对话」回到分栏；小窗自动全窗口时「返回对话」与 Esc 收起预览，
 *       标签、搜索词与阅读位置保留；
 *   * #53 切文件前立即写回当前标签的最新搜索词与位置；旧标签的迟到写入不覆盖后来编辑；
 *   * 文案 1：预览标题只出现一次「只读」；关标签提示是「关闭预览」。
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
import { tipState, hideTip } from "../../../composables/useQioTip";
import type { PreviewTabState } from "../../../conversation/previewTypes";

function codeTab(index: number, position: PreviewTabState["position"] = {}): PreviewTabState {
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
    position,
  };
}

function seedTabs(): PreviewTabState[] {
  const first = codeTab(1);
  const second = codeTab(2, { search: "alpha" });
  const shell = useConversationShellStore();
  shell.prefs.previewTabs = [first, second];
  shell.prefs.activePreviewTabId = first.id;
  shell.prefs.rightOpen = true;
  shell.viewportWidth = 1400;
  const preview = useConversationPreviewStore();
  for (const tab of [first, second]) {
    preview.documents[tab.id] = { text: "alpha\nbeta\ngamma", language: "python" };
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
  return [first, second];
}

async function mountPanel(full = false) {
  const wrapper = mount(PreviewPanel, { props: full ? { full: true } : {}, attachTo: document.body });
  await flushPromises();
  return wrapper;
}

type PanelWrapper = Awaited<ReturnType<typeof mountPanel>>;

function backButton(wrapper: PanelWrapper) {
  return wrapper.findAll("button").find((button) => button.text() === "返回对话");
}

function dispatchEscape(): KeyboardEvent {
  const event = new KeyboardEvent("keydown", { key: "Escape", bubbles: true, cancelable: true });
  window.dispatchEvent(event);
  return event;
}

beforeEach(() => {
  setActivePinia(createPinia());
  hideTip();
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
  hideTip();
  document.body.innerHTML = "";
});

describe("#50 全窗口退出：手动返回分栏 / 自动全窗口收起预览", () => {
  it("大窗手动全窗口：点「返回对话」发出 toggleFull（回到分栏），标签不动", async () => {
    const tabs = seedTabs();
    const shell = useConversationShellStore();
    shell.prefs.previewFull = true;
    shell.viewportWidth = 1400;
    expect(shell.effectivePreviewFull).toBe(true);

    const wrapper = await mountPanel(true);
    const back = backButton(wrapper);
    expect(back).toBeTruthy();
    await back!.trigger("click");
    expect(wrapper.emitted("toggleFull")).toHaveLength(1);
    expect(wrapper.emitted("close") ?? []).toHaveLength(0);
    expect(shell.prefs.previewTabs.map((tab) => tab.id)).toEqual(tabs.map((tab) => tab.id));
    wrapper.unmount();
  });

  it("小窗自动全窗口：点「返回对话」直接收起预览，不假装能回到分栏；标签与位置保留", async () => {
    const tabs = seedTabs();
    const shell = useConversationShellStore();
    shell.prefs.previewFull = false;
    shell.viewportWidth = 800;
    shell.prefs.rightWidth = 420;
    expect(shell.effectivePreviewFull).toBe(true);

    const wrapper = await mountPanel(true);
    await backButton(wrapper)!.trigger("click");
    expect(wrapper.emitted("close")).toHaveLength(1);
    expect(wrapper.emitted("toggleFull") ?? []).toHaveLength(0);
    // 收起后标签与阅读位置都还在（由外壳 @close 收起，不删标签）。
    expect(shell.prefs.previewTabs.map((tab) => tab.id)).toEqual(tabs.map((tab) => tab.id));
    expect(shell.prefs.activePreviewTabId).toBe(tabs[0].id);
    expect(shell.prefs.previewTabs[1].position.search).toBe("alpha");
    wrapper.unmount();
  });

  it("小窗自动全窗口：Esc 也收起预览（事件已处理，不再让页面重复处理）", async () => {
    seedTabs();
    const shell = useConversationShellStore();
    shell.prefs.previewFull = false;
    shell.viewportWidth = 800;

    const wrapper = await mountPanel(true);
    const event = dispatchEscape();
    await flushPromises();
    expect(wrapper.emitted("close")).toHaveLength(1);
    expect(wrapper.emitted("toggleFull") ?? []).toHaveLength(0);
    expect(event.defaultPrevented).toBe(true);
    wrapper.unmount();
  });

  it("全窗口根节点带 data-preview-full；分栏模式不带", async () => {
    seedTabs();
    useConversationShellStore().prefs.previewFull = true;
    useConversationShellStore().viewportWidth = 1400;
    const fullWrapper = await mountPanel(true);
    expect(fullWrapper.get("[data-preview-panel]").attributes("data-preview-full")).toBeDefined();
    fullWrapper.unmount();

    useConversationShellStore().prefs.previewFull = false;
    const splitWrapper = await mountPanel(false);
    expect(splitWrapper.get("[data-preview-panel]").attributes("data-preview-full")).toBeUndefined();
    splitWrapper.unmount();
  });
});

describe("#53 切换标签前立即记录搜索词与位置", () => {
  it("先改搜索词再立刻切走：搜索词保留；切回来输入框仍是它", async () => {
    const tabs = seedTabs();
    const wrapper = await mountPanel();
    const input = wrapper.get('input[type="search"]');
    await input.setValue("needle");
    // 不等防抖，立刻切到另一个文件。
    await wrapper.findAll('[role="tab"]')[1].trigger("click");
    await flushPromises();
    expect(useConversationShellStore().prefs.previewTabs[0].position.search).toBe("needle");

    await wrapper.findAll('[role="tab"]')[0].trigger("click");
    await flushPromises();
    expect((wrapper.get('input[type="search"]').element as HTMLInputElement).value).toBe("needle");
    expect(useConversationShellStore().prefs.activePreviewTabId).toBe(tabs[0].id);
    wrapper.unmount();
  });

  it("旧标签的迟到写入不覆盖后来编辑：切回后改成 new 词，等过旧防抖仍是 new", async () => {
    seedTabs();
    const wrapper = await mountPanel();
    await wrapper.get('input[type="search"]').setValue("first");
    await wrapper.findAll('[role="tab"]')[1].trigger("click");
    await flushPromises();
    await wrapper.findAll('[role="tab"]')[0].trigger("click");
    await flushPromises();
    await wrapper.get('input[type="search"]').setValue("second");
    // 等过旧搜索的 200ms 防抖与滚动防抖窗口。
    await new Promise((resolve) => setTimeout(resolve, 320));
    await flushPromises();
    expect(useConversationShellStore().prefs.previewTabs[0].position.search).toBe("second");
    wrapper.unmount();
  });

  it("先滚动再立刻切走：滚动位置保留", async () => {
    seedTabs();
    const wrapper = await mountPanel();
    const body = wrapper.get("[data-pv-scroll]").element as HTMLElement;
    body.scrollTop = 77;
    body.dispatchEvent(new Event("scroll"));
    await wrapper.findAll('[role="tab"]')[1].trigger("click");
    await flushPromises();
    expect(useConversationShellStore().prefs.previewTabs[0].position.scrollTop).toBe(77);
    wrapper.unmount();
  });
});

describe("文案 1：标题只留一次「只读」，关标签提示用「关闭预览」", () => {
  it("就绪的文本预览里「只读」只出现一次（标题徽标），正文说明不再重复", async () => {
    seedTabs();
    const wrapper = await mountPanel();
    // 记录组 #57 在面板里加了 .qio-sr-only 的读屏播报（「已打开「X」的只读预览」），
    // 它只给读屏、不是视觉重复的正文说明；文案 1 管的是「用户看得见的字样只出现一次」。
    // 所以这里按去掉读屏节点后的可见文字计数，同时断言读屏播报仍在（两组行为同时成立）。
    const visible = wrapper.element.cloneNode(true) as HTMLElement;
    visible.querySelectorAll(".qio-sr-only").forEach((node) => node.remove());
    const visibleText = visible.textContent ?? "";
    expect(visibleText.match(/只读/g) ?? []).toHaveLength(1);
    expect(visibleText).toContain("文件预览");
    const srText = wrapper.findAll(".qio-sr-only").map((node) => node.text()).join("");
    expect(srText).toContain("只读预览");
    wrapper.unmount();
  });

  it("关标签的用途提示是「关闭预览」", async () => {
    seedTabs();
    const wrapper = await mountPanel();
    const close = wrapper.findAll(".pv-tab-close")[0];
    close.element.dispatchEvent(new Event("focusin"));
    await flushPromises();
    expect(tipState.text).toBe("关闭预览");
    wrapper.unmount();
  });
});