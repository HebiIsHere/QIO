/**
 * PM-QIO032 · i-state 数据契约（#03 / #07 / #13 / #20 / #26 / #44-46）
 *
 * 这些用例锁死的是 store 层承诺给界面分支的行为：
 * - #20 整板读取失败：首次失败给重试事实；有上次数据时只读，编辑 / 保存 / 提交在 store 层被拒绝；
 * - #13 空提交：空白卡不算有效内容（按钮门禁与 submit 同一口径，不发请求）；
 * - #03 提交区主状态：轮次状态单调不倒退（迟到理解事实不把已回复降级成处理中）；
 * - #07 执行记录读取失败独立记录（首次失败 / 上次结果 / 重试成功）；
 * - #26 待确认任务事件按 intent_id 幂等应用（重复事件不生成重复卡）；
 * - #44-46 批次导入：5 份上限、未接收明细、终态不被覆盖、只重试对应文件、迟到回执不错挂卡片。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { flushPromises } from "@vue/test-utils";
import { useInteractiveStore } from "../interactive";
import * as api from "../../services/interactive";
import { emptyState, addCard } from "../../interactive/board";
import { EVENT_TYPES } from "../../services/events";
import type { BoardState, SubmissionResult, Understanding } from "../../interactive/types";

vi.mock("../../services/api", () => ({
  api: {
    sendTurn: vi.fn(),
    getSessionContext: vi.fn(async () => ({ topic_id: null })),
    stopTurn: vi.fn(),
    saveDrafts: vi.fn(async () => ({})),
  },
}));

vi.mock("../../services/interactive", () => ({
  fetchBoardState: vi.fn(),
  saveBoardState: vi.fn(),
  saveDrafts: vi.fn(),
  fetchVisibleRange: vi.fn(),
  previewMaterialImpact: vi.fn(),
  checkMaterialImpact: vi.fn(),
  submitBoard: vi.fn(),
  fetchAcceptance: vi.fn(),
  fetchUnderstanding: vi.fn(),
  retryUnderstanding: vi.fn(),
  fetchExecutions: vi.fn(),
  fetchIntentViews: vi.fn(),
  createIntents: vi.fn(),
  fetchIntents: vi.fn(),
}));

const NOW = new Date().toISOString();
const BOARD_ID = "board_default";

function boardState(over: Partial<BoardState> = {}): BoardState {
  return { boardId: BOARD_ID, seq: 3, updatedAt: NOW, cards: [], groups: [], links: [], selection: [], ...over };
}

function boardPayload(state: BoardState = boardState()) {
  return {
    board: { id: BOARD_ID, title: "默认板面" },
    state,
    seq: state.seq ?? 3,
    baseline: null,
    submissions: [],
    drafts: { drafts: {}, updatedAt: null },
  };
}

function emptySnapshot() {
  return { cards: [], groups: [], links: [], selection: [], empty: true };
}

function submitResult(over: Partial<SubmissionResult> = {}): SubmissionResult {
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
  } as SubmissionResult;
}

function understanding(over: Partial<Understanding> = {}): Understanding {
  return {
    id: "u1",
    boardId: BOARD_ID,
    submissionId: "s1",
    requestId: "req-1",
    status: "accepted",
    stage: "",
    kind: null,
    steps: [],
    materials: [],
    summary: "",
    clarification: null,
    validation: { ok: true, reasons: [] },
    error: null,
    attempt: 1,
    adapter: "fake",
    intentIds: [],
    memory: [],
    createdAt: NOW,
    updatedAt: NOW,
    ...over,
  };
}

function wireHappyPath(state: BoardState = boardState()) {
  vi.mocked(api.fetchBoardState).mockResolvedValue(boardPayload(state) as never);
  vi.mocked(api.saveBoardState).mockImplementation((async (_id: string, candidate: BoardState) => ({
    ok: true,
    seq: 9,
    savedAt: NOW,
    state: candidate,
  })) as never);
  vi.mocked(api.saveDrafts).mockResolvedValue({ drafts: {}, updatedAt: NOW } as never);
  vi.mocked(api.fetchVisibleRange).mockResolvedValue({ visibleRange: emptySnapshot() } as never);
  vi.mocked(api.fetchIntents).mockResolvedValue({ intents: [], conflicts: [], batchAvailable: false, recovery: { paused: [] } } as never);
  vi.mocked(api.fetchIntentViews).mockResolvedValue({ intents: [], source: "service" } as never);
  vi.mocked(api.fetchExecutions).mockResolvedValue({ executions: [] } as never);
  vi.mocked(api.fetchUnderstanding).mockResolvedValue({ understandings: [] } as never);
}

beforeEach(() => {
  localStorage.clear();
  vi.resetAllMocks();
  setActivePinia(createPinia());
  wireHappyPath();
});

afterEach(() => {
  vi.useRealTimers();
  localStorage.clear();
});

describe("#20 整板读取失败的只读门禁", () => {
  it("首次失败：boardLoadedOnce=false、boardReadOnly=false，编辑与提交在 store 层被拒绝；重试成功后恢复", async () => {
    vi.mocked(api.fetchBoardState).mockRejectedValueOnce(new Error("网络断了") as never);
    const store = useInteractiveStore();
    await store.load();
    expect(store.loadError).toContain("网络断了");
    expect(store.boardLoadedOnce).toBe(false);
    expect(store.boardReadOnly).toBe(false);
    expect(store.boardEditBlockedReason).toContain("重试读取或返回对话");
    // 编辑被拒绝：board 引用不变
    const before = store.board;
    const next = addCard(emptyState(BOARD_ID), { kind: "text", content: "失败期间写的字" });
    store.commit(next as BoardState, "添加");
    expect(store.board).toBe(before);
    // 提交被拒绝（不触碰网络）
    const result = await store.submit();
    expect(result).toBeNull();
    expect(store.submitStatus).toBe("failed");
    expect(store.submitError).toContain("板面读取失败");
    expect(api.submitBoard).not.toHaveBeenCalled();
    // 重试成功：回到可编辑
    vi.mocked(api.fetchBoardState).mockResolvedValue(boardPayload() as never);
    expect(await store.retryBoardLoad()).toBe(true);
    expect(store.loadError).toBeNull();
    expect(store.boardLoadedOnce).toBe(true);
    store.commit(addCard(emptyState(BOARD_ID), { kind: "text", content: "恢复后" }) as BoardState, "添加");
    expect(store.board?.cards.length).toBeGreaterThan(0);
  });

  it("已有上次成功数据时读取失败：boardReadOnly=true，保存与提交暂停但旧数据保留", async () => {
    const store = useInteractiveStore();
    await store.load();
    expect(store.boardLoadedOnce).toBe(true);
    vi.mocked(api.fetchBoardState).mockRejectedValueOnce(new Error("读取超时") as never);
    expect(await store.retryBoardLoad()).toBe(false);
    expect(store.boardReadOnly).toBe(true);
    expect(store.boardEditBlockedReason).toContain("只能查看");
    expect(store.board).not.toBeNull();
    const snapshot = store.board;
    store.commit(snapshot as BoardState, "无意义编辑");
    expect(store.board).toBe(snapshot);
    await store.saveNow();
    expect(api.saveBoardState).not.toHaveBeenCalled();
  });
});

describe("#13 空提交判定与后端同口径", () => {
  it("只有空白文字卡：canSubmit=false、submit 不发请求并给出下一步", async () => {
    const store = useInteractiveStore();
    await store.load();
    const blank = addCard(emptyState(BOARD_ID), { kind: "text", content: "   ", checked: true });
    store.commit(blank as BoardState, "添加空白卡");
    expect(store.canSubmit).toBe(false);
    expect(store.submitBlockedReason).toContain("空白卡片");
    await store.submit();
    expect(api.submitBoard).not.toHaveBeenCalled();
    expect(store.submitStatus).toBe("empty");
    expect(store.submitError).toContain("空白卡片");
  });

  it("有正文的文字卡算有效内容", async () => {
    const store = useInteractiveStore();
    await store.load();
    const filled = addCard(emptyState(BOARD_ID), { kind: "text", content: "真正的内容", checked: true });
    store.commit(filled as BoardState, "添加文字卡");
    expect(store.canSubmit).toBe(true);
    expect(store.submitBlockedReason).toBeNull();
  });

  it("独立关系算有效：两张空白卡之间建关系，关系本身构成提交内容", async () => {
    const { hasEffectiveSubmissionContent } = await import("../../interactive/submission");
    const blank = addCard(emptyState(BOARD_ID), { kind: "text", content: "", checked: true });
    const state = addCard(blank, { kind: "text", content: "", checked: true });
    const [cardA, cardB] = state.cards;
    state.links.push({
      id: "l1",
      src: cardA.id,
      dst: cardB.id,
      direction: false,
      meaning: "",
      deleted: false,
      createdAt: NOW,
      updatedAt: NOW,
    } as never);
    // 空白卡本身不算内容，但独立关系算有效改动
    expect(hasEffectiveSubmissionContent(state, null)).toBe(true);
    expect(hasEffectiveSubmissionContent(emptyState(BOARD_ID), null)).toBe(false);
  });
});

describe("#03 提交区唯一主状态单调不倒退", () => {
  it("受理→理解→已回复后，迟到的「理解中」事实不把主状态降级", async () => {
    vi.useFakeTimers();
    const store = useInteractiveStore();
    await store.load();
    const filled = addCard(emptyState(BOARD_ID), { kind: "text", content: "要提交的内容", checked: true });
    store.commit(filled as BoardState, "添加文字卡");
    vi.mocked(api.submitBoard).mockResolvedValue(submitResult() as never);
    await store.submit();
    await flushPromises();
    expect(store.roundStatus).toBe("queued");

    vi.mocked(api.fetchUnderstanding).mockResolvedValue({
      understanding: understanding({ status: "understood" }),
      understandings: [understanding({ status: "understood" })],
    } as never);
    await store.refreshUnderstanding();
    expect(store.understandingStatus).toBe("understood");
    expect(store.roundStatus).toBe("replied");

    // 迟到事实：同一提交 / 同一 attempt 的记录又回到 understanding —— 主状态不许倒退
    vi.mocked(api.fetchUnderstanding).mockResolvedValue({
      understanding: understanding({ status: "understanding" }),
      understandings: [understanding({ status: "understanding" })],
    } as never);
    await store.refreshUnderstanding();
    expect(store.understandingStatus).toBe("understanding");
    expect(store.roundStatus).toBe("replied");
  });
});

describe("#07 执行记录读取失败独立记录", () => {
  it("首次失败=读取失败；重试成功=上次结果可保留", async () => {
    vi.mocked(api.fetchExecutions).mockRejectedValueOnce(new Error("执行记录读取失败") as never);
    const store = useInteractiveStore();
    await store.load();
    expect(store.executionsLoadState).toBe("error");
    expect(store.executionsLoadedOnce).toBe(false);
    expect(store.executionsLoadError).toContain("执行记录读取失败");
    // 重试成功
    vi.mocked(api.fetchExecutions).mockResolvedValue({ executions: [] } as never);
    await store.loadExecutions();
    expect(store.executionsLoadState).toBe("ok");
    expect(store.executionsLoadedOnce).toBe(true);
    // 之后失败：旧记录保留，loadedOnce 仍为 true（界面说「上次结果」）
    vi.mocked(api.fetchExecutions).mockRejectedValueOnce(new Error("又断了") as never);
    await store.loadExecutions();
    expect(store.executionsLoadState).toBe("error");
    expect(store.executionsLoadedOnce).toBe(true);
  });
});

describe("#26 待确认任务事件幂等应用", () => {
  it("同一 intent 的重复事件按 intent_id 覆盖状态，不生成重复卡", () => {
    const store = useInteractiveStore();
    store.intentViews = [
      { intent_id: "i1", status: "proposed", title: "任务一", version: 1 },
      { intent_id: "i2", status: "proposed", title: "任务二", version: 1 },
    ] as never;
    const payload = { board_id: BOARD_ID, intent_ids: ["i1"], statuses: { i1: "awaiting_confirmation" }, reason: "proposed" };
    expect(store.applyInteractiveIntentsChanged(payload)).toBe(true);
    expect(store.intentViews).toHaveLength(2);
    expect(store.intentViews[0].status).toBe("awaiting_confirmation");
    // 重复投递：状态相同 → 仍是两条、内容不变
    expect(store.applyInteractiveIntentsChanged(payload)).toBe(true);
    expect(store.intentViews).toHaveLength(2);
    // 别的板面的事件不落地
    expect(store.applyInteractiveIntentsChanged({ ...payload, board_id: "board_other" })).toBe(false);
    expect(store.intentViews[0].status).toBe("awaiting_confirmation");
  });

  it("#26 订阅身份：事件类型在前端 EVENT_TYPES 里；未知 intent 不编造卡片", () => {
    expect(EVENT_TYPES).toContain("INTERACTIVE_INTENTS_CHANGED");
    const store = useInteractiveStore();
    store.intentViews = [{ intent_id: "i1", status: "proposed", title: "已知任务", version: 1 }] as never;
    expect(
      store.applyInteractiveIntentsChanged({
        board_id: BOARD_ID,
        intent_ids: ["i_new"],
        statuses: { i_new: "approved" },
        reason: "proposed",
      }),
    ).toBe(true);
    // 未知 id 不造卡：列表仍是原来那一条（权威刷新由 mock 的 fetchIntentViews 兜底）
    expect(store.intentViews).toHaveLength(1);
    expect(store.intentViews[0].intent_id).toBe("i1");
  });
});

describe("#44-46 批次导入数据层", () => {
  it("单批 5 份上限：第 6 份记未接收；终态不被后续回执覆盖；重试只重置对应文件", () => {
    const store = useInteractiveStore();
    const batch = store.beginImportBatch(
      Array.from({ length: 6 }, (_, index) => ({ name: "文件" + index + ".txt" })),
    );
    expect(batch.items).toHaveLength(6);
    expect(batch.items.map((item) => item.status)).toEqual([
      "pending", "pending", "pending", "pending", "pending", "unreceived",
    ]);
    const [first, second] = batch.items;
    store.noteImportItem(batch.id, second.key, { status: "failed", error: "第二份失败" });
    store.noteImportItem(batch.id, first.key, { status: "succeeded", attachmentId: "att_1" });
    // 后续成功不覆盖前面的失败
    store.noteImportItem(batch.id, second.key, { status: "succeeded" });
    const after = store.importBatchById(batch.id)!;
    expect(after.items[0].status).toBe("succeeded");
    expect(after.items[1].status).toBe("failed");
    expect(after.items[1].error).toContain("第二份失败");
    // 重试只操作对应文件
    store.retryImportItem(batch.id, second.key);
    store.retryImportItem(batch.id, batch.items[5].key);
    const retried = store.importBatchById(batch.id)!;
    expect(retried.items[1].status).toBe("pending");
    expect(retried.items[0].status).toBe("succeeded");
    expect(retried.items[5].status).toBe("pending");
  });

  it("迟到回执不关联已删除/被替换的文件卡", async () => {
    const store = useInteractiveStore();
    await store.load();
    const batch = store.beginImportBatch([{ name: "挂到卡上的文件.txt", cardId: "card_gone" }]);
    store.noteImportItem(batch.id, batch.items[0].key, { status: "succeeded", cardId: "card_gone" });
    const item = store.importBatchById(batch.id)!.items[0];
    expect(item.status).toBe("failed");
    expect(item.error).toContain("原文件卡已被删除或被替换");
  });
});

describe("#41 提交详情的理解读取三态", () => {
  it("首次读取失败=读取失败（可重试）；重试成功且为空=成功读取为空", async () => {
    vi.mocked(api.fetchUnderstanding).mockRejectedValueOnce(new Error("理解读取失败") as never);
    const store = useInteractiveStore();
    await store.load();
    expect(store.understandingLoadState).toBe("error");
    expect(store.understandingLoadedOnce).toBe(false);
    expect(store.understandingLoadError).toContain("理解读取失败");
    // 重试：成功读取为空 → 这才是「没有理解过程」，不是读取失败
    vi.mocked(api.fetchUnderstanding).mockResolvedValue({ understandings: [] } as never);
    await store.refreshUnderstanding();
    expect(store.understandingLoadState).toBe("ok");
    expect(store.understandingLoadedOnce).toBe(true);
    expect(store.understandingLoadError).toBeNull();
    expect(store.understanding).toBeNull();
  });

  it("已有上次结果后再读取失败：error 但 loadedOnce=true（上次结果），旧内容保留", async () => {
    vi.mocked(api.fetchUnderstanding).mockResolvedValue({
      understanding: understanding({ status: "understood" }),
      understandings: [understanding({ status: "understood" })],
    } as never);
    const store = useInteractiveStore();
    await store.load();
    expect(store.understandingLoadedOnce).toBe(true);
    expect(store.understanding?.id).toBe("u1");
    vi.mocked(api.fetchUnderstanding).mockRejectedValueOnce(new Error("又断了") as never);
    await store.refreshUnderstanding();
    expect(store.understandingLoadState).toBe("error");
    expect(store.understandingLoadedOnce).toBe(true);
    expect(store.understandingLoadError).toContain("又断了");
    expect(store.understanding?.id).toBe("u1");
  });
});
