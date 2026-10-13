/**
 * IRL-E 第二阶段独立复核 · 持续许可（§8.2）与状态身份矩阵（§8.3）。
 *
 * 独立角度：
 * 1) 许可：提交成功 / 失败 / 悬挂未回 / 重复提交 / 服务端快照回读 / 模式重载 六条路径，
 *    逐条断言 checked 不被自动取消；并把「服务端异常回非空 checkedCleared」这条
 *    legacy 路径单独钉住（当前服务端自述恒回空数组，见 SubmitCluster.vue:333）；
 * 2) 状态身份：对 6 种理解状态 × 有/无说明做**穷举矩阵**，断言「理解已结束（含本次无需任务）」
 *    时说明不再属于当前阶段、文案里不出现「正在理解」。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { flushPromises } from "@vue/test-utils";
import { useInteractiveStore } from "../interactive";
import { updateCard, emptyState } from "../../interactive/board";
import { understandingStatusLabel } from "../../interactive/approval";
import type { BoardCard as BoardCardModel, UnderstandingStatus } from "../../interactive/types";

vi.mock("../../services/interactive", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../services/interactive")>();
  return {
    ...actual,
    fetchBoardState: vi.fn(),
    saveBoardState: vi.fn(),
    saveDrafts: vi.fn(),
    fetchVisibleRange: vi.fn(),
    previewMaterialImpact: vi.fn(),
    checkMaterialImpact: vi.fn(),
    fetchIntents: vi.fn(),
    fetchUnderstanding: vi.fn(),
    fetchExecutions: vi.fn(),
    createIntents: vi.fn(),
    submitBoard: vi.fn(),
    fetchAcceptance: vi.fn(),
  };
});

const NOW = "2026-10-13T00:00:00.000Z";

function mkCard(id: string, patch: Partial<BoardCardModel> = {}): BoardCardModel {
  return {
    id,
    kind: "text",
    content: "注释 " + id,
    meta: {},
    x: 40,
    y: 40,
    w: 240,
    h: 96,
    checked: false,
    hidden: false,
    folded: false,
    bookmarked: false,
    deleted: false,
    createdAt: NOW,
    updatedAt: NOW,
    ...patch,
  };
}

const emptySnapshot = () => ({ cards: [], groups: [], links: [], selection: [], empty: true });

function submitResult(over: Record<string, unknown> = {}) {
  return {
    status: "succeeded",
    submission: { id: "s1", seq: 4, status: "succeeded", createdAt: NOW },
    before: emptySnapshot(),
    after: emptySnapshot(),
    expressions: [],
    baseline: { updated: true, previousSeq: 3, seq: 4 },
    delivery: { delivered: true, reason: "", detail: "", understandingId: "u1", status: "accepted" },
    visibleRange: emptySnapshot(),
    checkedCleared: [],
    ...over,
  };
}

function serverPayload(cards: BoardCardModel[]) {
  return {
    board: { id: "board_t", title: "" },
    state: { ...emptyState("board_t"), cards, seq: 4 },
    seq: 4,
    baseline: null,
    submissions: [],
    drafts: { drafts: {}, updatedAt: NOW },
    pending: [],
  };
}

async function seedStore(cards: BoardCardModel[]) {
  const api = await import("../../services/interactive");
  vi.mocked(api.fetchBoardState).mockResolvedValue(serverPayload(cards) as never);
  vi.mocked(api.saveBoardState).mockImplementation(
    async (_boardId: string, putState: unknown) => ({ ok: true, seq: 4, savedAt: NOW, state: putState }) as never,
  );
  vi.mocked(api.saveDrafts).mockResolvedValue({ drafts: {}, updatedAt: NOW } as never);
  vi.mocked(api.fetchVisibleRange).mockResolvedValue({ visibleRange: emptySnapshot() } as never);
  vi.mocked(api.previewMaterialImpact).mockResolvedValue({ affected: [] } as never);
  vi.mocked(api.checkMaterialImpact).mockResolvedValue({
    ok: true,
    checkId: "chk",
    stateVersion: 3,
    affected: [],
    impactConfirmationRequired: false,
  } as never);
  vi.mocked(api.fetchIntents).mockResolvedValue({ intents: [], conflicts: [], batchAvailable: false, recovery: { paused: [] } } as never);
  vi.mocked(api.fetchUnderstanding).mockResolvedValue({ understanding: null, understandings: [] } as never);
  vi.mocked(api.fetchExecutions).mockResolvedValue({ executions: [] } as never);
  vi.mocked(api.createIntents).mockResolvedValue({ created: [], demo: false } as never);
  vi.mocked(api.fetchAcceptance).mockResolvedValue({ found: false } as never);
  const store = useInteractiveStore();
  await store.load();
  return store;
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((res) => {
    resolve = res;
  });
  return { promise, resolve };
}

beforeEach(() => {
  localStorage.clear();
  setActivePinia(createPinia());
  vi.clearAllMocks();
});

describe("持续许可：六条路径都不自动取消（§8.2）", () => {
  it("提交成功（checkedCleared 为空）→ 许可保留，服务端快照回读也保留", async () => {
    const api = await import("../../services/interactive");
    const cards = [mkCard("t1", { checked: true })];
    const store = await seedStore(cards);
    expect(store.board!.cards[0].checked).toBe(true);

    vi.mocked(api.submitBoard).mockResolvedValue(submitResult() as never);
    await store.submit();
    await flushPromises();
    expect(store.submitStatus).toBe("succeeded");
    expect(store.board!.cards[0].checked).toBe(true);

    await store.refreshBoardFromServer();
    expect(store.board!.cards[0].checked).toBe(true);
  });

  it("提交失败（后端拒绝）→ 许可保留", async () => {
    const api = await import("../../services/interactive");
    const store = await seedStore([mkCard("t1", { checked: true })]);
    vi.mocked(api.submitBoard).mockRejectedValue(new Error("网络中断") as never);
    await store.submit();
    await flushPromises();
    expect(store.submitStatus).not.toBe("succeeded");
    expect(store.board!.cards[0].checked).toBe(true);
  });

  it("受理结果未知（悬挂未回）→ 许可保留", async () => {
    const api = await import("../../services/interactive");
    const store = await seedStore([mkCard("t1", { checked: true })]);
    const inflight = deferred<never>();
    vi.mocked(api.submitBoard).mockReturnValue(inflight.promise as never);
    const pending = store.submit();
    await flushPromises();
    expect(store.board!.cards[0].checked).toBe(true);
    inflight.resolve(submitResult() as never);
    await pending;
    await flushPromises();
    expect(store.board!.cards[0].checked).toBe(true);
  });

  it("重复提交（连提两次）→ 许可保留", async () => {
    const api = await import("../../services/interactive");
    const store = await seedStore([mkCard("t1", { checked: true })]);
    vi.mocked(api.submitBoard).mockResolvedValue(submitResult() as never);
    await store.submit();
    await flushPromises();
    await store.submit();
    await flushPromises();
    expect(store.board!.cards[0].checked).toBe(true);
    expect(vi.mocked(api.submitBoard).mock.calls.length).toBeGreaterThanOrEqual(2);
  });

  it("模式重载（重新 load 同一份服务端快照）→ 许可保留", async () => {
    const store = await seedStore([mkCard("t1", { checked: true })]);
    await store.load();
    await flushPromises();
    expect(store.board!.cards[0].checked).toBe(true);
  });

  it("用户手动取消 / 隐藏卡片才失效（契约允许的两条路径）", async () => {
    const store = await seedStore([mkCard("t1", { checked: true }), mkCard("t2", { checked: true })]);
    store.commit(updateCard(store.board!, "t1", { checked: false }), "取消勾选");
    store.commit(updateCard(store.board!, "t2", { hidden: true }), "隐藏卡片");
    const byId = (id: string) => store.board!.cards.find((card) => card.id === id)!;
    expect(byId("t1").checked).toBe(false);
    expect(byId("t2").hidden).toBe(true);
    expect(byId("t2").checked).toBe(false);
  });

  it("legacy 路径：非空 checkedCleared 与随后回读的服务端事实之间，最终以服务端事实为准", async () => {
    const api = await import("../../services/interactive");
    const store = await seedStore([mkCard("t1", { checked: true })]);
    vi.mocked(api.submitBoard).mockResolvedValue(submitResult({ checkedCleared: ["t1"] }) as never);
    await store.submit();
    await flushPromises();
    expect(vi.mocked(api.submitBoard)).toHaveBeenCalled();
    // 本用例的服务端快照仍然是 checked=true：回读后许可必须还是 true。
    // 也就是说「许可被清」只可能来自服务端自己回报的事实，不是前端自作主张。
    await store.refreshBoardFromServer();
    expect(store.board!.cards[0].checked).toBe(true);

    // 服务端事实真的说「这张卡已不再许可」时，前端跟随（这不是自动取消，是服务端事实）
    vi.mocked(api.fetchBoardState).mockResolvedValue(serverPayload([mkCard("t1", { checked: false })]) as never);
    await store.refreshBoardFromServer();
    expect(store.board!.cards[0].checked).toBe(false);
  });
});

describe("状态身份矩阵：6 种理解状态 × 有/无说明（§8.3）", () => {
  const STATUSES: UnderstandingStatus[] = ["accepted", "understanding", "understood", "needs_clarification", "no_action", "failed"];
  const SETTLED: UnderstandingStatus[] = ["understood", "needs_clarification", "no_action", "failed"];

  it("穷举：理解已结束时说明不再属于当前阶段，且文案里不出现「正在理解」", async () => {
    const store = await seedStore([]);
    const bad: string[] = [];
    for (const status of STATUSES) {
      store.understandingStatus = status;
      store.submitNote = "受理时的说明";
      store.understandingNote = "理解查询的说明";
      const settled = SETTLED.includes(status);
      if (store.submitNoteOwnsCurrentStage !== !settled) bad.push(status + " submitNote 归属错了");
      if (store.understandingNoteOwnsCurrentStage !== !settled) bad.push(status + " understandingNote 归属错了");
      const label = understandingStatusLabel(status);
      if (settled && label === "正在理解") bad.push(status + " 已结束却显示正在理解");
      if (status === "no_action" && label !== "本次无需开展任务") bad.push("no_action 文案不对：" + label);
      if (status === "understanding" && label !== "正在理解") bad.push("understanding 文案不对：" + label);
    }
    expect(bad).toEqual([]);
  });

  it("穷举：没有说明时归属判断不受影响；null 状态给出统一文案", async () => {
    const store = await seedStore([]);
    const bad: string[] = [];
    for (const status of STATUSES) {
      store.understandingStatus = status;
      store.submitNote = null;
      store.understandingNote = null;
      const settled = SETTLED.includes(status);
      if (store.submitNoteOwnsCurrentStage !== !settled) bad.push(status + " 无说明时归属错了");
    }
    store.understandingStatus = null;
    if (understandingStatusLabel(null) !== "还没有提交给 QIO") bad.push("null 文案不对");
    expect(bad).toEqual([]);
  });

  it("「本次无需开展任务」不会与「正在理解」并列：settled 后两条说明都不属于当前阶段", async () => {
    const store = await seedStore([]);
    store.understandingStatus = "understanding";
    store.submitNote = "已受理，正在理解";
    store.understandingNote = "正在读取材料";
    expect(store.submitNoteOwnsCurrentStage).toBe(true);

    store.understandingStatus = "no_action";
    expect(understandingStatusLabel(store.understandingStatus)).toBe("本次无需开展任务");
    expect(store.submitNoteOwnsCurrentStage).toBe(false);
    expect(store.understandingNoteOwnsCurrentStage).toBe(false);
  });
});
