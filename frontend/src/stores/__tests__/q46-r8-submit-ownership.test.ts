/**
 * #46 第八轮回归（任务书一.1）：**在 B 点提交，后端不得受理 A**。
 *
 * 反例（基线 c9fedc60 的真实行为）：在 B 做改动、挂起 B 的保存 → 点「提交给 QIO」→ 切到已经加载好的 A
 * → 放行 B 的保存。旧实现用「此刻的 boardId / board.seq」组装提交载荷，于是：
 *   - POST /boards/{A}/submissions（A 新增 1 条提交记录，B 为 0 条）；
 *   - 提交身份（clientRequestId）落在 A 的本机台账；
 *   - B 的提交回执、受理查证、提交后的板面 / 可见范围 / 任务刷新全部落到 A；
 *   - 「后端已受理、浏览器没收到响应」时，查证也发往 A，重试身份跟着换板。
 *
 * 本文件锁死的行为（只通过 store 公开动作驱动）：
 * 1. B 保存后在 A 上放行：submitBoard 只发往 B，baseStateVersion = B 本次保存确认的版本，
 *    提交身份记在 B 的台账；A 不显示 B 的提交中 / 回执；切回 B 能看到自己的受理结果；
 * 2. B 保存失败：不发出任何提交；失败原因、候选与重试入口留在 B；A 不受影响；
 * 3. B 等待影响确认：不提交；确认后只提交 B；
 * 4. B 的提交回执在 A 打开时才到：只更新 B（提交后读取 / 草稿恢复 / 任务刷新不碰 A）；
 * 5. 后端已受理但浏览器没收到响应：查证使用同一板面（B）与同一请求身份，重试不重复受理。
 *
 * 标注：【状态/单元】真实 store；【模拟】仅网络响应的到达顺序与延迟（受控 promise）。
 * 断言只落在 store 公开状态 + 真实 API mock 的调用参数 / 次数。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { flushPromises } from "@vue/test-utils";
import { useInteractiveStore } from "../interactive";
import * as api from "../../services/interactive";
import * as approval from "../../interactive/approval";
import type { BoardState, SubmissionResult } from "../../interactive/types";

vi.mock("../../services/interactive", () => ({
  fetchBoardState: vi.fn(),
  fetchBoards: vi.fn(),
  saveBoardState: vi.fn(),
  saveDrafts: vi.fn(),
  fetchVisibleRange: vi.fn(),
  previewMaterialImpact: vi.fn(),
  checkMaterialImpact: vi.fn(),
  submitBoard: vi.fn(),
  fetchIntents: vi.fn(),
  fetchAcceptance: vi.fn(),
  fetchUnderstanding: vi.fn(),
  retryUnderstanding: vi.fn(),
  fetchExecutions: vi.fn(),
  fetchIntentViews: vi.fn(),
  createIntents: vi.fn(),
}));

/**
 * 批次记录换成可观测的包装（其它仍是真实现）：跨板刷新会把「别的板面的新任务」
 * 记进原板面的提交批次 —— 这里用它锁死「批次只归发起提交的原板」。
 */
vi.mock("../../interactive/approval", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../interactive/approval")>();
  return { ...actual, recordIntentBatch: vi.fn(actual.recordIntentBatch) };
});

const NOW = "2026-11-16T00:00:00Z";
const BOARD_A = "board_a";
const BOARD_B = "board_b";
const B_SERVER_SEQ = 7;
const B_SAVED_SEQ = 8;
const SUBMISSION_ID = "sub_b_1";
const B_SERVER_TEXT = "B 的原正文";
const B_EDITED_TEXT = "B 提交前的正文";
const TASK_ID = "intent_b_1";
const AFFECTED_TASK = {
  intentId: TASK_ID,
  title: "任务-B-1",
  materials: ["card_b"],
  consequence: "暂停并保留进度",
};

function textCard(id: string, content: string): BoardState["cards"][number] {
  return {
    id,
    kind: "text",
    content,
    meta: {},
    x: 0,
    y: 0,
    w: 200,
    h: 120,
    checked: false,
    hidden: false,
    folded: false,
    bookmarked: false,
    deleted: false,
    createdAt: NOW,
    updatedAt: NOW,
  } as BoardState["cards"][number];
}

function boardState(boardId: string, cards: BoardState["cards"], seq = 3): BoardState {
  return { boardId, seq, updatedAt: NOW, cards, groups: [], links: [], selection: [] };
}

function boardPayload(state: BoardState) {
  return {
    board: { id: state.boardId, title: state.boardId },
    state,
    seq: state.seq ?? 3,
    baseline: null,
    submissions: [],
    drafts: { drafts: {}, updatedAt: null },
  };
}

function deferred<T = never>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

function apiError(status: number, message: string, payload: Record<string, unknown> = {}): Error {
  return Object.assign(new Error(message), { status, payload });
}

function runningIntent(boardId: string) {
  return {
    id: TASK_ID,
    boardId,
    submissionId: null,
    title: "任务-B-1",
    summary: "",
    status: "running",
    preview: { kind: "task" },
    impact: { materials: ["card_b"], consequence: "暂停并保留进度" },
    dependsOn: [],
    conflictsWith: [],
    conflictKey: "",
    materialRefs: ["card_b"],
    progress: { done: 0, total: 1, text: "" },
    reason: "",
    demo: true,
    createdAt: NOW,
    updatedAt: NOW,
  };
}

function affectedCheck(checkId: string, stateVersion: number) {
  return {
    ok: true,
    checkId,
    stateVersion,
    affected: [AFFECTED_TASK],
    impactConfirmationRequired: true,
  } as never;
}

function cleanCheck(stateVersion: number) {
  return { ok: true, stateVersion, affected: [], impactConfirmationRequired: false } as never;
}

function succeededResult(seq: number): SubmissionResult {
  return {
    status: "succeeded",
    submission: { id: SUBMISSION_ID, seq, status: "succeeded", createdAt: NOW },
    before: { cards: [], groups: [], links: [], selection: [], empty: true },
    after: { cards: [], groups: [], links: [], selection: [], empty: true },
    expressions: [],
    baseline: { updated: true, previousSeq: seq - 1, seq },
    delivery: { delivered: true, reason: "", detail: "", understandingId: "u_b_1", status: "accepted" },
    visibleRange: { cards: [], groups: [], links: [], selection: [], empty: true },
    checkedCleared: [],
  } as unknown as SubmissionResult;
}

