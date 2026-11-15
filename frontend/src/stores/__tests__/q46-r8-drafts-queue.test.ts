/**
 * #46 第八轮回归（store 层）：草稿读取与保存队列（任务书第四、五、六项）。
 *
 *  第 4 项：迟到的读取不得把新草稿显示成旧稿
 *   - B 的第 1 版草稿读取挂起 → 切 A → 另一窗口把 B 推到第 2 版 → 切回 B 读到第 2 版 →
 *     放行第 1 版响应：界面必须仍是第 2 版正文，后续保存的 baseRev / 逐键版本也必须还是第 2 版；
 *   - 反向顺序：第 1 版响应在第二次读取落定之前到达（B → A → B）：不得把界面改成第 1 版；
 *   - 控制组：第 1 版响应在用户已经切到 A 时到达（被板面归属丢弃）；晚到响应尊重读取期间输入的新草稿。
 *
 *  第 5 项：一块板的保存失败不得堵住其他板的草稿
 *   - B 输入草稿（防抖中）→ 切 A 编辑 → A 的 saveDrafts 返回 503：
 *     B 的草稿仍会被发出（请求次数 / 板面），B 不永远停在 saving，失败只归 A 该版本。
 *
 *  第 6 项：冲突板不得卡住队列
 *   - B 制造同键双窗口冲突（两份文字都保留）→ C 输入草稿 → 防抖前切 A：
 *     C 的草稿仍会发出；冲突项不重复发；正常项各发一次；flushDrafts 可重复进入、不悬挂。
 *
 * 断言只落在 store 公开状态、本机恢复记录与真实 api.saveDrafts / api.fetchBoardState 的调用参数上。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { flushPromises } from "@vue/test-utils";
import { useInteractiveStore } from "../interactive";
import * as api from "../../services/interactive";
import * as attachments from "../../services/attachments";
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
const BOARD_C = "board_c";
const CARD_A = "card_a";
const CARD_B = "card_b";
const CARD_C = "card_c";
const KEY_A = "card:card_a";
const KEY_B = "card:card_b";
const KEY_C = "card:card_c";

type Store = ReturnType<typeof useInteractiveStore>;

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

interface DraftBook {
  drafts: Record<string, string>;
  rev: number;
  keyVersions: Record<string, number>;
}

function emptyDraftBook(): DraftBook {
  return { drafts: {}, rev: 0, keyVersions: {} };
}

function mutableBook(initial: DraftBook): DraftBook {
  return { drafts: { ...initial.drafts }, rev: initial.rev, keyVersions: { ...initial.keyVersions } };
}

function boardPayload(state: BoardState, book: DraftBook) {
  return {
    board: { id: state.boardId, title: state.boardId },
    state,
    seq: state.seq ?? 3,
    baseline: null,
    submissions: [],
    drafts: {
      drafts: { ...book.drafts },
      updatedAt: NOW,
      rev: book.rev,
      keyVersions: { ...book.keyVersions },
    },
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

async function settle(times = 8): Promise<void> {
  for (let i = 0; i < times; i += 1) {
    await flushPromises();
  }
}

/** 等草稿防抖（600ms）真的到点：队列必须真的有机会发请求。 */
async function waitForDraftDebounce(): Promise<void> {
  await new Promise((resolve) => setTimeout(resolve, 800));
  await settle();
}

/**
 * 第 6 项用假计时器推进防抖。
 *
 * 基线在「没有任何可发送项、同步退出」路径上会把 draftInFlight 留成一份**已完成**的
 * Promise（赋值晚于同步执行的 finally），之后每次 flushDrafts 都会在
 * `while (draftInFlight) await draftInFlight;` 里自旋 —— 浏览器卡死正是任务书里的复现现象。
 * 用假计时器把这条自旋限制在推进点，收尾时 clearAllTimers，避免它占住测试进程的事件循环。
 */
