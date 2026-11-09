/**
 * D 组定向用例（问题 14）：全窗口预览的全局 Esc 不能和前方弹窗/下拉抢同一次按键。
 *
 * 预览已改为复用共用 Esc 层级栈（composables/useEscapeLayer，与问题 6 同一套规则）：
 * 只注册一层、不自己挂 window keydown；后打开的内层先处理 Esc，已处理过的关闭
 * 不会再传到后面的全窗口预览；输入法组合输入中的 Esc 不算关闭操作。
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
import QModal from "../../ui/QModal.vue";
import { registerEscapeLayer } from "../../../composables/useEscapeLayer";
import { useConversationShellStore } from "../../../stores/conversationShell";
import { useConversationPreviewStore } from "../../../stores/conversationPreview";
import type { PreviewTabState } from "../../../conversation/previewTypes";

function seedTab(): PreviewTabState {
  const tab: PreviewTabState = {
    id: "replycode:m1:0",
    target: {
      id: "replycode:m1:0",
      kind: "code",
      title: "回复中的代码",
      source: "reply_code",
      messageId: "m1",
      blockIndex: 0,
    },
    position: {},
  };
  const shell = useConversationShellStore();
  shell.prefs.previewTabs = [tab];
  shell.prefs.activePreviewTabId = tab.id;
  shell.prefs.rightOpen = true;
  const preview = useConversationPreviewStore();
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
  return tab;
}

function dispatchEscape(init: KeyboardEventInit = {}): KeyboardEvent {
  const event = new KeyboardEvent("keydown", { key: "Escape", bubbles: true, cancelable: true, ...init });
  window.dispatchEvent(event);
  return event;
}

async function mountFullPreview() {
  seedTab();
  useConversationShellStore().prefs.previewFull = true;
  const wrapper = mount(PreviewPanel, { props: { full: true }, attachTo: document.body });
  await flushPromises();
  return wrapper;
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

describe("全窗口预览的 Esc 层级", () => {
  it("只有全窗口预览时：Esc 返回对话一次，且事件被标记为已处理", async () => {
    const wrapper = await mountFullPreview();
    const event = dispatchEscape();
    await flushPromises();
    expect(wrapper.emitted("toggleFull")).toHaveLength(1);
    expect(event.defaultPrevented).toBe(true);
    wrapper.unmount();
  });

  it("前方弹窗（后注册的内层）先处理 Esc：预览不会同时退出全窗口", async () => {
    const wrapper = await mountFullPreview();
    const frontHandler = vi.fn(() => true);
    const dispose = registerEscapeLayer({ id: "d-test-front-dialog", onEscape: frontHandler });

    dispatchEscape();
    await flushPromises();
    expect(frontHandler).toHaveBeenCalledTimes(1);
    expect(wrapper.emitted("toggleFull") ?? []).toHaveLength(0);

    // 前方层关闭后，再按 Esc 才轮到预览。
    dispose();
    dispatchEscape();
    await flushPromises();
    expect(wrapper.emitted("toggleFull")).toHaveLength(1);
    wrapper.unmount();
  });

  it("真实 QModal 作为前方弹窗时：Esc 只关 QModal，再按 Esc 才退出全窗口（与问题 6 同一层级栈）", async () => {
    const wrapper = await mountFullPreview();
    const modal = mount(QModal, {
      props: { open: true, title: "示例弹窗", modalId: "d-test-qmodal" },
    });
    await flushPromises();

    dispatchEscape();
    await flushPromises();
    expect(modal.emitted("close")).toHaveLength(1);
    expect(wrapper.emitted("toggleFull") ?? []).toHaveLength(0);

    modal.unmount();
    await flushPromises();
    dispatchEscape();
    await flushPromises();
    expect(wrapper.emitted("toggleFull")).toHaveLength(1);
    wrapper.unmount();
  });

  it("前方层不处理（返回 false）时，Esc 继续交给全窗口预览", async () => {
    const wrapper = await mountFullPreview();
    const dispose = registerEscapeLayer({ id: "d-test-front-dropdown", onEscape: () => false });
    dispatchEscape();
    await flushPromises();
    expect(wrapper.emitted("toggleFull")).toHaveLength(1);
    dispose();
    wrapper.unmount();
  });

  it("已被前方界面处理的 Esc（defaultPrevented）不会再退出全窗口预览", async () => {
    const frontHandled = (event: KeyboardEvent) => {
      event.preventDefault();
    };
    window.addEventListener("keydown", frontHandled, true);
    try {
      const wrapper = await mountFullPreview();
      dispatchEscape();
      await flushPromises();
      expect(wrapper.emitted("toggleFull") ?? []).toHaveLength(0);
      wrapper.unmount();
    } finally {
      window.removeEventListener("keydown", frontHandled, true);
    }
  });

  it("前方还有未接入层级栈的阻挡弹层（恢复区 / 任务抽屉）时，Esc 让给前方，不退出全窗口", async () => {
    const wrapper = await mountFullPreview();
    const frontDialog = document.createElement("div");
    frontDialog.setAttribute("role", "dialog");
    frontDialog.setAttribute("aria-modal", "true");
    frontDialog.setAttribute("aria-label", "未完成事项");
    document.body.appendChild(frontDialog);
    const frontHandled = vi.fn();
    window.addEventListener("keydown", frontHandled);

    dispatchEscape();
    await flushPromises();
    // 预览层让开：前方界面自己的 Esc 处理照常进行，全窗口不退出。
    expect(frontHandled).toHaveBeenCalledTimes(1);
    expect(wrapper.emitted("toggleFull") ?? []).toHaveLength(0);

    frontHandled.mockClear();
    frontDialog.remove();
    dispatchEscape();
    await flushPromises();
    expect(wrapper.emitted("toggleFull")).toHaveLength(1);
    window.removeEventListener("keydown", frontHandled);
    wrapper.unmount();
  });

  it("输入法组合输入中的 Esc 不当作关闭界面", async () => {
    const wrapper = await mountFullPreview();
    dispatchEscape({ isComposing: true } as KeyboardEventInit);
    await flushPromises();
    expect(wrapper.emitted("toggleFull") ?? []).toHaveLength(0);
    wrapper.unmount();
  });

  it("非全窗口（普通侧栏）时 Esc 不切换布局", async () => {
    seedTab();
    const wrapper = mount(PreviewPanel, { attachTo: document.body });
    await flushPromises();
    dispatchEscape();
    await flushPromises();
    expect(wrapper.emitted("toggleFull") ?? []).toHaveLength(0);
    wrapper.unmount();
  });
});
