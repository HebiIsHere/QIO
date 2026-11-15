/**
 * #46 第七轮回归（store 层）：后台冲突也计入自动恢复上限。
 *
 * 覆盖任务书二项（只写测试，不改产品代码）：
 *
 *  C1. B 发起保存后**立刻切到 A**，让 409 在 A 显示期间到达（后台冲突）：
 *      - 每一轮 409 都必须计入 B 这一轮的自动恢复次数（不能因为「不在 B 上」就不算）；
 *      - 同一轮 PUT 总数 ≤ 4（首次 + 最多 3 次自动重试）；
 *      - 切板往返 / 重读 / 过防抖时间都不增加 PUT；
 *      - 回到 B：saveStatus=error、dirty=true、候选完整保留、没有待确认影响；
 *      - 用户点公开重试入口（saveNow）后只发 B 的 1 次 PUT 并成功。
 *
 *  C2. 前台不切板时序：同样最多 4 次 PUT，之后不再自动重发。
 *
 * 断言只落在 api.saveBoardState 的真实调用参数（boardId / seq / 候选卡片）与
 * store 公开状态（saveStatus / saveError / dirty / board / pendingImpact）上。
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

const NOW = "2026-11-14T00:00:00Z";
const BOARD_A = "board_a";
const BOARD_B = "board_b";

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

function boardState(boardId: string, cards: BoardState["cards"], seq: number): BoardState {
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

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

function apiError(status: number, payload: Record<string, unknown>): Error {
  return Object.assign(new Error(String(status)), { status, payload });
}

function liveCards(store: ReturnType<typeof useInteractiveStore>) {
  return (store.board?.cards ?? []).filter((card) => !card.deleted);
}

async function settle(times = 8): Promise<void> {
  for (let i = 0; i < times; i += 1) {
    await flushPromises();
  }
}

/** 等保存防抖（450ms）真的到点。 */
async function waitForDebouncedSave(): Promise<void> {
  // 保存防抖 450ms；留 200ms 余量即可，避免真实计时器把用例拖过默认超时
  await new Promise((resolve) => setTimeout(resolve, 650));
  await settle();
}

/** 每次 PUT 的「目标板面 + 候选 seq + 卡片集合」。 */
function putLog(): { boardId: string; seq: number; cardIds: string[] }[] {
  return vi.mocked(api.saveBoardState).mock.calls.map((call) => {
    const candidate = call[1] as BoardState;
    return {
      boardId: String(call[0]),
      seq: Number(candidate?.seq ?? 0),
      cardIds: (candidate?.cards ?? []).map((card) => card.id),
    };
  });
}

function putsFor(boardId: string) {
  return putLog().filter((item) => item.boardId === boardId);
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
  vi.mocked(api.saveDrafts).mockResolvedValue({ drafts: {}, updatedAt: NOW } as never);
  vi.mocked(api.fetchVisibleRange).mockResolvedValue({
    visibleRange: { cards: [], groups: [], links: [], selection: [], empty: true },
  } as never);
  vi.mocked(api.fetchIntents).mockResolvedValue({
    intents: [],
    conflicts: [],
    batchAvailable: false,
    recovery: { paused: [] },
  } as never);
  vi.mocked(api.fetchIntentViews).mockResolvedValue({ intents: [], source: "service" } as never);
  vi.mocked(api.fetchExecutions).mockResolvedValue({ executions: [] } as never);
  vi.mocked(api.fetchUnderstanding).mockResolvedValue({ understandings: [] } as never);
  vi.mocked(attachments.uploadAttachment).mockResolvedValue({} as never);
  vi.mocked(attachments.waitUntilSettled).mockResolvedValue({} as never);
  vi.mocked(attachments.registerSourceAttachment).mockResolvedValue({} as never);
});

afterEach(async () => {
  // 排空防抖计时器：不让上一个用例的保存请求落到下一个用例里
  await new Promise((resolve) => setTimeout(resolve, 800));
  await settle();
  vi.useRealTimers();
  localStorage.clear();
});

