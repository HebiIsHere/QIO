/**
 * #46 第七轮（任务书 三.7）：**任务清单必须按板面归属**。
 *
 * 已证实的残留缺陷（基线 0dbc387f）：
 * - `loadIntents()` 读取失败时按 F11 保留「最后一次成功读到的清单」——这条规则本身对，
 *   但它没有区分「这份清单属于哪块板面」。B 载入过运行中任务后切到 A，A 的首次读取失败，
 *   A 于是**显示着 B 的任务**（`intents` / `activeIntents` 都还是 B 的）；
 * - 更严重的是 `saveNow()` 的影响预判门用的是全局 `activeIntents`：
 *   A 上的一次普通编辑会因为 **B 的运行中任务**去发一次影响预判（目标板面 A），
 *   服务端一旦按这份错的清单返回受影响任务，A 的普通保存就被 B 的任务拦住。
 *
 * 本文件锁死的行为（全部走 store 的公开动作，断言落在真实 API 参数与公开状态上）：
 * 1. A 的 intents / intentViews 不使用 B 的清单（为空 + 进入 A 自己的失败状态）；
 * 2. A 的普通编辑保存**不发**影响预判（checkMaterialImpact / previewMaterialImpact 0 次），
 *    PUT 只指向 A 且成功；
 * 3. 同板刷新失败保留同板旧数据（F11 既有口径，防回归）；
 * 4. 晚到的整板读取 / 任务清单不覆盖当前板数据与保存错误。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { flushPromises } from "@vue/test-utils";
import { useInteractiveStore } from "../interactive";
import * as api from "../../services/interactive";
import * as attachments from "../../services/attachments";
import { addCard } from "../../interactive/board";
import type { BoardState } from "../../interactive/types";

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
  fetchBoards: vi.fn(),
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

vi.mock("../../services/attachments", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../services/attachments")>();
  return {
    ...actual,
    uploadAttachment: vi.fn(),
    waitUntilSettled: vi.fn(),
    registerSourceAttachment: vi.fn(),
  };
});

const NOW = "2026-11-15T00:00:00Z";
const BOARD_A = "board_a";
const BOARD_B = "board_b";
const TASK_B = "task_b_running";

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

function boardState(boardId: string, cards: BoardState["cards"] = [], seq = 3): BoardState {
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

function emptySnapshot() {
  return { cards: [], groups: [], links: [], selection: [], empty: true };
}

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

/** B 板上的运行中任务（服务端事实）。 */
function runningIntentB(): never {
  return {
    id: TASK_B,
    boardId: BOARD_B,
    submissionId: null,
    title: "B 的运行中任务",
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
  } as never;
}

function intentViewB(): never {
  return {
    intent_id: TASK_B,
    board_id: BOARD_B,
    version: 1,
    status: "running",
    title: "B 的运行中任务",
    created_at: NOW,
    updated_at: NOW,
  } as never;
}

function intentsPayload(list: unknown[]) {
  return { intents: list, conflicts: [], batchAvailable: false, recovery: { paused: [] } };
}

function viewsPayload(list: unknown[]) {
  return { available: true, source: "service", intents: list };
}

function cleanCheck(stateVersion = 3): never {
  return { ok: true, stateVersion, affected: [], impactConfirmationRequired: false } as never;
}

function affectedCheck(checkId: string, stateVersion = 3): never {
  return {
    ok: true,
    checkId,
    stateVersion,
    affected: [
      { intentId: TASK_B, title: "B 的运行中任务", materials: ["card_b"], consequence: "暂停并保留进度" },
    ],
    impactConfirmationRequired: true,
  } as never;
}

/** 每次影响预判的「目标板面 + 依据版本 + 候选卡片」。 */
function impactChecks(): { boardId: string; stateVersion: number; cardIds: string[] }[] {
  return vi.mocked(api.checkMaterialImpact).mock.calls.map((call) => {
    const candidate = call[2] as BoardState;
    return {
      boardId: String(call[0]),
      stateVersion: Number(call[1]),
      cardIds: (candidate?.cards ?? []).map((card) => card.id),
    };
  });
}

/** 每次 PUT 的「目标板面 + 卡片 id 集合」。 */
function puts(): { boardId: string; cardIds: string[] }[] {
  return vi.mocked(api.saveBoardState).mock.calls.map((call) => {
    const candidate = call[1] as BoardState;
    return {
      boardId: String(call[0]),
      cardIds: (candidate?.cards ?? []).map((card) => card.id),
    };
  });
}

function liveCards(store: ReturnType<typeof useInteractiveStore>) {
  return (store.board?.cards ?? []).filter((card) => !card.deleted);
}

async function settle(times = 8): Promise<void> {
  for (let i = 0; i < times; i += 1) {
    await flushPromises();
  }
}

