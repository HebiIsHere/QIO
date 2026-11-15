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
