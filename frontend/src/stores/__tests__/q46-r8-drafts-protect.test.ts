/**
 * #46 第八轮回归（store 层）：草稿清除保护（任务书第二、三项）。
 *
 * 覆盖两条「旧操作不得删掉更新的草稿 / 撤销后不得再清」的缺陷：
 *
 *  第 2 项：旧正式保存不得删除另一标签页的新恢复副本
 *   - 同一浏览器 localStorage + 两个独立 pinia（= 两个真实标签页）：
 *     标签页 1 完成编辑并发出正式保存（PUT 挂起）；
 *     标签页 2 输入新草稿、还没同步到后端；
 *     放行标签页 1 的保存成功回执 → 标签页 2 的本机恢复副本必须仍是新文字、版本未被推进/删除，
 *     重新打开编辑器仍能恢复新文字（基线会把它写成 kind=cleared 的空记录）；
 *   - 新稿已同步：标签页 2 已成功保存草稿（服务端 rev 推进），标签页 1 的旧回执到达后
 *     不得再对该键发 removed，服务端上更新的草稿仍在（可变 server book 校验）。
 *
 *  第 3 项：撤销正式编辑时保留对应草稿
 *   - 完成编辑 → 正式保存挂起 → 撤销 → 放行旧回执 / 再发一次无关保存成功：
 *     不得对该键发 removed，本机恢复副本仍在，服务端草稿仍在；
 *   - 删除卡片后撤销删除：同一规则（删除回执不能证明这次编辑仍然有效）；
 *   - 控制组：删除写入失败时草稿与登记都保留；正常完成编辑且正式内容落库时 removed 会发、
 *     本机记录被清（基线即绿，属回归护栏）。
 *
 * 断言只落在 store 公开状态、本机恢复记录（readCardLocalDraft）与真实 api.saveDrafts /
 * api.saveBoardState 的调用参数上；不碰任何内部 Map / 变量名。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { flushPromises } from "@vue/test-utils";
import { useInteractiveStore } from "../interactive";
import * as api from "../../services/interactive";
import * as attachments from "../../services/attachments";
import { cardLocalDraftStorageKey, readCardLocalDraft, writeCardLocalDraft } from "../../interactive/drafts";
import { removeCard, updateCard } from "../../interactive/board";
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
const CARD_B = "card_b";
const CARD_OTHER = "card_other";
const CARD_A = "card_a";
const KEY_B = "card:card_b";
const KEY_OTHER = "card:card_other";
const KEY_A = "card:card_a";

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

/** 一块板面的服务端草稿存簿（rev + 逐键版本，契约 C1）。 */
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

/** 等草稿防抖（600ms）真的到点：清除请求必须真的有机会发出去。 */
async function waitForDraftDebounce(): Promise<void> {
  await new Promise((resolve) => setTimeout(resolve, 800));
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
    // 真实后端：请求消费的版本低于服务端当前版本时拒绝这次删除（另一窗口已写入更新版本）
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

/** 新建一个「标签页」：独立 pinia + 独立 store 实例；localStorage 是同一个 jsdom 存储。 */
function createTab(): Store {
  setActivePinia(createPinia());
  return useInteractiveStore();
}

function seedBoard(boardId: string, cards: BoardState["cards"], book: DraftBook): void {
  boardStates[boardId] = boardState(boardId, cards, 3);
  books[boardId] = mutableBook(book);
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
    ],
  } as never);
  vi.mocked(api.fetchBoardState).mockImplementation((async (id: string) => {
    const boardId = String(id);
    return boardPayload(boardStates[boardId] ?? boardState(boardId, [], 3), books[boardId] ?? emptyDraftBook());
  }) as never);
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
  // 排空防抖计时器：不让上一个用例的草稿请求落到下一个用例里
  await new Promise((resolve) => setTimeout(resolve, 800));
  await settle();
  localStorage.clear();
});

