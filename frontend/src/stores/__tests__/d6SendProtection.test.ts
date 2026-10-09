/**
 * 发送记录的三个保护反例(契约 §12.3 / §12.4 / §12.5,2026-10-09 第五轮)。
 *
 * 与既有 d5* 用例的分工:这里只写**第五轮点名要修的三件事**,每条都按
 * 「用户能观察到的结果」断言,并要求**先在基线 403983f 上跑出失败**:
 *
 * §12.3 未绑定发送的失败归属:绑定真实话题 A 的那一刻就稳定为 A;
 *   切到 B、写新文字、解除旧草稿保护,都改变不了它。
 * §12.4 成功只处理「实际被这次发送接受」的记录:不许按文字相同匹配;
 *   找回后原样重发要显式关联;互换过的记录与缺身份的老格式记录保留。
 * §12.5 失败原文不做静默淘汰:九份全部保留(展示数量与保留数量分开,
 *   隐藏 != 删除),刷新不许再裁剪一次丢掉磁盘已有记录。
 *
 * 标注:
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

/** 受控请求:返回一个「还没结束」的发送请求,测试自己决定何时成功 / 失败 */
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

/** 「刷新」:新的会话实例,同一份本机存储 */
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
describe("§12.3 未绑定发送的失败归属在绑定时就稳定", () => {
  it("【会话层】未绑定发送→绑定 A→切 B→原请求失败:失败原文归 A,B 一无变化", async () => {
    vi.useFakeTimers();
    const session = useSessionStore();
    session.draft = "未绑定就要发出去的话";
    vi.advanceTimersByTime(500);

    pendingSend();
    const attribution = session.sendAttribution();
    session.draft = "";
    const sendPromise = session.send("未绑定就要发出去的话");
    await flushPromises();

    // 绑定真实话题 A(§11.5 的迁移路径)
    session.currentTopicId = "A";
    await flushPromises();
    // 用户切到 B 继续输入
    session.currentTopicId = "B";
    session.draft = "B 里后来写的新文字";
    vi.advanceTimersByTime(500);

    // 原请求此刻才失败
    deferred[0].reject(new Error("网络中断"));
    await sendPromise;

    // 失败事实仍属于 A:B 的输入、草稿与恢复入口都不许被它碰
    expect(
      session.failedSendsForTopic("A").map((record) => record.text),
      "失败发生时用户在看的话题(B)不能变成归属",
    ).toEqual(["未绑定就要发出去的话"]);
    expect(session.failedSendsForTopic("B"), "切到 B 不许把这次失败记录搬到 B").toEqual([]);
    expect(session.failedSendsForTopic(null), "绑定之后不许再留下占位记录").toEqual([]);
    expect(session.draft).toBe("B 里后来写的新文字");
    void attribution;
  });

  it("【会话层】绑定 A 后写新文字解除旧草稿保护、再切 C:归属还是 A,切回 A 能取回", async () => {
    vi.useFakeTimers();
    const session = useSessionStore();
    session.draft = "绑定时还在等回执的话";
    vi.advanceTimersByTime(500);

    pendingSend();
    const attribution = session.sendAttribution();
    session.draft = "";
    const sendPromise = session.send("绑定时还在等回执的话");
    await flushPromises();

    session.currentTopicId = "A";
    await flushPromises();
    // 在 A 里写下新文字(覆盖同键的旧草稿,解除对原文的保护)
    session.draft = "A 里后来写的新文字";
    vi.advanceTimersByTime(500);
    // 再切到 C
    session.currentTopicId = "C";
    session.draft = "C 里在写的话";
    vi.advanceTimersByTime(500);

    deferred[0].reject(new Error("网络中断"));
    await sendPromise;

    expect(session.failedSendsForTopic("A").map((record) => record.text)).toEqual([
      "绑定时还在等回执的话",
    ]);
    expect(session.failedSendsForTopic("C")).toEqual([]);

    // 切回 A:恢复入口在那里,可以取回
    session.currentTopicId = "A";
    await flushPromises();
    const restored = session.retryFailedSend(attribution.draftId);
    expect(restored.ok, "失败原文归属对了才谈得上取回").toBe(true);
    expect(session.draft).toBe("绑定时还在等回执的话");
    // C 里写的话没有丢
    expect(storedText(chatKey("C"))).toBe("C 里在写的话");
  });
});