async function advanceDraftDebounce(): Promise<void> {
  await vi.advanceTimersByTimeAsync(700);
  await settle();
}

interface DraftPatch {
  removed?: string[];
  baseRev?: number | null;
  keyVersions?: Record<string, number>;
}

interface DraftCall {
  boardId: string;
  drafts: Record<string, string>;
  removed: string[];
  baseRev: number | null | undefined;
  keyVersions: Record<string, number> | undefined;
}

function draftCalls(): DraftCall[] {
  return vi.mocked(api.saveDrafts).mock.calls.map((call) => {
    const patch = (call[2] ?? {}) as DraftPatch;
    return {
      boardId: String(call[0]),
      drafts: { ...(call[1] as Record<string, string>) },
      removed: [...(patch.removed ?? [])],
      baseRev: patch.baseRev,
      keyVersions: patch.keyVersions,
    };
  });
}

function draftCallsFor(boardId: string): DraftCall[] {
  return draftCalls().filter((call) => call.boardId === boardId);
}

/** 服务端草稿存簿：把一次 saveDrafts 请求真实落库（含 removed 的版本守卫）。 */
function applyDraftSave(boardId: string, drafts: Record<string, string>, patch: DraftPatch) {
  const book = (books[boardId] ??= emptyDraftBook());
  const blocked: string[] = [];
  const cleared: string[] = [];
  for (const key of patch.removed ?? []) {
    const known = patch.keyVersions?.[key];
    const serverVersion = book.keyVersions[key];
    if (typeof known === "number" && typeof serverVersion === "number" && known < serverVersion) {
      blocked.push(key);
      continue;
    }
    if (Object.prototype.hasOwnProperty.call(book.drafts, key)) {
      delete book.drafts[key];
      cleared.push(key);
    }
  }
  book.rev += 1;
  for (const [key, text] of Object.entries(drafts)) {
    book.drafts[key] = text;
    book.keyVersions[key] = book.rev;
  }
  return {
    drafts: { ...book.drafts },
    updatedAt: NOW,
    rev: book.rev,
    keyVersions: { ...book.keyVersions },
    conflicts: [] as { key: string; local: string; server: string }[],
    cleared,
    blockedRemovals: blocked,
  };
}

let store: Store;
let boardStates: Record<string, BoardState>;
let books: Record<string, DraftBook>;

function seedBoard(boardId: string, cards: BoardState["cards"], book: DraftBook): void {
  boardStates[boardId] = boardState(boardId, cards, 3);
  books[boardId] = mutableBook(book);
}

function payloadFor(boardId: string) {
  return boardPayload(boardStates[boardId] ?? boardState(boardId, [], 3), books[boardId] ?? emptyDraftBook());
}

beforeEach(() => {
  localStorage.clear();
  vi.resetAllMocks();
  boardStates = {};
  books = {};
  setActivePinia(createPinia());
  store = useInteractiveStore();
  vi.mocked(api.fetchBoards).mockResolvedValue({
    boards: [
      { id: BOARD_A, title: "板面 A" },
      { id: BOARD_B, title: "板面 B" },
      { id: BOARD_C, title: "板面 C" },
    ],
  } as never);
  vi.mocked(api.fetchBoardState).mockImplementation((async (id: string) => payloadFor(String(id))) as never);
  vi.mocked(api.saveBoardState).mockImplementation((async (id: string, candidate: BoardState) => ({
    ok: true,
    seq: Number(candidate?.seq ?? 3) + 1,
    savedAt: NOW,
    state: { ...candidate, boardId: String(id) },
  })) as never);
  vi.mocked(api.saveDrafts).mockImplementation((async (
    id: string,
    drafts: Record<string, string>,
    patch: DraftPatch,
  ) => applyDraftSave(String(id), drafts, patch ?? {})) as never);
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
  // 先清掉本用例可能留下的防抖计时器 / 假计时器，再用真实计时器排空
  vi.clearAllTimers();
  vi.useRealTimers();
  await new Promise((resolve) => setTimeout(resolve, 800));
  await settle();
  localStorage.clear();
});