describe("#46 第八轮 · 第 2 项：旧正式保存不得删除另一标签页的新恢复副本", () => {
  it("2A 另一标签页的新稿还没同步：旧回执落定后本机副本仍是新文字，重新打开编辑器仍能恢复", async () => {
    const cardB = textCard(CARD_B, "B 的旧正文");
    seedBoard(BOARD_B, [cardB], { drafts: { [KEY_B]: "B 的旧服务器草稿" }, rev: 3, keyVersions: { [KEY_B]: 3 } });

    // —— 标签页 1：完成编辑，正式保存发出但回执暂扣 ——
    const tab1 = createTab();
    await tab1.selectBoard(BOARD_B);
    expect(tab1.drafts[KEY_B], "前提：标签页 1 没有读到 B 的服务器草稿").toBe("B 的旧服务器草稿");
    tab1.commit(updateCard(tab1.board!, CARD_B, { content: "B 的正式内容" }), "完成编辑");
    tab1.requestDraftClear(KEY_B);
    expect(tab1.pendingDraftClearKeys, "前提：清除登记没有生效").toContain(KEY_B);
    const putFlight = deferred<never>();
    vi.mocked(api.saveBoardState).mockImplementationOnce(() => putFlight.promise as never);
    const saving1 = tab1.saveNow();
    await settle();
    expect(vi.mocked(api.saveBoardState).mock.calls.length, "前提：标签页 1 的正式保存已发出").toBe(1);

    // —— 标签页 2：同一 localStorage 里写下新恢复副本，但还没同步 ——
    const tab2 = createTab();
    await tab2.selectBoard(BOARD_B);
    const tab2Text = "标签页 2 还没同步的新文字";
    tab2.setCardDraftInput(CARD_B, { text: tab2Text });
    const tab2Record = readCardLocalDraft(CARD_B);
    expect(tab2Record?.text, "前提：标签页 2 的本机恢复副本没有写下").toBe(tab2Text);
    const tab2Version = tab2Record?.version;
    // 标签页 2 的草稿同步请求暂不返回：它确实「还没同步到后端」
    const tab2Sync = deferred<never>();
    vi.mocked(api.saveDrafts).mockImplementation((async (
      id: string,
      drafts: Record<string, string>,
      patch: DraftPatch,
    ) => {
      if (String(id) === BOARD_B && drafts[KEY_B] === tab2Text) return tab2Sync.promise;
      return applyDraftSave(String(id), drafts, patch ?? {});
    }) as never);

    // —— 放行标签页 1 的保存成功回执 ——
    putFlight.resolve({
      ok: true,
      seq: 4,
      savedAt: NOW,
      state: boardState(BOARD_B, [{ ...cardB, content: "B 的正式内容" }], 4),
    } as never);
    await saving1;
    await settle();

    // 标签页 2 的本机恢复副本必须原样保留（内容 + 版本 + 记录种类）
    const stored = readCardLocalDraft(CARD_B);
    expect(stored?.text, "标签页 1 的旧正式保存回执把标签页 2 还没同步的新恢复副本删掉了").toBe(tab2Text);
    expect(stored?.kind, "标签页 2 的新恢复副本被写成了「待清除」依据（重开就恢复不出新文字）").toBe("draft");
    expect(stored?.version, "标签页 2 的新恢复副本版本被旧回执推进/覆盖了").toBe(tab2Version);
    expect(
      localStorage.getItem(cardLocalDraftStorageKey(CARD_B)) ?? "",
      "localStorage 里标签页 2 的新文字已经不存在",
    ).toContain(tab2Text);

    // 标签页 2 重新打开编辑器：仍能恢复新文字（而不是旧服务器草稿）
    const reopened = createTab();
    await reopened.selectBoard(BOARD_B);
    expect(reopened.drafts[KEY_B], "重新打开编辑器恢复不到标签页 2 的新文字").toBe(tab2Text);
  });

  it("2B 另一标签页的新稿已同步：旧回执的删除请求被后端逐键版本守卫拒绝，服务端更新的草稿仍在", async () => {
    const cardB = textCard(CARD_B, "B 的旧正文");
    seedBoard(BOARD_B, [cardB], { drafts: { [KEY_B]: "B 的旧服务器草稿" }, rev: 3, keyVersions: { [KEY_B]: 3 } });

    const tab1 = createTab();
    await tab1.selectBoard(BOARD_B);
    tab1.commit(updateCard(tab1.board!, CARD_B, { content: "B 的正式内容" }), "完成编辑");
    tab1.requestDraftClear(KEY_B);
    const putFlight = deferred<never>();
    vi.mocked(api.saveBoardState).mockImplementationOnce(() => putFlight.promise as never);
    const saving1 = tab1.saveNow();
    await settle();
    expect(vi.mocked(api.saveBoardState).mock.calls.length, "前提：标签页 1 的正式保存已发出").toBe(1);

    // 标签页 2：写下新稿并成功同步到服务端（rev 推到 4 / 该键版本 4）
    const tab2 = createTab();
    await tab2.selectBoard(BOARD_B);
    const tab2Text = "标签页 2 已同步的新文字";
    tab2.setCardDraftInput(CARD_B, { text: tab2Text });
    await tab2.flushDrafts();
    await settle();
    expect(books[BOARD_B].drafts[KEY_B], "前提：标签页 2 的新稿没有同步到服务端").toBe(tab2Text);
    const serverVersionAfterTab2 = books[BOARD_B].keyVersions[KEY_B];
    expect(serverVersionAfterTab2, "前提：服务端逐键版本没有推进").toBeGreaterThan(3);

    // 放行标签页 1 的旧回执，并让清除请求真的有机会发出
    putFlight.resolve({
      ok: true,
      seq: 4,
      savedAt: NOW,
      state: boardState(BOARD_B, [{ ...cardB, content: "B 的正式内容" }], 4),
    } as never);
    await saving1;
    await settle();
    await waitForDraftDebounce();

    /**
     * 第九轮已删除跨标签页同步标记：本页不再知道另一标签页刚同步过，可能按自己读到过的旧版本（3）
     * 发一次 removed —— 保护由**后端逐键版本守卫**承担：请求消费的版本低于服务端当前版本，
     * 服务端拒绝删除并回报 blockedRemovals。断言：服务端更新的草稿与版本都还在；
     * 这次删除没有被服务端确认，界面不得显示成已删除。
     */
    expect(books[BOARD_B].drafts[KEY_B], "服务端上更新的草稿被旧回执清掉了").toBe(tab2Text);
    expect(books[BOARD_B].keyVersions[KEY_B], "服务端更新的草稿版本被旧回执消费/推进").toBe(serverVersionAfterTab2);
    const removedCalls = draftCallsFor(BOARD_B).filter((call) => call.removed.includes(KEY_B));
    expect(
      removedCalls.length,
      "旧回执没有发出删除请求：无法验证后端逐键版本守卫真的拒绝了这次删除",
    ).toBeGreaterThan(0);
    for (const call of removedCalls) {
      expect(
        call.keyVersions?.[KEY_B] ?? 0,
        "删除请求没有带本页已知的旧版本（无法证明后端是因为版本更旧而拒绝）",
      ).toBeLessThan(serverVersionAfterTab2 as number);
    }
    expect(
      tab1.draftRemovalStateFor(KEY_B).status,
      "后端拒绝这次删除后，界面不能显示成已删除 / 无事可做",
    ).toBe("error");
  });
});