describe("§12.4 成功只处理被这个发送接受的记录", () => {
  it("【会话层】同话题手打相同文字发送成功:先前那条失败记录不许被按文字清掉", async () => {
    vi.useFakeTimers();
    const session = useSessionStore();
    session.currentTopicId = "A";

    vi.mocked(api.sendTurn).mockRejectedValueOnce(new Error("第一次:网络中断"));
    session.draft = "同一句话";
    vi.advanceTimersByTime(500);
    session.sendAttribution();
    session.draft = "";
    await session.send("同一句话");
    expect(session.failedSendsForTopic("A").length).toBe(1);

    // 用户**手打**同样的文字再发一次(没有经历「找回」动作),这次成功
    vi.mocked(api.sendTurn).mockResolvedValueOnce(acceptedSend("turn_2") as never);
    session.draft = "同一句话";
    vi.advanceTimersByTime(500);
    session.sendAttribution();
    session.draft = "";
    await session.send("同一句话");
    await vi.advanceTimersByTimeAsync(800);

    expect(
      session.failedSendsForTopic("A").map((record) => record.text),
      "成功清理不许按「文字相同」匹配而误删失败记录",
    ).toEqual(["同一句话"]);
  });

  it("【会话层】找回原文后原样重发成功:显式关联到这条被重发的记录,只清这一份", async () => {
    vi.useFakeTimers();
    const session = useSessionStore();
    session.currentTopicId = "A";

    vi.mocked(api.sendTurn).mockRejectedValueOnce(new Error("网络中断"));
    session.draft = "找回后要重发的话";
    vi.advanceTimersByTime(500);
    const first = session.sendAttribution();
    session.draft = "";
    await session.send("找回后要重发的话");
    // 用户点「找回」:原文回到输入框(记录保留)
    session.retryFailedSend(first.draftId);
    expect(session.draft).toBe("找回后要重发的话");

    // 原样再发一次,这次成功
    vi.mocked(api.sendTurn).mockResolvedValueOnce(acceptedSend("turn_2") as never);
    session.sendAttribution();
    session.draft = "";
    await session.send("找回后要重发的话");
    await vi.advanceTimersByTimeAsync(800);

    expect(session.failedSendsForTopic("A")).toEqual([]);
    expect(session.draft).toBe("");
  });

  it("【会话层】互换过的记录(正文已变):发送成功后不许凭旧关联被删", async () => {
    vi.useFakeTimers();
    const session = useSessionStore();
    session.currentTopicId = "A";

    vi.mocked(api.sendTurn).mockRejectedValueOnce(new Error("网络中断"));
    session.draft = "失败的那段原文";
    vi.advanceTimersByTime(500);
    const first = session.sendAttribution();
    session.draft = "";
    await session.send("失败的那段原文");
    const record = session.failedSendsForTopic("A")[0];

    // 用户先写了别的话,再与失败原文互换:记录里换成了用户的草稿
    session.draft = "用户自己的草稿";
    vi.advanceTimersByTime(500);
    session.swapFailedSendText(record.id);
    expect(session.draft).toBe("失败的那段原文");
    // swap 会用新对象替换列表里的条目:改从 store 列表取改正后的记录
    const swapped = session.failedSendsForTopic("A")[0];
    expect(swapped.text).toBe("用户自己的草稿");

    // 输入框里就是失败原文,用户把它发出去并成功
    vi.mocked(api.sendTurn).mockResolvedValueOnce(acceptedSend("turn_2") as never);
    session.sendAttribution();
    session.draft = "";
    await session.send("失败的那段原文");
    await vi.advanceTimersByTimeAsync(800);

    // 记录里还留着「用户自己的草稿」——它没有被这次成功发送顶掉
    expect(session.failedSendsForTopic("A").map((r) => r.text)).toEqual(["用户自己的草稿"]);
    void first;
  });

  it("【会话层】老格式(缺身份)的失败记录:同文字发送成功后也必须保留", async () => {
    vi.useFakeTimers();
    const session = useSessionStore();
    session.currentTopicId = "A";
    // 既有调用方直写 failedSend 的旧形状:没有 id、没有 draftId
    session.failedSend = { topicId: "A", text: "老格式的失败原文", draftSeq: 1, at: Date.now() };

    vi.mocked(api.sendTurn).mockResolvedValueOnce(acceptedSend("turn_1") as never);
    session.draft = "老格式的失败原文";
    vi.advanceTimersByTime(500);
    session.sendAttribution();
    session.draft = "";
    await session.send("老格式的失败原文");
    await vi.advanceTimersByTimeAsync(800);

    expect(session.failedSend, "成功清理不许按文字猜测去删没有身份的老记录").not.toBeNull();
  });
});

