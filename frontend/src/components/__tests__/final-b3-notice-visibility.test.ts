/**
 * 收尾轮 15 的组件反例（Lead 接手 B3 范围）：
 *
 * - 普通对话页必须显示共享聊天草稿的保存失败状态与重试入口（v2：输入区上方入口①）；
 * - FailedSendNotice 的错误显示不能依赖「已经成功恢复出内容」：
 *   零条记录 + 读取失败 / 写入失败都要显示真实错误，且不虚报「已丢失 / 已恢复」。
 *
 * v2（2026-10-22）：对话页的发送异常/草稿恢复移到输入区上方入口①
 * （ComposerDock → ComposerEntries → SendIssuesPanel），入口默认收起、点开才渲染；
 * FailedSendNotice 只剩悬浮聊天（scope="chat"）一个使用方，本文件按它真实的作用域断言。
 *
 * 标注：【组件/DOM】挂载真实 ComposerDock 与 FailedSendNotice.vue；
 *      【模拟失败】直接设置会话层的真实失败状态（等同存储不可用/读取失败）。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { flushPromises, mount } from "@vue/test-utils";
import { useSessionStore } from "../../stores/session";
import ComposerDock from "../conversation/ComposerDock.vue";
import FailedSendNotice from "../interactive/FailedSendNotice.vue";

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

/** 挂载输入区（模式切换与本组用例无关：避免测试环境缺少 router） */
function mountDock() {
  return mount(ComposerDock, { attachTo: document.body });
}

/** v2：入口①默认收起；有事项时图标才出现，点开才渲染面板 */
async function openSendIssues(wrapper: ReturnType<typeof mount>): Promise<void> {
  const entry = wrapper.find('.entry-btn[data-entry="sendIssues"]');
  expect(entry.exists(), "草稿保存失败没有让入口①出现").toBe(true);
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

describe("15 普通对话页要看得见聊天草稿保存失败", () => {
  it("【组件/DOM】草稿保存失败时显示真实原因与重试入口，重试只重写草稿", async () => {
    const session = useSessionStore();
    session.draftSaveStatus = "error";
    session.draftSaveError = "本地存储被禁用";
    const retry = vi.spyOn(session, "retryDraftSave").mockResolvedValue(undefined);
    const wrapper = mountDock();
    await flushPromises();
    await openSendIssues(wrapper);

    expect(wrapper.text(), "对话页看不到共享聊天草稿的保存失败").toContain("本地存储被禁用");
    expect(wrapper.text(), "提示要说清是「草稿没有保存进本机」").toContain("草稿没有保存进本机");
    expect(wrapper.find('[data-im="composer-draft-status"]').exists()).toBe(true);
    const btn = wrapper.find('[data-im="composer-draft-retry"]');
    expect(btn.exists(), "对话页没有重试保存入口").toBe(true);
    await btn.trigger("click");
    expect(retry).toHaveBeenCalled();
    wrapper.unmount();
  });

  it("【组件/DOM】草稿保存正常时不显示失败提示", async () => {
    const session = useSessionStore();
    session.draftSaveStatus = "saved";
    session.draftSaveError = null;
    const wrapper = mountDock();
    await flushPromises();
    // 没有事项 → 入口行不渲染；面板从未挂载，自然也没有保存失败提示
    expect(wrapper.find('.entry-btn[data-entry="sendIssues"]').exists()).toBe(false);
    expect(wrapper.find('[data-im="composer-draft-status"]').exists()).toBe(false);
    expect(wrapper.text()).not.toContain("草稿没有保存进本机");
    wrapper.unmount();
  });
});

describe("15 失败原文：零条记录也要显示真实存储错误", () => {
  it("【组件/DOM】零条记录 + 写入失败：仍然显示「没能保存在本机」", async () => {
    const session = useSessionStore();
    session.failedSends = [];
    session.failedSendPersistError = "本地存储被禁用";
    // FailedSendNotice 在 v2 只由悬浮聊天使用（scope 默认 chat）
    const wrapper = mount(FailedSendNotice, { attachTo: document.body });
    await flushPromises();
    expect(wrapper.text()).toContain("没能保存在本机");
    expect(wrapper.text()).toContain("本地存储被禁用");
    expect(wrapper.find('[data-im="chat-recovery-persist"]').exists()).toBe(true);
    wrapper.unmount();
  });

  it("【组件/DOM】零条记录 + 读取失败：显示读取错误与「不代表原文已经丢失」，并提供重新读取", async () => {
    const session = useSessionStore();
    session.failedSends = [];
    session.failedSendPersistError = null;
    (session as unknown as { failedSendReadError: string | null }).failedSendReadError = "本机存储读取被拒绝";
    const retryRead = vi.fn();
    (session as unknown as { retryFailedSendRestore: () => void }).retryFailedSendRestore = retryRead;
    const wrapper = mount(FailedSendNotice, { attachTo: document.body });
    await flushPromises();
    expect(wrapper.text(), "零条恢复出来时读取失败被吞掉").toContain("读取本机保存的失败原文时出错");
    expect(wrapper.text()).toContain("不代表原文已经丢失");
    const btn = wrapper.find('[data-im="chat-recovery-read-retry"]');
    expect(btn.exists(), "读取失败没有重新读取入口").toBe(true);
    await btn.trigger("click");
    expect(retryRead).toHaveBeenCalled();
    wrapper.unmount();
  });

  it("【组件/DOM】零条记录且没有错误：不出现恢复区，也不提「已丢失」", async () => {
    const session = useSessionStore();
    session.failedSends = [];
    session.failedSendPersistError = null;
    (session as unknown as { failedSendReadError: string | null }).failedSendReadError = null;
    const wrapper = mount(FailedSendNotice, { attachTo: document.body });
    await flushPromises();
    expect(wrapper.find(".failed-send").exists()).toBe(false);
    expect(wrapper.text()).not.toContain("丢失");
    wrapper.unmount();
  });
});