/** 等防抖保存真的发出（store 的防抖是 450ms）。 */
async function waitForDebouncedSave(): Promise<void> {
  await new Promise((resolve) => setTimeout(resolve, 600));
  await settle();
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
      id === BOARD_B ? boardState(BOARD_B, [textCard("card_b", "B 的内容")]) : boardState(BOARD_A, []),
    )) as never);
  vi.mocked(api.saveBoardState).mockImplementation((async (_id: string, candidate: BoardState) => ({
    ok: true,
    seq: Number(candidate?.seq ?? 3) + 1,
    savedAt: NOW,
    state: candidate,
  })) as never);
  vi.mocked(api.saveDrafts).mockResolvedValue({ drafts: {}, updatedAt: NOW } as never);
  vi.mocked(api.fetchVisibleRange).mockResolvedValue({ visibleRange: emptySnapshot() } as never);
  vi.mocked(api.fetchIntentViews).mockResolvedValue(viewsPayload([]) as never);
  vi.mocked(api.fetchIntents).mockResolvedValue(intentsPayload([]) as never);
  vi.mocked(api.fetchExecutions).mockResolvedValue({ executions: [] } as never);
  vi.mocked(api.fetchUnderstanding).mockResolvedValue({ understandings: [] } as never);
  vi.mocked(api.checkMaterialImpact).mockResolvedValue(cleanCheck() as never);
  vi.mocked(attachments.uploadAttachment).mockImplementation((async (upload: File) => ({
    id: "att_" + upload.name,
    name: upload.name,
    sizeBytes: 16,
    kind: "copy",
    display: "已保存副本",
    state: "ready",
    error: null,
  })) as never);
  vi.mocked(attachments.waitUntilSettled).mockImplementation((async (item: never) => item) as never);
});

afterEach(() => {
  vi.useRealTimers();
  localStorage.clear();
});

/** B 载入「有运行中任务」的板面；A 的清单读取失败。 */
async function loadBWithRunningTaskThenSwitchToA(): Promise<void> {
  vi.mocked(api.fetchIntents).mockImplementation((async (id: string) => {
    if (id === BOARD_B) return intentsPayload([runningIntentB()]);
    throw new Error("任务清单读取失败：网络断了");
  }) as never);
  vi.mocked(api.fetchIntentViews).mockImplementation((async (id: string) => {
    if (id === BOARD_B) return viewsPayload([intentViewB()]);
    throw new Error("意图视图读取失败：网络断了");
  }) as never);

  await store.selectBoard(BOARD_B);
  await settle();
  expect(store.boardId).toBe(BOARD_B);
  expect(store.intents.map((item) => item.id), "前提：B 已载入运行中任务").toEqual([TASK_B]);
  expect(store.intentViews.map((view) => String(view.intent_id))).toEqual([TASK_B]);

  await store.selectBoard(BOARD_A);
  await settle();
  expect(store.boardId).toBe(BOARD_A);
}