describe("§12.5 失败原文不做静默淘汰", () => {
  it("【会话层】同话题九次不同原文全部失败且未放弃:九条全部保留(不许限制在 8 条)", async () => {
    vi.useFakeTimers();
    const session = useSessionStore();
    session.currentTopicId = "A";
    for (let i = 1; i <= 9; i += 1) {
      vi.mocked(api.sendTurn).mockRejectedValueOnce(new Error("失败 " + i + ":网络中断"));
      session.draft = "失败原文 " + i;
      vi.advanceTimersByTime(500);
      session.sendAttribution();
      session.draft = "";
      await session.send("失败原文 " + i);
    }
    const kept = session.failedSendsForTopic("A").map((record) => record.text);
    expect(
      kept.length,
      "「每话题保留 8 条」的静默裁剪被废止:第九份不许把第一份挤掉",
    ).toBe(9);
    expect(kept).toContain("失败原文 1");
    expect(kept).toContain("失败原文 9");
  });

  it("【会话层】刷新之后九条失败原文全都在:不许再裁剪一次丢掉磁盘已有记录", async () => {
    vi.useFakeTimers();
    const session = useSessionStore();
    session.currentTopicId = "A";
    for (let i = 1; i <= 9; i += 1) {
      vi.mocked(api.sendTurn).mockRejectedValueOnce(new Error("失败 " + i + ":网络中断"));
      session.draft = "失败原文 " + i;
      vi.advanceTimersByTime(500);
      session.sendAttribution();
      session.draft = "";
      await session.send("失败原文 " + i);
    }
    const reloaded = reloadSession();
    reloaded.currentTopicId = "A";
    expect(
      reloaded.failedSendsForTopic("A").length,
      "刷新时重新裁剪会把磁盘已保存的第 9 条直接丢掉",
    ).toBe(9);
  });

  it("【会话层】记录只在对应发送成功或用户明确放弃后清理:找回与互换都不算清理", async () => {
    vi.useFakeTimers();
    const session = useSessionStore();
    session.currentTopicId = "A";
    vi.mocked(api.sendTurn).mockRejectedValueOnce(new Error("网络中断"));
    session.draft = "既没成功也没放弃的话";
    vi.advanceTimersByTime(500);
    const attribution = session.sendAttribution();
    session.draft = "";
    await session.send("既没成功也没放弃的话");
    const record = session.failedSendsForTopic("A")[0];

    // 找回(放回输入框):记录保留
    session.retryFailedSend(attribution.draftId);
    expect(session.failedSendsForTopic("A").length).toBe(1);
    // 用户明确放弃:这时候才清理
    session.discardFailedSend(record.id);
    expect(session.failedSendsForTopic("A")).toEqual([]);
    // 刷新后也不再出现
    expect(reloadSession().failedSendsForTopic("A")).toEqual([]);
  });
});
