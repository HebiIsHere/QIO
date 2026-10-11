/**
 * 第二批独立对抗性探针 · 02/03/04：同正文不同身份时「成功只清它实际接受的那一条」。
 *
 * 与仓库内 final-b2-sendIdentity.test.ts 的差别（为什么另写一个）：
 *   1) 每条都补了**更强的可观察事实**：
 *      - 02：显式断言互换把记录 contentVersion 推到 2、旧回执接受的版本是 1；
 *      - 03：显式断言切话题后重发关联**没有跟着过去**（跨话题发送的新身份），
 *        并额外确认 A 的失败记录与 B 的空列表；
 *      - 04：补「切回 A 时绑定前写下的新稿仍在输入框」（输入保护与发送归属解耦）；
 *   2) 另加一个**隔离用例**：直接给成功清理喂一条「身份属于 B、话题算作 A」的归属，
 *      单独把 (recordId|draftId) + 内容版本 + 话题 三重条件里的**话题条件**逼到唯一；
 *      仓库用例里因为 bind() 会先解除关联，话题条件不是单独承重的。
 *
 * 契约（docs/interactive-final-closure-contract.md M3 / 收尾项 02、03、04）：
 *   发送尝试的身份 ≠ 内容版本；受理成功只清它实际接受的 (记录|身份, 内容版本, 话题)。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { flushPromises } from "@vue/test-utils";
import { useSessionStore } from "../../../frontend/src/stores/session";
import { api } from "../../../frontend/src/services/api";
import { draftStorageKey, readDraft, UNBOUND_DRAFT_ID } from "../../../frontend/src/interactive/drafts";

vi.mock("../../../frontend/src/services/api", () => ({
  api: {
    sendTurn: vi.fn(),
    getSessionContext: vi.fn(),
    stopTurn: vi.fn(),
    saveDrafts: vi.fn(),
  },
}));

vi.mock("../../../frontend/src/services/interactive", () => ({
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
function pendingSend() {
  const slots: { resolve: (v: unknown) => void; reject: (e: unknown) => void } = { resolve: () => {}, reject: () => {} };
  const promise = new Promise((resolve, reject) => { slots.resolve = resolve; slots.reject = reject; });
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

describe("B2-02 互换后旧重发成功：版本对不上就不许清", () => {
  it("互换把内容版本推到 2，旧回执只接受过版本 1 → 记录必须保留", async () => {
    vi.useFakeTimers();
    const session = useSessionStore();
    session.currentTopicId = "A";

    vi.mocked(api.sendTurn).mockRejectedValueOnce(new Error("网络中断"));
    session.draft = "没发出去的原文";
    await vi.advanceTimersByTimeAsync(500);
    session.sendAttribution();
    session.draft = "";
    expect(await session.send("没发出去的原文")).toBe(false);
    const record = session.failedSendsForTopic("A")[0];
    expect(record?.text).toBe("没发出去的原文");
    expect(record?.contentVersion ?? 1, "新记录初始内容版本应为 1").toBe(1);

    expect(session.retryFailedSend(record.id).ok).toBe(true);
    const resend = session.sendAttribution();
    expect(resend.draftId, "显式重发应沿用记录身份").toBe(record.draftId);
    expect(resend.contentVersion ?? 1, "显式重发携带的是记录当时的版本 1").toBe(1);
    session.draft = "";
    const deferred = pendingSend();
    const sending = session.send("没发出去的原文");
    await flushPromises();

    session.draft = "后来输入并保留的新文字";
    await vi.advanceTimersByTimeAsync(500);
    expect(session.swapFailedSendText(record.id).ok).toBe(true);
    const afterSwap = session.failedSendsForTopic("A")[0];
    expect(afterSwap?.text, "互换后记录里应当是用户后来输入的文字").toBe("后来输入并保留的新文字");
    expect(afterSwap?.contentVersion, "互换必须把内容版本 +1").toBe(2);
    expect(session.draft, "互换把失败原文放回输入框").toBe("没发出去的原文");

    deferred.resolve(acceptedSend("turn_resend"));
    expect(await sending).toBe(true);
    await vi.advanceTimersByTimeAsync(800);

    // 断言**状态里的失败记录列表**，而不是只看 failedSendsForTopic：
    // 后者在「列表被清空但 failedSend 镜像还在」时仍会返回镜像，会把假通过藏起来。
    expect(
      session.failedSends.map((r) => r.text + "@v" + (r.contentVersion ?? 1)),
      "旧回执只接受过版本 1，不许把状态里已经换成版本 2 的记录删掉",
    ).toEqual(["后来输入并保留的新文字@v2"]);
    expect(
      session.failedSendsForTopic("A").map((r) => r.text + "@v" + (r.contentVersion ?? 1)),
      "用户可见的 A 话题失败清单同样不许被旧回执清掉",
    ).toEqual(["后来输入并保留的新文字@v2"]);
    expect(session.draft, "放进输入框的失败原文不许被旧回执清掉").toBe("没发出去的原文");
  });
});

describe("B2-03 切话题手打相同文字发送成功：不许清别的话题的记录", () => {
  it("A 找回原文→切 B→B 手打同文发送成功：A 的记录仍在，B 不凭空多记录", async () => {
    vi.useFakeTimers();
    const session = useSessionStore();
    session.currentTopicId = "A";

    vi.mocked(api.sendTurn).mockRejectedValueOnce(new Error("网络中断"));
    session.draft = "两个话题里一样的文字";
    await vi.advanceTimersByTimeAsync(500);
    session.sendAttribution();
    session.draft = "";
    expect(await session.send("两个话题里一样的文字")).toBe(false);
    const record = session.failedSendsForTopic("A")[0];
    expect(record?.text).toBe("两个话题里一样的文字");

    expect(session.retryFailedSend(record.id).ok).toBe(true);
    expect(session.draft).toBe("两个话题里一样的文字");

    session.currentTopicId = "B";
    await flushPromises();
    expect(session.draft, "B 话题装的是自己的草稿").toBe("");

    session.draft = "两个话题里一样的文字";
    await vi.advanceTimersByTimeAsync(500);
    const atB = session.sendAttribution();
    expect(atB.draftId, "跨话题后重发关联不得跟过去").not.toBe(record.draftId);
    expect(atB.resendOf ?? null, "B 的手打发送不是 A 那条记录的重发").toBeNull();
    session.draft = "";
    vi.mocked(api.sendTurn).mockResolvedValueOnce(acceptedSend("turn_b") as never);
    expect(await session.send("两个话题里一样的文字")).toBe(true);
    await vi.advanceTimersByTimeAsync(800);

    expect(
      session.failedSendsForTopic("A").map((r) => r.text),
      "B 手打相同文字发送成功，误清了 A 的失败记录",
    ).toEqual(["两个话题里一样的文字"]);
    expect(session.failedSendsForTopic("B"), "B 里没失败过，不许凭空出现记录").toEqual([]);
  });

  it("隔离用例：直接喂一条「身份属 B、话题算 A」的成功归属 → B 的记录必须保留", () => {
    const session = useSessionStore();
    session.failedSends = [
      { id: "fA", draftId: "draft_A", topicId: "A", text: "相同正文", draftSeq: 1, contentVersion: 1, at: 1 },
      { id: "fB", draftId: "draft_B", topicId: "B", text: "相同正文", draftSeq: 1, contentVersion: 1, at: 2 },
    ];
    const attribution = {
      draftId: "draft_B", topicId: "A", contentVersion: 1, text: "相同正文",
      draftSeq: 1, key: "k", at: 3, recorded: true,
    };
    session._clearFailedSendsAccepted(attribution as never, "ok");
    expect(
      session.failedSends.map((r) => r.id),
      "话题不符（身份/版本都对得上）的记录不许被清 —— 话题条件必须单独承重",
    ).toEqual(["fA", "fB"]);

    session._clearFailedSendsAccepted({ ...attribution, topicId: "B" } as never, "ok");
    expect(session.failedSends.map((r) => r.id), "真正对应的那条才被清").toEqual(["fA"]);
  });
});

describe("B2-04 绑定话题前输入新稿：原发送稳定归真实话题", () => {
  it("未绑定发送→写新稿本机保存→绑定 A→切 B→失败：记录在 A，新稿回到 A 仍在", async () => {
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

    session.draft = "绑定之前写下的新稿";
    session.flushDraft();
    expect(readDraft(chatKey(null))?.text, "新稿必须先落进本机").toBe("绑定之前写下的新稿");

    session.currentTopicId = "A";
    await flushPromises();
    session.currentTopicId = "B";
    await flushPromises();

    deferred.reject(new Error("网络中断"));
    expect(await sending).toBe(false);
    await flushPromises();

    expect(
      session.failedSendsForTopic("A").map((r) => r.text),
      "绑定后原发送的失败原文必须稳定归 A",
    ).toEqual(["未绑定话题时发出的原文"]);
    expect(session.failedSendsForTopic(null), "绑定之后不许再留下 null 占位记录").toEqual([]);
    expect(session.failedSendsForTopic("B"), "与 B 无关的失败原文不许落在 B").toEqual([]);

    session.currentTopicId = "A";
    await flushPromises();
    expect(session.draft, "绑定前写下的新稿不许被失败原文顶掉").toBe("绑定之前写下的新稿");
  });
});
