/**
 * 失败原文的恢复入口（组件级，契约 §11.4 / §11.8）。
 *
 * 覆盖两个入口：普通对话页的输入区与互动模式的悬浮聊天。两者用同一个组件，
 * 所以断言的是「用户能看到、能点到、能拿回文字」：
 * 1. 失败后有真实原因与恢复入口，原文较长也不截断（限高滚动由样式承担）；
 * 2. 「找回原文」只把文字放回输入框，**不发送、不自动重试**；
 * 3. 输入框已有新文字时改为「与当前文字互换」，两份都保留；
 * 4. 「不再保留」清掉这一条，不动输入框、不发送；
 * 5. 连续两次失败两条都列出来，各自带自己的原因；
 * 6. ChatDock 的说明区不再是 <p> 包 <details> 的无效结构。
 *
 * 标注：【组件】只挂载真实组件 + 【模拟失败】受控的 api.sendTurn 拒绝。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { flushPromises, mount } from "@vue/test-utils";
import { nextTick } from "vue";
import Composer from "../Composer.vue";
import ChatDock from "../interactive/ChatDock.vue";
import { useSessionStore } from "../../stores/session";
import { useInteractiveStore } from "../../stores/interactive";
import { api } from "../../services/api";
import * as imApi from "../../services/interactive";

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
  vi.mocked(api.sendTurn).mockRejectedValueOnce(new Error(reason));
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

describe("对话页（Composer）的失败恢复入口（§11.4）", () => {
  it("【组件】失败后显示真实原因与恢复入口；找回原文只放回输入框，不重发", async () => {
    vi.useFakeTimers();
    const session = useSessionStore();
    session.currentTopicId = "A";
    const wrapper = track(mount(Composer, { attachTo: document.body }));
    await flushPromises();

    vi.mocked(api.sendTurn).mockRejectedValueOnce(new Error("网络中断"));
    await wrapper.find("#composer-input").setValue("对话页失败的话");
    await nextTick();
    await wrapper.find(".send-btn").trigger("click");
    await flushPromises();
    await vi.advanceTimersByTimeAsync(600);
    await flushPromises();

    // 真实原因（本次请求的事实）+ 恢复入口
    const failure = wrapper.find('[data-im="composer-failure"]');
    expect(failure.exists()).toBe(true);
    expect(failure.text()).toContain("网络中断");
    expect(wrapper.find('[data-im="composer-recovery"]').exists()).toBe(true);
    // 失败后原文自动放回空输入框：入口说明它已经在里面，不再重复显示原因
    expect(textareaValue(wrapper, "#composer-input")).toBe("对话页失败的话");
    expect(wrapper.find('[data-im="composer-recovery-in-input"]').exists()).toBe(true);
    expect(wrapper.find('[data-im="composer-recovery-reason"]').exists()).toBe(false);

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

  it("【组件】输入框已有新文字：改为「与当前文字互换」，两份都保留", async () => {
    vi.useFakeTimers();
    const session = useSessionStore();
    session.currentTopicId = "A";
    const wrapper = track(mount(Composer, { attachTo: document.body }));
    await flushPromises();

    vi.mocked(api.sendTurn).mockRejectedValueOnce(new Error("网关超时"));
    await wrapper.find("#composer-input").setValue("原来的失败原文");
    await nextTick();
    await wrapper.find(".send-btn").trigger("click");
    await flushPromises();
    await vi.advanceTimersByTimeAsync(600);
    await flushPromises();

    // 用户随后写下的是另一段文字：不覆盖、两份都在
    await wrapper.find("#composer-input").setValue("后来写的新文字");
    await nextTick();
    expect(wrapper.find('[data-im="composer-recovery-swap"]').exists()).toBe(true);
    expect(wrapper.find('[data-im="composer-recovery-restore"]').exists()).toBe(false);

    await wrapper.find('[data-im="composer-recovery-swap"]').trigger("click");
    await flushPromises();
    // 输入框拿到失败原文，刚才那份留在恢复入口里
    expect(textareaValue(wrapper, "#composer-input")).toBe("原来的失败原文");
    expect(wrapper.find('[data-im="composer-recovery-quote"]').text()).toBe("后来写的新文字");
    expect(vi.mocked(api.sendTurn).mock.calls.length).toBe(1);
  });

  it("【组件】「不再保留」清掉这一条：不动输入框、不发送", async () => {
    vi.useFakeTimers();
    const session = useSessionStore();
    session.currentTopicId = "A";
    const wrapper = track(mount(Composer, { attachTo: document.body }));
    await flushPromises();

    vi.mocked(api.sendTurn).mockRejectedValueOnce(new Error("网络中断"));
    await wrapper.find("#composer-input").setValue("不再保留的原文");
    await nextTick();
    await wrapper.find(".send-btn").trigger("click");
    await flushPromises();
    await vi.advanceTimersByTimeAsync(600);
    await flushPromises();

    const callsBefore = vi.mocked(api.sendTurn).mock.calls.length;
    await wrapper.find('[data-im="composer-recovery-discard"]').trigger("click");
    await flushPromises();
    expect(wrapper.find('[data-im="composer-recovery"]').exists()).toBe(false);
    expect(session.failedSendsForTopic("A")).toEqual([]);
    expect(textareaValue(wrapper, "#composer-input")).toBe("不再保留的原文");
    expect(vi.mocked(api.sendTurn).mock.calls.length).toBe(callsBefore);
  });

  it("【组件】连续两次失败：两条都列出来，各带自己的原因，长文不截断", async () => {
    vi.useFakeTimers();
    const session = useSessionStore();
    session.currentTopicId = "A";
    const longText = "很长的失败原文".repeat(12);
    await failOnceInSession("第一次失败的话", "第一次：网络中断");
    await failOnceInSession(longText, "第二次：网关超时");
    // 用户清空输入框：两条都应该能以「找回原文」取回
    session.draft = "";
    const wrapper = track(mount(Composer, { attachTo: document.body }));
    await flushPromises();

    const items = wrapper.findAll('[data-im="composer-recovery-item"]');
    expect(items.length).toBe(2);
    const text = wrapper.text();
    expect(text).toContain("第一次失败的话");
    expect(text).toContain("第一次：网络中断");
    expect(text).toContain("第二次：网关超时");
    // 原文完整留在 DOM 里（限高滚动，不裁掉内容）
    expect(wrapper.find('[data-im="composer-recovery-quote"]').text()).toBe(longText);
    expect(wrapper.findAll('[data-im="composer-recovery-restore"]').length).toBe(2);
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

    vi.mocked(api.sendTurn).mockRejectedValueOnce(new Error("网络中断"));
    await wrapper.find('[data-im="chat-input"]').setValue("悬浮聊天失败的话");
    await nextTick();
    await wrapper.find('[data-im="chat-send"]').trigger("click");
    await flushPromises();
    await vi.advanceTimersByTimeAsync(600);
    await flushPromises();

    const failure = wrapper.find('[data-im="chat-failure"]');
    expect(failure.exists()).toBe(true);
    expect(failure.text()).toContain("网络中断");
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

  it("【组件】说明区不再是 <p> 包 <details> 的无效结构，说明内容仍可打开", async () => {
    useInteractiveStore().chatOpen = true;
    const wrapper = track(mount(ChatDock, { attachTo: document.body }));
    await flushPromises();
    expect(wrapper.find("p details").exists()).toBe(false);
    expect(wrapper.find("details").exists()).toBe(true);
    const scope = wrapper.find('[data-im="chat-scope"]');
    expect(scope.text()).toContain("文字发送不会提交板面");
    expect(scope.find("details").exists()).toBe(true);
    expect(wrapper.find('[data-im="chat-scope-details"]').exists()).toBe(true);
  });
});
