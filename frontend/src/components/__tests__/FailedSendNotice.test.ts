/**
 * 失败原文的恢复入口（组件级，契约 §11.4 / §11.8）。
 *
 * 覆盖两个入口：普通对话页的输入区与互动模式的悬浮聊天。两者用同一份会话事实，
 * 所以断言的是「用户能看到、能点到、能拿回文字」：
 * 1. 失败后有真实原因与恢复入口，原文较长时预览也不丢全文（v2：入口①面板里用
 *    data-im="composer-recovery-quote" 的 title 携带完整原文，找回时原样放回）；
 * 2. 「找回原文」只把文字放回输入框，**不发送、不自动重试**；
 * 3. 输入框已有新文字时不覆盖；「与当前文字互换」两份都保留；
 * 4. 「不再保留」清掉这一条，不动输入框、不发送；
 * 5. 连续两次失败两条都列出来，各自带自己的原因；
 * 6. ChatDock 的说明区不再是 <p> 包 <details> 的无效结构。
 *
 * v2（2026-10-22）：对话页的这些界面不再内联渲染在 Composer.vue，而是移到输入区上方
 * 入口①（ComposerDock → ComposerEntries → SendIssuesPanel）。入口默认收起：
 * 有事项时图标才出现，点开才渲染面板 —— 所以对话页用例先打开入口①再断言。
 *
 * 标注：【组件】只挂载真实组件 + 【模拟失败】受控的 api.sendTurn 拒绝。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { flushPromises, mount } from "@vue/test-utils";
import { nextTick } from "vue";
import ComposerDock from "../conversation/ComposerDock.vue";
import ChatDock from "../interactive/ChatDock.vue";
import { SEND_REJECTED_NOTICE, useSessionStore } from "../../stores/session";
import { useInteractiveStore } from "../../stores/interactive";
import { api } from "../../services/api";
import * as imApi from "../../services/interactive";

// 模式切换与这些用例无关：用桩替换，避免测试环境缺少 router 造成噪声。
vi.mock("../conversation/ConversationModeSwitch.vue", () => ({
  default: { name: "ConversationModeSwitch", template: '<div class="mode-switch-stub" />' },
}));

vi.mock("../../services/api", () => ({
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

vi.mock("../../services/interactive", () => ({
  saveDrafts: vi.fn(),
  fetchBoardState: vi.fn(),
  saveBoardState: vi.fn(),
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
}));

function acceptedSend(turnId = "turn_1") {
  return { ok: true, accepted: true, turn_id: turnId, status: "accepted", topic_id: null };
}

/** 会话层层面积累一条失败原文（组件测试只关心界面怎么呈现它） */
async function failOnceInSession(text: string, reason: string) {
  const session = useSessionStore();
  vi.mocked(api.sendTurn).mockRejectedValueOnce(rejectedSend(reason));
  session.draft = text;
  const attribution = session.sendAttribution();
  session.draft = "";
  await session.send(text);
  session.retryFailedSend(attribution.draftId);
  return session.failedSendsForTopic(session.currentTopicId).find((record) => record.text === text)!;
}

function textareaValue(wrapper: ReturnType<typeof mount>, selector: string): string {
  return (wrapper.find(selector).element as HTMLTextAreaElement).value;
}

/** 挂载输入区（模式切换与本组用例无关：避免测试环境缺少 router） */
function mountDock() {
  return mount(ComposerDock, { attachTo: document.body });
}

/** v2：入口①默认收起；有失败事项时图标才出现，点开才渲染面板 */
async function openSendIssues(wrapper: ReturnType<typeof mount>): Promise<void> {
  const entry = wrapper.find('.entry-btn[data-entry="sendIssues"]');
  expect(entry.exists(), "发送失败后没有出现「发送异常与草稿恢复」入口").toBe(true);
  await entry.trigger("click");
  await flushPromises();
}

let wrappers: Array<{ unmount: () => void }> = [];

function track<T extends { unmount: () => void }>(wrapper: T): T {
  wrappers.push(wrapper);
  return wrapper;
}

beforeEach(() => {
  localStorage.clear();
  wrappers = [];
  vi.resetAllMocks();
  vi.mocked(api.sendTurn).mockResolvedValue(acceptedSend() as never);
  vi.mocked(api.getSessionContext).mockResolvedValue({ topic_id: null } as never);
  vi.mocked(imApi.saveDrafts).mockResolvedValue({ drafts: {}, updatedAt: "2026-10-08T00:00:00.000Z" } as never);
  setActivePinia(createPinia());
});

afterEach(() => {
  for (const wrapper of wrappers) wrapper.unmount();
  vi.useRealTimers();
  localStorage.clear();
});

function rejectedSend(message: string) {
  // 合并后的契约（main 契约 5 + 互动线 §11.4）：只有**明确未受理**（4xx）才登记
  // 可恢复原文并显示失败入口；没有回执一律走「正在确认」。这些用例要考的是
  // 「明确失败后的恢复入口」，所以把 mock 换成带 status 的明确拒绝。
  return Object.assign(new Error(message), { status: 400 });
}

