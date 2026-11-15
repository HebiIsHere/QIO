/**
 * #46 第八轮回归（任务书三.7 / 三.8）：**切板与撤回只接受对应操作的回执**。
 *
 * 三.7 反例（基线 c9fedc60）：从结果入口先打开 B（清单请求挂起）→ 再打开 A 并完成加载
 *   → 放行 B 的旧清单请求。旧 selectBoard() 在第一个 await 之后没有任何代次校验，
 *   于是把当前板面改回 B、再整块读入 B —— 用户刚选中的 A 被较早的请求覆盖。
 * 三.8 反例（基线 c9fedc60）：在 B 连续发起两次各自有回读的撤回，第二次回读挂起；
 *   放回第一次的成功回读。refreshBoardFromServer 的通用结算不带撤回代次，
 *   会把**较新的**撤回判成已确认；随后第二次真实失败的原因也就无处安放。
 *
 * 本文件锁死的行为（只通过 store 公开动作驱动）：
 * - 三.7：旧清单 / 旧整板读取的迟到响应不得覆盖较新的选择；交换两个响应的到达顺序同样成立；
 *   最新目标读取失败时错误只归这次选择，旧响应不能抢回页面；
 * - 三.8：同板旧撤回的成功回执不得结束新撤回的等待；新撤回失败后原因保留、可重试成功；
 *   B 的撤回回执在 A 打开时到达，A 的撤回等待不受影响。
 *
 * 标注：【状态/单元】真实 store；【模拟】仅网络响应的到达顺序与延迟（受控 promise）。
 * 断言只落在 store 公开状态 + 真实 API mock 的调用参数 / 次数。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { flushPromises } from "@vue/test-utils";
import { useInteractiveStore } from "../interactive";
import * as api from "../../services/interactive";
import type { BoardState } from "../../interactive/types";

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
const A_SERVER_SEQ = 3;
const B_SERVER_SEQ = 7;
const A_TEXT = "A 的正文";
const B_SERVER_TEXT = "B 的服务器原文";
const B_CANCELLED_TEXT = "被撤回掉的 B 新正文";
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

function aPayload() {
  return boardPayload(boardState(BOARD_A, [textCard("card_a", A_TEXT)], A_SERVER_SEQ));
}

function bPayload() {
  return boardPayload(boardState(BOARD_B, [textCard("card_b", B_SERVER_TEXT)], B_SERVER_SEQ));
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

function listBoards() {
  return {
    boards: [
      { id: BOARD_A, title: "板面 A" },
      { id: BOARD_B, title: "板面 B" },
    ],
  } as never;
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
  vi.mocked(api.fetchBoards).mockResolvedValue(listBoards());
  vi.mocked(api.fetchBoardState).mockImplementation((async (id: string) =>
    String(id) === BOARD_A ? aPayload() : bPayload()) as never);
  vi.mocked(api.saveBoardState).mockImplementation((async (id: string, candidate: BoardState) => {
    const nextSeq = Number(candidate?.seq ?? 3) + 1;
    return { ok: true, seq: nextSeq, savedAt: NOW, state: { ...candidate, boardId: id, seq: nextSeq } };
  }) as never);
  vi.mocked(api.saveDrafts).mockResolvedValue({ drafts: {}, updatedAt: NOW } as never);
  vi.mocked(api.fetchVisibleRange).mockResolvedValue({
    visibleRange: { cards: [], groups: [], links: [], selection: [], empty: true, notVisibleCount: 0 },
  } as never);
  vi.mocked(api.fetchIntents).mockResolvedValue({
    intents: [],
    conflicts: [],
    batchAvailable: false,
    recovery: { paused: [] },
  } as never);
  vi.mocked(api.fetchIntentViews).mockResolvedValue({ intents: [], source: "service" } as never);
  vi.mocked(api.fetchExecutions).mockResolvedValue({ executions: [] } as never);
  vi.mocked(api.fetchUnderstanding).mockResolvedValue({ understanding: null, understandings: [] } as never);
  vi.mocked(api.fetchAcceptance).mockResolvedValue({ acceptance: null } as never);
  vi.mocked(api.checkMaterialImpact).mockResolvedValue(cleanCheck(B_SERVER_SEQ) as never);
  vi.mocked(api.previewMaterialImpact).mockResolvedValue({ affected: [] } as never);
  vi.mocked(api.submitBoard).mockResolvedValue({ status: "empty" } as never);
});

afterEach(async () => {
  await new Promise((resolve) => setTimeout(resolve, 700));
  await settle();
  localStorage.clear();
});

/** 前提装置：B 上编辑出影响确认（保存被影响门拦下，形成可撤回的候选）。 */
async function armPendingImpactOnB() {
  await store.selectBoard(BOARD_A);
  vi.mocked(api.fetchIntents).mockImplementation((async (id: string) => ({
    intents: id === BOARD_B ? [runningIntent(id)] : [],
    conflicts: [],
    batchAvailable: false,
    recovery: { paused: [] },
  })) as never);
  await store.selectBoard(BOARD_B);
  vi.mocked(api.checkMaterialImpact).mockResolvedValueOnce(
    affectedCheck("chk_b", B_SERVER_SEQ) as never,
  );
  store.commit(editCard(store, "card_b", B_CANCELLED_TEXT), "改正文");
  await store.saveNow();
  await settle();
  expect(store.pendingImpact, "前提：B 出现了影响确认").not.toBeNull();
  expect(store.pendingImpact!.boardId).toBe(BOARD_B);
}

