/**
 * 收尾轮 B2 批 2：关闭重开之后的重发关联（契约 M3，反例 14）。
 *
 * 正确行为期望：
 * - 重开后失败原文已经在输入框里，用户直接发送成功 —— 这条失败记录必须被清掉
 *   （不许继续显示未发送）；恢复/重开时要按记录重新建立可靠的**显式关联**。
 * - 必须区分「恢复原文后发送」与「手动输入相同文字发送」：后者不清任何记录。
 * - 覆盖编辑后发送、互换、取消关联、切话题与再次重开。
 *
 * 标注：【状态】只依赖 stores/session.ts 的公开状态与动作；【模拟失败】受控 api.sendTurn。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { useSessionStore } from "../session";
import { api } from "../../services/api";
import { draftStorageKey, UNBOUND_DRAFT_ID } from "../../interactive/drafts";

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

/** 会话 1：在 A 里失败一次，然后用户点「找回原文」（原文进输入框并写进本机） */
async function failAndRestoreInA(text: string): Promise<void> {
  const session = useSessionStore();
  session.currentTopicId = "A";
  session.draft = text;
  session.flushDraft();
  vi.mocked(api.sendTurn).mockRejectedValueOnce(new Error("网络中断"));
  const attribution = session.sendAttribution();
  session.draft = "";
  await session.send(text);
  const restored = session.retryFailedSend(attribution.draftId);
  expect(restored.ok, "回到原话题应当能取回失败原文（这是本反例的前提）").toBe(true);
  void chatKey;
}

/** 关闭重开：新 pinia、同一份本机存储 */
function reopen() {
  setActivePinia(createPinia());
  return useSessionStore();
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

describe("14 重开后直接重发成功：失败记录必须被清掉", () => {
  it("【状态】重开后原文已在输入框，用户直接发送成功 → 记录清空", async () => {
    vi.useFakeTimers();
    await failAndRestoreInA("重开后要重发的话");

    const session = reopen();
    session.currentTopicId = "A";
    expect(session.draft, "重开后原文没有回到输入框（本反例的前提）").toBe("重开后要重发的话");
    expect(session.failedSendsForTopic("A").map((r) => r.text)).toEqual(["重开后要重发的话"]);

    vi.mocked(api.sendTurn).mockResolvedValueOnce(acceptedSend("turn_resend") as never);
    const at = session.sendAttribution();
    expect(at.resendOf, "重开后没有按记录重新建立显式关联").toBeTruthy();
    session.draft = "";
    expect(await session.send("重开后要重发的话")).toBe(true);
    await vi.advanceTimersByTimeAsync(800);

    expect(
      session.failedSendsForTopic("A"),
      "重开后重发成功了，失败记录仍留着（界面还会显示「未发送」）",
    ).toEqual([]);
    expect(session.draft, "已经发出去的原文又留在输入框里").toBe("");
  });

  it("【状态】重开后编辑原文再发送：仍算这条记录的重发（M3 编辑后发送），记录清掉", async () => {
    vi.useFakeTimers();
    await failAndRestoreInA("重开后要重发的话");

    const session = reopen();
    session.currentTopicId = "A";
    const edited = "重开后要重发的话（补充）";
    session.draft = edited;
    await vi.advanceTimersByTimeAsync(500);

    vi.mocked(api.sendTurn).mockResolvedValueOnce(acceptedSend("turn_edit") as never);
    const at = session.sendAttribution();
    expect(at.resendOf, "编辑后的发送应当仍关联到那条失败记录").toBeTruthy();
    session.draft = "";
    expect(await session.send(edited)).toBe(true);
    await vi.advanceTimersByTimeAsync(800);

    expect(
      session.failedSendsForTopic("A"),
      "编辑后发送成功，失败记录没有被清掉",
    ).toEqual([]);
  });

  it("【状态】重开后清空输入、再手打相同文字发送成功：记录必须保留（区分手打）", async () => {
    vi.useFakeTimers();
    await failAndRestoreInA("重开后要重发的话");

    const session = reopen();
    session.currentTopicId = "A";
    // 用户主动清空输入：这次取回被放弃（取消关联）
    session.draft = "";
    await vi.advanceTimersByTimeAsync(500);
    // 再手打同样的文字发送
    session.draft = "重开后要重发的话";
    await vi.advanceTimersByTimeAsync(500);

    vi.mocked(api.sendTurn).mockResolvedValueOnce(acceptedSend("turn_manual") as never);
    const at = session.sendAttribution();
    expect(at.resendOf, "手打相同文字被判成了「恢复原文后的重发」").toBeNull();
    session.draft = "";
    expect(await session.send("重开后要重发的话")).toBe(true);
    await vi.advanceTimersByTimeAsync(800);

    expect(
      session.failedSendsForTopic("A").map((r) => r.text),
      "手动输入相同文字发送成功，按文字猜测把失败记录清掉了",
    ).toEqual(["重开后要重发的话"]);
  });

  it("【状态】重开后切到别的话题：在别处手打相同文字发送成功不清原话题的记录", async () => {
    vi.useFakeTimers();
    await failAndRestoreInA("跨话题重开的话");

    const session = reopen();
    session.currentTopicId = "A";
    expect(session.draft).toBe("跨话题重开的话");
    // 切到 B：关联必须随话题失效
    session.currentTopicId = "B";
    session.draft = "跨话题重开的话";
    await vi.advanceTimersByTimeAsync(500);

    vi.mocked(api.sendTurn).mockResolvedValueOnce(acceptedSend("turn_b") as never);
    const at = session.sendAttribution();
    expect(at.resendOf, "B 的手动发送沿用了 A 的记录关联").toBeNull();
    session.draft = "";
    expect(await session.send("跨话题重开的话")).toBe(true);
    await vi.advanceTimersByTimeAsync(800);

    expect(
      session.failedSendsForTopic("A").map((r) => r.text),
      "B 的成功发送把 A 的失败记录清掉了",
    ).toEqual(["跨话题重开的话"]);
  });

  it("【状态】再次重开：记录与输入都在，之后发送仍能清掉这条记录", async () => {
    vi.useFakeTimers();
    await failAndRestoreInA("再次重开的话");

    let session = reopen();
    session.currentTopicId = "A";
    expect(session.draft).toBe("再次重开的话");

    // 不发送，再关一次再开
    session = reopen();
    session.currentTopicId = "A";
    expect(session.draft, "再次重开后原文不在输入框").toBe("再次重开的话");
    expect(
      session.failedSendsForTopic("A").map((r) => r.text),
      "再次重开后失败记录不见了",
    ).toEqual(["再次重开的话"]);

    vi.mocked(api.sendTurn).mockResolvedValueOnce(acceptedSend("turn_again") as never);
    const at = session.sendAttribution();
    expect(at.resendOf, "再次重开后没有重建显式关联").toBeTruthy();
    session.draft = "";
    expect(await session.send("再次重开的话")).toBe(true);
    await vi.advanceTimersByTimeAsync(800);
    expect(session.failedSendsForTopic("A")).toEqual([]);
  });
});
