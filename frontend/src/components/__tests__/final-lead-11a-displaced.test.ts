/**
 * 收尾轮 11(a) 的组件证据（Lead 侧，B3 文件域）：
 *
 * 未绑定新输入迁入话题 A 时，A 里原有的草稿被让位但**不删** ——
 * 对话页与互动聊天都要给出「取回（互换）/ 放弃」入口，两份都保留、不拼接、不自动发送。
 *
 * 标注：【组件/DOM】挂载真实 Composer.vue / ChatDock.vue + 真实会话 store。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { flushPromises, mount } from "@vue/test-utils";
import { useSessionStore, type DisplacedChatDraft } from "../../stores/session";
import Composer from "../Composer.vue";
import ChatDock from "../interactive/ChatDock.vue";
import { useInteractiveStore } from "../../stores/interactive";

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
    const wrapper = mount(Composer, { attachTo: document.body });
    await flushPromises();

    expect(wrapper.text(), "被让位的旧草稿入口不可见").toContain("这个话题原来还有一份草稿");
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
    const wrapper = mount(Composer, { attachTo: document.body });
    await flushPromises();
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
