/**
 * 子 agent B 定向验收（F12）：两个聊天入口共用未决发送的状态规则与恢复动作。
 *
 * 要点：
 * - 普通对话页（Composer）与悬浮聊天（ChatDock）对同一次未决发送给**同一套**结果与动作；
 * - 在原话题提供查证 / 同身份重试 / 查看原文 / 明确放弃；切到别的话题一个字都不串；
 * - 输入框为空时不得再提示「输入已保留，可以重试」——原文保存在独立记录里，要说清在哪里；
 * - 收起 / 展开面板不自动发送、不丢状态。
 *
 * 标注：【组件/DOM】挂载真实 Composer / ChatDock；【模拟失败】受控 api.sendTurn。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { flushPromises, mount } from "@vue/test-utils";
import { nextTick } from "vue";
import ChatDock from "../interactive/ChatDock.vue";
import Composer from "../Composer.vue";
import FailedSendNotice from "../interactive/FailedSendNotice.vue";
import { useSessionStore } from "../../stores/session";
import { useInteractiveStore } from "../../stores/interactive";
import { api } from "../../services/api";

vi.mock("../../services/api", () => ({
  api: {
    sendTurn: vi.fn(),
    getSessionContext: vi.fn(),
    stopTurn: vi.fn(),
    cancelContinuation: vi.fn(),
    cancelTurn: vi.fn(),
    cancelActiveTurn: vi.fn(),
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

let wrappers: Array<{ unmount: () => void }> = [];
function track<T extends { unmount: () => void }>(wrapper: T): T {
  wrappers.push(wrapper);
  return wrapper;
}

beforeEach(() => {
  localStorage.clear();
  vi.useFakeTimers();
  wrappers = [];
  vi.resetAllMocks();
  vi.mocked(api.sendTurn).mockResolvedValue({
    ok: true,
    accepted: true,
    turn_id: "turn_ok",
    status: "accepted",
    topic_id: "A",
  } as never);
  vi.mocked(api.getSessionContext).mockResolvedValue({ topic_id: null } as never);
  setActivePinia(createPinia());
});

afterEach(() => {
  for (const wrapper of wrappers) wrapper.unmount();
  vi.useRealTimers();
  localStorage.clear();
});

/** 用悬浮聊天真的发一条、并真的失败（受理情况未知） */
async function failThroughDock(text: string): Promise<ReturnType<typeof mount>> {
  const session = useSessionStore();
  session.currentTopicId = "A";
  useInteractiveStore().chatOpen = true;
  const wrapper = track(mount(ChatDock, { attachTo: document.body }));
  await flushPromises();
  vi.mocked(api.sendTurn).mockRejectedValueOnce(new Error("网络中断"));
  await wrapper.find('[data-im="chat-input"]').setValue(text);
  await nextTick();
  await wrapper.find('[data-im="chat-send"]').trigger("click");
  await flushPromises();
  await vi.advanceTimersByTimeAsync(800);
  await flushPromises();
  return wrapper;
}

