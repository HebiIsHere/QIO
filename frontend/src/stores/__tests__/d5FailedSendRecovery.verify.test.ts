/**
 * 独立验收 D5-B：失败原文的持久化与未绑定话题迁移后的清理（契约 §11.4 / §11.5）。
 *
 * 由独立验收子智能体 D 编写，**不修改任何产品代码**。
 * 按用户行为断言：发送失败 → 刷新 → 还能看到失败原因并取回原文；未绑定话题发送成功后，
 * 已发送的文字不许再回到输入框。
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

describe("场景 5：发送失败后刷新，失败原文与原因不许丢（§11.4）", () => {
  it("【会话层】刷新（新 store）后仍能知道这次失败并取回原文", async () => {
    vi.useFakeTimers();
    const session = useSessionStore();
    session.currentTopicId = null;
    session.draft = "服务器挂了的时候写下的原文";
    await vi.advanceTimersByTimeAsync(500);
    expect(readDraft(chatKey(null))?.text).toBe("服务器挂了的时候写下的原文");

    // 用户点发送：归属先定下，输入框清空，请求失败
    session.sendAttribution();
    session.draft = "";
    vi.mocked(api.sendTurn).mockRejectedValueOnce(new Error("网络中断：请求没有到达服务端"));
    const ok = await session.send("服务器挂了的时候写下的原文");
    expect(ok).toBe(false);
    expect(session.failedSend?.text).toBe("服务器挂了的时候写下的原文");

    // 用户刷新页面：新的 pinia / 新的 store，内存状态全部重建
    setActivePinia(createPinia());
    const reloaded = useSessionStore();
    await flushPromises();

    expect(
      reloaded.failedSend?.text,
      "刷新之后失败原文不见了（它只存在内存里）",
    ).toBe("服务器挂了的时候写下的原文");
    expect(
      reloaded.failedSendError ?? "",
      "刷新之后看不到这次失败的真实原因",
    ).toContain("网络中断");
  });
});

describe("场景 6：未绑定话题发送成功后，已发送的文字不许回到输入框（§11.5）", () => {
  it("【会话层】currentTopicId=null 发送 → 绑定真实话题 → 受理成功：输入框与草稿都要干净", async () => {
    vi.useFakeTimers();
    const session = useSessionStore();
    session.currentTopicId = null;
    session.draft = "未绑定话题时写的字";
    await vi.advanceTimersByTimeAsync(500);
    expect(readDraft(chatKey(null))?.text).toBe("未绑定话题时写的字");

    session.sendAttribution();
    session.draft = "";
    const pending = pendingSend();
    const sending = session.send("未绑定话题时写的字");
    await flushPromises();

    // 受理返回之前，后端把真实话题绑定进来（会话上下文到达）
    session.currentTopicId = "T1";
    await flushPromises();

    pending.resolve(acceptedSend());
    await sending;
    await flushPromises();

    expect(session.draft, "已经成功发送的文字又回到了输入框（清理找的是旧位置）").toBe("");
    const kept = readDraft(chatKey("T1"))?.text ?? null;
    expect(kept, "已发送的文字被当成 T1 的新草稿留下来了").not.toBe("未绑定话题时写的字");
  });
});