describe("#46 R7 三.7 任务清单按板面归属", () => {
  it("B 有运行中任务、A 首次读取失败：A 的 intents / intentViews 不使用 B 的清单", async () => {
    await loadBWithRunningTaskThenSwitchToA();

    expect(
      store.intents.map((item) => item.id),
      "A 显示了 B 的任务清单（任务清单没有按板面归属）",
    ).toEqual([]);
    expect(
      store.intentViews.map((view) => String(view.intent_id)),
      "A 显示了 B 的意图视图（意图视图没有按板面归属）",
    ).toEqual([]);
    expect(store.activeIntents.map((item) => item.id), "A 的 activeIntents 里还有 B 的运行中任务").toEqual([]);
    expect(store.intentsLoadState, "A 的清单读取失败没有如实标成 error").toBe("error");
    expect(store.intentsLoadError ?? "", "A 看不到自己这次读取失败的原因").toContain("网络断了");
    expect(store.intentViewsLoadState, "A 的意图视图读取失败没有如实标成 error").toBe("error");
  });

  it("B 有运行中任务、A 首次读取失败：A 的普通保存不发影响预判，PUT 只指向 A 且成功", async () => {
    await loadBWithRunningTaskThenSwitchToA();

    const before = impactChecks().length;
    store.commit(addCard(store.board!, { kind: "text", content: "A 的普通改动" }), "新增卡片");
    await waitForDebouncedSave();

    expect(
      impactChecks().length - before,
      "A 的普通保存因为 B 的运行中任务发了影响预判：" + JSON.stringify(impactChecks()),
    ).toBe(0);
    expect(
      vi.mocked(api.previewMaterialImpact).mock.calls.length,
      "A 的普通保存走了旧的影响预判接口（同样是被 B 的任务触发的）",
    ).toBe(0);

    const aPuts = puts().filter((item) => item.boardId === BOARD_A);
    expect(aPuts.length, "A 的普通保存没有发出 PUT").toBe(1);
    expect(puts().filter((item) => item.boardId === BOARD_B).length, "A 的保存写到了 B").toBe(0);
    expect(aPuts[0].cardIds, "A 的 PUT 候选里混进了 B 的卡片").not.toContain("card_b");
    expect(store.saveStatus, "A 的普通保存没有成功").toBe("saved");
    expect(store.saveError, "A 的保存失败原因不为空").toBeNull();
    expect(store.pendingImpact, "A 上出现了不属于它的影响确认").toBeNull();
    expect(liveCards(store).map((card) => card.id), "A 的卡片被改动").toHaveLength(1);
  });

  it("A 自己真的有运行中任务时，影响预判照常发往 A（隔离不等于关掉预判）", async () => {
    // 前提：A 的清单读取成功且含运行中任务
    vi.mocked(api.fetchIntents).mockResolvedValue(intentsPayload([runningIntentB()]) as never);
    vi.mocked(api.fetchIntentViews).mockResolvedValue(viewsPayload([intentViewB()]) as never);
    await store.selectBoard(BOARD_A);
    await settle();
    expect(store.activeIntents.map((item) => item.id), "前提：A 载入了运行中任务").toEqual([TASK_B]);

    vi.mocked(api.checkMaterialImpact).mockResolvedValueOnce(affectedCheck("chk_a", 3) as never);
    store.commit(addCard(store.board!, { kind: "text", content: "A 的改动" }), "新增卡片");
    await waitForDebouncedSave();

    const checks = impactChecks();
    expect(checks.length, "A 有真实任务时没有做影响预判").toBe(1);
    expect(checks[0].boardId, "影响预判没有发往 A").toBe(BOARD_A);
    expect(puts().length, "影响确认还没决定就发出了 PUT").toBe(0);
    expect(store.pendingImpact, "没有形成可操作的确认").not.toBeNull();
    expect(store.pendingImpact!.boardId).toBe(BOARD_A);
    expect(store.pendingImpact!.checkId).toBe("chk_a");
  });

  it("同板刷新失败保留同板旧数据（F11 口径不被本轮隔离改坏）", async () => {
    vi.mocked(api.fetchIntents).mockResolvedValue(intentsPayload([runningIntentB()]) as never);
    vi.mocked(api.fetchIntentViews).mockResolvedValue(viewsPayload([intentViewB()]) as never);
    await store.selectBoard(BOARD_A);
    await settle();
    expect(store.intents.map((item) => item.id)).toEqual([TASK_B]);

    // 同一块板面刷新失败：保留旧数据，只标刷新失败
    vi.mocked(api.fetchIntents).mockRejectedValue(new Error("刷新失败：网络断了"));
    await store.loadIntents();
    await settle();

    expect(store.intents.map((item) => item.id), "同板刷新失败把同板旧数据清掉了").toEqual([TASK_B]);
    expect(store.intentsLoadState, "同板刷新失败没有标明").toBe("error");
    expect(store.intentsLoadError ?? "").toContain("网络断了");
    expect(store.boardId).toBe(BOARD_A);
  });

  it("晚到的整板读取与任务清单不覆盖当前板数据与保存错误", async () => {
    // B 的整板读取挂起
    const readingB = deferred<never>();
    vi.mocked(api.fetchBoardState).mockImplementationOnce(() => readingB.promise as never);
    const selectingB = store.selectBoard(BOARD_B);
    await settle();
    expect(store.boardId, "前提：boardId 已经是 B").toBe(BOARD_B);

    // 切到 A 并成功读取
    await store.selectBoard(BOARD_A);
    await settle();
    expect(store.boardId).toBe(BOARD_A);
    const aCards = liveCards(store).map((card) => card.id);

    // A 上保存失败：错误归 A
    vi.mocked(api.saveBoardState).mockRejectedValueOnce(
      Object.assign(new Error("保存失败：服务端拒绝"), { status: 500 }) as never,
    );
    store.commit(addCard(store.board!, { kind: "text", content: "A 的改动" }), "新增卡片");
    await waitForDebouncedSave();
    expect(store.saveStatus, "前提：A 的保存失败").toBe("error");
    const aError = store.saveError;

    // B 的整板读取此刻才返回：不许写进 A
    readingB.resolve(boardPayload(boardState(BOARD_B, [textCard("card_b", "B 的内容")], 9)) as never);
    await selectingB;
    await settle();

    expect(store.boardId).toBe(BOARD_A);
    expect(store.saveStatus, "B 的迟到读取改写了 A 的保存状态").toBe("error");
    expect(store.saveError, "B 的迟到读取改写了 A 的保存错误").toBe(aError);
    expect(
      liveCards(store).map((card) => card.id),
      "B 的迟到读取把 B 的卡片写进了 A",
    ).not.toContain("card_b");
    expect(liveCards(store).map((card) => card.id).length, "A 的候选被 B 的迟到读取覆盖").toBe(
      aCards.length + 1,
    );
  });
});
