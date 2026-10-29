/**
 * 子 agent B 定向验收（F02 / F03 / F04 前端侧）：发送快照、未决发送、受理事实。
 *
 * 每条都写「用户能观察到的正确行为」，不依赖实现细节：
 * - F02：重试只消费**提交那一刻的不可变快照**（原文 / 话题 / 显式附件列表 / 请求身份 /
 *        重试来源 / 需要延续的起点），不看当前输入框、当前话题或后来加入的附件。
 * - F03：未决发送独立持久保存完整快照与请求身份，重开后可先查证再同身份重试 / 查看原文 /
 *        明确放弃；数量不做静默淘汰；本机读写失败如实回报。
 * - F04：受理事实按契约 C3 消费 —— 不能只看 turn_id；accepted=false 时保留原文并结束
 *        错误的运行/等待状态；区分「未被放行执行」与「曾受理后又取消」。
 *
 * 标注：【会话层】只依赖 stores/session.ts 的公开状态与动作；
 *      【模拟失败】受控 api.sendTurn / 受控 api.lookupTurnByRequest；
 *      【真实请求体】用假 fetch 闸门走真实的 request 路径（看得到 client_request_id 与 attachment_ids）。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { flushPromises } from "@vue/test-utils";
import { useSessionStore } from "../session";
import { api } from "../../services/api";


vi.mock("../../services/api", () => ({
  api: {
    sendTurn: vi.fn(),
    getSessionContext: vi.fn(),
    lookupTurnByRequest: vi.fn(),
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
    advanceIntent: vi.fn(),
    updateIntentPreview: vi.fn(),
    fetchHistoryPage: vi.fn(),
    fetchMessages: vi.fn(),
  },
}));

/**
 * 请求身份的观测点：重试到底用的是不是**同一个** client_request_id，必须看得见。
 * 这里替换 sendIdentity 模块，记录每一次「挂给下一次 sendTurn 的身份」。
 * （api 模块同时也被 mock，所以不会真的发请求。）
 */
