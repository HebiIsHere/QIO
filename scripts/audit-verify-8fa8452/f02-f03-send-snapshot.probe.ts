/**
 * E 路独立核验探针 · F02 未知回执重试丢失附件选择 + F03 未确认发送没有可靠保留
 * 基线 8fa845255c118a7104b1de8e37298ca745924482
 *
 * 规格来源：D:\qio-dev\_audit-findings-8fa8452.md 的 F02 / F03 段。
 *
 * F02 要求：每次发送保存**完整不可变提交快照**（原文、话题、client_request_id、
 *   明确附件列表、重试来源）；同一发送的所有重试复用同一身份与原请求语义，
 *   不得读当前输入框 / 当前话题 / 后来加入的附件；attachment_ids=[] 仍须是显式空列表。
 * F03 要求：未决发送的完整快照与身份独立持久保存；未决记录不因数量限制被静默淘汰；
 *   网络超时不得当「明确未受理」、不得自动生成新身份重发。
 *
 * 反例设计（区分缺陷与修复的关键）：
 *   - F02-a：发了一条**带附件**的未决消息 → 用户切话题、输入框加上别的附件 → 重试时
 *     必须按快照重放原附件（基线漏传附件参数 → 后端只拿到 text/topic，附件选择丢失）；
 *   - F02-b：发了一条**显式无附件**的未决消息 → 重试必须仍显式传 []（基线漏传 →
 *     后端走遗留兜底，把话题下未绑定附件绑到这条纯文字消息上）；
 *   - F02-c：基线首次发送已带上显式空数组（api.ts 已修），本用例是**回归保护**；
 *   - F03-a：同一话题连续 51 条未决发送，第 1 条不得被 slice(-49) 静默淘汰。
 *
 * 运行（只跑本探针）：
 *   cd D:\qio-dev\qa-e
 *   npx --prefix frontend vitest run --root . -c scripts\audit-verify-8fa8452\vitest.config.ts
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { useSessionStore } from "../../frontend/src/stores/session";

vi.mock("../../frontend/src/services/api", () => ({
  api: {
    sendTurn: vi.fn(),
    getSessionContext: vi.fn(),
    stopTurn: vi.fn(),
    saveDrafts: vi.fn(),
    lookupTurnByRequest: vi.fn(),
    getInstance: vi.fn(),
  },
}));

vi.mock("../../frontend/src/services/interactive", () => ({
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
  updateIntentPreview: vi.fn(),
}));

import { api } from "../../frontend/src/services/api";

/** 内存 localStorage：jsdom 默认有，但显式装一份更稳（探针只读全局，不碰产品代码）。 */
function installLocalStorage(): void {
  const map = new Map<string, string>();
  Object.defineProperty(globalThis, "localStorage", {
    configurable: true,
    value: {
      getItem: (k: string) => (map.has(k) ? (map.get(k) as string) : null),
      setItem: (k: string, v: string) => void map.set(k, String(v)),
      removeItem: (k: string) => void map.delete(k),
      clear: () => map.clear(),
      key: (i: number) => Array.from(map.keys())[i] ?? null,
      get length() {
        return map.size;
      },
    },
  });
}

/** 无 status 的错误 = 没拿到回执（needs-confirm）。 */
function networkError(): Error {
  return new TypeError("Failed to fetch");
}

beforeEach(() => {
  installLocalStorage();
  vi.resetAllMocks();
  vi.mocked(api.getSessionContext).mockResolvedValue({ topic_id: null } as never);
  setActivePinia(createPinia());
});