describe("F12：悬浮聊天的未决发送入口", () => {
  it("网络失败后：原文留在未决台账里，输入框是空的，界面给四个明确动作", async () => {
    const wrapper = await failThroughDock("还没确认发出去的话");
    const session = useSessionStore();

    expect(session.draft, "未决时原文被当成输入框里的新草稿").toBe("");
    expect(
      session.unresolvedSendsForTopic("A").map((record) => record.snapshot.text),
      "未决发送的原文没有保存在会话层",
    ).toEqual(["还没确认发出去的话"]);

    const block = wrapper.find('[data-im="chat-pending"]');
    expect(block.exists(), "悬浮聊天没有未决发送入口").toBe(true);
    expect(wrapper.find('[data-im="chat-pending-recheck"]').exists()).toBe(true);
    expect(wrapper.find('[data-im="chat-pending-retry"]').exists()).toBe(true);
    expect(wrapper.find('[data-im="chat-pending-view"]').exists()).toBe(true);
    expect(wrapper.find('[data-im="chat-pending-abandon"]').exists()).toBe(true);

    // 输入框为空时不许提示「输入已保留，可以重试」
    expect(wrapper.text()).not.toContain("输入已保留");
    expect(wrapper.text()).toContain("网络中断");

    // 查看原文：只读，能看到完整原文，且不改输入框、不发送
    const callsBefore = vi.mocked(api.sendTurn).mock.calls.length;
    await wrapper.find('[data-im="chat-pending-view"]').trigger("click");
    await flushPromises();
    expect(wrapper.find('[data-im="chat-pending-quote"]').text()).toBe("还没确认发出去的话");
    expect(session.draft).toBe("");
    expect(vi.mocked(api.sendTurn).mock.calls.length).toBe(callsBefore);
  });

  it("同身份重试：复用同一个请求身份，成功后就地收尾", async () => {
    const wrapper = await failThroughDock("重试要复用身份的话");
    const session = useSessionStore();
    const requestId = session.unresolvedSendsForTopic("A")[0].clientRequestId;

    vi.mocked(api.sendTurn).mockResolvedValueOnce({
      ok: true,
      accepted: true,
      turn_id: "turn_retry",
      status: "accepted",
      topic_id: "A",
    } as never);
    await wrapper.find('[data-im="chat-pending-retry"]').trigger("click");
    await flushPromises();

    const call = vi.mocked(api.sendTurn).mock.calls[vi.mocked(api.sendTurn).mock.calls.length - 1];
    expect(call[0]).toBe("重试要复用身份的话");
    expect(call[1]).toBe("A");
    expect(session.unresolvedSendsForTopic("A"), "受理成功后未决入口没有收掉").toEqual([]);
  });

  it("明确放弃：撤回消息、原文放回输入框，未决入口收掉", async () => {
    const wrapper = await failThroughDock("要放弃的那条");
    const session = useSessionStore();
    expect(session.unresolvedSendsForTopic("A").length).toBe(1);

    await wrapper.find('[data-im="chat-pending-abandon"]').trigger("click");
    await flushPromises();
    expect(session.unresolvedSendsForTopic("A")).toEqual([]);
    expect(session.messages.some((m) => m.content === "要放弃的那条")).toBe(false);
    expect(session.draft, "放弃后原文没有回到输入框").toBe("要放弃的那条");
  });

  it("切到别的话题：未决这块整个不出现，也不显示那边的错误", async () => {
    const wrapper = await failThroughDock("A 的话题里没定论的话");
    const session = useSessionStore();
    expect(wrapper.find('[data-im="chat-pending"]').exists()).toBe(true);

    session.currentTopicId = "B";
    await flushPromises();
    expect(wrapper.find('[data-im="chat-pending"]').exists(), "B 里串入了 A 的未决发送").toBe(false);
    // 未决状态与原因也不许串到 B：消息气泡本身留在流里（受理情况未知，不能撤），
    // 但「这条还没确认」的提示与原因行只属于 A。
    expect(wrapper.text()).not.toContain("确认这条消息是否已经发出");
    expect(wrapper.text()).not.toContain("原因：网络中断");

    // 回到 A：入口还在，动作照旧
    session.currentTopicId = "A";
    await flushPromises();
    expect(wrapper.find('[data-im="chat-pending"]').exists()).toBe(true);
  });
});