describe("对话页（ComposerDock 入口①）的失败恢复入口（§11.4）", () => {
  it("【组件】失败后显示真实原因与恢复入口；找回原文只放回输入框，不重发", async () => {
    vi.useFakeTimers();
    const session = useSessionStore();
    session.currentTopicId = "A";
    const wrapper = track(mountDock());
    await flushPromises();

    vi.mocked(api.sendTurn).mockRejectedValueOnce(rejectedSend("网络中断"));
    await wrapper.find("#composer-input").setValue("对话页失败的话");
    await nextTick();
    await wrapper.find(".send-btn").trigger("click");
    await flushPromises();
    await vi.advanceTimersByTimeAsync(600);
    await flushPromises();

    // 真实原因（本次请求的事实）+ 恢复入口：v2 在入口①面板里
    await openSendIssues(wrapper);
    const recovery = wrapper.find('[data-im="composer-recovery"]');
    expect(recovery.exists()).toBe(true);
    // 合并后的契约：4xx 是**明确拒绝**，提示行走 main 的统一口径（契约 5）；
    // 本次请求的真实原因仍如实记进失败事实（互动线 §11.6：不误读上一次结果）。
    // 统一口径现在由页面级状态区（ChatColumn 的 status-zone）呈现，入口①面板
    // 保留本次请求的真实原因 —— 比只显示统一口径更有信息量。
    expect(session.lastError).toBe(SEND_REJECTED_NOTICE);
    const composerFailed = session.failedSends[0];
    expect(session.failedSendErrors[composerFailed?.id ?? ""] ?? "").toContain("网络中断");
    // 失败后原文自动放回空输入框（Composer 的行为不变）
    expect(textareaValue(wrapper, "#composer-input")).toBe("对话页失败的话");
    const reasonRow = wrapper.find('[data-im="composer-recovery-reason"]');
    expect(reasonRow.exists()).toBe(true);
    expect(reasonRow.text()).toContain("网络中断");

    // 用户清空输入框后，「找回原文」把原文放回来，且**不发出去**
    await wrapper.find("#composer-input").setValue("");
    await nextTick();
    const callsBefore = vi.mocked(api.sendTurn).mock.calls.length;
    const restore = wrapper.find('[data-im="composer-recovery-restore"]');
    expect(restore.exists()).toBe(true);
    await restore.trigger("click");
    await flushPromises();
    expect(textareaValue(wrapper, "#composer-input")).toBe("对话页失败的话");
    expect(vi.mocked(api.sendTurn).mock.calls.length).toBe(callsBefore);
  });

  it("【组件】输入框已有新文字：不覆盖；「与当前文字互换」两份都保留", async () => {
    vi.useFakeTimers();
    const session = useSessionStore();
    session.currentTopicId = "A";
    const wrapper = track(mountDock());
    await flushPromises();

    vi.mocked(api.sendTurn).mockRejectedValueOnce(rejectedSend("网关超时"));
    await wrapper.find("#composer-input").setValue("原来的失败原文");
    await nextTick();
    await wrapper.find(".send-btn").trigger("click");
    await flushPromises();
    await vi.advanceTimersByTimeAsync(600);
    await flushPromises();
    await openSendIssues(wrapper);

    // 用户随后写下的是另一段文字：不覆盖、两份都在
    await wrapper.find("#composer-input").setValue("后来写的新文字");
    await nextTick();
    const callsBefore = vi.mocked(api.sendTurn).mock.calls.length;
    // v2 面板同时给出「找回」与「互换」；输入框非空时找回只回报原因，绝不覆盖
    await wrapper.find('[data-im="composer-recovery-restore"]').trigger("click");
    await flushPromises();
    expect(textareaValue(wrapper, "#composer-input")).toBe("后来写的新文字");
    expect(session.failedSendsForTopic("A")[0]?.text).toBe("原来的失败原文");

    expect(wrapper.find('[data-im="composer-recovery-swap"]').exists()).toBe(true);
    await wrapper.find('[data-im="composer-recovery-swap"]').trigger("click");
    await flushPromises();
    // 输入框拿到失败原文，刚才那份留在恢复入口里
    expect(textareaValue(wrapper, "#composer-input")).toBe("原来的失败原文");
    expect(wrapper.find('[data-im="composer-recovery-quote"]').text()).toBe("后来写的新文字");
    expect(vi.mocked(api.sendTurn).mock.calls.length).toBe(callsBefore);
  });

  it("【组件】「不再保留」清掉这一条：不动输入框、不发送", async () => {
    vi.useFakeTimers();
    const session = useSessionStore();
    session.currentTopicId = "A";
    const wrapper = track(mountDock());
    await flushPromises();

    vi.mocked(api.sendTurn).mockRejectedValueOnce(rejectedSend("网络中断"));
    await wrapper.find("#composer-input").setValue("不再保留的原文");
    await nextTick();
    await wrapper.find(".send-btn").trigger("click");
    await flushPromises();
    await vi.advanceTimersByTimeAsync(600);
    await flushPromises();
    await openSendIssues(wrapper);

    const callsBefore = vi.mocked(api.sendTurn).mock.calls.length;
    await wrapper.find('[data-im="composer-recovery-discard"]').trigger("click");
    await flushPromises();
    expect(wrapper.find('[data-im="composer-recovery"]').exists()).toBe(false);
    expect(session.failedSendsForTopic("A")).toEqual([]);
    expect(textareaValue(wrapper, "#composer-input")).toBe("不再保留的原文");
    expect(vi.mocked(api.sendTurn).mock.calls.length).toBe(callsBefore);
  });

  it("【组件】连续两次失败：两条都列出来，各带自己的原因，长文预览不丢全文", async () => {
    vi.useFakeTimers();
    const session = useSessionStore();
    session.currentTopicId = "A";
    const longText = "很长的失败原文".repeat(12);
    await failOnceInSession("第一次失败的话", "第一次：网络中断");
    await failOnceInSession(longText, "第二次：网关超时");
    // 用户清空输入框：两条都应该能以「找回原文」取回
    session.draft = "";
    const wrapper = track(mountDock());
    await flushPromises();
    await openSendIssues(wrapper);

    const items = wrapper.findAll('[data-im="composer-recovery-item"]');
    expect(items.length).toBe(2);
    const text = wrapper.text();
    expect(text).toContain("第一次失败的话");
    expect(text).toContain("第一次：网络中断");
    expect(text).toContain("第二次：网关超时");
    // v2 列表是精简预览：长文在界面上截断显示，但完整原文仍在元素上（title），
    // 且「找回原文」放回的是**完整**原文 —— 原文一个字都没丢。
    const quotes = wrapper.findAll('[data-im="composer-recovery-quote"]');
    expect(quotes[0].attributes("title")).toBe(longText);
    expect(quotes[0].text().length).toBeLessThan(longText.length);
    expect(wrapper.findAll('[data-im="composer-recovery-restore"]').length).toBe(2);
    await wrapper.findAll('[data-im="composer-recovery-restore"]')[0].trigger("click");
    await flushPromises();
    expect(session.draft).toBe(longText);
    expect(textareaValue(wrapper, "#composer-input")).toBe(longText);
    // 可访问名称说清了这组入口是什么
    expect(
      wrapper.find('[data-im="composer-recovery"]').attributes("aria-label"),
    ).toContain("上一次没有发出去的文字");
  });
});

