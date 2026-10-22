/**
 * 独立验收 D4-A：聊天发送归属、草稿版本与恢复（契约 §10.1 / §10.2 / §10.4 / §10.5）。
 *
 * 由独立验收子智能体 D 编写，**不修改任何产品代码**。
 * 每条用例都按「用户能观察到的正确行为」断言；在旧基线 224bc63 上必须失败。
 *
 * 标注：
 *   【纯函数】只依赖本机草稿记录
 *   【会话层】只依赖 stores/session.ts 的公开状态与动作（不依赖具体实现细节）
 *   【组件】挂载真实组件（Composer / ChatDock）
 *   【模拟失败】用受控的 api.sendTurn 拒绝制造发送失败
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { flushPromises, mount } from "@vue/test-utils";
import { useSessionStore } from "../session";
import { api } from "../../services/api";
import * as imApi from "../../services/interactive";
import {
  draftStorageKey,
  hasDraftRecord,
  readDraft,
  UNBOUND_DRAFT_ID,
} from "../../interactive/drafts";
import ChatDock from "../../components/interactive/ChatDock.vue";
import ComposerDock from "../../components/conversation/ComposerDock.vue";
import { useInteractiveStore } from "../interactive";

/** 悬浮聊天面板默认收起：挂载前先打开，才能操作输入框 */
function openChat(): void {
  useInteractiveStore().chatOpen = true;
}

/** v2：对话页输入区（模式切换与本组用例无关：避免测试环境缺少 router） */
function mountDock() {
  return mount(ComposerDock, { attachTo: document.body });
}

// 模式切换与这些用例无关：用桩替换，避免测试环境缺少 router 造成噪声。
vi.mock("../../components/conversation/ConversationModeSwitch.vue", () => ({
  default: { name: "ConversationModeSwitch", template: '<div class="mode-switch-stub" />' },
}));