describe("#46 第七轮 C：后台冲突也计入自动恢复上限", () => {
  it("C1 B 保存后立刻切 A、409 在 A 上到达：每轮都计数，总 PUT ≤ 4，切板往返不增加，回 B 失败态保留候选，重试只发 B 一次", async () => {
    const cardA = textCard("card_a", "A 的内容");
    const cardB = textCard("card_b", "B 的内容");
    let serverSeq = 7;
    vi.mocked(api.fetchBoardState).mockImplementation((async (id: string) =>
      boardPayload(
        boardState(id, id === BOARD_A ? [cardA] : [cardB], id === BOARD_A ? 3 : serverSeq),
      )) as never);

    /** 每次 PUT 都挂起，由用例决定什么时候以 409 结束（这样才能精确制造「在 A 上收到 409」）。 */
    const flights: ReturnType<typeof deferred<never>>[] = [];
    vi.mocked(api.saveBoardState).mockImplementation((async () => {
      const flight = deferred<never>();
      flights.push(flight);
      return flight.promise;
    }) as never);

    await store.selectBoard(BOARD_A);
    const aStatus = store.saveStatus;
    await store.selectBoard(BOARD_B);

    store.commit(addCard(store.board!, { kind: "text", content: "B 新增的卡" }), "新增卡片");
    await waitForDebouncedSave();
    expect(putsFor(BOARD_B).length, "前提：B 的首次 PUT 已发出").toBe(1);

    // 每一轮：切到 A → 放行 409（后台冲突）→ 切回 B → 等防抖再发下一次
    for (let round = 1; round <= 4; round += 1) {
      await store.selectBoard(BOARD_A);
      await settle();
      expect(store.boardId, "前提：已经切到 A").toBe(BOARD_A);
      serverSeq += 1;
      flights[flights.length - 1].reject(
        apiError(409, {
          error: "stale_state",
          reason: "服务端已有更新的已保存版本",
          currentSeq: serverSeq,
        }),
      );
      await settle();
      // 后台冲突不许写进 A
      expect(store.saveStatus, "B 的后台冲突写进了 A 的保存状态").toBe(aStatus);
      expect(store.dirty, "B 的后台冲突把 A 标成了未保存").toBe(false);
      expect(putsFor(BOARD_A).length, "向 A 发出了 B 的保存").toBe(0);

      await store.selectBoard(BOARD_B);
      await settle();
      if (round < 4) {
        await waitForDebouncedSave();
      }
    }

    // 上限：首次 + 最多 3 次自动重试 = 4 次
    expect(
      putsFor(BOARD_B).length,
      "后台冲突没有计入自动恢复上限（PUT 超过 4 次）",
    ).toBe(4);
    expect(putsFor(BOARD_A).length, "向 A 发出了 B 的保存").toBe(0);

    // 达到上限后：切出切回 + 过防抖时间都不许再自动发请求
    await store.selectBoard(BOARD_A);
    await store.selectBoard(BOARD_B);
    await waitForDebouncedSave();
    await waitForDebouncedSave();
    expect(
      putsFor(BOARD_B).length,
      "达到上限后切板往返 / 重读又自动发了保存",
    ).toBe(4);

    // 回到 B：失败态、候选保留、没有待确认影响
    expect(store.boardId).toBe(BOARD_B);
    expect(store.saveStatus, "达到上限后没有退出保存中 / 失败态").toBe("error");
    expect(store.saveError, "没有给出可理解的上限原因").toContain("3 次");
    expect(store.dirty, "失败后候选被当成已保存").toBe(true);
    expect(liveCards(store).length, "原改动丢了").toBe(2);
    expect(store.pendingImpact).toBeNull();

    // 用户点公开重试入口：只发 B 的 1 次 PUT 并成功
    vi.mocked(api.saveBoardState).mockImplementation((async (id: string, candidate: BoardState) => ({
      ok: true,
      seq: Number(candidate?.seq ?? 0) + 1,
      savedAt: NOW,
      state: { ...candidate, boardId: String(id), seq: Number(candidate?.seq ?? 0) + 1 },
    })) as never);
    const beforeRetry = putsFor(BOARD_B).length;
    await store.saveNow();
    await settle();
    await waitForDebouncedSave();
    const afterRetry = putsFor(BOARD_B);
    expect(afterRetry.length - beforeRetry, "重试没有只发一次 B 的保存").toBe(1);
    expect(afterRetry[afterRetry.length - 1].boardId).toBe(BOARD_B);
    expect(putsFor(BOARD_A).length, "重试发到了 A").toBe(0);
    expect(store.saveStatus, "重试成功后没有回到已保存").toBe("saved");
    expect(store.dirty).toBe(false);
  }, 20000);

  it("C2 前台不切板时序：同样最多 4 次 PUT，之后不再自动重发", async () => {
    const cardA = textCard("card_a", "A 的内容");
    const cardB = textCard("card_b", "B 的内容");
    let serverSeq = 7;
    vi.mocked(api.fetchBoardState).mockImplementation((async (id: string) =>
      boardPayload(
        boardState(id, id === BOARD_A ? [cardA] : [cardB], id === BOARD_A ? 3 : serverSeq),
      )) as never);
    let failuresRemaining = 4;
    vi.mocked(api.saveBoardState).mockImplementation((async (id: string, candidate: BoardState) => {
      const state = candidate as BoardState;
      if (failuresRemaining > 0) {
        failuresRemaining -= 1;
        serverSeq += 1;
        throw apiError(409, {
          error: "stale_state",
          reason: "服务端已有更新的已保存版本",
          currentSeq: serverSeq,
        });
      }
      return {
        ok: true,
        seq: Number(state.seq ?? 0) + 1,
        savedAt: NOW,
        state: { ...state, boardId: String(id), seq: Number(state.seq ?? 0) + 1 },
      } as never;
    }) as never);

    await store.selectBoard(BOARD_A);
    await store.selectBoard(BOARD_B);

    vi.useFakeTimers();
    try {
      store.commit(addCard(store.board!, { kind: "text", content: "B 新增的卡" }), "新增卡片");
      await vi.advanceTimersByTimeAsync(4200);
      expect(putsFor(BOARD_B).length, "首次 + 自动重试不是 4 次").toBe(4);
      await vi.advanceTimersByTimeAsync(3000);
      expect(putsFor(BOARD_B).length, "达到上限后仍在自动重试").toBe(4);
      expect(store.saveStatus).toBe("error");
      expect(store.saveError).toContain("3 次");
      expect(store.dirty).toBe(true);
      expect(liveCards(store).length).toBe(2);
      expect(putsFor(BOARD_A).length, "向 A 发出了 B 的保存").toBe(0);
    } finally {
      vi.useRealTimers();
    }
  });
});