function liveCards(store: ReturnType<typeof useInteractiveStore>) {
  return (store.board?.cards ?? []).filter((card) => !card.deleted);
}

function editCard(store: ReturnType<typeof useInteractiveStore>, cardId: string, content: string) {
  const current = store.board!;
  return {
    ...current,
    cards: current.cards.map((card) => (card.id === cardId ? { ...card, content } : card)),
  };
}

/** 每次提交请求的「目标板面 + 基准版本 + 请求身份」。 */
function submitCalls() {
  return vi.mocked(api.submitBoard).mock.calls.map((call) => ({
    boardId: String(call[0]),
    baseStateVersion: call[3] as number | undefined,
    clientRequestId: String(call[5] ?? ""),
  }));
}

/** 每次受理查证的「目标板面 + 请求身份」。 */
function acceptanceCalls() {
  return vi.mocked(api.fetchAcceptance).mock.calls.map((call) => ({
    boardId: String(call[0]),
    clientRequestId: String(call[1]),
  }));
}

function identityKey(boardId: string): string {
  return "qio.interactive.pending-submission." + boardId;
}

/** 某一类目标板面的 API 调用次数。 */
function boardReadCounts() {
  return {
    fetchBoardState: (id: string) =>
      vi.mocked(api.fetchBoardState).mock.calls.filter((call) => String(call[0]) === id).length,
    fetchVisibleRange: (id: string) =>
      vi.mocked(api.fetchVisibleRange).mock.calls.filter((call) => String(call[0]) === id).length,
    fetchIntents: (id: string) =>
      vi.mocked(api.fetchIntents).mock.calls.filter((call) => String(call[0]) === id).length,
    fetchUnderstanding: (id: string) =>
      vi.mocked(api.fetchUnderstanding).mock.calls.filter((call) => String(call[0]) === id).length,
  };
}

/** 一件等待审批的任务（用于「别的板面的新任务被记进 B 的提交批次」这个归属断言）。 */
function pendingIntent(id: string, boardId: string = BOARD_A) {
  return {
    id,
    boardId,
    submissionId: null,
    title: "任务-" + id,
    summary: "",
    status: "pending",
    preview: { kind: "task" },
    impact: { materials: [], consequence: "" },
    dependsOn: [],
    conflictsWith: [],
    conflictKey: "",
    materialRefs: [],
    progress: { done: 0, total: 0, text: "" },
    reason: "",
    demo: true,
    createdAt: NOW,
    updatedAt: NOW,
  };
}

/** 两块板各自「整板 / 可见范围 / 任务清单」的请求次数快照（用来锁死「没对别的板面发读取」）。 */
function readCounts() {
  const counts = boardReadCounts();
  return {
    [BOARD_A]: {
      board: counts.fetchBoardState(BOARD_A),
      visible: counts.fetchVisibleRange(BOARD_A),
      intents: counts.fetchIntents(BOARD_A),
    },
    [BOARD_B]: {
      board: counts.fetchBoardState(BOARD_B),
      visible: counts.fetchVisibleRange(BOARD_B),
      intents: counts.fetchIntents(BOARD_B),
    },
  };
}

async function settle(times = 10): Promise<void> {
  for (let i = 0; i < times; i += 1) await flushPromises();
}

let store: ReturnType<typeof useInteractiveStore>;

beforeEach(() => {
  localStorage.clear();
  vi.resetAllMocks();
  setActivePinia(createPinia());
  store = useInteractiveStore();
  vi.mocked(api.fetchBoards).mockResolvedValue({
    boards: [
      { id: BOARD_A, title: "板面 A" },
      { id: BOARD_B, title: "板面 B" },
    ],
  } as never);
  vi.mocked(api.fetchBoardState).mockImplementation((async (id: string) =>
    boardPayload(
      id === BOARD_A
        ? boardState(BOARD_A, [textCard("card_a", "A 的正文")], 3)
        : boardState(BOARD_B, [textCard("card_b", B_SERVER_TEXT)], B_SERVER_SEQ),
    )) as never);
  vi.mocked(api.saveBoardState).mockImplementation((async (id: string, candidate: BoardState) => {
    const nextSeq = Number(candidate?.seq ?? 3) + 1;
    return { ok: true, seq: nextSeq, savedAt: NOW, state: { ...candidate, boardId: id, seq: nextSeq } };
  }) as never);
  vi.mocked(api.saveDrafts).mockResolvedValue({ drafts: {}, updatedAt: NOW } as never);
  vi.mocked(api.fetchVisibleRange).mockResolvedValue({
    visibleRange: { cards: [], groups: [], links: [], selection: [], empty: true, notVisibleCount: 0 },
  } as never);
  // 默认没有执行中任务：保存不会先走影响预判（第 3 个用例单独覆盖「等待影响确认」）
  vi.mocked(api.fetchIntents).mockResolvedValue({
    intents: [],
    conflicts: [],
    batchAvailable: false,
    recovery: { paused: [] },
  } as never);
  vi.mocked(api.fetchIntentViews).mockResolvedValue({ intents: [], source: "service" } as never);
  vi.mocked(api.fetchExecutions).mockResolvedValue({ executions: [] } as never);
  vi.mocked(api.fetchUnderstanding).mockResolvedValue({ understanding: null, understandings: [] } as never);
  vi.mocked(api.fetchAcceptance).mockResolvedValue({ acceptance: null, reason: "没有这条请求身份的记录" } as never);
  vi.mocked(api.checkMaterialImpact).mockResolvedValue(cleanCheck(B_SERVER_SEQ) as never);
  vi.mocked(api.previewMaterialImpact).mockResolvedValue({ affected: [] } as never);
  vi.mocked(api.submitBoard).mockResolvedValue({ status: "empty" } as never);
});

afterEach(async () => {
  // 排空防抖保存（450ms 真实计时器），避免污染下一个用例的请求记录
  await new Promise((resolve) => setTimeout(resolve, 700));
  await settle();
  localStorage.clear();
});

/** 前提装置：先打开 A，再打开 B（两块板面都已加载过）。 */
async function openABothLoaded() {
  await store.selectBoard(BOARD_A);
  await store.selectBoard(BOARD_B);
}