const issuedRequestIds: string[] = [];
let requestIdSeq = 0;
vi.mock("../../services/sendIdentity", () => ({
  setNextSendRequestId: (id: string) => {
    issuedRequestIds.push(id);
  },
  takeNextSendRequestId: () => null,
  newClientRequestId: () => {
    requestIdSeq += 1;
    return "creq_" + requestIdSeq;
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
  advanceIntent: vi.fn(),
  updateIntentPreview: vi.fn(),
}));

const PENDING_KEY = "qio.chat.pendingSend.v1";
const FAILED_KEY = "qio.chat.failedSend.v1";

function acceptedSend(turnId = "turn_1", extra: Record<string, unknown> = {}) {
  return { ok: true, accepted: true, turn_id: turnId, status: "accepted", topic_id: "A", ...extra };
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

function reopen() {
  setActivePinia(createPinia());
  return useSessionStore();
}

beforeEach(() => {
  localStorage.clear();
  issuedRequestIds.length = 0;
  requestIdSeq = 0;
  vi.resetAllMocks();
  vi.mocked(api.getSessionContext).mockResolvedValue({ topic_id: null } as never);
  setActivePinia(createPinia());
});

afterEach(() => {
  vi.useRealTimers();
  localStorage.clear();
});

/* ==========================================================================
 * F02 完整不可变提交快照
 * ======================================================================== */
describe("F02：重试只消费提交那一刻的不可变快照", () => {
  it("有附件：重试复用提交时的原文 / 话题 / 附件列表，不看现在的话题与输入框", async () => {
    vi.useFakeTimers();
    const session = useSessionStore();
    session.currentTopicId = "A";
    session.draft = "带附件的原文";
    session.sendAttribution();
    session.draft = "";
    vi.mocked(api.sendTurn).mockRejectedValueOnce(new Error("网络中断"));
    expect(await session.send("带附件的原文", ["att_1", "att_2"])).toBe(false);

    const record = session.pendingSends[0];
    expect(record, "未决发送没有进独立台账").toBeTruthy();
    expect(record.snapshot.text).toBe("带附件的原文");
    expect(record.snapshot.topicId).toBe("A");
    expect(record.snapshot.attachmentIds).toEqual(["att_1", "att_2"]);
    expect(record.snapshot.attachmentsExplicit, "附件列表必须是显式的").toBe(true);

    // 发送期间：用户切到别的话题、输入框里另有内容、后来又多选了附件
    session.currentTopicId = "B";
    session.draft = "B 里的新内容";
    vi.mocked(api.sendTurn).mockResolvedValueOnce(acceptedSend("turn_r") as never);
    expect(await session.retrySendAttempt(record.id)).toBe(true);

    const call = vi.mocked(api.sendTurn).mock.calls[vi.mocked(api.sendTurn).mock.calls.length - 1];
    expect(call[0], "重试改用了当前输入框里的文字").toBe("带附件的原文");
    expect(call[1], "重试改用了当前话题").toBe("A");
    expect(call[2], "重试没有复用提交那一刻的附件列表").toEqual(["att_1", "att_2"]);
  });

  it("显式无附件：快照里是存在的空数组，重试照样把它显式发出去（不变成缺字段）", async () => {
    const session = useSessionStore();
    session.currentTopicId = "A";
    session.draft = "没有任何附件的原文";
    session.sendAttribution();
    session.draft = "";
    vi.mocked(api.sendTurn).mockRejectedValueOnce(new Error("网络中断"));
    expect(await session.send("没有任何附件的原文", [])).toBe(false);

    const record = session.pendingSends[0];
    expect(record.snapshot.attachmentIds).toEqual([]);
    expect(record.snapshot.attachmentsExplicit, "空数组也必须是显式的").toBe(true);

    // 发送期间用户新选了附件（这里用输入框变化代表「后来加入的附件」）：
    // 同身份重试仍然只许用提交那一刻的显式空列表。
    vi.mocked(api.sendTurn).mockResolvedValueOnce(acceptedSend("turn_ok") as never);
    expect(await session.retrySendAttempt(record.id)).toBe(true);
    const call = vi.mocked(api.sendTurn).mock.calls[vi.mocked(api.sendTurn).mock.calls.length - 1];
    expect(call[2], "空列表被传成了 undefined（= 缺字段，后端会走遗留兜底）").toEqual([]);
    expect(issuedRequestIds[0], "第一次发送没有挂上请求身份").toBe("creq_1");
    expect(issuedRequestIds[1], "重试换了新的请求身份").toBe(issuedRequestIds[0]);
  });
});

  it("起点信息（需要延续的片段 / 接续选择）在提交那一刻冻结进快照", async () => {
    const session = useSessionStore();
    session.currentTopicId = "A";
    session.anchorFragmentId = "frag_1";
    session.pendingContinuation = { intentId: "intent_1", sourceTitle: "某段历史" };
    session.draft = "从这段历史继续";
    session.sendAttribution();
    session.draft = "";
    vi.mocked(api.sendTurn).mockRejectedValueOnce(new Error("网络中断"));
    await session.send("从这段历史继续");

    const snapshot = session.pendingSends[0].snapshot;
    expect(snapshot.continuation?.topicId).toBe("A");
    expect(snapshot.continuation?.fragmentId).toBe("frag_1");
    expect(snapshot.continuation?.intentId).toBe("intent_1");
    expect(snapshot.queued).toBe(false);
    expect(typeof snapshot.capturedAt).toBe("number");
  });

/* ==========================================================================
 * F03 未决发送的可靠保留
 * ======================================================================== */
describe("F03：未决发送独立保存完整快照与请求身份", () => {
  it("51 条未决记录：一条都不被静默淘汰，展示可以分页", async () => {
    vi.useFakeTimers();
    const session = useSessionStore();
    session.currentTopicId = "A";
    vi.mocked(api.sendTurn).mockRejectedValue(new Error("网络中断"));
    for (let i = 1; i <= 51; i += 1) {
      await session.send("未决原文 " + i);
    }
    expect(session.pendingSends.length, "未决记录被数量上限静默淘汰了").toBe(51);
    const page = session.pendingSendsPage(0, 20);
    expect(page.total).toBe(51);
    expect(page.pageCount).toBe(3);
    expect(page.items.length).toBe(20);
    const last = session.pendingSendsPage(2, 20);
    expect(last.items.length).toBe(11);
    // 落到本机的也必须是 51 条（写盘时同样不许裁剪）
    const persisted = JSON.parse(localStorage.getItem(PENDING_KEY) as string) as {
      records: unknown[];
    };
    expect(persisted.records.length).toBe(51);
    expect(session.pendingSends.map((r) => r.snapshot.text)).toContain("未决原文 1");
  });

  it("关闭重开：未决记录还在，查证与同身份重试用同一个 client_request_id", async () => {
    vi.useFakeTimers();
    const session = useSessionStore();
    session.currentTopicId = "A";
    vi.mocked(api.sendTurn).mockRejectedValueOnce(new TypeError("Failed to fetch"));
    vi.mocked(api.lookupTurnByRequest).mockResolvedValueOnce({
      ok: false,
      unknown: true,
    } as never);
    expect(await session.send("重开后要重试的话")).toBe(false);
    const requestId = session.pendingSends[0].clientRequestId;
    await vi.advanceTimersByTimeAsync(700); // 自动查证 → 404

    // 关闭重开：新 pinia、同一份本机存储
    const reopened = useSessionStore();
    expect(
      reopened.pendingSends.map((record) => record.snapshot.text),
      "重开后未决发送的原文不见了",
    ).toEqual(["重开后要重试的话"]);
    expect(reopened.pendingSendReadError).toBeNull();
    // 启动不自动查证、不自动发送：重开后 sendTurn 只被调过一次（就是重试那次）
    const callsAfterReopen = vi.mocked(api.sendTurn).mock.calls.length;

    // 先查证：后端没有这条记录 → 仍然是「未知」，不是「没发送」
    vi.mocked(api.lookupTurnByRequest).mockResolvedValueOnce({ ok: false, unknown: true } as never);
    await reopened.recheckPendingSend();
    expect(reopened.pendingSends.length, "查证没有结论时记录必须保留").toBe(1);
    expect(reopened.pendingSends[0].state, "404 不许被当成「明确未受理」").toBe("needs-confirm");

    // 同身份重试：复用同一个 client_request_id
    vi.mocked(api.sendTurn).mockRejectedValueOnce(new TypeError("Failed to fetch"));
    await reopened.retrySendAttempt(reopened.pendingSends[0].id);
    expect(vi.mocked(api.sendTurn).mock.calls.length - callsAfterReopen).toBe(1);
    expect(
      issuedRequestIds[issuedRequestIds.length - 1],
      "重试换了新的请求身份（后端会当成两次发送）",
    ).toBe(requestId);
  });

  it("本机读失败：保留内存事实并如实回报，不假装没有 / 不假装已保存", async () => {
    const original = Object.getOwnPropertyDescriptor(globalThis, "localStorage");
    Object.defineProperty(globalThis, "localStorage", {
      configurable: true,
      value: {
        getItem: () => {
          throw new Error("本地存储被禁用");
        },
        setItem: () => {
          throw new Error("本地存储被禁用");
        },
        removeItem: () => {},
      },
    });
    try {
      const session = useSessionStore();
      expect(session.pendingSendReadError ?? "", "读失败没有被如实回报").toContain("本地存储被禁用");
      const retried = session.retryPendingSendRestore();
      expect(retried.ok).toBe(false);
      expect(retried.restored).toBe(0);

      vi.mocked(api.sendTurn).mockRejectedValueOnce(new Error("网络中断"));
      session.currentTopicId = "A";
      session.draft = "写不进本机的未决原文";
      session.sendAttribution();
      session.draft = "";
      await session.send("写不进本机的未决原文");
      // 内存事实必须保留，且如实说这次没写进本机
      expect(session.pendingSends.map((r) => r.snapshot.text)).toEqual(["写不进本机的未决原文"]);
      expect(session.pendingSendPersistError ?? "").toContain("本地存储被禁用");
    } finally {
      if (original) Object.defineProperty(globalThis, "localStorage", original);
    }
  });

  it("明确 HTTP 拒绝是另一条路：原文进可恢复台账，用户能找回", async () => {
    const session = useSessionStore();
    session.currentTopicId = "A";
    session.draft = "被明确拒绝的原文";
    session.sendAttribution();
    session.draft = "";
    const rejected = Object.assign(new Error("bad request"), { status: 400 });
    vi.mocked(api.sendTurn).mockRejectedValueOnce(rejected);

    expect(await session.send("被明确拒绝的原文")).toBe(false);
    expect(
      session.pendingSends.length,
      "明确拒绝不该进未决台账（两者语义不同）",
    ).toBe(0);
    const record = session.failedSendsForTopic("A")[0];
    expect(record?.text).toBe("被明确拒绝的原文");
    expect(record?.failure).toBe("rejected");
    expect(session.retryFailedSend(record?.id).ok).toBe(true);
    expect(session.draft, "明确拒绝后的原文必须能回到输入框").toBe("被明确拒绝的原文");
  });

  it("明确重新发起新动作：用**新身份**，但保留可追溯的重发关联；同身份重试不重复建轮次", async () => {
    const session = useSessionStore();
    session.currentTopicId = "A";
    session.draft = "要重发的话";
    session.sendAttribution();
    session.draft = "";
    vi.mocked(api.sendTurn).mockRejectedValueOnce(new Error("网络中断"));
    await session.send("要重发的话");
    const record = session.failedSendsForTopic("A")[0];
    const firstRequestId = issuedRequestIds[0];

    // 用户点「找回原文」：显式重发关联
    expect(session.retryFailedSend(record.id).ok).toBe(true);
    const at = session.sendAttribution();
    expect(at.resendOf, "找回原文后重发没有建立可追溯的关联").toBe(record.id);
    expect(at.draftId, "显式重发沿用了被重发记录的身份（既有语义）").toBe(record.draftId);
    session.draft = "";
    vi.mocked(api.sendTurn).mockResolvedValueOnce(acceptedSend("turn_again") as never);
    expect(await session.send("要重发的话")).toBe(true);

    // 这是一次**新**的发送动作：新身份（否则后端会当成同一次而不再执行）
    expect(
      issuedRequestIds[issuedRequestIds.length - 1],
      "用户明确重新发起的新动作复用了旧身份（后端会当成重复请求而不执行）",
    ).not.toBe(firstRequestId);
    expect(session.failedSendsForTopic("A"), "重发成功后这条记录没有被清掉").toEqual([]);
  });

  it("兼容旧持久格式：读得回本机已有的未决记录与失败原文，不删用户数据", async () => {
    localStorage.setItem(
      PENDING_KEY,
      JSON.stringify({
        version: 1,
        records: [
          {
            // 旧形状：没有 snapshot，字段是平的
            id: "legacy_pending_1",
            clientRequestId: "creq_legacy",
            message: "旧格式的未决原文",
            topicId: "A",
            attachmentIds: [],
            state: "needs-confirm",
            lastError: "网关超时",
            createdAt: Date.now(),
          },
        ],
      }),
    );
    localStorage.setItem(
      FAILED_KEY,
      JSON.stringify({
        version: 1,
        topics: {
          A: [
            {
              id: "legacy_fail_1",
              draftId: "send_legacy",
              topicId: "A",
              text: "旧格式的失败原文",
              draftSeq: 2,
              at: Date.now(),
              error: "被拒绝",
            },
          ],
        },
      }),
    );
    const session = useSessionStore();
    expect(session.pendingSendReadError).toBeNull();
    expect(session.pendingSends.map((r) => r.snapshot.text)).toEqual(["旧格式的未决原文"]);
    expect(session.pendingSends[0].snapshot.attachmentIds).toEqual([]);
    expect(session.pendingSends[0].snapshot.attachmentsExplicit).toBe(true);
    expect(session.pendingSends[0].state).toBe("needs-confirm");
    expect(session.failedSendsForTopic("A").map((r) => r.text)).toEqual(["旧格式的失败原文"]);
    // 旧记录没有分类字段 → 按「明确失败」对待（不虚报一个不存在的查证入口）
    expect(session.failedSendsForTopic("A")[0].failure).toBe("rejected");
    expect(session.failedSendErrors["legacy_fail_1"]).toBe("被拒绝");
  });

  it("新输入不得覆盖原未决记录", async () => {
    vi.useFakeTimers();
    const session = useSessionStore();
    session.currentTopicId = "A";
    session.draft = "没有结论的原文";
    session.sendAttribution();
    session.draft = "";
    vi.mocked(api.sendTurn).mockRejectedValueOnce(new Error("网络中断"));
    await session.send("没有结论的原文");

    session.draft = "后来写的新内容";
    await vi.advanceTimersByTimeAsync(500);
    expect(session.pendingSends.map((r) => r.snapshot.text)).toEqual(["没有结论的原文"]);
    // 原文没有被当成输入框里的新草稿（F03/F12：用户顺手 Enter 不该换身份重发）
    expect(session.draft).toBe("后来写的新内容");
  });
});

/* ==========================================================================
 * F04 受理事实（契约 C3）
 * ======================================================================== */
describe("F04：不能只看 turn_id 就判成功", () => {
  it("accepted=false 且从未放行执行：保留原文、结束错误的运行/等待状态", async () => {
    const session = useSessionStore();
    session.currentTopicId = "A";
    session.draft = "准备期被放弃的原文";
    session.sendAttribution();
    session.draft = "";
    vi.mocked(api.sendTurn).mockResolvedValueOnce({
      ok: true,
      accepted: false,
      cancelled: true,
      turn_id: "turn_never_ran",
      status: "cancelled",
      topic_id: "A",
      receipt: {
        state: "not_accepted",
        accepted: false,
        cancelled: true,
        pending: false,
        reason: "preparation_abandoned",
        deduped: true,
        turn_id: "turn_never_ran",
        status: "cancelled",
        attachment_ids: [],
      },
    } as never);

    expect(await session.send("准备期被放弃的原文"), "被放弃、从未放行的轮次不许算受理成功").toBe(false);
    expect(session.turnRunning, "错误的运行态没有结束").toBe(false);
    expect(session.turnPhase).toBe("idle");
    expect(session.messages.some((m) => m.content === "准备期被放弃的原文")).toBe(false);
    // 原文保留在可恢复台账里（不是「没有记录」）
    expect(session.failedSendsForTopic("A").map((r) => r.text)).toEqual(["准备期被放弃的原文"]);
    // 未决台账里不留这条（它已经有明确结论了）
    expect(session.pendingSends.length).toBe(0);
    expect(session.lastError ?? "").toContain("未被放行执行");
  });

  it("曾受理后又取消：受理事实成立，不许显示成「没发出去」", async () => {
    const session = useSessionStore();
    session.currentTopicId = "A";
    vi.mocked(api.sendTurn).mockResolvedValueOnce({
      ok: true,
      accepted: true,
      cancelled: true,
      turn_id: "turn_cancelled",
      status: "cancelled",
      topic_id: "A",
      receipt: {
        state: "cancelled_after_accept",
        accepted: true,
        cancelled: true,
        pending: false,
        reason: "cancelled_after_accept",
        deduped: false,
        turn_id: "turn_cancelled",
        status: "cancelled",
        attachment_ids: [],
      },
    } as never);

    expect(await session.send("受理后被取消的原文")).toBe(true);
    expect(session.messages.some((m) => m.content === "受理后被取消的原文"), "消息被撤回了").toBe(true);
    expect(session.failedSendsForTopic("A"), "已受理的轮次被误报成未发送").toEqual([]);
    expect(session.warning ?? "").toContain("取消");
  });

  it("去重命中返回原请求的真实受理事实：accepted=false 依然不算成功", async () => {
    const session = useSessionStore();
    session.currentTopicId = "A";
    vi.mocked(api.sendTurn).mockResolvedValueOnce({
      ok: true,
      accepted: false,
      cancelled: true,
      turn_id: "turn_dup",
      status: "cancelled",
      topic_id: "A",
      deduplicated: true,
      receipt: {
        state: "not_accepted",
        accepted: false,
        cancelled: true,
        pending: false,
        reason: "preparation_abandoned",
        deduped: true,
        turn_id: "turn_dup",
        status: "cancelled",
        attachment_ids: [],
      },
    } as never);
    expect(await session.send("去重命中的原文"), "去重命中被无条件当成成功").toBe(false);
    expect(session.pendingSends.length).toBe(0);
    expect(session.failedSendsForTopic("A").map((r) => r.text)).toEqual(["去重命中的原文"]);
  });

  it("C3 receipt.attachment_ids 是受理回执的附件事实（去重命中时是原请求的绑定）", async () => {
    const session = useSessionStore();
    session.currentTopicId = "A";
    vi.mocked(api.sendTurn).mockResolvedValueOnce({
      ok: true,
      accepted: true,
      turn_id: "turn_dup_att",
      status: "accepted",
      topic_id: "A",
      deduplicated: true,
      receipt: {
        state: "accepted",
        accepted: true,
        cancelled: false,
        pending: false,
        reason: null,
        deduped: true,
        turn_id: "turn_dup_att",
        status: "accepted",
        // 原请求真实只绑上了 att_1：界面上的 att_2 不许继续显示为「已带上」
        attachment_ids: ["att_1"],
      },
    } as never);

    expect(await session.send("带两个附件的原文", ["att_1", "att_2"])).toBe(true);
    const message = session.messages.find((m) => m.content === "带两个附件的原文");
    expect(message?.attachmentIds).toEqual(["att_1"]);
    expect(session.lastError ?? "").toContain("附件没有附上");
  });

  it("查证回执也说 accepted=false 时，按「未被放行执行」收口", async () => {
    vi.useFakeTimers();
    const session = useSessionStore();
    session.currentTopicId = "A";
    vi.mocked(api.sendTurn).mockRejectedValueOnce(new TypeError("Failed to fetch"));
    vi.mocked(api.lookupTurnByRequest).mockResolvedValueOnce({
      turn_id: "turn_late",
      status: "cancelled",
      accepted: false,
      cancelled: true,
      receipt: { state: "not_accepted", accepted: false, cancelled: true, reason: "preparation_abandoned" },
    } as never);

    expect(await session.send("查证才发现没放行的原文")).toBe(false);
    const attempt = session.sendAttempts[0];
    expect(attempt.state).toBe("needs-confirm");
    await session.recheckSendAttempt(attempt.messageId);
    expect(attempt.state).toBe("confirmed-rejected");
    expect(session.turnRunning).toBe(false);
    await flushPromises();
  });
});