describe("#46 第八轮 · 第 3 项：撤销正式编辑时保留对应草稿", () => {
  it("3A 完成编辑后撤销：旧回执与随后无关保存成功都不得清掉对应草稿", async () => {
    const cardB = textCard(CARD_B, "B 的旧正文");
    const cardOther = textCard(CARD_OTHER, "另一张卡");
    seedBoard(BOARD_B, [cardB, cardOther], {
      drafts: { [KEY_B]: "B 的旧服务器草稿" },
      rev: 3,
      keyVersions: { [KEY_B]: 3 },
    });
    const draftText = "完成编辑时还没落库的草稿文字";

    store = createTab();
    await store.selectBoard(BOARD_B);
    store.setCardDraftInput(CARD_B, { text: draftText });
    // 与组件一致：先提交正式变更（完成编辑），再登记清除
    store.commit(updateCard(store.board!, CARD_B, { content: "正式编辑后的文字" }), "完成编辑");
    store.requestDraftClear(KEY_B);

    // 正式保存已经发出但还没回执
    const putFlight = deferred<never>();
    vi.mocked(api.saveBoardState).mockImplementationOnce(() => putFlight.promise as never);
    const saving = store.saveNow();
    await settle();
    expect(vi.mocked(api.saveBoardState).mock.calls.length, "前提：正式保存已发出").toBe(1);

    // 回执到达前用户撤销（正文恢复成旧值）
    store.undo();
    await settle();
    const reverted = store.board!.cards.find((card) => card.id === CARD_B);
    expect(reverted?.content, "前提：撤销没有把正文恢复成旧值").toBe("B 的旧正文");
    putFlight.resolve({
      ok: true,
      seq: 4,
      savedAt: NOW,
      state: boardState(BOARD_B, [{ ...cardB, content: "正式编辑后的文字" }, cardOther], 4),
    } as never);
    await saving;
    await settle();

    // 再做一次无关的板面修改并保存成功（这张卡的编辑并没有生效）
    store.commit(updateCard(store.board!, CARD_OTHER, { x: 40 }), "移动卡片");
    await store.saveNow();
    await settle();
    // 让可能登记的清除真的有机会发出请求（不等待：只推一次队列）
    store.flushDrafts();
    await settle();

    const removedCalls = draftCallsFor(BOARD_B).filter((call) => call.removed.includes(KEY_B));
    expect(removedCalls.length, "撤销后的无关保存成功仍向服务器发起了针对该草稿的 removed").toBe(0);
    const stored = readCardLocalDraft(CARD_B);
    const serverDraft = books[BOARD_B].drafts[KEY_B];
    const draftStillExists = (stored?.kind === "draft" && stored.text === draftText) || serverDraft === draftText;
    expect(
      draftStillExists,
      "撤销正式编辑后对应草稿丢了（本机恢复副本与服务端草稿都没有保留它）",
    ).toBe(true);
    expect(
      Object.prototype.hasOwnProperty.call(books[BOARD_B].drafts, KEY_B),
      "撤销正式编辑后服务端上的草稿键被删掉了（应该保留并继续走原有的同步/冲突语义）",
    ).toBe(true);
  });

  it("3B 删除卡片后撤销删除：删除回执不得清掉这张卡的草稿", async () => {
    const cardB = textCard(CARD_B, "B 的旧正文");
    const cardOther = textCard(CARD_OTHER, "另一张卡");
    seedBoard(BOARD_B, [cardB, cardOther], {
      drafts: { [KEY_B]: "B 的旧服务器草稿" },
      rev: 3,
      keyVersions: { [KEY_B]: 3 },
    });
    const draftText = "删除前编辑器里的草稿文字";

    store = createTab();
    await store.selectBoard(BOARD_B);
    store.setCardDraftInput(CARD_B, { text: draftText });
    // 与 BoardCanvas.onCardRemove 一致：先删除卡片，再登记这张卡的草稿清除
    store.commit(removeCard(store.board!, CARD_B), "删除卡片");
    store.requestDraftClear(KEY_B);
    expect(store.pendingDraftClearKeys, "前提：删除时的清除登记没有生效").toContain(KEY_B);

    const putFlight = deferred<never>();
    vi.mocked(api.saveBoardState).mockImplementationOnce(() => putFlight.promise as never);
    const saving = store.saveNow();
    await settle();
    expect(vi.mocked(api.saveBoardState).mock.calls.length, "前提：删除的正式保存已发出").toBe(1);

    // 用户撤销删除：卡片又回来了
    store.undo();
    await settle();
    expect(
      store.board!.cards.some((card) => card.id === CARD_B),
      "前提：撤销删除没有把卡片恢复回来",
    ).toBe(true);
    putFlight.resolve({
      ok: true,
      seq: 4,
      savedAt: NOW,
      state: boardState(BOARD_B, [cardOther], 4),
    } as never);
    await saving;
    await settle();

    // 再做一次无关修改并保存成功
    store.commit(updateCard(store.board!, CARD_OTHER, { x: 40 }), "移动卡片");
    await store.saveNow();
    await settle();
    store.flushDrafts();
    await settle();

    const removedCalls = draftCallsFor(BOARD_B).filter((call) => call.removed.includes(KEY_B));
    expect(removedCalls.length, "撤销删除后，删除回执/无关保存成功仍清掉了这张卡的草稿").toBe(0);
    const stored = readCardLocalDraft(CARD_B);
    const serverDraft = books[BOARD_B].drafts[KEY_B];
    const draftStillExists = (stored?.kind === "draft" && stored.text === draftText) || serverDraft === draftText;
    expect(draftStillExists, "撤销删除后对应草稿丢了（本机恢复副本与服务端草稿都没有保留它）").toBe(true);
    expect(
      Object.prototype.hasOwnProperty.call(books[BOARD_B].drafts, KEY_B),
      "撤销删除后服务端上的草稿键被删掉了（应该保留并继续走原有的同步/冲突语义）",
    ).toBe(true);
  });

  it("3C 控制组：删除写入失败时草稿与清除登记都保留（等重试）", async () => {
    const cardB = textCard(CARD_B, "B 的旧正文");
    seedBoard(BOARD_B, [cardB], { drafts: { [KEY_B]: "B 的旧服务器草稿" }, rev: 3, keyVersions: { [KEY_B]: 3 } });
    const draftText = "删除前编辑器里的草稿文字";

    store = createTab();
    await store.selectBoard(BOARD_B);
    store.setCardDraftInput(CARD_B, { text: draftText });
    store.commit(removeCard(store.board!, CARD_B), "删除卡片");
    store.requestDraftClear(KEY_B);
    vi.mocked(api.saveBoardState).mockRejectedValueOnce(new Error("模拟删除写入失败") as never);
    await store.saveNow();
    await settle();

    expect(store.pendingDraftClearKeys, "删除写入失败后清除登记丢了（重试入口也就没了）").toContain(KEY_B);
    expect(store.drafts[KEY_B], "删除写入失败后内存里的草稿被清掉了").toBe(draftText);
    expect(readCardLocalDraft(CARD_B)?.text, "删除写入失败后本机恢复副本被清掉了").toBe(draftText);
    expect(
      draftCallsFor(BOARD_B).filter((call) => call.removed.includes(KEY_B)).length,
      "删除还没写成功就向服务器发了 removed",
    ).toBe(0);
  });

  it("3D 控制组：正常完成编辑且正式内容落库时，该键的 removed 会发、本机记录被清", async () => {
    const cardB = textCard(CARD_B, "B 的旧正文");
    seedBoard(BOARD_B, [cardB], { drafts: { [KEY_B]: "B 的旧服务器草稿" }, rev: 3, keyVersions: { [KEY_B]: 3 } });
    const draftText = "完成编辑前的草稿文字";

    store = createTab();
    await store.selectBoard(BOARD_B);
    store.setCardDraftInput(CARD_B, { text: draftText });
    store.commit(updateCard(store.board!, CARD_B, { content: "正式编辑后的文字" }), "完成编辑");
    store.requestDraftClear(KEY_B);
    await store.saveNow();
    await settle();
    expect(store.pendingDraftClearKeys, "正式内容落库后清除登记没有被消化").not.toContain(KEY_B);

    await store.flushDrafts();
    await settle();

    const removedCalls = draftCallsFor(BOARD_B).filter((call) => call.removed.includes(KEY_B));
    expect(removedCalls.length, "正常完成编辑且落库后没有向服务器发 removed").toBeGreaterThan(0);
    expect(store.drafts[KEY_B], "正常完成后内存里的草稿没有清掉").toBeUndefined();
    expect(readCardLocalDraft(CARD_B), "正常完成后本机恢复副本没有清掉").toBeNull();
    expect(books[BOARD_B].drafts[KEY_B], "正常完成后服务端上的旧草稿没有清掉").toBeUndefined();
  });
});