describe("一.1 提交归属于发起时的板面", () => {
  it("B 保存后在 A 上放行：提交只发往 B，版本用 B 本次保存确认的 8，A 收不到任何提交", async () => {
    await openABothLoaded();
    store.commit(editCard(store, "card_b", B_EDITED_TEXT), "改正文");

    const saveFlight = deferred<void>();
    vi.mocked(api.saveBoardState).mockImplementationOnce(async (_id, candidate) => {
      await saveFlight.promise;
      const snapshot = candidate as BoardState;
      return {
        ok: true,
        seq: B_SAVED_SEQ,
        savedAt: NOW,
        state: { ...snapshot, boardId: BOARD_B, seq: B_SAVED_SEQ },
      } as never;
    });
    const submitFlight = deferred<never>();
    vi.mocked(api.submitBoard).mockImplementationOnce(() => submitFlight.promise as never);

    const saving = store.saveNow();
    const submitting = store.submit();
    await settle();
    expect(api.submitBoard, "B 的保存还没落定就发出了提交").not.toHaveBeenCalled();

    // 挂起 B 的保存回执期间，切到已经加载好的 A
    await store.selectBoard(BOARD_A);
    await settle();
    expect(store.boardId).toBe(BOARD_A);

    // 放行 B 的保存成功
    saveFlight.resolve();
    await saving;
    await settle();

    const posts = submitCalls();
    expect(posts.length, "B 的提交请求没有发出").toBe(1);
    expect(posts[0].boardId, "提交发到了切板后的 A（应发往发起提交的 B）").toBe(BOARD_B);
    expect(
      posts[0].baseStateVersion,
      "提交体使用的不是 B 本次保存确认的版本（seq）",
    ).toBe(B_SAVED_SEQ);
    const requestId = posts[0].clientRequestId;
    expect(requestId, "提交没有带稳定请求身份").toMatch(/^creq_/);
    expect(
      localStorage.getItem(identityKey(BOARD_B)) ?? "",
      "本次提交身份没有记在 B 的本机台账里",
    ).toContain(requestId);
    expect(
      localStorage.getItem(identityKey(BOARD_A)),
      "本次提交身份记到了 A 的本机台账里",
    ).toBeNull();
    expect(acceptanceCalls().filter((call) => call.boardId === BOARD_A), "受理查证发到了 A").toHaveLength(0);
    expect(store.boardId, "提交请求发出时把当前板面切走了").toBe(BOARD_A);
    expect(store.submitStatus, "A 上显示了 B 的「提交中」").toBe("idle");
    expect(store.lastSubmission, "A 上显示了 B 的提交回执").toBeNull();

    // 提交回执在 A 打开时到达
    submitFlight.resolve(succeededResult(B_SAVED_SEQ) as never);
    await submitting;
    await settle();

    expect(store.boardId, "提交回执把当前板面切走了").toBe(BOARD_A);
    expect(store.submitStatus, "B 的提交回执落到了 A 的状态上").toBe("idle");
    expect(store.lastSubmission, "B 的提交回执出现在 A 上").toBeNull();

    // 切回 B：自己的受理结果可见
    await store.selectBoard(BOARD_B);
    await settle();
    expect(store.boardId).toBe(BOARD_B);
    expect(store.submitStatus, "切回 B 看不到自己的提交结果").toBe("succeeded");
    expect(
      store.lastSubmission?.submission?.id,
      "切回 B 看不到自己的提交回执",
    ).toBe(SUBMISSION_ID);
  });

  it("B 保存失败：不发出任何提交，失败原因 / 候选 / 重试入口留在 B，A 不受影响", async () => {
    await openABothLoaded();
    store.commit(editCard(store, "card_b", B_EDITED_TEXT), "改正文");

    const saveFlight = deferred<void>();
    vi.mocked(api.saveBoardState).mockImplementationOnce(async () => {
      await saveFlight.promise;
      throw apiError(500, "B 板保存失败：服务端 500");
    });
    vi.mocked(api.submitBoard).mockResolvedValue(succeededResult(B_SAVED_SEQ) as never);

    const saving = store.saveNow();
    const submitting = store.submit();
    await settle();
    await store.selectBoard(BOARD_A);
    await settle();

    saveFlight.reject(apiError(500, "B 板保存失败：服务端 500"));
    await saving;
    await submitting;
    await settle();

    expect(api.submitBoard, "B 的保存失败后仍从 A 发出了提交").not.toHaveBeenCalled();
    expect(store.boardId).toBe(BOARD_A);
    expect(store.submitStatus, "B 的提交失败显示在 A 上").toBe("idle");
    expect(store.saveError, "B 的保存失败原因显示在 A 上").toBeNull();
    expect(
      vi.mocked(api.saveBoardState).mock.calls.filter((call) => String(call[0]) === BOARD_A),
      "向 A 发出了 B 的保存",
    ).toHaveLength(0);

    // 回到 B：失败原因、候选与重试入口都在 B
    await store.selectBoard(BOARD_B);
    await settle();
    expect(store.saveStatus, "B 自己的保存失败状态丢了").toBe("error");
    expect(String(store.saveError ?? ""), "B 看不到保存失败原因").toContain("B 板保存失败");
    expect(store.dirty, "B 的候选在保存失败后丢了").toBe(true);
    expect(store.submitStatus, "B 上没有留下本次提交的失败状态").toBe("failed");
    expect(liveCards(store).find((card) => card.id === "card_b")?.content).toBe(B_EDITED_TEXT);

    // 重试入口：只向 B 重发保存，成功后失败态收敛
    vi.mocked(api.saveBoardState).mockImplementation(async (id: string, candidate: BoardState) => {
      const nextSeq = Number(candidate?.seq ?? 3) + 1;
      return { ok: true, seq: nextSeq, savedAt: NOW, state: { ...candidate, boardId: id, seq: nextSeq } };
    });
    const before = vi.mocked(api.saveBoardState).mock.calls.length;
    await store.saveNow();
    await settle();
    expect(vi.mocked(api.saveBoardState).mock.calls.length, "重试没有向 B 发出保存").toBe(before + 1);
    expect(String(vi.mocked(api.saveBoardState).mock.calls[before][0])).toBe(BOARD_B);
    expect(store.saveStatus, "B 保存重试成功后仍显示失败").toBe("saved");
    expect(store.saveError, "B 保存重试成功后仍留着失败原因").toBeNull();
    expect(store.dirty, "B 保存重试成功后仍显示有未保存内容").toBe(false);
  });

  it("B 等待影响确认：本次不提交；切回 B 确认后只提交 B", async () => {
    await store.selectBoard(BOARD_A);
    // B 上有执行中的任务：保存会先做影响预判
    vi.mocked(api.fetchIntents).mockImplementation((async (id: string) => ({
      intents: id === BOARD_B ? [runningIntent(id)] : [],
      conflicts: [],
      batchAvailable: false,
      recovery: { paused: [] },
    })) as never);
    await store.selectBoard(BOARD_B);
    store.commit(editCard(store, "card_b", B_EDITED_TEXT), "改正文");

    const checkFlight = deferred<never>();
    vi.mocked(api.checkMaterialImpact).mockImplementationOnce(() => checkFlight.promise as never);
    const saveFlight = deferred<void>();
    vi.mocked(api.saveBoardState).mockImplementationOnce(async (_id, candidate) => {
      await saveFlight.promise;
      const snapshot = candidate as BoardState;
      const nextSeq = Number(snapshot.seq ?? 3) + 1;
      return { ok: true, seq: nextSeq, savedAt: NOW, state: { ...snapshot, boardId: BOARD_B, seq: nextSeq } } as never;
    });

    const saving = store.saveNow();
    const submitting = store.submit();
    await settle();
    expect(api.submitBoard, "影响确认还没落定就发出了提交").not.toHaveBeenCalled();

    await store.selectBoard(BOARD_A);
    await settle();

    // 影响预判在 A 打开时返回：B 需要确认
    checkFlight.resolve(affectedCheck("chk_b", B_SERVER_SEQ) as never);
    await saving;
    await submitting;
    await settle();

    expect(api.submitBoard, "B 还在等待影响确认却发出了提交").not.toHaveBeenCalled();
    expect(store.boardId).toBe(BOARD_A);
    expect(store.pendingImpact, "B 的待确认说明落到了 A").toBeNull();
    expect(
      vi.mocked(api.saveBoardState).mock.calls.filter((call) => String(call[0]) === BOARD_A),
      "向 A 发出了 B 的保存",
    ).toHaveLength(0);

    // 回到 B：确认入口属于 B
    await store.selectBoard(BOARD_B);
    await settle();
    expect(store.pendingImpact?.boardId, "B 看不到自己的影响确认").toBe(BOARD_B);
    expect(store.pendingImpact?.checkId).toBe("chk_b");

    // 确认本身会等到这次 PUT 的回执：先发起确认，再放行挂起的保存（避免用例自己死锁）
    const confirming = store.confirmImpact();
    await settle();
    saveFlight.resolve();
    await confirming;
    await settle();
    expect(store.saveStatus, "B 确认影响后保存没有落定").toBe("saved");
    expect(store.pendingImpact, "B 确认影响后仍停在确认里").toBeNull();

    // 用户再点「提交给 QIO」：只提交 B，版本用 B 刚确认的保存版本
    vi.mocked(api.submitBoard).mockResolvedValue(succeededResult(Number(store.board?.seq ?? B_SAVED_SEQ)) as never);
    await store.submit();
    await settle();
    const posts = submitCalls();
    expect(posts.length, "确认后没有只发出一次提交").toBe(1);
    expect(posts[0].boardId, "确认后的提交发到了 A").toBe(BOARD_B);
    const boardSeqAtSubmit = Number(store.board?.seq ?? 0);
    expect(
      posts[0].baseStateVersion,
      "确认后的提交没有使用 B 本次确认的保存版本（提交体 seq 与 B 当前已保存版本不一致）",
    ).toBe(boardSeqAtSubmit);
    expect(
      boardSeqAtSubmit,
      "前提：B 的已保存版本没有在确认过程中推进（提交仍使用加载时的旧版本）",
    ).toBeGreaterThan(B_SERVER_SEQ);
    expect(
      vi.mocked(api.saveBoardState).mock.calls.filter((call) => String(call[0]) === BOARD_A),
      "向 A 发出了 B 的保存",
    ).toHaveLength(0);
  });

  it("B 的提交回执在 A 打开时才到：只更新 B（提交后读取 / 任务刷新不碰 A）", async () => {
    await openABothLoaded();
    store.commit(editCard(store, "card_b", B_EDITED_TEXT), "改正文");

    const saveFlight = deferred<void>();
    vi.mocked(api.saveBoardState).mockImplementationOnce(async (_id, candidate) => {
      await saveFlight.promise;
      const snapshot = candidate as BoardState;
      return {
        ok: true,
        seq: B_SAVED_SEQ,
        savedAt: NOW,
        state: { ...snapshot, boardId: BOARD_B, seq: B_SAVED_SEQ },
      } as never;
    });
    const submitFlight = deferred<never>();
    vi.mocked(api.submitBoard).mockImplementationOnce(() => submitFlight.promise as never);

    const saving = store.saveNow();
    const submitting = store.submit();
    await settle();
    await store.selectBoard(BOARD_A);
    await settle();

    saveFlight.resolve();
    await saving;
    await settle();
    expect(submitCalls().length, "前提：B 的提交已经发出").toBe(1);

    const beforeCounts = boardReadCounts();
    const aBefore = {
      fetchBoardState: beforeCounts.fetchBoardState(BOARD_A),
      fetchVisibleRange: beforeCounts.fetchVisibleRange(BOARD_A),
      fetchIntents: beforeCounts.fetchIntents(BOARD_A),
      fetchUnderstanding: beforeCounts.fetchUnderstanding(BOARD_A),
    };

    // B 的提交回执在 A 打开时到达
    submitFlight.resolve(succeededResult(B_SAVED_SEQ) as never);
    await submitting;
    await settle();

    const afterCounts = boardReadCounts();
    expect(store.boardId, "B 的提交回执把当前板面切走了").toBe(BOARD_A);
    expect(store.submitStatus, "B 的提交回执写进了 A 的提交状态").toBe("idle");
    expect(store.lastSubmission, "B 的提交回执出现在 A 上").toBeNull();
    expect(
      afterCounts.fetchBoardState(BOARD_A),
      "B 的提交回执触发了对 A 的整板读取（提交后刷新没有绑定原板面）",
    ).toBe(aBefore.fetchBoardState);
    expect(
      afterCounts.fetchVisibleRange(BOARD_A),
      "B 的提交回执触发了对 A 的可见范围刷新",
    ).toBe(aBefore.fetchVisibleRange);
    expect(
      afterCounts.fetchIntents(BOARD_A),
      "B 的提交回执触发了对 A 的任务清单刷新",
    ).toBe(aBefore.fetchIntents);
    expect(
      afterCounts.fetchUnderstanding(BOARD_A),
      "B 的提交回执触发了对 A 的理解查询",
    ).toBe(aBefore.fetchUnderstanding);

    // 切回 B：提交结果与回执可见
    await store.selectBoard(BOARD_B);
    await settle();
    expect(store.submitStatus, "切回 B 看不到自己的提交结果").toBe("succeeded");
    expect(store.lastSubmission?.submission?.id, "切回 B 看不到自己的提交回执").toBe(SUBMISSION_ID);
  });

  it("后端已受理但浏览器没收到响应：查证只针对 B、沿用同一请求身份，重试不重复受理", async () => {
    await openABothLoaded();
    store.commit(editCard(store, "card_b", B_EDITED_TEXT), "改正文");

    const saveFlight = deferred<void>();
    vi.mocked(api.saveBoardState).mockImplementationOnce(async (_id, candidate) => {
      await saveFlight.promise;
      const snapshot = candidate as BoardState;
      return {
        ok: true,
        seq: B_SAVED_SEQ,
        savedAt: NOW,
        state: { ...snapshot, boardId: BOARD_B, seq: B_SAVED_SEQ },
      } as never;
    });
    const submitFlight = deferred<never>();
    vi.mocked(api.submitBoard).mockImplementationOnce(() => submitFlight.promise as never);
    const acceptanceFlight = deferred<never>();
    vi.mocked(api.fetchAcceptance).mockImplementationOnce(() => acceptanceFlight.promise as never);

    const saving = store.saveNow();
    const submitting = store.submit();
    await settle();
    await store.selectBoard(BOARD_A);
    await settle();

    saveFlight.resolve();
    await saving;
    await settle();
    expect(submitCalls().length, "前提：B 的提交已经发出").toBe(1);
    // 注：提交目标属于第 1 个用例的断言范围；本用例聚焦「受理查证」也要沿用同一板面与身份。
    const requestId = submitCalls()[0].clientRequestId;

    // 提交请求没有得到回执（网络中断，没有 status → 结果未知）
    submitFlight.reject(new Error("网络中断，没有收到回执"));
    await settle();

    const lookups = acceptanceCalls();
    expect(lookups.length, "结果未知时没有按请求身份查证受理情况").toBe(1);
    expect(lookups[0].boardId, "受理查证发到了切板后的 A（应查发起提交的 B）").toBe(BOARD_B);
    expect(lookups[0].clientRequestId, "受理查证的身份不是这次提交的身份").toBe(requestId);
    expect(store.boardId).toBe(BOARD_A);
    expect(store.submitStatus, "A 上显示了 B 的「结果未知」").toBe("idle");
    expect(store.lastAcceptance, "B 的受理查证结果出现在 A 上").toBeNull();

    // 服务端没有这条身份的记录 → 继续「结果未知」，不盲目重发
    acceptanceFlight.resolve({ acceptance: null, reason: "没有这条请求身份的记录" } as never);
    await submitting;
    await settle();
    expect(submitCalls(), "查证未命中却重复发出了提交").toHaveLength(1);
    expect(store.boardId).toBe(BOARD_A);

    // 用户重试查证：同一板面 + 同一请求身份
    const retryFlight = deferred<never>();
    vi.mocked(api.fetchAcceptance).mockImplementationOnce(() => retryFlight.promise as never);
    const retrying = store.confirmAcceptanceAfterUnclear();
    await settle();
    const afterRetry = acceptanceCalls();
    expect(afterRetry.length, "重试查证没有发出请求").toBe(2);
    expect(afterRetry[1].boardId, "重试查证换到了 A").toBe(BOARD_B);
    expect(afterRetry[1].clientRequestId, "重试查证换了请求身份").toBe(requestId);
    expect(submitCalls(), "重试查证又发了一次提交（重复受理）").toHaveLength(1);
    retryFlight.resolve({ acceptance: null } as never);
    await retrying;
    await settle();

    // 台账保留：切回 B 仍是「结果未知」，并可用同一身份继续查证
    expect(
      localStorage.getItem(identityKey(BOARD_B)) ?? "",
      "未决提交身份没有保留在 B 的台账里",
    ).toContain(requestId);
    await store.selectBoard(BOARD_B);
    await settle();
    expect(store.submitStatus, "切回 B 看不到「结果未知」").toBe("unknown");
    expect(store.submitResultUnknown).toBe(true);
    expect(store.pendingSubmission?.clientRequestId, "切回 B 丢了这次提交的身份").toBe(requestId);
    expect(String(store.submitError ?? ""), "B 上没有「结果未知、不要重复提交」的说明").toContain(
      "提交结果未知",
    );
  });
});