describe("三.7 较早的切板请求不得覆盖较新的选择", () => {
  it("先打开 B、再打开 A：B 的旧清单请求迟到，不得把当前板面与内容改回 B", async () => {
    // 先加载 A 作为「已经加载好的较新选择」目标
    await store.selectBoard(BOARD_A);
    await settle();
    const aCards = liveCards(store).map((card) => card.id);
    const aContents = liveCards(store).map((card) => card.content);
    const aSaveStatus = store.saveStatus;

    // 旧的选择：B 的清单请求挂起
    const oldList = deferred<never>();
    vi.mocked(api.fetchBoards).mockImplementationOnce(() => oldList.promise as never);
    const selectB = store.selectBoard(BOARD_B);
    await settle();
    expect(store.boardId, "前提：B 的清单还没回来，当前板面仍是 A").toBe(BOARD_A);

    // 新的选择：打开 A（清单立刻返回、整板读取成功）
    const selectA = await store.selectBoard(BOARD_A);
    await settle();
    expect(selectA.ok, "前提：较新的选择 A 加载成功").toBe(true);
    expect(store.boardId).toBe(BOARD_A);

    // 旧请求这时才回来
    oldList.resolve(listBoards());
    const bResult = await selectB;
    await settle();

    expect(store.boardId, "旧的切板请求把当前板面改回了 B（覆盖了较新的选择）").toBe(BOARD_A);
    expect(bResult.ok, "已经过期的切板请求仍报告成功").toBe(false);
    expect(store.board?.boardId, "当前板面内容不属于 A").toBe(BOARD_A);
    expect(liveCards(store).map((card) => card.id), "A 的板面内容被 B 的旧请求覆盖了").toEqual(aCards);
    expect(liveCards(store).map((card) => card.content), "A 的正文被 B 的旧请求覆盖了").toEqual(
      aContents,
    );
    expect(store.saveStatus, "A 的保存状态被 B 的旧请求覆盖了").toBe(aSaveStatus);
    expect(store.loadError, "旧切板请求给当前板面留下了读取错误").toBeNull();
  });

  it("交换到达顺序：A 的整板读取仍在飞时 B 的旧清单先到，A 仍必须最终胜出", async () => {
    await store.selectBoard(BOARD_A);
    await settle();

    const oldList = deferred<never>();
    vi.mocked(api.fetchBoards).mockImplementationOnce(() => oldList.promise as never);
    const selectB = store.selectBoard(BOARD_B);
    await settle();
    expect(store.boardId).toBe(BOARD_A);

    // 新的选择 A：清单成功，整板读取挂起
    const aRead = deferred<never>();
    vi.mocked(api.fetchBoardState).mockImplementationOnce(() => aRead.promise as never);
    const selectA = store.selectBoard(BOARD_A);
    await settle();
    expect(store.boardId, "前提：新的选择 A 已经开始读取").toBe(BOARD_A);

    // B 的旧清单先回来（A 的读取还没落定）
    oldList.resolve(listBoards());
    await settle();

    // A 的读取这时成功
    aRead.resolve(aPayload() as never);
    await selectA;
    await settle();

    expect(store.boardId, "B 的旧响应在 A 读取期间抢走了当前板面").toBe(BOARD_A);
    expect(store.board?.boardId, "最终板面内容不属于 A").toBe(BOARD_A);
    expect(liveCards(store).map((card) => card.content), "最终内容仍是 B（旧响应覆盖了 A）").toEqual([
      A_TEXT,
    ]);
    const bResult = await selectB;
    expect(bResult.ok, "已经过期的切板请求仍报告成功").toBe(false);
    expect(store.loadError, "旧切板请求给当前板面留下了读取错误").toBeNull();
  });

  it("最新目标 A 读取失败：错误只归这次选择，B 的旧响应迟到不能抢回页面", async () => {
    await store.selectBoard(BOARD_A);
    await settle();

    const oldList = deferred<never>();
    vi.mocked(api.fetchBoards).mockImplementationOnce(() => oldList.promise as never);
    const selectB = store.selectBoard(BOARD_B);
    await settle();

    // 新的选择 A 读取失败
    vi.mocked(api.fetchBoardState).mockImplementationOnce(async () => {
      throw apiError(503, "A 板读取失败：服务端 503");
    });
    const selectA = await store.selectBoard(BOARD_A);
    await settle();
    expect(selectA.ok, "前提：较新的选择 A 确实读取失败").toBe(false);
    expect(store.boardId).toBe(BOARD_A);
    expect(String(store.loadError ?? ""), "A 的读取失败原因没有留下").toContain("503");

    // B 的旧清单这时才回来（成功）：不许抢回页面，也不许清掉 A 自己的错误
    oldList.resolve(listBoards());
    const bResult = await selectB;
    await settle();

    expect(store.boardId, "B 的旧响应在 A 读取失败后抢回了页面").toBe(BOARD_A);
    expect(bResult.ok, "过期的切板请求在 A 失败后报告成功").toBe(false);
    expect(String(store.loadError ?? ""), "A 的读取失败原因被 B 的旧响应清掉了").toContain("503");
    expect(store.board?.boardId, "当前板面内容不属于 A").toBe(BOARD_A);
    expect(liveCards(store).map((card) => card.content), "A 的内容被 B 的旧响应覆盖了").toEqual([
      A_TEXT,
    ]);
  });
});

