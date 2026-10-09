/**
 * 收尾轮 B2：发送尝试的内容版本与显式重发关联（契约 M3 / 反例 02、03、04）。
 *
 * 本文件只写「用户能观察到的正确行为期望」，对应 _final-closure-task.md 的反例原文：
 * - 02 重发期间互换，旧成功误删后来保留的文字：
 *      身份 ≠ 内容版本；互换后记录、正文版本与发送关系一并更新；
 *      旧成功只处理它实际接受的 (recordId, contentVersion, topicId)。
 * - 03 重发关联跨话题，成功误清别的话题失败记录：
 *      显式重发关联必须同时约束话题、记录与内容版本；切到 B 后手动输入相同文字发送成功，
 *      不许清掉 A 的失败记录。
 * - 04 绑定话题前输入新稿，原发送失败仍留在未绑定位置：
 *      发送归属与输入框保护独立；话题绑定后这次发送稳定归真实话题（A），
 *      切到 B 后才失败也仍旧在 A 找得到，不留在 null 占位位置。
 *
 * 标注：【状态】只依赖 stores/session.ts 的公开状态与动作；【模拟失败】受控 api.sendTurn。
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
    saveDrafts: vi.fn(),
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

function acceptedSend(turnId = "turn_1") {
  return { ok: true, accepted: true, turn_id: turnId, status: "accepted", topic_id: null };
}

/** 受控请求：请求停在半路，由测试决定何时成功 / 失败 */
function pendingSend() {
  const slots: { resolve: (v: unknown) => void; reject: (e: unknown) => void } = {
    resolve: () => {},
    reject: () => {},
  };
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

describe("02 重发期间互换：旧成功只处理它实际接受的版本", () => {
  it("【会话层】互换后记录正文已更新，旧重发成功不许按身份把它删掉", async () => {
    vi.useFakeTimers();
    const session = useSessionStore();
    session.currentTopicId = "A";

    // 第一次发送失败，留下一条失败原文
    vi.mocked(api.sendTurn).mockRejectedValueOnce(new Error("网络中断"));
    session.draft = "没发出去的原文";
    await vi.advanceTimersByTimeAsync(500);
    session.sendAttribution();
    session.draft = "";
    await session.send("没发出去的原文");
    const record = session.failedSendsForTopic("A")[0];
    expect(record?.text, "失败原文没有记录下来").toBe("没发出去的原文");

    // 用户点「找回原文」：建立显式重发关联
    expect(session.retryFailedSend(record.id).ok, "回到原话题应当能取回原文").toBe(true);
    expect(session.draft).toBe("没发出去的原文");

    // 原样重发：回执还没到
    const resend = session.sendAttribution();
    expect(resend.draftId, "显式重发关联应当沿用被重发记录的身份（既有语义）").toBe(record.draftId);
    session.draft = "";
    const deferred = pendingSend();
    const sending = session.send("没发出去的原文");
    await flushPromises();

    // 回执未到：用户输入新文字，并与失败原文互换（记录改为保存这份新文字）
    session.draft = "后来输入并保留的新文字";
    await vi.advanceTimersByTimeAsync(500);
    expect(session.swapFailedSendText(record.id).ok, "互换应当成功").toBe(true);
    expect(session.draft, "互换后输入框里应当是失败原文").toBe("没发出去的原文");
    expect(
      session.failedSendsForTopic("A").map((r) => r.text),
      "互换后记录里应当保存用户输入的新文字",
    ).toEqual(["后来输入并保留的新文字"]);

    // 旧重发此刻才成功
    deferred.resolve(acceptedSend("turn_resend"));
    await sending;
    await vi.advanceTimersByTimeAsync(800);

    expect(
      session.failedSendsForTopic("A").map((r) => r.text),
      "互换后的记录被旧重发成功按身份误删了（身份≠内容版本；成功只许处理它实际接受的版本）",
    ).toEqual(["后来输入并保留的新文字"]);
    expect(session.draft, "互换放到输入框里的失败原文不许被旧的成功回执清掉").toBe("没发出去的原文");
  });
});

describe("03 重发关联跨话题：成功不许清别的话题的失败记录", () => {
  it("【会话层】A 找回原文后切 B，B 手动输入相同文字发送成功，A 的记录必须还在", async () => {
    vi.useFakeTimers();
    const session = useSessionStore();
    session.currentTopicId = "A";

    vi.mocked(api.sendTurn).mockRejectedValueOnce(new Error("网络中断"));
    session.draft = "两个话题里一样的文字";
    await vi.advanceTimersByTimeAsync(500);
    session.sendAttribution();
    session.draft = "";
    await session.send("两个话题里一样的文字");
    const record = session.failedSendsForTopic("A")[0];
    expect(record?.text, "A 话题的失败原文没有记录").toBe("两个话题里一样的文字");

    // A 里找回原文：建立显式重发关联
    expect(session.retryFailedSend(record.id).ok).toBe(true);
    expect(session.draft).toBe("两个话题里一样的文字");

    // 用户切到 B 继续（关联不许跟着过去）
    session.currentTopicId = "B";
    await flushPromises();
    expect(session.draft, "B 话题应当装载 B 自己的草稿（这里是空）").toBe("");

    // B 里手动输入与原文相同的文字，并且这次真的发送成功
    session.draft = "两个话题里一样的文字";
    await vi.advanceTimersByTimeAsync(500);
    const atB = session.sendAttribution();
    expect(atB.draftId, "B 的手动发送沿用了 A 那条记录的关联身份").not.toBe(record.draftId);
    session.draft = "";
    vi.mocked(api.sendTurn).mockResolvedValueOnce(acceptedSend("turn_b") as never);
    expect(await session.send("两个话题里一样的文字")).toBe(true);
    await vi.advanceTimersByTimeAsync(800);

    expect(
      session.failedSendsForTopic("A").map((r) => r.text),
      "B 的手动发送沿用了 A 的重发关联，成功清掉了 A 的失败记录",
    ).toEqual(["两个话题里一样的文字"]);
    expect(session.failedSendsForTopic("B"), "B 里并没有失败过，不许凭空出现失败记录").toEqual([]);
  });
});

describe("04 绑定话题前输入新稿：原发送稳定归属绑定的真实话题", () => {
  it("【会话层】未绑定发送→用户输入新稿→绑定 A→切 B→失败：记录必须在 A 找得到", async () => {
    vi.useFakeTimers();
    const session = useSessionStore();
    expect(session.currentTopicId).toBeNull();

    session.draft = "未绑定话题时发出的原文";
    await vi.advanceTimersByTimeAsync(500);

    const deferred = pendingSend();
    const at = session.sendAttribution();
    expect(at.topicId, "点击发送时话题还没确定").toBeNull();
    session.draft = "";
    const sending = session.send("未绑定话题时发出的原文");
    await flushPromises();

    // 真实话题尚未确定：用户又输入新稿，并完成本机保存
    session.draft = "绑定之前写下的新稿";
    session.flushDraft();
    expect(readDraft(chatKey(null))?.text, "新稿没有保存进本机（这一步是本反例的前提）").toBe(
      "绑定之前写下的新稿",
    );

    // 服务器此刻绑定真实话题 A，随后用户切到 B
    session.currentTopicId = "A";
    await flushPromises();
    session.currentTopicId = "B";
    await flushPromises();

    // 原请求这时才失败
    deferred.reject(new Error("网络中断"));
    await sending;
    await flushPromises();

    expect(
      session.failedSendsForTopic("A").map((r) => r.text),
      "原发送的失败原文留在了未绑定位置：绑定后它稳定归 A，回到 A 必须找得到",
    ).toEqual(["未绑定话题时发出的原文"]);
    expect(session.failedSendsForTopic(null), "绑定之后不许再留下占位话题下的失败记录").toEqual([]);
    expect(session.failedSendsForTopic("B"), "与 B 无关的失败原文不许落在 B").toEqual([]);
  });
});
