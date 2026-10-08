/**
 * 独立验收 D2-B：失败原文与新草稿在刷新后同时保留；未绑定话题迁移后的清理（契约 §11.4 / §11.5）。
 *
 * 由独立验收子智能体 D2 编写，**不修改任何产品代码**。
 * 我挑的是两条最容易被「谁覆盖谁」写坏的路：
 * 1. 失败之后用户又写了新草稿 → 正常刷新（新 store）→ 两份都必须还在，且取回失败原文不覆盖新草稿；
 * 2. 未绑定话题发送期间话题被绑定、用户又写了新文字 → 受理成功只许清掉被发送的那一版，
 *    迁移后写下的新文字一个字都不能动；迁移时失败的原文要落在真实话题上。
 *
 * 标注：【会话层】只依赖 stores/session.ts 的公开状态与动作；【模拟失败】用受控的 api.sendTurn。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { flushPromises } from "@vue/test-utils";
import { useSessionStore } from "../session";
import { api } from "../../services/api";
import { draftStorageKey, readDraft, UNBOUND_DRAFT_ID } from "../../interactive/drafts";

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

function chatKey(topicId: string | null): string {
  return draftStorageKey("chat", topicId || UNBOUND_DRAFT_ID);
}

function acceptedSend(turnId = "turn_1") {
  return { ok: true, accepted: true, turn_id: turnId, status: "accepted", topic_id: null };
}

/** 受控请求：测试自己决定什么时候受理成功 / 失败。 */
function pendingSend() {
  const slots = { resolve: (_v: unknown) => {}, reject: (_e: unknown) => {} };
  const promise = new Promise((resolve, reject) => {
    slots.resolve = resolve;
    slots.reject = reject;
  });
  vi.mocked(api.sendTurn).mockReturnValueOnce(promise as never);
  return slots;
}

/** 正常刷新：新的 pinia / 新的 store，只剩本机存储。 */
function reloadSession(topicId: string | null) {
  setActivePinia(createPinia());
  const reloaded = useSessionStore();
  reloaded.currentTopicId = topicId;
  return reloaded;
}

beforeEach(() => {
  localStorage.clear();
  vi.resetAllMocks();
  vi.mocked(api.getSessionContext).mockResolvedValue({ topic_id: null } as never);
  setActivePinia(createPinia());
});

afterEach(() => {
  vi.useRealTimers();
  localStorage.clear();
});

describe("D2 反例：失败原文与新草稿在刷新后同时保留（§11.4）", () => {
  it("【会话层】刷新后：输入框里是新草稿，失败原文与原因仍能找回，取回时不覆盖新草稿", async () => {
    vi.useFakeTimers();
    const session = useSessionStore();
    session.currentTopicId = "T1";
    session.draft = "没发出去的原文";
    await vi.advanceTimersByTimeAsync(500);
    expect(readDraft(chatKey("T1"))?.text, "发送前草稿没有写进本机").toBe("没发出去的原文");

    session.sendAttribution();
    session.draft = "";
    vi.mocked(api.sendTurn).mockRejectedValueOnce(new Error("网络中断：请求没有到达服务端"));
    expect(await session.send("没发出去的原文"), "受控失败居然被当成发送成功").toBe(false);
    expect(session.failedSend?.text, "失败原文没有记进会话层").toBe("没发出去的原文");

    // 失败之后用户又写下一条新草稿（还没发送）
    session.draft = "后续要发的新草稿";
    await vi.advanceTimersByTimeAsync(500);

    const reloaded = reloadSession("T1");
    await flushPromises();

    expect(reloaded.draft, "刷新后新草稿没有回到输入框").toBe("后续要发的新草稿");
    const records = reloaded.failedSendsForTopic("T1");
    expect(records.map((r) => r.text), "刷新后失败原文不见了（只存在内存里）").toContain("没发出去的原文");
    expect(reloaded.failedSendError ?? "", "刷新后看不到这次失败的真实原因").toContain("网络中断");

    const target = records.find((r) => r.text === "没发出去的原文");
    expect(target, "找不到那条失败原文，后面的恢复动作无从验证").toBeTruthy();
    // 恢复不是发送：从刷新到现在，取回/互换都不许多打一次发送请求
    const sendsBeforeRecovery = vi.mocked(api.sendTurn).mock.calls.length;
    const retried = reloaded.retryFailedSend(target!.id);
    expect(retried.restored, "输入框已有新草稿时，取回失败原文竟然直接覆盖").toBe(false);
    expect(reloaded.draft, "取回失败原文时把用户的新草稿覆盖掉了").toBe("后续要发的新草稿");
    expect(reloaded.failedSendsForTopic("T1").map((r) => r.text), "取回失败后原文记录被静默丢弃").toContain("没发出去的原文");

    // 用户明确选择「互换」：两份都必须保留
    const swapped = reloaded.swapFailedSendText(target!.id);
    expect(swapped.swapped, "互换没有发生").toBe(true);
    expect(reloaded.draft, "互换后输入框里不是失败原文").toBe("没发出去的原文");
    expect(reloaded.failedSendsForTopic("T1").map((r) => r.text), "互换后新草稿被丢掉了").toContain("后续要发的新草稿");
    expect(
      vi.mocked(api.sendTurn).mock.calls.length - sendsBeforeRecovery,
      "取回/互换原文时又打了一次发送请求（恢复不是发送）",
    ).toBe(0);
  });
});