/**
 * #46（第九轮，第 3 项）：**提交后回读只归发起板**。
 *
 * 反例（基线 645e5a1f）：refreshAfterSubmit() 用全局 boardId / 全局 submitRefreshState：
 * 在 B 提交、切到已经加载好的 A 之后，B 的提交后回读要么继续读 A（整板 / 可见范围 / 任务清单），
 * 要么把失败写进 A 的刷新状态；回到 B 又看不到自己的失败，或者永远停在 pending。
 *
 * 本块锁死：
 * 1. B 的回读在 A 显示时失败 → A 保持 idle（错误只回到 B，回 B 可见）；
 * 2. B 的回读在 A 显示时成功 → A 不被刷新、也不记 B 的批次，回 B 后收敛为 idle；
 * 3. B→A→B 后新的刷新成功，旧刷新迟到的失败不得覆盖新结果；
 * 4. 在 B 点「重试刷新」后立刻切 A：不在 A 写状态、不对 A 发任何读取。
 */
describe("一.1b 提交后回读只归发起板（第九轮）", () => {
  /** 在 B 点完成一次提交（POST 成功），并让提交后的整板回读挂起。 */
  async function submitOnBWithRefreshInFlight() {
    await openABothLoaded();
    store.commit(editCard(store, "card_b", B_EDITED_TEXT), "改正文");
    vi.mocked(api.submitBoard).mockResolvedValue(succeededResult(B_SAVED_SEQ) as never);
    const refreshRead = deferred<never>();
    vi.mocked(api.fetchBoardState).mockImplementationOnce(() => refreshRead.promise as never);
    const submitted = store.submit();
    await settle();
    return { refreshRead, submitted };
  }

  it("B 的提交后回读在 A 打开时失败：A 保持 idle，错误只回到 B", async () => {
    const { refreshRead, submitted } = await submitOnBWithRefreshInFlight();
    expect(store.boardId, "前提：B 的提交已经发出").toBe(BOARD_B);
    expect(store.submitStatus, "前提：B 的提交请求已经成功").toBe("succeeded");
    expect(store.submitRefreshState, "前提：B 的提交后刷新在飞").toBe("pending");

    // 回读在飞期间切到已经加载好的 A
    await store.selectBoard(BOARD_A);
    await settle();
    const atA = readCounts();

    // B 的回读这时失败
    refreshRead.reject(apiError(502, "B 提交后回读失败：服务端 502"));
    await submitted;
    await settle();

    expect(store.boardId, "B 的提交后回读把当前板面切走了").toBe(BOARD_A);
    expect(store.submitRefreshState, "B 的回读失败写进了 A 的刷新状态").toBe("idle");
    expect(store.submitRefreshError, "B 的回读失败原因显示在 A 上").toBeNull();
    expect(readCounts(), "B 的提交后回读对 A 发起了读取").toEqual(atA);
    expect(submitCalls(), "提交后回读失败触发了重复提交").toHaveLength(1);

    // 回到 B：失败属于 B
    await store.selectBoard(BOARD_B);
    await settle();
    expect(store.submitRefreshState, "切回 B 看不到自己的刷新失败").toBe("error");
    expect(String(store.submitRefreshError ?? ""), "B 上看不到回读的真实失败原因").toContain("502");
  });

  it("B 的提交后回读在 A 打开时成功：A 不被刷新也不再记 B 的批次，回 B 后收敛为 idle", async () => {
    // A 的清单里有一件新任务：旧实现会在 A 上刷新清单并把它记进 B 的提交批次
    vi.mocked(api.fetchIntents).mockImplementation((async (id: string) => ({
      intents: String(id) === BOARD_A ? [pendingIntent("intent_a_1")] : [],
      conflicts: [],
      batchAvailable: false,
      recovery: { paused: [] },
    })) as never);
    const { refreshRead, submitted } = await submitOnBWithRefreshInFlight();
    expect(store.submitRefreshState, "前提：B 的提交后刷新在飞").toBe("pending");

    await store.selectBoard(BOARD_A);
    await settle();
    const atA = readCounts();
    vi.mocked(approval.recordIntentBatch).mockClear();

    // B 的回读这时成功返回（内容属于 B）
    refreshRead.resolve(
      boardPayload(boardState(BOARD_B, [textCard("card_b", B_EDITED_TEXT)], B_SAVED_SEQ)) as never,
    );
    await submitted;
    await settle();

    expect(store.boardId, "B 的提交后回读把当前板面切走了").toBe(BOARD_A);
    expect(store.submitRefreshState, "B 的成功回读写进了 A 的刷新状态").toBe("idle");
    expect(readCounts(), "B 的提交后回读继续对 A 发起了读取").toEqual(atA);
    expect(
      vi.mocked(approval.recordIntentBatch).mock.calls,
      "A 上新出现的任务被记进了 B 的提交批次（批次归属串板）",
    ).toHaveLength(0);
    expect(submitCalls(), "提交后回读触发了重复提交").toHaveLength(1);

    // 回到 B 时服务器事实已经是保存/提交后的版本（seq 8）：延后的刷新读到的就是它
    vi.mocked(api.fetchBoardState).mockResolvedValue(
      boardPayload(boardState(BOARD_B, [textCard("card_b", B_EDITED_TEXT)], B_SAVED_SEQ)) as never,
    );
    // 回到 B：延后的刷新按原板面收尾 → idle
    await store.selectBoard(BOARD_B);
    await settle();
    expect(store.submitRefreshState, "回 B 后延后的提交后刷新没有收尾（一直 pending）").toBe("idle");
    expect(store.submitRefreshError).toBeNull();
    expect(store.board?.boardId).toBe(BOARD_B);
  });

  it("B→A→B 之后新刷新成功：旧刷新迟到的失败不得覆盖新结果", async () => {
    const { refreshRead, submitted } = await submitOnBWithRefreshInFlight();
    expect(store.submitRefreshState, "前提：B 的旧提交后刷新在飞").toBe("pending");

    // 旧刷新还在飞时切走再切回
    await store.selectBoard(BOARD_A);
    await settle();
    await store.selectBoard(BOARD_B);
    await settle();

    // 回到 B 后用户点「重试刷新」：新的一次刷新读到提交后的新版本（seq 9）
    vi.mocked(api.fetchBoardState).mockResolvedValueOnce(
      boardPayload(boardState(BOARD_B, [textCard("card_b", B_EDITED_TEXT)], B_SAVED_SEQ + 1)) as never,
    );
    const retried = await store.retrySubmitRefresh();
    await settle();
    expect(retried.ok, "新的刷新没有成功").toBe(true);
    expect(store.submitRefreshState, "新刷新成功后状态不是 idle").toBe("idle");
    expect(store.submitRefreshError).toBeNull();

    // 旧刷新这时才失败返回：不得覆盖新一次的结果
    refreshRead.reject(apiError(502, "旧刷新迟到失败：服务端 502"));
    await submitted;
    await settle();

    expect(store.submitRefreshState, "旧代次的迟到失败覆盖了更新一次的成功结果").toBe("idle");
    expect(store.submitRefreshError, "旧代次的迟到失败给新结果写入了错误").toBeNull();
    expect(submitCalls(), "旧刷新失败触发了重复提交").toHaveLength(1);
  });

  it("在 B 点重试刷新后立刻切到 A：不在 A 写状态、不对 A 发读取", async () => {
    await openABothLoaded();
    store.commit(editCard(store, "card_b", B_EDITED_TEXT), "改正文");
    vi.mocked(api.submitBoard).mockResolvedValue(succeededResult(B_SAVED_SEQ) as never);

    // 第一次提交后的回读失败 → B 记录「刷新失败」并冻结了原板刷新上下文
    vi.mocked(api.fetchBoardState).mockImplementationOnce(async () => {
      throw apiError(502, "B 提交后回读失败：服务端 502");
    });
    await store.submit();
    await settle();
    expect(store.boardId).toBe(BOARD_B);
    expect(store.submitRefreshState, "前提：B 的提交后刷新失败").toBe("error");

    // 点「重试刷新」：这次回读挂起，随后立刻切到 A
    const retryRead = deferred<never>();
    vi.mocked(api.fetchBoardState).mockImplementationOnce(() => retryRead.promise as never);
    const retrying = store.retrySubmitRefresh();
    await settle();
    await store.selectBoard(BOARD_A);
    await settle();
    const atA = readCounts();

    retryRead.resolve(
      boardPayload(boardState(BOARD_B, [textCard("card_b", B_EDITED_TEXT)], B_SAVED_SEQ)) as never,
    );
    const retried = await retrying;
    await settle();

    expect(store.boardId, "B 的重试刷新把当前板面切走了").toBe(BOARD_A);
    expect(retried.ok, "重试刷新在切板后仍报告成功").toBe(false);
    expect(store.submitRefreshState, "B 的重试刷新写进了 A 的刷新状态").toBe("idle");
    expect(store.submitRefreshError, "B 的重试刷新错误显示在 A 上").toBeNull();
    expect(readCounts(), "B 的重试刷新继续对 A 发起了读取").toEqual(atA);
    expect(submitCalls(), "重试刷新触发了重复提交").toHaveLength(1);

    // 回到 B 时服务器事实已经是保存/提交后的版本（seq 8）
    vi.mocked(api.fetchBoardState).mockResolvedValue(
      boardPayload(boardState(BOARD_B, [textCard("card_b", B_EDITED_TEXT)], B_SAVED_SEQ)) as never,
    );
    // 回到 B：延后的刷新按原板面收尾
    await store.selectBoard(BOARD_B);
    await settle();
    expect(store.submitRefreshState, "回 B 后延后的重试刷新没有收尾").toBe("idle");
    expect(store.submitRefreshError).toBeNull();
  });

  it("B 提交后回读只合并了旧草稿（未采纳板面事实）：不得置 idle，重试拿到新版本才收敛", async () => {
    await openABothLoaded();
    store.commit(editCard(store, "card_b", B_EDITED_TEXT), "改正文");
    vi.mocked(api.submitBoard).mockResolvedValue(succeededResult(B_SAVED_SEQ) as never);

    // 保存把 B 推到 seq 8；提交后的整板回读只返回旧版本 seq 7：
    // refreshBoardFromServer 会返回 true，但只合并草稿、没有采纳板面事实 —— 本次刷新没有完成。
    await store.submit();
    await settle();

    expect(store.boardId).toBe(BOARD_B);
    expect(store.submitStatus, "前提：提交请求已经成功").toBe("succeeded");
    expect(
      store.submitRefreshState,
      "回读只合并了旧草稿（没有采纳板面事实），刷新却被当成成功（idle）",
    ).toBe("error");
    expect(String(store.submitRefreshError ?? ""), "没有说明「这次回读没有被采纳」").toContain(
      "没有被采纳",
    );

    // 重试：服务器给出提交后的新版本 seq 9 → 真正采纳 → 收敛为 idle
    vi.mocked(api.fetchBoardState).mockResolvedValueOnce(
      boardPayload(boardState(BOARD_B, [textCard("card_b", B_EDITED_TEXT)], B_SAVED_SEQ + 1)) as never,
    );
    const retried = await store.retrySubmitRefresh();
    await settle();
    expect(retried.ok, "重试读到提交后的新版本却没有成功").toBe(true);
    expect(store.submitRefreshState, "重试成功后状态不是 idle").toBe("idle");
    expect(store.submitRefreshError).toBeNull();
    expect(submitCalls(), "刷新/重试触发了重复提交").toHaveLength(1);
  });
});