describe("#46 第八轮 · 第 4 项：迟到的读取不得把新草稿显示成旧稿", () => {
  it("4A 第 1 版响应在读到第 2 版之后才到：界面与后续保存版本都必须还是第 2 版", async () => {
    const cardA = textCard(CARD_A, "A 的正文");
    const cardB = textCard(CARD_B, "B 的正文");
    seedBoard(BOARD_A, [cardA], { drafts: {}, rev: 1, keyVersions: {} });
    seedBoard(BOARD_B, [cardB], { drafts: { [KEY_B]: "B 的草稿第 1 版" }, rev: 1, keyVersions: { [KEY_B]: 1 } });

    const firstBRead = deferred<never>();
    let stalePayload: unknown = null;
    let bReads = 0;
    vi.mocked(api.fetchBoardState).mockImplementation((async (id: string) => {
      const boardId = String(id);
      if (boardId === BOARD_B) {
        bReads += 1;
        if (bReads === 1) {
          // 第 1 版读取：内容就是此刻服务端的第 1 版，响应被扣住
          stalePayload = payloadFor(BOARD_B);
          return firstBRead.promise;
        }
      }
      return payloadFor(boardId);
    }) as never);

    const firstRead = store.selectBoard(BOARD_B);
    await settle();
    expect(bReads, "前提：B 的第 1 版读取没有发出").toBe(1);

    // 切到 A（第一次 B 读取还挂着）
    await store.selectBoard(BOARD_A);
    await settle();

    // 另一窗口把 B 的草稿推到第 2 版
    books[BOARD_B].drafts[KEY_B] = "B 的草稿第 2 版";
    books[BOARD_B].rev = 2;
    books[BOARD_B].keyVersions[KEY_B] = 2;

    // 切回 B，读到第 2 版
    await store.selectBoard(BOARD_B);
    await settle();
    expect(store.drafts[KEY_B], "前提：切回 B 没有读到第 2 版").toBe("B 的草稿第 2 版");

    // 放行迟到（更早代次）的第 1 版响应
    firstBRead.resolve(stalePayload as never);
    await firstRead;
    await settle();

    expect(store.drafts[KEY_B], "迟到的第 1 版读取把界面改回了旧稿（正文与版本没有成对应用）").toBe(
      "B 的草稿第 2 版",
    );

    // 用户接着编辑：这次保存必须用第 2 版的基线版本，而不是迟到的第 1 版
    store.setCardDraftInput(CARD_B, { text: "用户接着写的新草稿" });
    await store.flushDrafts();
    await settle();
    const bCalls = draftCallsFor(BOARD_B);
    expect(bCalls.length, "后续草稿保存没有发出").toBeGreaterThanOrEqual(1);
    const last = bCalls[bCalls.length - 1];
    expect(last.drafts[KEY_B], "后续保存发的不是用户的新草稿").toBe("用户接着写的新草稿");
    expect(last.baseRev, "后续保存用了迟到的旧基线版本").toBe(2);
    expect(last.keyVersions?.[KEY_B], "后续保存的逐键版本不是第 2 版（正文与版本不成对）").toBe(2);
  });

  it("4B 反向顺序：第 1 版响应在第二次读取落定前到达，界面不得先退回旧稿", async () => {
    const cardA = textCard(CARD_A, "A 的正文");
    const cardB = textCard(CARD_B, "B 的正文");
    seedBoard(BOARD_A, [cardA], { drafts: {}, rev: 1, keyVersions: {} });
    seedBoard(BOARD_B, [cardB], { drafts: { [KEY_B]: "B 的草稿第 1 版" }, rev: 1, keyVersions: { [KEY_B]: 1 } });

    const firstBRead = deferred<never>();
    const secondBRead = deferred<never>();
    let stalePayload: unknown = null;
    let bReads = 0;
    vi.mocked(api.fetchBoardState).mockImplementation((async (id: string) => {
      const boardId = String(id);
      if (boardId === BOARD_B) {
        bReads += 1;
        if (bReads === 1) {
          stalePayload = payloadFor(BOARD_B);
          return firstBRead.promise;
        }
        if (bReads === 2) return secondBRead.promise;
      }
      return payloadFor(boardId);
    }) as never);

    const firstRead = store.selectBoard(BOARD_B);
    await settle();
    await store.selectBoard(BOARD_A);
    await settle();
    books[BOARD_B].drafts[KEY_B] = "B 的草稿第 2 版";
    books[BOARD_B].rev = 2;
    books[BOARD_B].keyVersions[KEY_B] = 2;

    // 切回 B：第 2 版读取发出但还没落定
    const secondRead = store.selectBoard(BOARD_B);
    await settle();
    expect(bReads, "前提：切回 B 的第 2 次读取没有发出").toBe(2);

    // 更早的第 1 版响应先到
    firstBRead.resolve(stalePayload as never);
    await firstRead;
    await settle();
    expect(store.drafts[KEY_B], "更早的读取在第二次读取落定前把界面改成了旧稿").not.toBe("B 的草稿第 1 版");

    // 第 2 版读取落定：最终必须是第 2 版
    secondBRead.resolve(payloadFor(BOARD_B) as never);
    await secondRead;
    await settle();
    expect(store.drafts[KEY_B], "第二次读取落定后界面不是第 2 版").toBe("B 的草稿第 2 版");
  });

  it("4C 控制组：第 1 版响应在用户已经切到 A 时到达 → 按板面归属丢弃，回到 B 仍是第 2 版", async () => {
    const cardA = textCard(CARD_A, "A 的正文");
    const cardB = textCard(CARD_B, "B 的正文");
    seedBoard(BOARD_A, [cardA], { drafts: {}, rev: 1, keyVersions: {} });
    seedBoard(BOARD_B, [cardB], { drafts: { [KEY_B]: "B 的草稿第 1 版" }, rev: 1, keyVersions: { [KEY_B]: 1 } });

    const firstBRead = deferred<never>();
    let stalePayload: unknown = null;
    let bReads = 0;
    vi.mocked(api.fetchBoardState).mockImplementation((async (id: string) => {
      const boardId = String(id);
      if (boardId === BOARD_B) {
        bReads += 1;
        if (bReads === 1) {
          stalePayload = payloadFor(BOARD_B);
          return firstBRead.promise;
        }
      }
      return payloadFor(boardId);
    }) as never);

    const firstRead = store.selectBoard(BOARD_B);
    await settle();
    await store.selectBoard(BOARD_A);
    await settle();
    books[BOARD_B].drafts[KEY_B] = "B 的草稿第 2 版";
    books[BOARD_B].rev = 2;
    books[BOARD_B].keyVersions[KEY_B] = 2;

    // 旧响应到达时用户不在 B：必须按板面归属丢弃
    firstBRead.resolve(stalePayload as never);
    await firstRead;
    await settle();
    expect(store.drafts[KEY_B], "旧板面的迟到响应写进了当前板面 A").toBeUndefined();

    await store.selectBoard(BOARD_B);
    await settle();
    expect(store.drafts[KEY_B], "回到 B 后读到的不是第 2 版").toBe("B 的草稿第 2 版");
  });

  it("4D 控制组：晚到的读取响应尊重用户在读取期间输入的新草稿", async () => {
    const cardB = textCard(CARD_B, "B 的正文");
    seedBoard(BOARD_B, [cardB], { drafts: { [KEY_B]: "B 的旧服务器草稿" }, rev: 1, keyVersions: { [KEY_B]: 1 } });
    await store.selectBoard(BOARD_B);
    await settle();

    const refreshFlight = deferred<never>();
    vi.mocked(api.fetchBoardState).mockImplementationOnce(() => refreshFlight.promise as never);
    const refreshing = store.refreshBoardFromServer(BOARD_B);
    await settle();

    // 读取在飞期间用户输入了新草稿
    store.setCardDraftInput(CARD_B, { text: "读取期间输入的新草稿" });

    // 放行读取响应（内容仍是旧服务器草稿）
    refreshFlight.resolve(payloadFor(BOARD_B) as never);
    await refreshing;
    await settle();

    expect(store.drafts[KEY_B], "晚到的读取响应覆盖了用户读取期间输入的新草稿").toBe("读取期间输入的新草稿");
  });
});