describe("F12：两个入口共用同一套规则与动作", () => {
  it("Composer 与 ChatDock 对同一条未决发送给出相同的状态与四个动作", async () => {
    const session = useSessionStore();
    session.currentTopicId = "A";
    vi.mocked(api.sendTurn).mockRejectedValueOnce(new Error("网络中断"));
    session.draft = "两个入口共用的话";
    session.sendAttribution();
    session.draft = "";
    await session.send("两个入口共用的话");

    const composer = track(mount(Composer, { attachTo: document.body }));
    const dock = track(mount(ChatDock, { attachTo: document.body }));
    useInteractiveStore().chatOpen = true;
    await flushPromises();

    for (const hook of ["pending-recheck", "pending-retry", "pending-view", "pending-abandon"]) {
      expect(composer.find(`[data-im="composer-${hook}"]`).exists(), `Composer 缺 ${hook}`).toBe(true);
      expect(dock.find(`[data-im="chat-${hook}"]`).exists(), `ChatDock 缺 ${hook}`).toBe(true);
    }
    // 两个入口显示同一个状态文案（来自会话层同一份事实）
    const stateText = session.pendingSends[0]
      ? composer.find('[data-im="composer-pending-state"]').text()
      : "";
    expect(stateText).toContain("确认");
    expect(dock.find('[data-im="chat-pending-state"]').text()).toBe(stateText);
    // 输入框为空时两边都不许提示「输入已保留，可以重试」
    expect(composer.text()).not.toContain("输入已保留");
    expect(dock.text()).not.toContain("输入已保留");
  });

  it("Composer 自己发失败（未决）：输入框为空时提示改为说清原文保存在哪里", async () => {
    const session = useSessionStore();
    session.currentTopicId = "A";
    const composer = track(mount(Composer, { attachTo: document.body }));
    await flushPromises();
    vi.mocked(api.sendTurn).mockRejectedValueOnce(new Error("网络中断"));
    await composer.find("#composer-input").setValue("对话页里没定论的话");
    await nextTick();
    await composer.find(".send-btn").trigger("click");
    await flushPromises();
    await vi.advanceTimersByTimeAsync(800);
    await flushPromises();

    const failure = composer.find('[data-im="composer-failure"]');
    expect(failure.exists()).toBe(true);
    expect(failure.text()).toContain("网络中断");
    expect(
      failure.text(),
      "输入框是空的，却还提示「输入已保留，可以重试」",
    ).not.toContain("输入已保留");
    expect(failure.text()).toContain("还没有确认是否发出的文字");
    expect(session.draft).toBe("");
  });

  it("收起再展开面板：不自动发送，未决状态与入口原样保留", async () => {
    const wrapper = await failThroughDock("收起重开还应在的话");
    const session = useSessionStore();
    const interactive = useInteractiveStore();
    const callsBefore = vi.mocked(api.sendTurn).mock.calls.length;

    interactive.chatOpen = false;
    await flushPromises();
    expect(wrapper.find('[data-im="chat-panel"]').exists()).toBe(false);
    interactive.chatOpen = true;
    await flushPromises();

    expect(wrapper.find('[data-im="chat-pending"]').exists()).toBe(true);
    expect(session.unresolvedSendsForTopic("A").map((r) => r.snapshot.text)).toEqual([
      "收起重开还应在的话",
    ]);
    expect(
      vi.mocked(api.sendTurn).mock.calls.length - callsBefore,
      "展开/收起竟然自动发了一次",
    ).toBe(0);
    expect(session.draft, "展开/收起改动或恢复了输入框").toBe("");
  });

  it("新草稿保护：用户在未决期间打的字不被原文覆盖，未决记录也仍在", async () => {
    const session = useSessionStore();
    session.currentTopicId = "A";
    vi.mocked(api.sendTurn).mockRejectedValueOnce(new Error("网络中断"));
    session.draft = "未决的原话";
    session.sendAttribution();
    session.draft = "";
    await session.send("未决的原话");

    const composer = track(mount(Composer, { attachTo: document.body }));
    await flushPromises();
    await composer.find("#composer-input").setValue("用户后来写的新草稿");
    await flushPromises();

    expect(session.draft).toBe("用户后来写的新草稿");
    expect(
      session.unresolvedSendsForTopic("A").map((r) => r.snapshot.text),
      "新输入把未决记录覆盖掉了",
    ).toEqual(["未决的原话"]);
    // 明确点「找回原文」时才互换：两份都保留
    await composer.find('[data-im="composer-pending-view"]').trigger("click");
    await flushPromises();
    expect(composer.find('[data-im="composer-pending-quote"]').text()).toBe("未决的原话");
  });

  it("共用组件在零未决记录时不渲染未决块（不虚报）", async () => {
    const session = useSessionStore();
    session.currentTopicId = "A";
    const wrapper = track(
      mount(FailedSendNotice, { props: { scope: "conversation" }, attachTo: document.body }),
    );
    await flushPromises();
    expect(wrapper.find('[data-im="composer-pending"]').exists()).toBe(false);
  });
});