/**
 * #46（第十轮，Task 2）：**查证成功后的刷新按原提交归组**。
 *
 * 反例（基线 70e475c2）：confirmAcceptanceAfterUnclear() 在 owner 不在当前时把
 * `deferredRefreshAfterSubmit: { intentsBefore: [] }`（空基线）写死；回到原板面后
 * recordNewIntents(new Set([]), batchKey) 把原板面的旧任务与本次提交的新任务一起
 * 记进本次提交批次（旧任务被改写到本次批次里，批量列表跟着串批）。
 *
 * 本块锁死：
 * 1. B 的提交回执丢失、在 A 显示时按身份查证成功 → 切回 B：服务端明确归属本次提交
 *    （intent.submissionId === acceptance.submissionId）的任务进本次批次；旧任务留在原批次
 *    （本地批次映射不被改写），且 submitBoard 只调用 1 次（后端只受理一次）；
 * 2. owner 当前时的查证刷新同样只把本次新任务记进本次批次（按服务端归属，而不是本地增量）。
 */
describe("一.1c 查证成功后按原提交归组（第十轮）", () => {
  /** 一件带服务端归属的任务（submissionId 是服务端写入的本次/历史提交 id）。 */
  function intentOfSubmission(id: string, boardId: string, submissionId: string | null) {
    return { ...pendingIntent(id, boardId), submissionId } as never;
  }

  /** 服务端按请求身份返回的受理记录（submissionId = 服务端生成的提交 id）。 */
  function acceptanceOf(clientRequestId: string, submissionId: string) {
    return {
      acceptance: {
        clientRequestId,
        submissionId,
        status: "succeeded",
        delivery: {
          delivered: true,
          reason: "",
          detail: "",
          understandingId: "u_new",
          status: "accepted",
        },
        understandingId: "u_new",
        understandingStatus: "accepted",
        createdAt: NOW,
      },
    } as never;
  }

  it("B 的回执丢失、在 A 显示时查证成功：切回 B 只把本次新任务记进本次批次，旧任务留在原批次", async () => {
    const oldIntent = intentOfSubmission("i_old", BOARD_B, "sub_old");
    const newIntent = intentOfSubmission("i_new", BOARD_B, "sub_new");
    // B 上已经加载过一件更早提交产生的旧任务（服务端归属 sub_old）
    let bIntents: unknown[] = [oldIntent];
    vi.mocked(api.fetchIntents).mockImplementation((async (id: string) => ({
      intents: String(id) === BOARD_B ? bIntents : [],
      conflicts: [],
      batchAvailable: false,
      recovery: { paused: [] },
    })) as never);

    await openABothLoaded();
    expect(store.intents.map((item) => item.id), "前提：B 的旧任务已加载").toEqual(["i_old"]);
    store.commit(editCard(store, "card_b", B_EDITED_TEXT), "改正文");

    const saveFlight = deferred<void>();
    vi.mocked(api.saveBoardState).mockImplementationOnce(async (_id, candidate) => {
      await saveFlight.promise;
      const snapshot = candidate as BoardState;
      return {
        ok: true,
        seq: B_SAVED_SEQ,
        savedAt: NOW,
        state: { ...snapshot, boardId: BOARD_B, seq: B_SAVED_SEQ },
      } as never;
    });
    const submitFlight = deferred<never>();
    vi.mocked(api.submitBoard).mockImplementationOnce(() => submitFlight.promise as never);
    const acceptanceFlight = deferred<never>();
    vi.mocked(api.fetchAcceptance).mockImplementationOnce(() => acceptanceFlight.promise as never);

    const saving = store.saveNow();
    const submitting = store.submit();
    await settle();
    // 挂起 B 的保存回执期间切到 A：提交回执丢失时 A 正在显示
    await store.selectBoard(BOARD_A);
    await settle();
    saveFlight.resolve();
    await saving;
    await settle();
    expect(submitCalls().length, "前提：B 的提交已经发出").toBe(1);
    const requestId = submitCalls()[0].clientRequestId;

    // 提交请求没有得到回执（没有 status → 结果未知）：按同一身份查证 B
    submitFlight.reject(new Error("网络中断，没有收到回执"));
    await settle();
    const lookups = acceptanceCalls();
    expect(lookups.length, "结果未知时没有按请求身份查证受理情况").toBe(1);
    expect(lookups[0].boardId, "受理查证发到了切板后的 A（应查发起提交的 B）").toBe(BOARD_B);
    expect(lookups[0].clientRequestId, "受理查证的身份不是这次提交的身份").toBe(requestId);
    expect(store.boardId, "前提：查证期间显示的是 A").toBe(BOARD_A);

    // 查证在 A 显示时成功；B 的清单里同时出现旧的与本次提交产生的任务
    bIntents = [oldIntent, newIntent];
    vi.mocked(approval.recordIntentBatch).mockClear();
    acceptanceFlight.resolve(acceptanceOf(requestId, "sub_new"));
    await submitting;
    await settle();

    expect(store.boardId, "查证结果把当前板面切走了").toBe(BOARD_A);
    expect(store.submitStatus, "B 的查证结果写到了 A 的提交状态上").toBe("idle");
    expect(store.lastAcceptance, "B 的查证结果出现在 A 上").toBeNull();

    // 切回 B：延后的提交后刷新按原提交归组。服务器事实已是提交后的版本 seq 8。
    vi.mocked(api.fetchBoardState).mockResolvedValue(
      boardPayload(boardState(BOARD_B, [textCard("card_b", B_EDITED_TEXT)], B_SAVED_SEQ)) as never,
    );
    await store.selectBoard(BOARD_B);
    await settle();

    const batches = vi.mocked(approval.recordIntentBatch).mock.calls;
    expect(batches, "本次查证后的刷新没有记录批次（或记录了多次）").toHaveLength(1);
    expect(
      batches[0][0],
      "批次键不是本次提交（查证恢复的 submissionId）",
    ).toBe("session:submission:sub_new");
    expect(batches[0][1], "本次批次里混进了 B 的旧任务（旧任务被改写到本次提交批次）").toEqual([
      "i_new",
    ]);
    const oldIntentNow = store.intents.find((item) => item.id === "i_old")!;
    expect(
      approval.batchKeyOf(oldIntentNow),
      "旧任务的批次被改写成本次提交（应留在它原来的 sub_old 批次）",
    ).toBe("submission:sub_old");
    expect(submitCalls(), "查证/刷新触发了重复提交（后端重复受理）").toHaveLength(1);
    expect(store.submitStatus, "切回 B 看不到查证恢复的受理结果").toBe("succeeded");
    expect(store.submitAcceptedByLookup, "切回 B 丢掉了「按身份查证确认受理」的事实").toBe(true);
  });

  it("owner 当前时查证刷新：也按服务端归属只记本次新任务，不把旧任务并进本次批次", async () => {
    const oldIntent = intentOfSubmission("i_old", BOARD_B, "sub_old");
    const newIntent = intentOfSubmission("i_new", BOARD_B, "sub_new");
    // B 的本地清单在提交时是空的（旧任务此前没读到）：只有在查证后的这次刷新里才出现
    let bIntents: unknown[] = [];
    vi.mocked(api.fetchIntents).mockImplementation((async (id: string) => ({
      intents: String(id) === BOARD_B ? bIntents : [],
      conflicts: [],
      batchAvailable: false,
      recovery: { paused: [] },
    })) as never);

    await openABothLoaded();
    store.commit(editCard(store, "card_b", B_EDITED_TEXT), "改正文");

    const submitFlight = deferred<never>();
    vi.mocked(api.submitBoard).mockImplementationOnce(() => submitFlight.promise as never);
    const acceptanceFlight = deferred<never>();
    vi.mocked(api.fetchAcceptance).mockImplementationOnce(() => acceptanceFlight.promise as never);

    const submitting = store.submit();
    await settle();
    expect(submitCalls().length, "前提：B 的提交已经发出").toBe(1);
    const requestId = submitCalls()[0].clientRequestId;

    submitFlight.reject(new Error("网络中断，没有收到回执"));
    await settle();
    expect(acceptanceCalls().length, "结果未知时没有按请求身份查证").toBe(1);

    // 查证成功时 B 仍是当前板面：清单里出现旧任务（此前未加载）与本次新任务
    bIntents = [oldIntent, newIntent];
    vi.mocked(api.fetchBoardState).mockResolvedValue(
      boardPayload(boardState(BOARD_B, [textCard("card_b", B_EDITED_TEXT)], B_SAVED_SEQ)) as never,
    );
    vi.mocked(approval.recordIntentBatch).mockClear();
    acceptanceFlight.resolve(acceptanceOf(requestId, "sub_new"));
    await submitting;
    await settle();

    const batches = vi.mocked(approval.recordIntentBatch).mock.calls;
    expect(batches, "查证后的刷新没有记录批次（或记录了多次）").toHaveLength(1);
    expect(batches[0][0], "批次键不是本次提交").toBe("session:submission:sub_new");
    expect(
      batches[0][1],
      "本次批次里混进了旧任务（没有按服务端归属只记本次新任务）",
    ).toEqual(["i_new"]);
    const oldIntentNow = store.intents.find((item) => item.id === "i_old")!;
    expect(
      approval.batchKeyOf(oldIntentNow),
      "旧任务的批次被改写成本次提交",
    ).toBe("submission:sub_old");
    expect(submitCalls(), "查证/刷新触发了重复提交（后端重复受理）").toHaveLength(1);
    expect(store.submitStatus, "查证成功后没有恢复受理结果").toBe("succeeded");
    expect(store.submitAcceptedByLookup, "没有记录「按身份查证确认受理」的事实").toBe(true);
  });
});