describe("D2 反例：未绑定话题迁移后的清理（§11.5）", () => {
  it("【会话层】未绑定→绑定→发送成功：只清被发送的那一版，迁移后写下的新文字不被误删", async () => {
    vi.useFakeTimers();
    const session = useSessionStore();
    session.currentTopicId = null;
    session.draft = "未绑定话题时写的字";
    await vi.advanceTimersByTimeAsync(500);
    expect(readDraft(chatKey(null))?.text, "未绑定话题的草稿没有写进本机").toBe("未绑定话题时写的字");

    session.sendAttribution();
    session.draft = "";
    const pending = pendingSend();
    const sending = session.send("未绑定话题时写的字");
    await flushPromises();

    // 受理返回之前：服务器把真实话题绑定进来，用户在真实话题下写下新文字
    session.currentTopicId = "T1";
    await flushPromises();
    session.draft = "迁移之后写的新文字";
    await vi.advanceTimersByTimeAsync(500);

    pending.resolve(acceptedSend());
    await sending;
    await flushPromises();

    expect(session.draft, "已经成功发送的旧文字又回到了输入框").toBe("迁移之后写的新文字");
    expect(readDraft(chatKey("T1"))?.text ?? null, "迁移之后写下的新文字被成功回执删掉了").toBe("迁移之后写的新文字");
    expect(readDraft(chatKey(null))?.text ?? null, "未绑定位置的旧记录没有被清掉（换个话题还会重复出现）").toBeNull();

    // 刷新之后仍然是新文字，旧文字不许复活
    const reloaded = reloadSession("T1");
    await flushPromises();
    expect(reloaded.draft, "刷新后新文字丢了").toBe("迁移之后写的新文字");
    expect(readDraft(chatKey("T1"))?.text ?? null).not.toBe("未绑定话题时写的字");
  });

  it("【会话层】未绑定→绑定→发送失败：原文归到真实话题，用户新输入不被覆盖，刷新后两份都在", async () => {
    vi.useFakeTimers();
    const session = useSessionStore();
    session.currentTopicId = null;
    session.draft = "迁移时失败的原文";
    await vi.advanceTimersByTimeAsync(500);

    session.sendAttribution();
    session.draft = "";
    const pending = pendingSend();
    const sending = session.send("迁移时失败的原文");
    await flushPromises();

    session.currentTopicId = "T1";
    await flushPromises();
    session.draft = "失败之后写的新文字";
    await vi.advanceTimersByTimeAsync(500);

    pending.reject(new Error("网关超时：这次发送没有被受理"));
    await sending;
    await flushPromises();

    expect(session.draft, "失败后用户正在写的新文字被改动").toBe("失败之后写的新文字");
    expect(
      session.failedSendsForTopic("T1").map((r) => r.text),
      "失败原文没有跟着迁移到真实话题（回到这个话题就找不回了）",
    ).toContain("迁移时失败的原文");
    expect(session.failedSendsForTopic(null).map((r) => r.text), "失败原文还残留在未绑定话题下").not.toContain("迁移时失败的原文");

    const reloaded = reloadSession("T1");
    await flushPromises();
    expect(reloaded.draft, "刷新后新文字丢了").toBe("失败之后写的新文字");
    expect(reloaded.failedSendsForTopic("T1").map((r) => r.text), "刷新后失败原文不见了").toContain("迁移时失败的原文");
    expect(reloaded.failedSendError ?? "", "刷新后失败原因不见了").toContain("网关超时");
  });
});