/**
 * #46 第九轮：删除跨标签页草稿同步标记（共享 localStorage 的写 / 读 / 清除判定）。
 *
 * 旧实现每次草稿同步成功都把「逐键版本 + 正文」写进共享标记，清理旧草稿前先读它判断
 * 「另一标签页是否已经同步过更新的版本」。该机制已整体移除：
 * - 初始化草稿存储时只幂等清掉遗留的 `qio.draft.sync-marker.v1`（不动其它任何键）；
 * - 同步成功不再写标记；
 * - 「不误删另一标签页的新恢复副本」改由本机记录身份重读（expectLocal）+ 后端逐键版本守卫承担。
 *
 * 断言只看 store 公开状态、本机记录与真实 api.saveDrafts / saveBoardState 调用参数。
 */
describe("#46 第九轮 · 移除跨标签页草稿标记并清理旧键", () => {
  const MARKER_KEY = "qio.draft.sync-marker.v1";

  it("4A 初始化草稿存储时只清旧标记键：旧标记消失，真实未同步草稿与正式内容都保留", async () => {
    const cardB = textCard(CARD_B, "B 的正式正文");
    seedBoard(BOARD_B, [cardB], { drafts: { [KEY_B]: "B 的旧服务器草稿" }, rev: 3, keyVersions: { [KEY_B]: 3 } });
    // 旧标记：带一段可识别的旧正文（第九轮以前写下的共享数据）
    const markerText = "旧标记里的可识别旧正文-不得恢复";
    localStorage.setItem(MARKER_KEY, JSON.stringify({ [BOARD_B]: { [KEY_B]: { version: 4, text: markerText } } }));
    // 真实未同步草稿：只在本机的恢复副本（不能因为清旧键被删 / 被覆盖 / 被旧标记正文顶掉）
    const localText = "真实未同步草稿（本机恢复记录）";
    const localWritten = writeCardLocalDraft(CARD_B, localText, { boardId: BOARD_B, seq: 1 });
    expect(localWritten.ok, "前提：真实未同步草稿没有写进本机").toBe(true);
    const localVersion = readCardLocalDraft(CARD_B)?.version;
    // 一个无关的草稿键：清旧键只能动标记本身
    localStorage.setItem("qio.draft.chat.topic-a", JSON.stringify({ text: "聊天草稿要保留", updatedAt: 1, seq: 1 }));

    // 初始化 store = 初始化草稿存储：旧标记必须被清掉
    const fresh = createTab();
    expect(localStorage.getItem(MARKER_KEY), "初始化后旧版跨标签页标记键仍然存在").toBeNull();
    // 只动这一个键：真实未同步草稿与无关草稿都还在
    expect(readCardLocalDraft(CARD_B)?.text, "初始化清旧键时把真实未同步草稿删了").toBe(localText);
    expect(readCardLocalDraft(CARD_B)?.version, "初始化清旧键时推进/覆盖了真实未同步草稿的版本").toBe(localVersion);
    expect(
      localStorage.getItem("qio.draft.chat.topic-a") ?? "",
      "初始化清旧键时误删/改动了无关草稿键",
    ).toContain("聊天草稿要保留");

    // 正式内容与本机恢复记录按原样恢复；旧标记里的正文一个字都不许出现
    await fresh.selectBoard(BOARD_B);
    expect(fresh.drafts[KEY_B], "真实未同步草稿没有被恢复").toBe(localText);
    expect(Object.values(fresh.drafts).join("|"), "旧标记里的正文被当成草稿恢复了").not.toContain(markerText);
    expect(
      fresh.board?.cards.find((card) => card.id === CARD_B)?.content,
      "正式内容被旧标记改写了",
    ).toBe("B 的正式正文");
    expect(books[BOARD_B].drafts[KEY_B], "初始化/恢复不得改动服务器上的正式草稿事实").toBe("B 的旧服务器草稿");
  });

  it("4B 草稿同步成功后不再写入旧标记键", async () => {
    const cardB = textCard(CARD_B, "B 的正式正文");
    seedBoard(BOARD_B, [cardB], { drafts: {}, rev: 3, keyVersions: {} });
    const syncedText = "同步到服务端的草稿";

    store = createTab();
    await store.selectBoard(BOARD_B);
    store.setCardDraftInput(CARD_B, { text: syncedText });
    await store.flushDrafts();
    await settle();
    expect(books[BOARD_B].drafts[KEY_B], "前提：草稿没有同步到服务端").toBe(syncedText);
    expect(localStorage.getItem(MARKER_KEY), "草稿同步成功后仍在写入旧版跨标签页标记键").toBeNull();

    // 同一窗口再同步一版也不能写
    store.setCardDraftInput(CARD_B, { text: syncedText + "（第二版）" });
    await store.flushDrafts();
    await settle();
    expect(books[BOARD_B].drafts[KEY_B]).toBe(syncedText + "（第二版）");
    expect(localStorage.getItem(MARKER_KEY), "第二次同步又写入了旧标记键").toBeNull();
  });

  it("4C 同一窗口内旧保存响应延迟时重新编辑同一张卡：旧回执不得清掉新草稿", async () => {
    const cardB = textCard(CARD_B, "B 的旧正文");
    seedBoard(BOARD_B, [cardB], { drafts: { [KEY_B]: "B 的旧服务器草稿" }, rev: 3, keyVersions: { [KEY_B]: 3 } });

    store = createTab();
    await store.selectBoard(BOARD_B);
    store.setCardDraftInput(CARD_B, { text: "完成编辑前的第一版草稿" });
    store.commit(updateCard(store.board!, CARD_B, { content: "正式编辑后的文字" }), "完成编辑");
    store.requestDraftClear(KEY_B);

    const putFlight = deferred<never>();
    vi.mocked(api.saveBoardState).mockImplementationOnce(() => putFlight.promise as never);
    const saving = store.saveNow();
    await settle();
    expect(vi.mocked(api.saveBoardState).mock.calls.length, "前提：正式保存已发出").toBe(1);

    // 回执到达前用户又编辑同一张卡（同一窗口内的迟到回执）
    const retyped = "回执到达前重新编辑的新草稿";
    store.setCardDraftInput(CARD_B, { text: retyped });

    putFlight.resolve({
      ok: true,
      seq: 4,
      savedAt: NOW,
      state: boardState(BOARD_B, [{ ...cardB, content: "正式编辑后的文字" }], 4),
    } as never);
    await saving;
    await settle();

    expect(store.drafts[KEY_B], "迟到的旧保存回执清掉了重新编辑的新草稿").toBe(retyped);
    expect(readCardLocalDraft(CARD_B)?.text, "本机恢复副本被旧回执清掉了").toBe(retyped);
    expect(store.pendingDraftClearKeys, "又编辑了新版本，旧清除登记却还在").not.toContain(KEY_B);
    await store.flushDrafts();
    await settle();
    const removedCalls = draftCallsFor(BOARD_B).filter((call) => call.removed.includes(KEY_B));
    expect(removedCalls.length, "重新编辑后仍对同一草稿键发起了 removed").toBe(0);
  });

  it("4D 本机清除写入失败后重试：旧草稿保留，补写 cleared 依据后才完成清除", async () => {
    const cardB = textCard(CARD_B, "B 的旧正文");
    seedBoard(BOARD_B, [cardB], { drafts: { [KEY_B]: "B 的旧服务器草稿" }, rev: 3, keyVersions: { [KEY_B]: 3 } });
    const draftText = "完成编辑前还没同步的草稿";

    store = createTab();
    await store.selectBoard(BOARD_B);
    store.setCardDraftInput(CARD_B, { text: draftText });
    store.commit(updateCard(store.board!, CARD_B, { content: "正式编辑后的文字" }), "完成编辑");
    store.requestDraftClear(KEY_B);
    expect(readCardLocalDraft(CARD_B)?.kind, "前提：本机应有一条编辑草稿").toBe("draft");

    // 让「写 cleared 依据」这一步真的失败（本机存储写入失败）
    const cardLocalKey = cardLocalDraftStorageKey(CARD_B);
    const originalSetItem = Storage.prototype.setItem;
    const setItemSpy = vi.spyOn(Storage.prototype, "setItem").mockImplementation((key: string, value: string) => {
      if (key === cardLocalKey) throw new DOMException("模拟本机存储写入失败", "QuotaExceededError");
      originalSetItem.call(localStorage, key, value);
    });
    try {
      await store.saveNow();
      await settle();
    } finally {
      setItemSpy.mockRestore();
    }

    // 本机那一步失败必须是可见失败，不能把磁盘上仍是草稿的记录当作已清掉
    expect(readCardLocalDraft(CARD_B)?.kind, "本机清除写失败后磁盘上的编辑草稿不见了").toBe("draft");
    expect(readCardLocalDraft(CARD_B)?.text, "本机清除写失败后旧草稿正文丢了").toBe(draftText);
    expect(store.draftLocalStates[KEY_B]?.ok, "本机清除写失败没有如实显示").toBe(false);
    expect(store.draftRemovalStateFor(KEY_B).status, "本机清除写失败后界面不能显示成等待服务器确认").toBe("error");

    // 用户重试：先补写 cleared 依据（幂等），再由服务器确认清除
    await store.retryDraftSave(KEY_B);
    await settle();
    const afterRetry = readCardLocalDraft(CARD_B);
    expect(
      afterRetry === null || afterRetry.kind === "cleared",
      "重试没有补写 cleared 依据（重开后会当成编辑草稿把旧正文恢复出来）",
    ).toBe(true);
    expect(afterRetry?.text ?? "", "重试后磁盘上还留着旧草稿正文").toBe("");
    expect(store.draftLocalStates[KEY_B]?.ok, "重试补写成功后本机状态仍是失败").toBe(true);
    expect(store.draftRemovalStateFor(KEY_B).status, "服务器确认清除后删除状态没有收敛").not.toBe("error");
    expect(books[BOARD_B].drafts[KEY_B], "重试后服务端上的旧草稿没有被清除").toBeUndefined();
  });

  it("4E 正式保存等待期间另一张卡写入草稿：旧回执只处理自己的键，另一张卡的草稿保留", async () => {
    const cardB = textCard(CARD_B, "B 的旧正文");
    const cardOther = textCard(CARD_OTHER, "另一张卡");
    seedBoard(BOARD_B, [cardB, cardOther], {
      drafts: { [KEY_B]: "B 的旧服务器草稿" },
      rev: 3,
      keyVersions: { [KEY_B]: 3 },
    });

    store = createTab();
    await store.selectBoard(BOARD_B);
    store.setCardDraftInput(CARD_B, { text: "B 完成编辑前的草稿" });
    store.commit(updateCard(store.board!, CARD_B, { content: "正式编辑后的文字" }), "完成编辑");
    store.requestDraftClear(KEY_B);
    const putFlight = deferred<never>();
    vi.mocked(api.saveBoardState).mockImplementationOnce(() => putFlight.promise as never);
    const saving = store.saveNow();
    await settle();
    expect(vi.mocked(api.saveBoardState).mock.calls.length, "前提：正式保存已发出").toBe(1);

    // 另一个标签页同时为另一张卡写入未同步草稿
    const tab2 = createTab();
    await tab2.selectBoard(BOARD_B);
    const otherText = "另一张卡还没同步的草稿";
    tab2.setCardDraftInput(CARD_OTHER, { text: otherText });
    expect(readCardLocalDraft(CARD_OTHER)?.text, "前提：另一张卡的本机恢复副本没有写下").toBe(otherText);

    putFlight.resolve({
      ok: true,
      seq: 4,
      savedAt: NOW,
      state: boardState(BOARD_B, [{ ...cardB, content: "正式编辑后的文字" }, cardOther], 4),
    } as never);
    await saving;
    await settle();

    expect(readCardLocalDraft(CARD_OTHER)?.text, "旧回执把另一张卡的本机恢复副本清掉了").toBe(otherText);
    expect(readCardLocalDraft(CARD_OTHER)?.kind, "另一张卡的草稿被写成了「待清除」依据").toBe("draft");
    expect(
      draftCallsFor(BOARD_B).filter((call) => call.removed.includes(KEY_OTHER)).length,
      "旧回执对另一张卡的草稿键发起了 removed",
    ).toBe(0);
    expect(store.drafts[KEY_OTHER], "当前 store 出现了另一张卡的草稿（串板）").toBeUndefined();
  });

  it("4F 正式保存等待期间切板：回执按原板清理，不碰当前板面的草稿与正文", async () => {
    const cardA = textCard(CARD_A, "A 的正式正文");
    const cardB = textCard(CARD_B, "B 的旧正文");
    seedBoard(BOARD_A, [cardA], { drafts: {}, rev: 3, keyVersions: {} });
    seedBoard(BOARD_B, [cardB], { drafts: { [KEY_B]: "B 的旧服务器草稿" }, rev: 3, keyVersions: { [KEY_B]: 3 } });

    store = createTab();
    await store.selectBoard(BOARD_B);
    store.setCardDraftInput(CARD_B, { text: "B 完成编辑前的草稿" });
    store.commit(updateCard(store.board!, CARD_B, { content: "正式编辑后的文字" }), "完成编辑");
    store.requestDraftClear(KEY_B);
    const putFlight = deferred<never>();
    vi.mocked(api.saveBoardState).mockImplementationOnce(() => putFlight.promise as never);
    const saving = store.saveNow();
    await settle();

    // 保存还在等待时就切到 A，并在 A 上继续写草稿
    await store.selectBoard(BOARD_A);
    const textA = "A 的草稿";
    store.setCardDraftInput(CARD_A, { text: textA });

    putFlight.resolve({
      ok: true,
      seq: 4,
      savedAt: NOW,
      state: boardState(BOARD_B, [{ ...cardB, content: "正式编辑后的文字" }], 4),
    } as never);
    await saving;
    await settle();

    expect(store.boardId, "回执把当前板面切走了").toBe(BOARD_A);
    expect(store.drafts[KEY_A], "B 的旧保存回执清掉了 A 的草稿").toBe(textA);
    expect(readCardLocalDraft(CARD_A)?.text, "B 的旧保存回执改动了 A 的本机恢复副本").toBe(textA);
    expect(store.board?.cards.find((card) => card.id === CARD_A)?.content, "B 的回执改写了 A 的正文").toBe("A 的正式正文");
    expect(readCardLocalDraft(CARD_B)?.kind, "B 的正式编辑落库后，B 的本机草稿依据没有按原板清理").toBe("cleared");
    expect(
      draftCallsFor(BOARD_A).filter((call) => call.removed.includes(KEY_A)).length,
      "B 的旧保存回执对 A 的草稿键发起了 removed",
    ).toBe(0);
  });

  it("4G 退出重开后恢复未同步草稿（不再依赖旧标记）", async () => {
    const cardB = textCard(CARD_B, "B 的正式正文");
    seedBoard(BOARD_B, [cardB], { drafts: {}, rev: 3, keyVersions: {} });
    const text = "关闭前还没同步的草稿";

    store = createTab();
    await store.selectBoard(BOARD_B);
    store.setCardDraftInput(CARD_B, { text });
    expect(readCardLocalDraft(CARD_B)?.text, "前提：本机恢复副本没有写下").toBe(text);

    // 退出重开：新的 pinia / store，同一 localStorage
    const reopened = createTab();
    await reopened.selectBoard(BOARD_B);
    expect(reopened.drafts[KEY_B], "重开后恢复不到未同步草稿").toBe(text);
    expect(reopened.hasCardDraft(CARD_B), "重开后这张卡没有被当成有草稿").toBe(true);
  });
});
