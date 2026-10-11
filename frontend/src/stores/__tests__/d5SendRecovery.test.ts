/**
 * 失败原文的找回与未绑定话题迁移（契约 §11.4 / §11.5）。
 *
 * 与既有独立用例（d4ChatDraftRace）的分工：这里只写**本轮新增/修复的行为**，
 * 每条都按「用户能观察到的结果」断言：
 * 1. 失败原文是持久事实：刷新/关闭重开后仍在，且与用户后来输入的新文字并存；
 * 2. 连续两次失败不静默丢掉前一份原文（不再是单个内存槽）；
 * 3. 未绑定话题时发送 → 服务器绑定真实话题 → 成功/失败都能正确归属与清理；
 * 4. 多个回执先后到达不互相清错（晚到的成功不许删新版本，失败记录不许被误清）；
 * 5. 找回原文**不是发送**：不自动重试、不自动重发。
 *
 * 标注：
 *   【会话层】只依赖 stores/session.ts 的公开状态与动作
 *   【模拟失败】用受控的 api.sendTurn 拒绝制造发送失败
 *   【模拟刷新】新的 pinia 实例 + 同一份 localStorage
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { flushPromises } from "@vue/test-utils";
import { useSessionStore } from "../session";
import { api } from "../../services/api";
import * as imApi from "../../services/interactive";
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

/** 「刷新」：新的会话实例，同一份本机存储 */
function reloadSession() {
  setActivePinia(createPinia());
  return useSessionStore();
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

describe("§11.4 失败原文是可持久的事实（不是内存槽）", () => {
  it("【会话层】【模拟刷新】发送中继续输入→失败：输入框是新文字，恢复入口里是失败原文，两份都还在", async () => {
    vi.useFakeTimers();
    const session = useSessionStore();
    session.currentTopicId = "A";
    session.draft = "第一条：会失败的话";
    vi.advanceTimersByTime(500);
    expect(storedText(chatKey("A"))).toBe("第一条：会失败的话");

    pendingSend();
    const attribution = session.sendAttribution();
    session.draft = ""; // 组件行为：发送前清空
    const sendPromise = session.send("第一条：会失败的话");
    await flushPromises();

    // 请求还没回来：用户又写了新内容（它会被正常保存）
    session.draft = "第二条：后来写的新内容";
    vi.advanceTimersByTime(500);
    expect(storedText(chatKey("A"))).toBe("第二条：后来写的新内容");

    deferred[0].reject(new Error("网络中断"));
    await sendPromise;
    session.retryFailedSend(attribution.draftId);
    await vi.advanceTimersByTimeAsync(600);
    expect(session.draft).toBe("第二条：后来写的新内容");

    // 「刷新」：新的会话实例，同一份本机存储
    const callsBeforeReload = vi.mocked(api.sendTurn).mock.calls.length;
    const reloaded = reloadSession();
    expect(reloaded.currentTopicId).toBeNull();
    reloaded.currentTopicId = "A";
    await flushPromises();

    // 输入框里是用户后来写的新文字；失败原文在恢复入口里，两份都还在
    expect(reloaded.draft).toBe("第二条：后来写的新内容");
    expect(reloaded.failedSendsForTopic("A").map((record) => record.text)).toEqual([
      "第一条：会失败的话",
    ]);
    expect(reloaded.failedSendError).toContain("网络中断");
    // 刷新之后也不自动重发（恢复只把原文放回可编辑状态）
    expect(vi.mocked(api.sendTurn).mock.calls.length).toBe(callsBeforeReload);
  });

  it("【会话层】连续两次失败：前一份原文不被顶掉，两份都能分别找回", async () => {
    vi.useFakeTimers();
    const session = useSessionStore();
    session.currentTopicId = "A";

    // 第一次失败：输入框空 → 原文自动回到输入框（记录保留）
    session.draft = "第一次失败的话";
    vi.advanceTimersByTime(500);
    vi.mocked(api.sendTurn).mockRejectedValueOnce(new Error("第一次：网络中断"));
    const firstAttribution = session.sendAttribution();
    session.draft = "";
    await session.send("第一次失败的话");
    session.retryFailedSend(firstAttribution.draftId);
    expect(session.draft).toBe("第一次失败的话");

    // 用户改写后再发一次，同样失败
    session.draft = "第二次失败的话";
    vi.advanceTimersByTime(500);
    vi.mocked(api.sendTurn).mockRejectedValueOnce(new Error("第二次：网关超时"));
    const secondAttribution = session.sendAttribution();
    session.draft = "";
    await session.send("第二次失败的话");
    session.retryFailedSend(secondAttribution.draftId);
    expect(session.draft).toBe("第二次失败的话");

    const records = session.failedSendsForTopic("A");
    expect(records.map((record) => record.text)).toEqual(["第二次失败的话", "第一次失败的话"]);
    // 两条各自带自己的真实原因，不串用
    expect(session.failedSendErrors[records[0].id!]).toBe("第二次：网关超时");
    expect(session.failedSendErrors[records[1].id!]).toBe("第一次：网络中断");

    // 先清空输入框，再取回**前一份**：它还在，而且真的能放回输入框
    session.draft = "";
    const older = session.retryFailedSend(records[1].id);
    expect(older.ok).toBe(true);
    expect(older.restored).toBe(true);
    expect(session.draft).toBe("第一次失败的话");
    expect(session.failedSendsForTopic("A").map((record) => record.text)).toEqual([
      "第二次失败的话",
      "第一次失败的话",
    ]);

    // 刷新后仍然两条都在（持久化按话题保存）
    const reloaded = reloadSession();
    expect(reloaded.failedSendsForTopic("A").map((record) => record.text)).toEqual([
      "第二次失败的话",
      "第一次失败的话",
    ]);
    expect(reloaded.failedSendErrors[records[0].id!]).toBe("第二次：网关超时");
  });

  it("【会话层】找回原文不是发送：点恢复只改输入框，绝不自动重试", async () => {
    vi.useFakeTimers();
    const session = useSessionStore();
    session.currentTopicId = "A";
    vi.mocked(api.sendTurn).mockRejectedValueOnce(new Error("网络中断"));
    session.draft = "失败后要找回的话";
    const attribution = session.sendAttribution();
    session.draft = "";
    await session.send("失败后要找回的话");
    const callsAfterFail = vi.mocked(api.sendTurn).mock.calls.length;

    // 用户先写下别的内容，再决定找回
    session.draft = "我先写点别的";
    const blocked = session.retryFailedSend(attribution.draftId);
    expect(blocked.ok).toBe(false);
    expect(blocked.reason).toContain("已有更新文字");
    expect(session.draft).toBe("我先写点别的");

    session.draft = "";
    const restored = session.retryFailedSend(attribution.draftId);
    expect(restored.restored).toBe(true);
    expect(session.draft).toBe("失败后要找回的话");
    await vi.advanceTimersByTimeAsync(3000);
    expect(vi.mocked(api.sendTurn).mock.calls.length).toBe(callsAfterFail);
  });

  it("【会话层】「不再保留」只清这一条失败事实：不删草稿、不清输入框、不发送", async () => {
    vi.useFakeTimers();
    const session = useSessionStore();
    session.currentTopicId = "A";
    vi.mocked(api.sendTurn).mockRejectedValueOnce(new Error("网络中断"));
    session.draft = "失败原文";
    session.sendAttribution();
    session.draft = "";
    await session.send("失败原文");
    const record = session.failedSendsForTopic("A")[0];
    expect(record).toBeTruthy();

    session.draft = "正在写的新内容";
    vi.advanceTimersByTime(500);
    session.discardFailedSend(record.id);
    expect(session.failedSendsForTopic("A")).toEqual([]);
    expect(session.draft).toBe("正在写的新内容");
    expect(storedText(chatKey("A"))).toBe("正在写的新内容");
    expect(vi.mocked(api.sendTurn).mock.calls.length).toBe(1);

    // 刷新后这条失败事实也不再出现（用户已经明确放弃）
    const reloaded = reloadSession();
    expect(reloaded.failedSendsForTopic("A")).toEqual([]);
  });
});

describe("§11.4 A→B 失败隔离与回到原话题取回", () => {
  it("【会话层】A 的失败不插入 B：B 的输入、草稿与恢复入口都不受影响", async () => {
    vi.useFakeTimers();
    const session = useSessionStore();
    session.currentTopicId = "A";
    session.draft = "A 的话";
    vi.advanceTimersByTime(500);

    pendingSend();
    const attribution = session.sendAttribution();
    session.draft = "";
    const sendPromise = session.send("A 的话");
    await flushPromises();

    // 请求在飞：切到 B 并写下新内容
    session.currentTopicId = "B";
    await flushPromises();
    session.draft = "B 正在写的话";
    vi.advanceTimersByTime(500);

    deferred[0].reject(new Error("网络中断"));
    await sendPromise;
    session.retryFailedSend(attribution.draftId);
    await vi.advanceTimersByTimeAsync(600);

    // B：一个字都不许变，也没有属于它的恢复入口
    expect(session.draft).toBe("B 正在写的话");
    expect(storedText(chatKey("B"))).toBe("B 正在写的话");
    expect(session.failedSendsForTopic("B")).toEqual([]);

    // 回到 A：失败原文在那里等着，可以取回
    expect(session.failedSendsForTopic("A").map((record) => record.text)).toEqual(["A 的话"]);
    session.currentTopicId = "A";
    await flushPromises();
    expect(session.draft).toBe("A 的话");
    const restored = session.retryFailedSend();
    expect(restored.ok).toBe(true);
    expect(session.draft).toBe("A 的话");
  });
});

describe("§11.5 未绑定话题迁移", () => {
  it("【会话层】未绑定→绑定→成功：已发出的旧文字不再回到输入框，旧键也被清掉", async () => {
    vi.useFakeTimers();
    const session = useSessionStore();
    expect(session.currentTopicId).toBeNull();
    session.draft = "话题还没确定时写的话";
    vi.advanceTimersByTime(500);
    expect(storedText(chatKey(null))).toBe("话题还没确定时写的话");

    pendingSend();
    const attribution = session.sendAttribution();
    session.draft = "";
    const sendPromise = session.send("话题还没确定时写的话");
    await flushPromises();

    // 服务器确定真实话题：草稿记录迁移到 T
    session.currentTopicId = "T";
    await flushPromises();
    // 等待回执期间输入框被草稿保护带回原文（迁移把同一份版本搬到了新键）
    expect(session.draft).toBe("话题还没确定时写的话");

    deferred[0].resolve(acceptedSend("turn_1"));
    await sendPromise;
    await vi.advanceTimersByTimeAsync(800);

    // 已经成功发出：旧文字不许再留在输入框，也不许留在存储里
    expect(session.draft).toBe("");
    expect(readDraft(chatKey("T"))).toBeNull();
    expect(readDraft(chatKey(null))).toBeNull();
    expect(vi.mocked(api.sendTurn).mock.calls[0][1], "请求要用点击那一刻的话题").toBeNull();
  });

  it("【会话层】未绑定→绑定→用户已输入新文字：受理成功不误删新草稿", async () => {
    vi.useFakeTimers();
    const session = useSessionStore();
    session.draft = "未绑定时发出的原文";
    vi.advanceTimersByTime(500);

    pendingSend();
    const attribution = session.sendAttribution();
    session.draft = "";
    const sendPromise = session.send("未绑定时发出的原文");
    await flushPromises();

    session.currentTopicId = "T";
    await flushPromises();
    // 迁移之后用户马上又写了新内容（还没到防抖时间）
    session.draft = "绑定后写的新内容";

    deferred[0].resolve(acceptedSend("turn_1"));
    await sendPromise;
    await vi.advanceTimersByTimeAsync(800);

    expect(session.draft).toBe("绑定后写的新内容");
    expect(storedText(chatKey("T"))).toBe("绑定后写的新内容");
    expect(storedText(chatKey(null))).toBeNull();
    void attribution;
  });

  it("【会话层】迁移时失败：失败原文归到真实话题，入口在 T 里看得到、原文不丢", async () => {
    vi.useFakeTimers();
    const session = useSessionStore();
    session.draft = "迁移过程中失败的话";
    vi.advanceTimersByTime(500);

    pendingSend();
    const attribution = session.sendAttribution();
    session.draft = "";
    const sendPromise = session.send("迁移过程中失败的话");
    await flushPromises();

    session.currentTopicId = "T";
    await flushPromises();

    deferred[0].reject(new Error("网络中断"));
    await sendPromise;
    session.retryFailedSend(attribution.draftId);
    await vi.advanceTimersByTimeAsync(600);

    // 失败事实属于真实话题 T（不是 null 的占位位置）
    expect(session.failedSendsForTopic("T").map((record) => record.text)).toEqual([
      "迁移过程中失败的话",
    ]);
    expect(session.failedSendsForTopic(null)).toEqual([]);
    // 原文就在这个输入框里（迁移把它带过来、失败后也没有被丢掉）
    expect(session.draft).toBe("迁移过程中失败的话");

    // 刷新后入口仍在这个话题里
    const reloaded = reloadSession();
    reloaded.currentTopicId = "T";
    await flushPromises();
    expect(reloaded.failedSendsForTopic("T").map((record) => record.text)).toEqual([
      "迁移过程中失败的话",
    ]);
  });

  it("【会话层】迁移后未绑定位置残留的记录会跟着进入真实话题（不丢在占位话题里）", async () => {
    vi.useFakeTimers();
    const session = useSessionStore();
    // 未绑定话题时先失败一条
    vi.mocked(api.sendTurn).mockRejectedValueOnce(new Error("网络中断"));
    session.draft = "占位话题里的失败原文";
    const attribution = session.sendAttribution();
    session.draft = "";
    await session.send("占位话题里的失败原文");
    expect(session.failedSendsForTopic(null).map((record) => record.text)).toEqual([
      "占位话题里的失败原文",
    ]);

    // 服务器随后确定了真实话题
    session.currentTopicId = "T";
    await flushPromises();
    expect(session.failedSendsForTopic(null)).toEqual([]);
    expect(session.failedSendsForTopic("T").map((record) => record.text)).toEqual([
      "占位话题里的失败原文",
    ]);
    expect(session.retryFailedSend(attribution.draftId).ok).toBe(true);
  });
});

describe("§11.4 多个回执先后到达不互相清错", () => {
  it("【会话层】后一条失败、前一条迟到成功：成功不许删掉新草稿与失败原文", async () => {
    vi.useFakeTimers();
    const session = useSessionStore();
    session.currentTopicId = "A";
    session.draft = "第一条：旧的话";
    vi.advanceTimersByTime(500);

    pendingSend();
    const firstAttribution = session.sendAttribution();
    session.draft = "";
    const firstSend = session.send("第一条：旧的话");
    await flushPromises();

    // 用户在等待期间写下并保存了第二条，然后发出第二条
    session.draft = "第二条：新的话";
    vi.advanceTimersByTime(500);
    pendingSend();
    const secondAttribution = session.sendAttribution();
    session.draft = "";
    const secondSend = session.send("第二条：新的话");
    await flushPromises();

    // 第二条先失败：原文回到输入框（并写进本机）
    deferred[1].reject(new Error("网络中断"));
    await secondSend;
    session.retryFailedSend(secondAttribution.draftId);
    await vi.advanceTimersByTimeAsync(400);
    expect(session.draft).toBe("第二条：新的话");

    // 第一条的成功回执此刻才到
    deferred[0].resolve(acceptedSend("turn_1"));
    await firstSend;
    await vi.advanceTimersByTimeAsync(800);

    // 新版本一个字都没被删，失败原文也还在
    expect(session.draft).toBe("第二条：新的话");
    expect(storedText(chatKey("A"))).toBe("第二条：新的话");
    expect(session.failedSendsForTopic("A").map((record) => record.text)).toEqual([
      "第二条：新的话",
    ]);
    void firstAttribution;
  });

  it("【会话层】同一条失败原文被原样重新发出并成功：只清这一条，输入框随之清空", async () => {
    vi.useFakeTimers();
    const session = useSessionStore();
    session.currentTopicId = "A";
    vi.mocked(api.sendTurn).mockRejectedValueOnce(new Error("网络中断"));
    session.draft = "重发成功的话";
    const firstAttribution = session.sendAttribution();
    session.draft = "";
    await session.send("重发成功的话");
    session.retryFailedSend(firstAttribution.draftId);
    expect(session.failedSendsForTopic("A").length).toBe(1);

    // 用户原样再发一次：这次受理成功
    vi.mocked(api.sendTurn).mockResolvedValueOnce(acceptedSend("turn_2") as never);
    session.sendAttribution();
    session.draft = "";
    const ok = await session.send("重发成功的话");
    await vi.advanceTimersByTimeAsync(800);

    expect(ok).toBe(true);
    expect(session.failedSendsForTopic("A")).toEqual([]);
    expect(session.draft).toBe("");
    expect(readDraft(chatKey("A"))).toBeNull();
  });

  it("【会话层】A→B→A→B：A 的成功回执不许删 B 的草稿，也不许清 B 的失败原文", async () => {
    vi.useFakeTimers();
    const session = useSessionStore();
    session.currentTopicId = "A";
    session.draft = "A 的第一条";
    vi.advanceTimersByTime(500);

    pendingSend();
    const attribution = session.sendAttribution();
    session.draft = "";
    const sendPromise = session.send("A 的第一条");
    await flushPromises();

    // 切到 B 写下并保存，然后回 A 写第二条，再去 B
    session.currentTopicId = "B";
    await flushPromises();
    session.draft = "B 的草稿";
    vi.advanceTimersByTime(500);
    session.currentTopicId = "A";
    await flushPromises();
    session.draft = "A 的第二条";
    vi.advanceTimersByTime(500);
    session.currentTopicId = "B";
    await flushPromises();

    deferred[0].resolve(acceptedSend("turn_1"));
    await sendPromise;
    session.retryFailedSend(attribution.draftId);
    await vi.advanceTimersByTimeAsync(800);

    expect(storedText(chatKey("B"))).toBe("B 的草稿");
    expect(storedText(chatKey("A"))).toBe("A 的第二条");
    expect(session.draft).toBe("B 的草稿");
  });
});

describe("§11.8 失败原文的持久化失败要如实说", () => {
  it("【会话层】本机存储写不进时：内存里保留原文，并给出真实原因（不显示成已保存）", async () => {
    vi.useFakeTimers();
    const session = useSessionStore();
    session.currentTopicId = "A";
    session.draft = "写不进本机的失败原文";
    vi.advanceTimersByTime(500);

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
      vi.mocked(api.sendTurn).mockRejectedValueOnce(new Error("网络中断"));
      const attribution = session.sendAttribution();
      session.draft = "";
      await session.send("写不进本机的失败原文");
      session.retryFailedSend(attribution.draftId);

      expect(session.failedSendsForTopic("A").map((record) => record.text)).toEqual([
        "写不进本机的失败原文",
      ]);
      expect(session.failedSendPersistError).toContain("本地存储被禁用");
    } finally {
      if (original) Object.defineProperty(globalThis, "localStorage", original);
    }
  });
});
