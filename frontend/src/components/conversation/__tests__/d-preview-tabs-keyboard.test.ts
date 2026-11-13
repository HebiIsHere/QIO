/**
 * D 组定向用例（问题 15）：方向键切换预览文件后，键盘焦点必须跟着新标签。
 *
 * 验证：多文件连续切换时选中状态 / 当前文件 / DOM 焦点 / 标签可见性同步；
 * 首尾循环与 Home/End；切换后继续按 Enter 不会意外激活旧标签；
 * 普通 Tab 进出标签组不被拦截（不 preventDefault），标签按钮符合 roving tabindex 约定。
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
  shell.prefs.activePreviewTabId = tabs[0].id;
  shell.prefs.rightOpen = true;
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

async function pressTablistKey(
  wrapper: ReturnType<typeof mount>,
  key: string,
  init: KeyboardEventInit = {},
): Promise<KeyboardEvent> {
  const tablist = wrapper.get('[role="tablist"]').element;
  const event = new KeyboardEvent("keydown", { key, bubbles: true, cancelable: true, ...init });
  tablist.dispatchEvent(event);
  await flushPromises();
  return event;
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

describe("预览标签方向键切换", () => {
  it("←/→ 切换时选中、当前文件与真实焦点同步；随后按 Enter 不会回到旧文件", async () => {
    const tabs = seedTabs(3);
    const wrapper = await mountPanel();
    const shell = useConversationShellStore();
    const buttons = wrapper.findAll('[role="tab"]');
    expect(buttons).toHaveLength(3);
    // roving tabindex：只有当前标签进 Tab 序列
    expect(buttons[0].attributes("tabindex")).toBe("0");
    expect(buttons[1].attributes("tabindex")).toBe("-1");
    expect(buttons[2].attributes("tabindex")).toBe("-1");

    (buttons[0].element as HTMLElement).focus();
    expect(document.activeElement).toBe(buttons[0].element);

    await pressTablistKey(wrapper, "ArrowRight");
    expect(shell.prefs.activePreviewTabId).toBe(tabs[1].id);
    expect(wrapper.findAll('[role="tab"]')[1].attributes("aria-selected")).toBe("true");
    expect(document.activeElement).toBe(wrapper.findAll('[role="tab"]')[1].element);
    expect(wrapper.findAll('[role="tab"]')[0].attributes("tabindex")).toBe("-1");
    expect(wrapper.findAll('[role="tab"]')[1].attributes("tabindex")).toBe("0");

    // 焦点在新标签上：Enter（浏览器会点击聚焦按钮）只会重新激活当前文件。
    (document.activeElement as HTMLElement).click();
    await flushPromises();
    expect(shell.prefs.activePreviewTabId).toBe(tabs[1].id);

    await pressTablistKey(wrapper, "ArrowRight");
    expect(shell.prefs.activePreviewTabId).toBe(tabs[2].id);
    expect(document.activeElement).toBe(wrapper.findAll('[role="tab"]')[2].element);
    wrapper.unmount();
  });

  it("首尾边界：End/Home 到末/首，方向键循环；每步焦点都跟随", async () => {
    const tabs = seedTabs(3);
    const wrapper = await mountPanel();
    const shell = useConversationShellStore();

    await pressTablistKey(wrapper, "End");
    expect(shell.prefs.activePreviewTabId).toBe(tabs[2].id);
    expect(document.activeElement).toBe(wrapper.findAll('[role="tab"]')[2].element);

    await pressTablistKey(wrapper, "ArrowRight");
    expect(shell.prefs.activePreviewTabId).toBe(tabs[0].id);
    expect(document.activeElement).toBe(wrapper.findAll('[role="tab"]')[0].element);

    await pressTablistKey(wrapper, "ArrowLeft");
    expect(shell.prefs.activePreviewTabId).toBe(tabs[2].id);
    expect(document.activeElement).toBe(wrapper.findAll('[role="tab"]')[2].element);

    await pressTablistKey(wrapper, "Home");
    expect(shell.prefs.activePreviewTabId).toBe(tabs[0].id);
    expect(document.activeElement).toBe(wrapper.findAll('[role="tab"]')[0].element);
    wrapper.unmount();
  });

  it("标签多到需要滚动时，切换后把新标签滚进可视范围", async () => {
    seedTabs(8);
    const wrapper = await mountPanel();
    const scrollIntoView = vi.fn();
    const original = (Element.prototype as { scrollIntoView?: unknown }).scrollIntoView;
    (Element.prototype as unknown as { scrollIntoView: () => void }).scrollIntoView = scrollIntoView;
    try {
      await pressTablistKey(wrapper, "End");
      expect(scrollIntoView).toHaveBeenCalled();
      const calls = scrollIntoView.mock.calls as unknown as Array<[{ inline?: string }]>;
      expect(calls[calls.length - 1]?.[0]?.inline).toBe("nearest");
    } finally {
      if (original === undefined) delete (Element.prototype as { scrollIntoView?: unknown }).scrollIntoView;
      else (Element.prototype as unknown as { scrollIntoView: unknown }).scrollIntoView = original;
    }
    wrapper.unmount();
  });

  it("普通 Tab 不被拦截（不 preventDefault），离开标签组按浏览器默认顺序", async () => {
    seedTabs(2);
    const wrapper = await mountPanel();
    const buttons = wrapper.findAll('[role="tab"]');
    (buttons[0].element as HTMLElement).focus();
    const tabEvent = await pressTablistKey(wrapper, "Tab");
    expect(tabEvent.defaultPrevented).toBe(false);
    wrapper.unmount();
  });

  it("删除键按实际焦点所指标签关（旧行为「只删当前标签」被本项否定）", async () => {
    const tabs = seedTabs(2);
    const wrapper = await mountPanel();
    const shell = useConversationShellStore();
    // 激活的是乙，焦点在甲的关闭按钮上：不能误关乙。
    shell.prefs.activePreviewTabId = tabs[1].id;
    const closeButtons = wrapper.findAll(".pv-tab-close");
    (closeButtons[0].element as HTMLElement).focus();
    const event = new KeyboardEvent("keydown", { key: "Delete", bubbles: true, cancelable: true });
    closeButtons[0].element.dispatchEvent(event);
    await flushPromises();

    expect(event.defaultPrevented).toBe(true);
    expect(shell.prefs.previewTabs.map((tab) => tab.id)).toEqual([tabs[1].id]);
    expect(shell.prefs.activePreviewTabId).toBe(tabs[1].id);
    expect(document.activeElement).toBe(wrapper.findAll('[role="tab"]')[0].element);
    wrapper.unmount();
  });
});
