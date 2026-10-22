/**
 * 收尾轮 11(a) 的组件证据（Lead 侧，B3 文件域）：
 *
 * 未绑定新输入迁入话题 A 时，A 里原有的草稿被让位但**不删** ——
 * 对话页与互动聊天都要给出「取回（互换）/ 放弃」入口，两份都保留、不拼接、不自动发送。
 *
 * v2（2026-10-22）：对话页的入口不再内联渲染在 Composer.vue，而是移到输入区上方
 * 入口①（ComposerDock → ComposerEntries → SendIssuesPanel）。入口默认收起：
 * 有被让位草稿时才出现，点开才渲染面板。
 *
 * 标注：【组件/DOM】挂载真实 ComposerDock / ChatDock + 真实会话 store。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { flushPromises, mount } from "@vue/test-utils";
import { useSessionStore, type DisplacedChatDraft } from "../../stores/session";
import ComposerDock from "../conversation/ComposerDock.vue";
import ChatDock from "../interactive/ChatDock.vue";
import { useInteractiveStore } from "../../stores/interactive";

// 模式切换与这些用例无关：用桩替换，避免测试环境缺少 router 造成噪声。
vi.mock("../conversation/ConversationModeSwitch.vue", () => ({
  default: { name: "ConversationModeSwitch", template: '<div class="mode-switch-stub" />' },
}));

vi.mock("../services/api", () => ({
  api: {
    sendTurn: vi.fn(),
    getSessionContext: vi.fn(),
    stopTurn: vi.fn(),
    cancelContinuation: vi.fn(),
    saveDrafts: vi.fn(),
    saveBoardState: vi.fn(),
    fetchBoardState: vi.fn(),
    fetchVisibleRange: vi.fn(),
    previewMaterialImpact: vi.fn(),
    submitBoard: vi.fn(),
    fetchIntents: vi.fn(),
    approveIntent: vi.fn(),
    rejectIntent: vi.fn(),
    batchDecide: vi.fn(),
    createDemoIntents: vi.fn(),
    advanceIntent: vi.fn(),
    updateIntentPreview: vi.fn(),
    fetchHistoryPage: vi.fn(),
    fetchMessages: vi.fn(),
  },
}));

vi.mock("../services/interactive", () => ({
  fetchBoardState: vi.fn(),
  saveBoardState: vi.fn(),
  saveDrafts: vi.fn(),
  fetchVisibleRange: vi.fn(),
  previewMaterialImpact: vi.fn(),
  checkMaterialImpact: vi.fn(),
  submitBoard: vi.fn(),
  fetchIntents: vi.fn(),
}));

function seedDisplaced(session: ReturnType<typeof useSessionStore>, text: string): DisplacedChatDraft {
  const record: DisplacedChatDraft = {
    id: "disp_1",
    topicId: session.currentTopicId,
    key: "qio.chat.draft.v1:" + (session.currentTopicId ?? "unbound"),
    text,
    seq: 1,
    at: Date.now(),
  };
  session.chatDraftDisplaced = [record];
  return record;
}

/** 挂载输入区（模式切换与本组用例无关：避免测试环境缺少 router） */
function mountDock() {
  return mount(ComposerDock, { attachTo: document.body });
}

/** v2：入口①默认收起；有被让位草稿时图标才出现，点开才渲染面板 */
async function openSendIssues(wrapper: ReturnType<typeof mount>): Promise<void> {
  const entry = wrapper.find('.entry-btn[data-entry="sendIssues"]');
  expect(entry.exists(), "被让位的旧草稿没有让入口①出现").toBe(true);
  await entry.trigger("click");
  await flushPromises();
}

beforeEach(() => {
  localStorage.clear();
  setActivePinia(createPinia());
});

afterEach(() => {
  localStorage.clear();
});

describe("11a 被让位的旧草稿入口", () => {
  it("【组件/DOM】对话页：入口可见；取回 = 与当前文字互换（两份都还在）", async () => {
    const session = useSessionStore();
    session.draft = "现在正在写的新文字";
    seedDisplaced(session, "话题里原来的旧草稿");
    const wrapper = mountDock();
    await flushPromises();
    await openSendIssues(wrapper);

    expect(wrapper.text(), "被让位的旧草稿入口不可见").toContain("这个话题原本还有一份草稿");
    expect(wrapper.text()).toContain("话题里原来的旧草稿");
    expect(wrapper.find('[data-im="composer-displaced-restore"]').exists()).toBe(true);
    expect(wrapper.find('[data-im="composer-displaced-discard"]').exists()).toBe(true);

    await wrapper.find('[data-im="composer-displaced-restore"]').trigger("click");
    await flushPromises();
    // 互换：输入框变成旧草稿，刚才的文字留在入口里
    expect(session.draft).toBe("话题里原来的旧草稿");
    expect(session.chatDraftDisplaced.map((item) => item.text)).toEqual(["现在正在写的新文字"]);
    wrapper.unmount();
  });

  it("【组件/DOM】对话页：放弃只清登记，不动当前输入", async () => {
    const session = useSessionStore();
    session.draft = "现在正在写的新文字";
    seedDisplaced(session, "话题里原来的旧草稿");
    const wrapper = mountDock();
    await flushPromises();
    await openSendIssues(wrapper);
    await wrapper.find('[data-im="composer-displaced-discard"]').trigger("click");
    await flushPromises();
    expect(session.chatDraftDisplaced).toEqual([]);
    expect(session.draft).toBe("现在正在写的新文字");
    wrapper.unmount();
  });

  it("【组件/DOM】互动聊天：同一份会话状态，入口同样可用", async () => {
    const interactive = useInteractiveStore();
    interactive.chatOpen = true;
    const session = useSessionStore();
    session.draft = "聊天里当前的新文字";
    seedDisplaced(session, "聊天里被让位的旧草稿");
    const wrapper = mount(ChatDock, { attachTo: document.body });
    await flushPromises();
    expect(wrapper.text()).toContain("聊天里被让位的旧草稿");
    const btn = wrapper.find('[data-im="chat-displaced-restore"]');
    expect(btn.exists()).toBe(true);
    await btn.trigger("click");
    await flushPromises();
    expect(session.draft).toBe("聊天里被让位的旧草稿");
    wrapper.unmount();
  });
});