describe("悬浮聊天（ChatDock）的失败恢复入口（§11.4 / §11.8）", () => {
  it("【组件】失败后同样有原因与恢复入口；找回原文不重发", async () => {
    vi.useFakeTimers();
    const session = useSessionStore();
    session.currentTopicId = "A";
    useInteractiveStore().chatOpen = true;
    const wrapper = track(mount(ChatDock, { attachTo: document.body }));
    await flushPromises();

    vi.mocked(api.sendTurn).mockRejectedValueOnce(rejectedSend("网络中断"));
    await wrapper.find('[data-im="chat-input"]').setValue("悬浮聊天失败的话");
    await nextTick();
    await wrapper.find('[data-im="chat-send"]').trigger("click");
    await flushPromises();
    await vi.advanceTimersByTimeAsync(600);
    await flushPromises();

    const failure = wrapper.find('[data-im="chat-failure"]');
    expect(failure.exists()).toBe(true);
    // 同 Composer：明确拒绝统一口径 + 真实原因进失败事实
    expect(failure.text()).toContain("被拒绝");
    const dockFailed = session.failedSends[0];
    expect(session.failedSendErrors[dockFailed?.id ?? ""] ?? "").toContain("网络中断");
    expect(failure.text()).toContain("输入已保留");
    expect(wrapper.find('[data-im="chat-recovery"]').exists()).toBe(true);
    expect(textareaValue(wrapper, '[data-im="chat-input"]')).toBe("悬浮聊天失败的话");

    const callsBefore = vi.mocked(api.sendTurn).mock.calls.length;
    await wrapper.find('[data-im="chat-input"]').setValue("");
    await nextTick();
    await wrapper.find('[data-im="chat-recovery-restore"]').trigger("click");
    await flushPromises();
    expect(textareaValue(wrapper, '[data-im="chat-input"]')).toBe("悬浮聊天失败的话");
    expect(vi.mocked(api.sendTurn).mock.calls.length).toBe(callsBefore);
  });

  it("【组件】输入框下方的帮助整块已删除（2026-10-14 用户要求），输入区本身仍在", async () => {
    useInteractiveStore().chatOpen = true;
    const wrapper = track(mount(ChatDock, { attachTo: document.body }));
    await flushPromises();
    expect(wrapper.find('[data-im="chat-scope"]').exists(), "帮助块还在").toBe(false);
    expect(wrapper.find('[data-im="chat-help-toggle"]').exists(), "标题栏的帮助按钮还在").toBe(false);
    expect(wrapper.find("details").exists(), "帮助的 details 结构还在").toBe(false);
    expect(wrapper.text()).not.toContain("文字发送不会提交板面");
    expect(wrapper.find('[data-im="chat-composer"]').exists(), "输入区本身不该被删").toBe(true);
    expect(wrapper.find('[data-im="chat-input"]').exists()).toBe(true);
  });
});