describe("#46 第八轮 · 第 5 项：一块板的保存失败不得堵住其他板的草稿", () => {
  it("5A A 的草稿保存返回 503：B 的草稿仍会被发出，B 不永远停在 saving，失败只归 A", async () => {
    const cardA = textCard(CARD_A, "A 的正文");
    const cardB = textCard(CARD_B, "B 的正文");
    seedBoard(BOARD_A, [cardA], { drafts: {}, rev: 1, keyVersions: {} });
    seedBoard(BOARD_B, [cardB], { drafts: {}, rev: 1, keyVersions: {} });

    // 先在 B 输入草稿（防抖中），再切到 A 编辑
    await store.selectBoard(BOARD_B);
    store.setCardDraftInput(CARD_B, { text: "B 的草稿" });
    await store.selectBoard(BOARD_A);
    await settle();
    store.setCardDraftInput(CARD_A, { text: "A 的草稿" });
    await settle();

    vi.mocked(api.saveDrafts).mockImplementation((async (
      id: string,
      drafts: Record<string, string>,
      patch: DraftPatch,
    ) => {
      if (String(id) === BOARD_A) {
        const error = new Error("模拟草稿保存 503") as Error & { status: number };
        error.status = 503;
        throw error;
      }
      return applyDraftSave(String(id), drafts, patch ?? {});
    }) as never);

    await waitForDraftDebounce();

    const aCalls = draftCallsFor(BOARD_A);
    const bCalls = draftCallsFor(BOARD_B);
    expect(aCalls.length, "前提：A 的草稿保存没有发出").toBe(1);
    expect(
      bCalls.length,
      "A 板的草稿保存失败把 B 板还等在队列里的草稿堵住了（B 一直显示保存中、请求也没发）",
    ).toBeGreaterThanOrEqual(1);
    expect(bCalls[0].drafts[KEY_B], "B 的草稿保存发错了内容").toBe("B 的草稿");
    expect(store.draftStateFor(KEY_A).status, "A 的失败没有被如实标成 error").toBe("error");

    // 切回 B：B 不能永远停在「保存中」
    await store.selectBoard(BOARD_B);
    await settle();
    expect(store.draftStateFor(KEY_B).status, "B 的草稿永远停在 saving（失败被算到了 B 头上）").not.toBe("saving");
    expect(store.draftStateFor(KEY_B).status, "B 的草稿被算成了失败（失败只应归 A）").not.toBe("error");
    expect(store.drafts[KEY_B]).toBe("B 的草稿");
    expect(books[BOARD_B].drafts[KEY_B], "服务端没有收到 B 的草稿").toBe("B 的草稿");
  });
});