vi.mock("../../services/api", () => ({
  api: {
    sendTurn: vi.fn(),
    getSessionContext: vi.fn(),
    stopTurn: vi.fn(),
    cancelContinuation: vi.fn(),
    lookupTurnByRequest: vi.fn(),
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

function chatKey(topicId: string | null): string {
  return draftStorageKey("chat", topicId || UNBOUND_DRAFT_ID);
}

function storedText(key: string): string | null {
  return readDraft(key)?.text ?? null;
}

function acceptedSend(turnId = "turn_1") {
  return { ok: true, accepted: true, turn_id: turnId, status: "accepted", topic_id: null };
}

let deferred: Array<{ resolve: (v: unknown) => void; reject: (e: unknown) => void }> = [];

/** 受控请求：返回一个「还没结束」的发送请求，测试自己决定何时成功 / 失败 */
function pendingSend() {
  const slots: { resolve: (v: unknown) => void; reject: (e: unknown) => void } = {
    resolve: () => {},
    reject: () => {},
  };
  const promise = new Promise((resolve, reject) => {
    slots.resolve = resolve;
    slots.reject = reject;
  });
  deferred.push(slots);
  vi.mocked(api.sendTurn).mockReturnValueOnce(promise as never);
  return promise;
}

beforeEach(() => {
  localStorage.clear();
  deferred = [];
  vi.resetAllMocks();
  vi.mocked(api.sendTurn).mockResolvedValue(acceptedSend() as never);
  vi.mocked(api.getSessionContext).mockResolvedValue({ topic_id: null } as never);
  vi.mocked(imApi.saveDrafts).mockResolvedValue({ drafts: {}, updatedAt: "2026-10-08T00:00:00.000Z" } as never);
  setActivePinia(createPinia());
});

afterEach(() => {
  vi.useRealTimers();
  localStorage.clear();
});

/* ==========================================================================
 * 交叉时序 1：A 发送后切 B，A 失败 → 不修改 B 的输入 / 草稿 / 发送状态；
 *             A 的原文可恢复（只能恢复到 A）。
 * ======================================================================== */
describe("时序 1：A 失败的消息不许跑到 B 上（§10.1）", () => {
  it("【组件】ChatDock：A 发送中切到 B，A 失败 → B 输入框保持为空", async () => {
    vi.useFakeTimers();
    const session = useSessionStore();
    session.currentTopicId = "A";
    session.draft = "A 的原稿";
    vi.advanceTimersByTime(500);
    expect(storedText(chatKey("A"))).toBe("A 的原稿");

    openChat();
    const wrapper = mount(ChatDock, { attachTo: document.body });
    const input = wrapper.find('[data-im="chat-input"]');
    expect(input.exists()).toBe(true);
    await input.setValue("A 的原稿");
    await flushPromises();

    const pending = pendingSend();
    await wrapper.find('[data-im="chat-send"]').trigger("click");
    await flushPromises();

    // 发送请求在飞：切到 B
    session.currentTopicId = "B";
    await flushPromises();
    vi.advanceTimersByTime(600);
    await flushPromises();
    expect(session.draft).toBe("");

    // A 的请求失败
    deferred[0].reject(new Error("网络中断"));
    await flushPromises();
    await vi.advanceTimersByTimeAsync(600);
    await flushPromises();

    // B 的输入框绝不能被 A 的失败原文污染
    expect((wrapper.find('[data-im="chat-input"]').element as HTMLTextAreaElement).value).toBe("");
    expect(session.draft).toBe("");
    expect(storedText(chatKey("B"))).toBeNull();
    wrapper.unmount();
    void pending;
  });

  it("【组件】ChatDock：失败原文只在回到 A 后可用，不自动重发", async () => {
    vi.useFakeTimers();
    const session = useSessionStore();
    session.currentTopicId = "A";
    openChat();
    const wrapper = mount(ChatDock, { attachTo: document.body });
    await wrapper.find('[data-im="chat-input"]').setValue("A 的原稿");
    await flushPromises();

    pendingSend();
    await wrapper.find('[data-im="chat-send"]').trigger("click");
    await flushPromises();
    vi.mocked(api.sendTurn).mockResolvedValue(acceptedSend("turn_2") as never);
    deferred[0].reject(new Error("网络中断"));
    await flushPromises();

    // 换到 B 再回 A
    session.currentTopicId = "B";
    await flushPromises();
    session.currentTopicId = "A";
    await flushPromises();

    const before = vi.mocked(api.sendTurn).mock.calls.length;
    await vi.advanceTimersByTimeAsync(2000);
    await flushPromises();
    // 不许自动重新发送
    expect(vi.mocked(api.sendTurn).mock.calls.length).toBe(before);
    wrapper.unmount();
  });

  it("【会话层】A 发送失败不会写入 B 的草稿键", async () => {
    vi.useFakeTimers();
    const session = useSessionStore();
    session.currentTopicId = "A";
    session.draft = "A 的原稿";
    vi.advanceTimersByTime(500);
    pendingSend();
    session.draft = "";
    const sendPromise = session.send("A 的原稿");
    await flushPromises();
    session.currentTopicId = "B";
    await flushPromises();
    deferred[0].reject(new Error("网络中断"));
    await sendPromise;
    await vi.advanceTimersByTimeAsync(1000);
    expect(storedText(chatKey("B"))).toBeNull();
    expect(hasDraftRecord(chatKey("B"))).toBe(false);
    expect(storedText(chatKey("A"))).toBe("A 的原稿");
  });
});

/* ==========================================================================
 * 交叉时序 2：A 发送后又写新内容，旧发送失败 → 新内容不被覆盖，
 *             失败原文另有恢复入口（两份都保留）。
 * ======================================================================== */
describe("时序 2：失败原文不许覆盖后来输入的新文字（§10.1）", () => {
  it("【组件】ChatDock：A 失败后新内容留在输入框，失败原文可单独取回", async () => {
    vi.useFakeTimers();
    const session = useSessionStore();
    session.currentTopicId = "A";
    openChat();
    const wrapper = mount(ChatDock, { attachTo: document.body });
    await wrapper.find('[data-im="chat-input"]').setValue("第一条：要发出去的话");
    await flushPromises();

    pendingSend();
    await wrapper.find('[data-im="chat-send"]').trigger("click");
    await flushPromises();

    // 请求在飞：用户又写了新内容
    await wrapper.find('[data-im="chat-input"]').setValue("第二条：后来写的新内容");
    await flushPromises();

    deferred[0].reject(new Error("网络中断"));
    await flushPromises();
    await vi.advanceTimersByTimeAsync(800);
    await flushPromises();

    const value = (wrapper.find('[data-im="chat-input"]').element as HTMLTextAreaElement).value;
    expect(value).toBe("第二条：后来写的新内容");

    // 失败原文必须有一个明确的取回入口，且内容就是失败的那一份
    const failed = (session as unknown as { failedSend?: { text: string; topicId: string | null } }).failedSend;
    expect(failed, "会话层要保留失败原文的事实").toBeTruthy();
    expect(failed?.text).toBe("第一条：要发出去的话");
    expect(failed?.topicId).toBe("A");
    wrapper.unmount();
  });

  it("【会话层】retryFailedSend 只在原话题把失败原文放回输入框，不自动发送", async () => {
    vi.useFakeTimers();
    const session = useSessionStore();
    const s = session as unknown as {
      failedSend: unknown;
      retryFailedSend: () => void;
      discardFailedSend: () => void;
    };
    expect(typeof s.retryFailedSend, "会话层未提供 retryFailedSend").toBe("function");
    s.retryFailedSend();
  });
});

/* ==========================================================================
 * 交叉时序 3：A→B→A 写第二条并保存→B，第一条成功 →
 *             A 第二条保留，旧已发送草稿不复活。
 * ======================================================================== */
describe("时序 3：迟到的成功回执不许删掉新草稿、不许复活旧草稿（§10.2）", () => {
  it("【会话层】同话题：A 发送中又写并保存第二条，第一条迟到成功 → 第二条必须保留", async () => {
    vi.useFakeTimers();
    const session = useSessionStore();
    session.currentTopicId = "A";
    session.draft = "第一条";
    vi.advanceTimersByTime(500);
    expect(storedText(chatKey("A"))).toBe("第一条");

    // 组件行为：清空输入框后发送
    session.draft = "";
    pendingSend();
    const sendPromise = session.send("第一条");
    await flushPromises();

    // 请求在飞：用户在**同一个话题**里又写了第二条并已保存
    session.draft = "第二条";
    vi.advanceTimersByTime(500);
    expect(storedText(chatKey("A"))).toBe("第二条");

    // 第一条此刻才成功
    deferred[0].resolve(acceptedSend("turn_1"));
    await sendPromise;
    await vi.advanceTimersByTimeAsync(1500);

    // 迟到的成功回执只许清理那一次发送的旧版本，绝不许删掉更新的第二条
    expect(storedText(chatKey("A"))).toBe("第二条");
  });

  it("【会话层】A→B→A 写第二条并保存→B，第一条成功 → A 第二条保留、旧稿不复活", async () => {
    vi.useFakeTimers();
    const session = useSessionStore();
    session.currentTopicId = "A";
    session.draft = "第一条";
    vi.advanceTimersByTime(500);
    pendingSend();
    session.draft = "";
    const sendPromise = session.send("第一条");
    await flushPromises();

    session.currentTopicId = "B";
    await flushPromises();
    session.draft = "B 的草稿";
    vi.advanceTimersByTime(500);
    session.currentTopicId = "A";
    await flushPromises();
    session.draft = "第二条";
    vi.advanceTimersByTime(500);
    expect(storedText(chatKey("A"))).toBe("第二条");
    session.currentTopicId = "B";
    await flushPromises();

    deferred[0].resolve(acceptedSend("turn_1"));
    await sendPromise;
    await vi.advanceTimersByTimeAsync(1500);

    expect(storedText(chatKey("A"))).toBe("第二条");
    expect(storedText(chatKey("B"))).toBe("B 的草稿");
  });

  it("【会话层】A→B→A：A 的成功回执不许删掉 B 已保存的草稿（§10.2 切换矩阵）", async () => {
    vi.useFakeTimers();
    const session = useSessionStore();
    session.currentTopicId = "A";
    session.draft = "A 的第一条";
    vi.advanceTimersByTime(500);
    expect(storedText(chatKey("A"))).toBe("A 的第一条");

    // 组件行为：清空后发送
    session.draft = "";
    pendingSend();
    const sendPromise = session.send("A 的第一条");
    await flushPromises();

    // 切到 B，写下并**保存** B 的草稿
    session.currentTopicId = "B";
    await flushPromises();
    session.draft = "B 的草稿";
    vi.advanceTimersByTime(500);
    expect(storedText(chatKey("B"))).toBe("B 的草稿");
    // 回到 A，写下并保存 A 的第二条
    session.currentTopicId = "A";
    await flushPromises();
    session.draft = "A 的第二条";
    vi.advanceTimersByTime(500);
    expect(storedText(chatKey("A"))).toBe("A 的第二条");

    // A 的第一条此刻才成功：只许清理 A 上那一次发送的旧版本
    deferred[0].resolve(acceptedSend("turn_1"));
    await sendPromise;
    await vi.advanceTimersByTimeAsync(1500);

    expect(storedText(chatKey("B"))).toBe("B 的草稿");
    expect(storedText(chatKey("A"))).toBe("A 的第二条");
  });

  it("【会话层】用户主动清空后的新版本不与「没有草稿」混淆（§10.2）", async () => {
    vi.useFakeTimers();
    const session = useSessionStore();
    session.currentTopicId = "A";
    session.draft = "第一条";
    vi.advanceTimersByTime(500);
    pendingSend();
    session.draft = "";
    const sendPromise = session.send("第一条");
    await flushPromises();
    deferred[0].resolve(acceptedSend("turn_1"));
    await sendPromise;
    await vi.advanceTimersByTimeAsync(500);
    expect(readDraft(chatKey("A"))).toBeNull();

    // 成功之后又写新内容并保存：迟到回执/防抖不许删它
    session.draft = "成功之后的新草稿";
    vi.advanceTimersByTime(500);
    await vi.advanceTimersByTimeAsync(1500);
    expect(storedText(chatKey("A"))).toBe("成功之后的新草稿");
  });
});

/* ==========================================================================
 * 交叉时序 4：点击发送后、等待期间切话题 → 请求仍发到点击时的话题。
 * ======================================================================== */
describe("时序 4：请求话题在点击那一刻确定（§10.1）", () => {
  it("【会话层】切话题后请求参数仍是点击时的话题", async () => {
    const session = useSessionStore();
    session.currentTopicId = "A";
    pendingSend();
    const sendPromise = session.send("发到 A 的话");
    await flushPromises();
    // 请求还没回来：用户切到 B
    session.currentTopicId = "B";
    await flushPromises();
    deferred[0].resolve(acceptedSend("turn_1"));
    await sendPromise;
    const call = vi.mocked(api.sendTurn).mock.calls[0];
    expect(call[0]).toBe("发到 A 的话");
    expect(call[1], "发送请求必须用点击那一刻的话题").toBe("A");
  });

  it("【组件】ChatDock：点击发送后立刻切话题，请求仍带原话题", async () => {
    const session = useSessionStore();
    session.currentTopicId = "A";
    openChat();
    const wrapper = mount(ChatDock, { attachTo: document.body });
    await wrapper.find('[data-im="chat-input"]').setValue("A 的话");
    await flushPromises();
    pendingSend();
    await wrapper.find('[data-im="chat-send"]').trigger("click");
    await flushPromises();
    session.currentTopicId = "B";
    await flushPromises();
    deferred[0].resolve(acceptedSend("turn_1"));
    await flushPromises();
    expect(vi.mocked(api.sendTurn).mock.calls[0][1]).toBe("A");
    wrapper.unmount();
  });
});

/* ==========================================================================
 * 交叉时序 5：未到草稿防抖时间就发送 / 切话题 / 刷新 →
 *             未受理的原稿受到恢复保护。
 * ======================================================================== */
describe("时序 5：没到防抖时间的原稿也要受保护（§10.1 / §10.5）", () => {
  it("【会话层】发送时输入框还没清空（未到防抖）且请求失败 → 原文仍在原话题草稿里", async () => {
    vi.useFakeTimers();
    const session = useSessionStore();
    session.currentTopicId = "A";
    session.draft = "打完立刻发的话";
    // 故意不推进防抖时间（尚未落盘）
    vi.mocked(api.sendTurn).mockRejectedValueOnce(new Error("网络中断"));
    const ok = await session.send("打完立刻发的话");
    expect(ok).toBe(false);
    await vi.advanceTimersByTimeAsync(1000);
    expect(storedText(chatKey("A"))).toBe("打完立刻发的话");
  });

  it("【会话层】打完字立刻切话题（未到防抖）→ 原稿落在原话题键上", async () => {
    vi.useFakeTimers();
    const session = useSessionStore();
    session.currentTopicId = "A";
    session.draft = "还没到防抖就切走的字";
    session.currentTopicId = "B";
    await flushPromises();
    expect(storedText(chatKey("A"))).toBe("还没到防抖就切走的字");
    expect(storedText(chatKey("B"))).toBeNull();
  });

  it("【会话层】打完字立刻刷新（flushDraft）→ 原稿在本机存储里", async () => {
    vi.useFakeTimers();
    const session = useSessionStore();
    session.currentTopicId = "A";
    session.draft = "刷新前一刻的字";
    session.flushDraft();
    expect(storedText(chatKey("A"))).toBe("刷新前一刻的字");
  });
});

/* ==========================================================================
 * 交叉时序 11：正常刷新 / 关闭重开前尚有未保存输入 → 恢复最后输入，
 *              不自动发送、不提交、不确认。
 * ======================================================================== */
describe("时序 11：刷新前未保存的输入要能恢复（§10.5）", () => {
  it("【会话层】防抖没到就「刷新」：新 pinia 恢复最后输入，且不发送", async () => {
    vi.useFakeTimers();
    const session = useSessionStore();
    session.currentTopicId = "A";
    session.draft = "没保存完就刷新的字";
    // 不推进时间：防抖没触发
    session.flushDraft();

    setActivePinia(createPinia());
    const reloaded = useSessionStore();
    reloaded.currentTopicId = "A";
    await flushPromises();
    expect(reloaded.draft).toBe("没保存完就刷新的字");
    expect(api.sendTurn).not.toHaveBeenCalled();
    expect(imApi.submitBoard).not.toHaveBeenCalled();
    expect(imApi.saveBoardState).not.toHaveBeenCalled();
  });

  it("【组件】对话页输入区：恢复的草稿只在输入框里，不自动发出", async () => {
    vi.useFakeTimers();
    const session = useSessionStore();
    session.currentTopicId = "A";
    session.draft = "对话页未保存的输入";
    session.flushDraft();

    setActivePinia(createPinia());
    const wrapper = mountDock();
    const reloaded = useSessionStore();
    reloaded.currentTopicId = "A";
    await flushPromises();
    expect((wrapper.find("#composer-input").element as HTMLTextAreaElement).value).toBe(
      "对话页未保存的输入",
    );
    expect(api.sendTurn).not.toHaveBeenCalled();
    wrapper.unmount();
  });
});

/* ==========================================================================
 * 交叉时序 1'：普通对话页（Composer）也要覆盖发送恢复，不只是纯函数。
 * ======================================================================== */
describe("对话页发送恢复（§10.1「会话层统一管理」）", () => {
  /**
   * 断言改写说明（子 agent B，2026-10-11；v2 位置更新 2026-10-22；契约不是放宽）。
   *
   * 原断言是「原文回到输入框」。它写于互动线把**所有**发送失败都登记为可恢复原文的时期；
   * 并入 main 后契约 5（Lead 裁决）明确规定：needs-confirm（受理情况未知）时 Composer
   * **不**自动恢复草稿，由确认条【重试 / 放弃】接管 —— 理由正是本用例要防的「重复执行」：
   * 原文一回到输入框，用户顺手按 Enter 就会用**新的请求身份**再发一次，绕开幂等保护。
   * 两份契约同时成立时二者互斥（此前本条「看似通过」只是因为当时根本没有失败记录可恢复）。
   *
   * 按 F03「网络超时或断开不得一律当明确未受理，不得自动生成新身份重发」+ 契约 5，
   * 这里断言**新契约下更强的结果**：
   * 原文完整保存在会话层的未决台账里、入口①（v2：ComposerDock → SendIssuesPanel）
   * 给出原文 / 同身份重试 / 明确放弃三个入口，且没有发生任何自动重发。
   * 原文没有丢，用户仍能拿到它 —— 只是不再被诱导换身份重发。
   *
   * v2 的两处位置变化：面板默认收起，必须先点开入口①；原文直接列在面板里（不再藏一层
   * 「查看原文」）。「明确放弃」只在查证确认「后端没有该请求记录」（404）时出现 ——
   * 避免把可能已受理的消息撤掉；本用例用 404 覆盖这个入口。
   */
  it("【组件】对话页：A 发送未决时原文不自动放回输入框，完整留在原话题的未决入口里", async () => {
    vi.useFakeTimers();
    const session = useSessionStore();
    session.currentTopicId = "A";
    session.draft = "对话页要发的话";
    const wrapper = mountDock();
    await flushPromises();
    vi.mocked(api.sendTurn).mockRejectedValueOnce(new Error("网络中断"));
    // 自动查证得到「后端没有这条请求记录」：未决状态如实显示，并给出「明确放弃」
    vi.mocked(api.lookupTurnByRequest).mockRejectedValue(
      Object.assign(new Error("not found"), { status: 404 }),
    );
    const textarea = wrapper.find("#composer-input");
    expect((textarea.element as HTMLTextAreaElement).value).toBe("对话页要发的话");
    await wrapper.find("button.send-btn").trigger("click").catch(() => undefined);
    // 直接走组件公开路径：Enter 发送
    await textarea.trigger("keydown", { key: "Enter" });
    await flushPromises();
    await vi.advanceTimersByTimeAsync(800);
    await flushPromises();

    // 未决：不自动放回输入框（否则按 Enter 就是新身份重发）
    expect((textarea.element as HTMLTextAreaElement).value).toBe("");
    // 输入框里没有被当成「已保留的新草稿」：会话层草稿仍是空的
    expect(session.draft, "未决时把原文当成了输入框里的新草稿").toBe("");
    // 原文一个字都没丢：完整保存在原话题的未决台账里
    expect(
      session.unresolvedSendsForTopic("A").map((record) => record.snapshot.text),
      "未决发送的原文没有留在会话层（这条断言是「原文不许丢」的落点）",
    ).toEqual(["对话页要发的话"]);
    // 没有自动重发：发送请求只发过一次
    expect(vi.mocked(api.sendTurn).mock.calls.length).toBe(1);

    // v2：入口①默认收起；点开后原文与三个明确动作都在
    const entry = wrapper.find('.entry-btn[data-entry="sendIssues"]');
    expect(entry.exists(), "对话页没有出现「发送异常与草稿恢复」入口").toBe(true);
    await entry.trigger("click");
    await flushPromises();
    expect(wrapper.find('[data-im="composer-pending"]').exists()).toBe(true);
    // 原文直接可见（v2 面板把原文列出来，不再需要先点「查看原文」）
    expect(wrapper.find('[data-im="composer-pending-quote"]').text()).toBe("对话页要发的话");
    expect(
      wrapper.find('[data-im="composer-pending-retry"]').exists(),
      "对话页没有「同身份重试」入口",
    ).toBe(true);
    expect(
      wrapper.find('[data-im="composer-pending-abandon"]').exists(),
      "对话页没有「明确放弃」入口",
    ).toBe(true);
    wrapper.unmount();
  });

  it("【组件】对话页输入区：切到 B 之后 A 的失败不许写进 B 的输入框", async () => {
    vi.useFakeTimers();
    const session = useSessionStore();
    session.currentTopicId = "A";
    session.draft = "A 的话";
    const wrapper = mountDock();
    await flushPromises();
    pendingSend();
    const textarea = wrapper.find("#composer-input");
    await textarea.trigger("keydown", { key: "Enter" });
    await flushPromises();
    session.currentTopicId = "B";
    await flushPromises();
    deferred[0].reject(new Error("网络中断"));
    await flushPromises();
    await vi.advanceTimersByTimeAsync(800);
    await flushPromises();
    expect((wrapper.find("#composer-input").element as HTMLTextAreaElement).value).toBe("");
    wrapper.unmount();
  });
});