describe("F02 未知回执重试必须按快照重放", () => {
  it("F02-a 带附件的未决发送，重试必须带原来的附件（不读当前输入框/当前话题）", async () => {
    const session = useSessionStore();
    session.currentTopicId = "A";
    vi.mocked(api.sendTurn).mockRejectedValueOnce(networkError()); // 首次：无回执
    await session.send("原始文字", ["att_1", "att_2"], []);

    const attempt = session.sendAttempts[0];
    expect(attempt, "首次发送后必须留下一条未决发送记录").toBeTruthy();
    const messageId = attempt.messageId;
    expect(
      attempt.state,
      "无回执的发送必须停在『未知受理』而不是『明确未受理』",
    ).toBe("needs-confirm");

    // 用户在这期间切了话题、把输入框改成别的东西并加了别的附件
    session.currentTopicId = "B";
    session.draft = "之后才输入的文字";
    vi.mocked(api.sendTurn).mockResolvedValueOnce({
      ok: true,
      accepted: true,
      turn_id: "turn_retry",
      status: "accepted",
      topic_id: "A",
    } as never);

    await session.retrySendAttempt(messageId);

    const calls = vi.mocked(api.sendTurn).mock.calls;
    console.log("[F02-a] sendTurn 调用 =", JSON.stringify(calls));
    expect(calls.length, "首次 + 重试 = 2 次调用").toBe(2);
    const retryCall = calls[calls.length - 1];
    // 重试必须按快照重放原文与话题
    expect(retryCall[0], "重试必须重放快照里的原文，不能读当前输入框").toBe("原始文字");
    expect(retryCall[1], "重试必须回到快照里的话题，不能跟当前话题走").toBe("A");
    // 关键：重试必须带原附件
    expect(
      retryCall[2],
      "F02 缺陷复现：重试没有携带快照里的 attachmentIds（基线 retrySendAttempt 只传 message+topicId），" +
        "原请求的附件选择在重试时丢失。实际第三参=" + JSON.stringify(retryCall[2]),
    ).toEqual(["att_1", "att_2"]);
  });

  it("F02-b 显式无附件的未决发送，重试必须仍是显式空数组（不得变成字段缺失）", async () => {
    const session = useSessionStore();
    session.currentTopicId = "A";
    vi.mocked(api.sendTurn).mockRejectedValueOnce(networkError());
    await session.send("纯文字", [], []);
    const messageId = session.sendAttempts[0].messageId;

    // 用户后来给输入框加了附件，但这跟原来那条无关
    vi.mocked(api.sendTurn).mockResolvedValueOnce({
      ok: true,
      accepted: true,
      turn_id: "turn_retry",
      status: "accepted",
      topic_id: "A",
    } as never);
    await session.retrySendAttempt(messageId);

    const calls = vi.mocked(api.sendTurn).mock.calls;
    const retryCall = calls[calls.length - 1];
    console.log("[F02-b] 重试调用 =", JSON.stringify(retryCall));
    expect(
      retryCall[2],
      "F02 缺陷复现：显式无附件的发送在重试时把 attachment_ids 丢了（undefined = 字段缺失），" +
        "后端会走遗留兜底把话题下未绑定附件绑到这条纯文字消息上。实际第三参=" +
        JSON.stringify(retryCall[2]),
    ).toEqual([]);
  });

  it("F02-c 首次发送：显式无附件必须传空数组而不是缺字段（回归保护）", async () => {
    const session = useSessionStore();
    session.currentTopicId = "A";
    vi.mocked(api.sendTurn).mockResolvedValueOnce({
      ok: true,
      accepted: true,
      turn_id: "turn_1",
      status: "accepted",
      topic_id: "A",
    } as never);
    await session.send("纯文字", [], []);

    const first = vi.mocked(api.sendTurn).mock.calls[0];
    console.log("[F02-c] 首次调用 =", JSON.stringify(first));
    expect(first[2]).toEqual([]);
  });

  it("F02-d 未决重试必须复用同一 clientRequestId（不得换身份重发）", async () => {
    const session = useSessionStore();
    session.currentTopicId = "A";
    vi.mocked(api.sendTurn).mockRejectedValueOnce(networkError());
    await session.send("原始文字", [], []);
    const attempt = session.sendAttempts[0];
    const idBefore = attempt.clientRequestId;

    vi.mocked(api.sendTurn).mockRejectedValueOnce(networkError());
    await session.retrySendAttempt(attempt.messageId);
    console.log("[F02-d] clientRequestId 前/后 =", idBefore, "/", session.sendAttempts[0].clientRequestId);
    expect(session.sendAttempts[0].clientRequestId).toBe(idBefore);
  });
});

describe("F03 未确认发送的保留", () => {
  it("F03-a 同一话题 51 条未决发送：第 1 条不得被数量上限静默淘汰", async () => {
    const session = useSessionStore();
    session.currentTopicId = "A";
    vi.mocked(api.sendTurn).mockRejectedValue(networkError());
    const firstIds: string[] = [];
    for (let i = 0; i < 51; i += 1) {
      await session.send("消息 " + i, [], []);
      firstIds.push(session.sendAttempts[session.sendAttempts.length - 1].messageId);
    }
    const kept = new Set(session.sendAttempts.map((a) => a.messageId));
    const lost = firstIds.filter((id) => !kept.has(id));
    console.log("[F03-a] 未决记录数 =", session.sendAttempts.length, "被淘汰 =", JSON.stringify(lost));
    expect(
      lost,
      "F03 缺陷复现：第 " +
        (firstIds.indexOf(lost[0]) + 1) +
        " 条未决发送被 sendAttempts.slice(-49) 静默淘汰（数据悄悄消失）。lost=" +
        JSON.stringify(lost),
    ).toEqual([]);
  });
});