describe("三.8 同板面的旧撤回回执不得结束新撤回", () => {
  it("B 连续两次撤回：第一次成功回执迟到不关闭第二次等待；第二次失败原因保留且可重试", async () => {
    await armPendingImpactOnB();

    // 第一次撤回：回读挂起
    const firstRead = deferred<never>();
    const secondRead = deferred<never>();
    vi.mocked(api.fetchBoardState)
      .mockImplementationOnce(() => firstRead.promise as never)
      .mockImplementationOnce(() => secondRead.promise as never);

    const firstCancel = store.cancelImpact();
    await settle();
    expect(store.cancelRecovery?.boardId, "第一次撤回的恢复状态不属于 B").toBe(BOARD_B);
    expect(store.cancelRecovery?.active, "前提：第一次撤回在等待服务器确认").toBe(true);

    // 第二次撤回（同一块板面）：自己的回读挂起
    const secondCancel = store.cancelImpact();
    await settle();
    expect(store.cancelRecovery?.boardId).toBe(BOARD_B);
    expect(store.cancelRecovery?.active, "前提：第二次撤回也在等待服务器确认").toBe(true);

    // 第一次的成功回执迟到：不得结束第二次的等待
    firstRead.resolve(bPayload() as never);
    await firstCancel;
    await settle();
    expect(
      store.cancelRecovery?.active,
      "较早的撤回成功回执结束了较新的撤回（新撤回被提前判成已确认）",
    ).toBe(true);
    expect(store.cancelRecovery?.reason, "较早的成功回执给新撤回写入了失败原因").toBeNull();

    // 第二次真实失败：如实保留原因
    secondRead.reject(apiError(502, "第二次撤回读取失败：服务端 502"));
    await secondCancel;
    await settle();
    expect(store.cancelRecovery?.active, "第二次撤回失败后却宣称已确认").toBe(true);
    expect(String(store.cancelRecovery?.reason ?? ""), "第二次撤回的失败原因没有保留").toContain(
      "502",
    );

    // 页面上的「重新核对」入口：重试成功后才收敛
    vi.mocked(api.fetchBoardState).mockResolvedValue(bPayload() as never);
    const retried = await store.retryCancelRecovery();
    expect(retried.ok, "重试入口没有取到服务器最新事实").toBe(true);
    expect(store.cancelRecovery?.active, "重试成功后撤回仍显示未被确认").toBe(false);
    expect(store.cancelRecovery?.reason, "重试成功后仍留着失败原因").toBeNull();

    /**
     * 同一用例的后半段（任务书三.8 验收第三句）：B 的撤回回执在 A 打开时到达 ——
     * A 的撤回等待不受影响。这一段在基线上单独跑是绿的（refreshBoardFromServer 对
     * 别的板面的回执有板面身份保护），所以作为上面同一缺陷的跨板护栏放在同一用例里，
     * 不单独计为一条基线红用例。
     */
    // B 上再准备一次撤回（回读挂起）
    vi.mocked(api.checkMaterialImpact).mockResolvedValueOnce(
      affectedCheck("chk_b_again", B_SERVER_SEQ) as never,
    );
    store.commit(editCard(store, "card_b", "第二次被撤回的正文"), "改正文");
    await store.saveNow();
    await settle();
    expect(store.pendingImpact?.boardId, "前提：B 又出现了影响确认").toBe(BOARD_B);
    const bRead = deferred<never>();
    vi.mocked(api.fetchBoardState).mockImplementationOnce(() => bRead.promise as never);
    const bCancel = store.cancelImpact();
    await settle();
    expect(store.cancelRecovery?.boardId).toBe(BOARD_B);
    expect(store.cancelRecovery?.active).toBe(true);

    // 切到 A，并在 A 上开始一次撤回（回读挂起）
    await store.selectBoard(BOARD_A);
    await settle();
    const aRead = deferred<never>();
    vi.mocked(api.fetchBoardState).mockImplementationOnce(() => aRead.promise as never);
    const aCancel = store.cancelImpact();
    await settle();
    expect(store.cancelRecovery?.boardId, "前提：A 自己的撤回状态已建立").toBe(BOARD_A);
    expect(store.cancelRecovery?.active).toBe(true);

    // B 的撤回回执在 A 打开时到达
    bRead.resolve(bPayload() as never);
    await bCancel;
    await settle();
    expect(store.boardId).toBe(BOARD_A);
    expect(store.cancelRecovery?.boardId, "B 的迟到撤回回执改写了 A 的撤回状态").toBe(BOARD_A);
    expect(store.cancelRecovery?.active, "B 的迟到撤回回执结束了 A 的撤回等待").toBe(true);

    // A 自己的回读成功：A 的撤回收敛
    aRead.resolve(aPayload() as never);
    await aCancel;
    await settle();
    expect(store.cancelRecovery?.active, "A 自己的回读成功后撤回仍未确认").toBe(false);
    expect(store.cancelRecovery?.reason).toBeNull();
    expect(store.boardId).toBe(BOARD_A);
  });
});