describe("#46 第八轮 · 第 6 项：冲突板不得卡住队列", () => {
  it("6A B 存在待选择的同键冲突时：C 的草稿仍会发出，冲突项不重复发，正常项各发一次", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    const cardA = textCard(CARD_A, "A 的正文");
    const cardB = textCard(CARD_B, "B 的正文");
    const cardC = textCard(CARD_C, "C 的正文");
    seedBoard(BOARD_A, [cardA], { drafts: {}, rev: 1, keyVersions: {} });
    seedBoard(BOARD_B, [cardB], { drafts: {}, rev: 1, keyVersions: {} });
    seedBoard(BOARD_C, [cardC], { drafts: {}, rev: 1, keyVersions: {} });

    // B 制造同键双窗口冲突：服务端保留另一份文字，两份都留下等用户选择
    const serverConflictText = "另一窗口写进 B 的文字";
    vi.mocked(api.saveDrafts).mockImplementation((async (
      id: string,
      drafts: Record<string, string>,
      patch: DraftPatch,
    ) => {
      if (String(id) === BOARD_B) {
        const book = (books[BOARD_B] ??= emptyDraftBook());
        book.rev += 1;
        book.drafts[KEY_B] = serverConflictText;
        book.keyVersions[KEY_B] = book.rev;
        return {
          drafts: { ...book.drafts },
          updatedAt: NOW,
          rev: book.rev,
          keyVersions: { ...book.keyVersions },
          conflicts: [{ key: KEY_B, local: drafts[KEY_B] ?? "", server: serverConflictText }],
          cleared: [],
          blockedRemovals: [],
        };
      }
      return applyDraftSave(String(id), drafts, patch ?? {});
    }) as never);

    await store.selectBoard(BOARD_B);
    store.setCardDraftInput(CARD_B, { text: "B 的本机候选" });
    await store.flushDrafts();
    await settle();
    const conflict = store.draftConflictFor(KEY_B);
    expect(conflict, "前提：B 的同键双窗口冲突没有建立").not.toBeNull();
    expect(conflict?.server, "前提：冲突里没有保留另一窗口的文字").toBe(serverConflictText);
    expect(store.draftStateFor(KEY_B).status).toBe("error");
    const bConflictCallsBefore = draftCallsFor(BOARD_B).filter((call) => call.drafts[KEY_B] === "B 的本机候选")
      .length;

    // 在 C 输入草稿，防抖前切到 A
    await store.selectBoard(BOARD_C);
    store.setCardDraftInput(CARD_C, { text: "C 的草稿" });
    await store.selectBoard(BOARD_A);
    await settle();

    await advanceDraftDebounce();

    const cCalls = draftCallsFor(BOARD_C);
    expect(
      cCalls.length,
      "B 板存在待选择的草稿冲突时，C 板的草稿保存被卡住（队列停在冲突板上）",
    ).toBeGreaterThanOrEqual(1);
    expect(cCalls[0].boardId).toBe(BOARD_C);
    expect(cCalls[0].drafts[KEY_C], "C 的草稿保存发错了内容").toBe("C 的草稿");
    expect(books[BOARD_C].drafts[KEY_C], "服务端没有收到 C 的草稿").toBe("C 的草稿");
    expect(
      draftCallsFor(BOARD_B).filter((call) => call.drafts[KEY_B] === "B 的本机候选").length,
      "冲突项被重复发给了服务器（应该停在等用户选择）",
    ).toBe(bConflictCallsBefore);
    expect(draftCallsFor(BOARD_C).length, "正常项被重复发送（应各发一次）").toBe(1);
  });

  it("6B 控制组：没有可发送项时重复进入 flushDrafts 不悬挂、不发请求", async () => {
    const cardA = textCard(CARD_A, "A 的正文");
    seedBoard(BOARD_A, [cardA], { drafts: {}, rev: 1, keyVersions: {} });
    await store.selectBoard(BOARD_A);
    await settle();
    expect(draftCallsFor(BOARD_A).length, "前提：没有待发送草稿").toBe(0);

    let finished = false;
    void (async () => {
      await store.flushDrafts();
      await store.flushDrafts();
      finished = true;
    })();
    await new Promise((resolve) => setTimeout(resolve, 300));
    expect(finished, "没有可发送项时重复进入 flushDrafts 悬挂").toBe(true);
    expect(draftCallsFor(BOARD_A).length, "没有可发送项却发出了草稿请求").toBe(0);
  });
});
