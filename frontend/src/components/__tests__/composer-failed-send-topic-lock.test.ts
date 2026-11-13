/**
 * #42 提交时锁定原话题与请求身份（回归）。
 *
 * 红（修复前）：甲话题的发送还在等回执时用户切到乙；甲的明确失败回执回来后，
 * Composer 的失败兜底无视话题把甲的原话写进乙的输入框（text.value = draftSnapshot），
 * 随后还会被草稿保存器当作「乙的草稿」落盘 —— 甲的文字回填了乙。
 *
 * 绿（修复后）：
 * - 请求用**提交那一刻**的话题发出，切话题不改去向；
 * - 失败回执只落在发起话题；乙的输入框与乙的草稿存储一个字都不变；
 * - 甲的原文仍是甲的可恢复失败事项（回到甲能从「发送异常」取回）；
 * - 甲已有新草稿时，取回不覆盖，互换后两份内容都在。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { draftStorageKey, readDraft } from "../../interactive/drafts";

const mocks = vi.hoisted(() => ({
  sendTurn: vi.fn(),
  getSessionContext: vi.fn(async () => ({
    topic_id: "",
    topic_name: null,
    anchor_fragment: null,
    messages: [],
  })),
  cancelTurn: vi.fn(async () => ({ ok: true, cancelled: true })),
  cancelActiveTurn: vi.fn(async () => ({ ok: true, cancelled: true })),
  stopTurn: vi.fn(async () => ({ ok: true })),
}));

vi.mock("../../services/api", () => ({
  api: {
    sendTurn: mocks.sendTurn,
    getSessionContext: mocks.getSessionContext,
    cancelTurn: mocks.cancelTurn,
    cancelActiveTurn: mocks.cancelActiveTurn,
    stopTurn: mocks.stopTurn,
    cancelContinuation: vi.fn(async () => ({ ok: true })),
    saveDrafts: vi.fn(async () => ({ ok: true })),
    saveBoardState: vi.fn(async () => ({ ok: true })),
    fetchBoardState: vi.fn(async () => ({})),
    fetchVisibleRange: vi.fn(async () => ({})),
    previewMaterialImpact: vi.fn(async () => ({})),
    submitBoard: vi.fn(async () => ({})),
    fetchIntents: vi.fn(async () => ({})),
    approveIntent: vi.fn(async () => ({})),
    rejectIntent: vi.fn(async () => ({})),
    batchDecide: vi.fn(async () => ({})),
    advanceIntent: vi.fn(async () => ({})),
    updateIntentPreview: vi.fn(async () => ({})),
    fetchHistoryPage: vi.fn(async () => ({})),
    fetchMessages: vi.fn(async () => ({})),
  },
}));

vi.mock("../../services/attachments", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../services/attachments")>();
  return {
    ...actual,
    restorePendingAttachments: vi.fn(async () => ({ items: [], dropped: [], unconfirmed: [] })),
    onPathDrop: vi.fn(async () => null),
    isDesktopShell: () => false,
  };
});

import Composer from "../Composer.vue";
import { useSessionStore } from "../../stores/session";

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (err: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

function rejectedSend(message: string) {
  // 明确未受理（4xx）：会话层据 status 判定「明确失败」并登记可恢复原文。
  return Object.assign(new Error(message), { status: 400 });
}

async function mountComposer(topicId: string | null) {
  const pinia = createPinia();
  setActivePinia(pinia);
  const session = useSessionStore();
  session.currentTopicId = topicId;
  const w = mount(Composer, { global: { plugins: [pinia] } });
  await flushPromises();
  return { w, session };
}

beforeEach(() => {
  localStorage.clear();
  vi.clearAllMocks();
});

describe("#42 失败回执只落在发起话题", () => {
  it("甲的发送在飞时切到乙：甲失败后不把原文写进乙的输入框，也不写进乙的草稿存储", async () => {
    const pending = deferred<unknown>();
    mocks.sendTurn.mockReturnValueOnce(pending.promise as never);
    const { w, session } = await mountComposer("A");

    await w.find("#composer-input").setValue("甲的原话");
    await w.find(".send-btn").trigger("click");
    await flushPromises();
    expect(mocks.sendTurn).toHaveBeenCalledTimes(1);
    // 请求用提交那一刻的话题发出（锁定请求身份/话题），与此刻界面在哪无关
    expect(mocks.sendTurn.mock.calls[0]?.[1]).toBe("A");
    expect(session.draft).toBe("");

    // 回执回来之前切到乙：乙没有自己的草稿
    session.currentTopicId = "B";
    await flushPromises();
    expect(session.draft).toBe("");

    pending.reject(rejectedSend("网络中断：请求没有到达服务端"));
    await flushPromises();

    // 甲的失败回执不得回填乙
    expect(session.draft, "甲的失败原文被写进了乙的输入框").toBe("");
    expect(
      readDraft(draftStorageKey("chat", "B")),
      "甲的失败原文被当作乙的草稿落盘",
    ).toBeNull();

    // 原文确实作为甲的可恢复事项保留着（回到甲可以取回）
    const failedA = session.failedSendsForTopic("A");
    expect(failedA).toHaveLength(1);
    expect(failedA[0]?.text).toBe("甲的原话");
    expect(session.failedSendsForTopic("B")).toHaveLength(0);
    w.unmount();
  });

  it("甲已有新草稿：从失败事项取回不覆盖，互换后两份内容都在", async () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const session = useSessionStore();
    session.currentTopicId = "A";
    session.draft = "甲的原话";
    session.sendAttribution();
    session.draft = "";
    mocks.sendTurn.mockRejectedValueOnce(rejectedSend("后端拒绝了这次发送"));
    await session.send("甲的原话");

    const failed = session.failedSendsForTopic("A");
    expect(failed).toHaveLength(1);
    const id = failed[0]!.id;

    // 甲后来写下了新草稿：取回失败原文不得覆盖它
    session.draft = "甲的新草稿";
    const restore = session.retryFailedSend(id);
    expect(restore.restored).toBe(false);
    expect(session.draft).toBe("甲的新草稿");

    // 互换：两份都保留（输入框与记录各拿一份）
    const swapped = session.swapFailedSendText(id);
    expect(swapped.ok).toBe(true);
    expect(session.draft).toBe("甲的原话");
    expect(session.failedSendsForTopic("A")[0]?.text).toBe("甲的新草稿");
  });
});
