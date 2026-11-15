/**
 * #46 第七轮回归（store 层）：草稿的跨板归属 / 版本基线 / 后台成功清理。
 *
 * 覆盖任务书一、二项（只写测试，不改产品代码）：
 *
 *  A. 双窗口并发静默覆盖
 *     - 本窗口读到 A 的草稿存簿 rev1 / 逐键版本 1 → 切 B 读到 rev2 → 切回 A（仍是 rev1）；
 *     - 另一窗口把 A 的**同一个键**写到 rev2（本窗口没有读到它，用 mock 的服务端回执模拟）；
 *     - 本窗口编辑 A 的同一张卡草稿并触发 flushDrafts：
 *       请求必须写 A、baseRev 必须是 **A 自己读到的版本（1，不是 B 的 2）**、
 *       逐键版本是 A 的版本、正文只含 A 的键；
 *     - 服务端回同键冲突时两份都保留：drafts[key] = 本地新内容、
 *       draftConflicts[key].server = 另一窗口的文字；B 的草稿与状态不受影响。
 *     - 再覆盖 A、B 两块板面先后有草稿写入在途：各自请求的板面 / 版本 / 键互不串。
 *
 *  B. 正式保存后台成功清旧草稿
 *     - B 完成编辑并 requestDraftClear，PUT 在飞时切到 A，放行 B 的 PUT 成功：
 *       B 的旧草稿必须被清掉（待清除登记消化 + 真的发一次 removed），A 的草稿保持原状态；
 *     - B 在回执前又输入新版本：新草稿保留、不被后台成功的旧回执清掉。
 *
 * 断言只落在 api.saveDrafts 的真实调用参数与 store 公开状态（drafts / draftConflicts /
 * draftStateFor / pendingDraftClearKeys / saveStatus / dirty）上，不断言内部 Map 与变量名。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { flushPromises } from "@vue/test-utils";
import { useInteractiveStore } from "../interactive";
import * as api from "../../services/interactive";
import * as attachments from "../../services/attachments";
import { updateCard } from "../../interactive/board";
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
const KEY_A = "card:card_a";
const KEY_B = "card:card_b";

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

/** 一份可在用例过程中被「服务端」改动的草稿存簿（模拟另一窗口 / 清除落地）。 */
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

/** 等草稿防抖（600ms）真的到点：清除 / 新输入必须真的有机会发出去。 */
async function waitForDraftDebounce(): Promise<void> {
  await new Promise((resolve) => setTimeout(resolve, 800));
  await settle();
}

interface DraftCall {
  boardId: string;
  drafts: Record<string, string>;
  removed: string[];
  baseRev: number | null | undefined;
  keyVersions: Record<string, number> | undefined;
}

/** 每次 saveDrafts 的真实参数（板面 / 正文 / 明确删除 / 基线版本 / 逐键版本）。 */
function draftCalls(): DraftCall[] {
  return vi.mocked(api.saveDrafts).mock.calls.map((call) => {
    const patch = (call[2] ?? {}) as {
      removed?: string[];
      baseRev?: number | null;
      keyVersions?: Record<string, number>;
    };
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
    boardPayload(boardState(id, [], 3), { drafts: {}, rev: 0, keyVersions: {} })) as never);
  vi.mocked(api.saveBoardState).mockImplementation((async (id: string, candidate: BoardState) => ({
    ok: true,
    seq: Number(candidate?.seq ?? 3) + 1,
    savedAt: NOW,
    state: { ...candidate, boardId: String(id) },
  })) as never);
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
  // 排空防抖计时器：不让上一个用例的草稿请求落到下一个用例里
  await new Promise((resolve) => setTimeout(resolve, 800));
  await settle();
  localStorage.clear();
});

describe("#46 第七轮 A：双窗口并发下的草稿版本与内容归属", () => {
  it("A1 本窗口写 A 的草稿：baseRev / 逐键版本必须是 A 自己读到的版本，同键冲突两份都保留，B 不受影响", async () => {
    const cardA = textCard("card_a", "A 的正文");
    const cardB = textCard("card_b", "B 的正文");
    // A 的存簿：本窗口读到 rev1 / 逐键版本 1；B 的存簿：rev2（更高，用来暴露全局 rev 串板）
    const bookA: DraftBook = { drafts: { [KEY_A]: "A 的服务器草稿 v1" }, rev: 1, keyVersions: { [KEY_A]: 1 } };
    const bookB: DraftBook = { drafts: { [KEY_B]: "B 的服务器草稿" }, rev: 2, keyVersions: { [KEY_B]: 2 } };
    vi.mocked(api.fetchBoardState).mockImplementation((async (id: string) =>
      id === BOARD_A
        ? boardPayload(boardState(BOARD_A, [cardA], 3), bookA)
        : boardPayload(boardState(BOARD_B, [cardB], 7), bookB)) as never);

    await store.selectBoard(BOARD_A);
    expect(store.drafts[KEY_A], "A 的服务器草稿没有读进来").toBe("A 的服务器草稿 v1");
    await store.selectBoard(BOARD_B);
    expect(store.drafts[KEY_B], "B 的服务器草稿没有读进来").toBe("B 的服务器草稿");
    await store.selectBoard(BOARD_A);
    expect(store.drafts[KEY_A], "切回 A 后 A 的草稿不是 A 读到的版本").toBe("A 的服务器草稿 v1");

    // 另一窗口把 A 的同一个键写到 rev2：本窗口没有读到它，服务端回执里才出现
    const otherWindowText = "另一窗口写的 v2";
    vi.mocked(api.saveDrafts).mockImplementation((async (
      _boardId: string,
      drafts: Record<string, string>,
    ) => ({
      drafts: { ...bookA.drafts, [KEY_A]: otherWindowText },
      updatedAt: NOW,
      rev: 2,
      keyVersions: { [KEY_A]: 2 },
      conflicts: [{ key: KEY_A, local: drafts[KEY_A] ?? "", server: otherWindowText }],
    })) as never);

    // 本窗口编辑 A 的同一张卡草稿并触发保存
    store.setCardDraftInput("card_a", { text: "本窗口的新草稿" });
    await store.flushDrafts();
    await settle();

    const calls = draftCalls();
    expect(calls.length, "草稿保存没有发出").toBe(1);
    const call = calls[0];
    expect(call.boardId, "草稿写到了错误的板面").toBe(BOARD_A);
    expect(
      Object.keys(call.drafts),
      "这次请求不该带别的板面的草稿键",
    ).toEqual([KEY_A]);
    expect(call.drafts[KEY_A]).toBe("本窗口的新草稿");
    expect(
      call.baseRev,
      "baseRev 用了别的板面的存簿版本（A 实际读到的是 1）",
    ).toBe(1);
    expect(
      call.keyVersions?.[KEY_A],
      "逐键版本不是 A 读到的版本",
    ).toBe(1);

    // 同键冲突：两份都保留，不静默挑一个
    expect(store.drafts[KEY_A], "冲突时本机候选被服务器那份覆盖了").toBe("本窗口的新草稿");
    const conflict = store.draftConflictFor(KEY_A);
    expect(conflict, "同键冲突没有登记").not.toBeNull();
    expect(conflict?.server, "冲突里没有保留另一窗口的文字").toBe(otherWindowText);
    expect(conflict?.local, "冲突里没有保留本窗口的候选").toBe("本窗口的新草稿");
    expect(store.draftStateFor(KEY_A).status, "冲突没有被如实标成未保存").toBe("error");

    // B 的草稿与状态不受 A 的冲突影响
    expect(store.draftConflictFor(KEY_B), "A 的冲突串到了 B 的草稿键").toBeNull();
    expect(draftCallsFor(BOARD_B).length, "A 的草稿保存发到了 B").toBe(0);
    await store.selectBoard(BOARD_B);
    await settle();
    expect(store.drafts[KEY_B], "B 的草稿内容被 A 的冲突改掉了").toBe("B 的服务器草稿");
    expect(store.draftConflictFor(KEY_B), "A 的冲突在 B 上仍然可见").toBeNull();
    expect(store.draftStateFor(KEY_B).status, "B 的草稿被标成了失败").not.toBe("error");
  });

  it("A2 两块板面的草稿写入先后返回：各自请求的板面 / 版本 / 键互不串", async () => {
    const cardA = textCard("card_a", "A 的正文");
    const cardB = textCard("card_b", "B 的正文");
    // 先读 A（rev9 / 逐键 9）再读 B（rev5 / 逐键 5）：
    // B 的请求必须仍用 5，A 的请求必须仍是 A 自己的版本（9 或它自己的回执 10）
    const bookA = mutableBook({ drafts: { [KEY_A]: "A 的服务器草稿" }, rev: 9, keyVersions: { [KEY_A]: 9 } });
    const bookB = mutableBook({ drafts: { [KEY_B]: "B 的服务器草稿" }, rev: 5, keyVersions: { [KEY_B]: 5 } });
    vi.mocked(api.fetchBoardState).mockImplementation((async (id: string) =>
      id === BOARD_A
        ? boardPayload(boardState(BOARD_A, [cardA], 3), bookA)
        : boardPayload(boardState(BOARD_B, [cardB], 7), bookB)) as never);

    await store.selectBoard(BOARD_A);
    await store.selectBoard(BOARD_B);
    await store.selectBoard(BOARD_A);

    // A 的草稿写入挂起（在飞）；之后的请求按各自板面的存簿落地
    const aFlight = deferred<never>();
    let callIndex = 0;
    vi.mocked(api.saveDrafts).mockImplementation((async (
      boardId: string,
      drafts: Record<string, string>,
    ) => {
      callIndex += 1;
      if (callIndex === 1) return aFlight.promise;
      const book = String(boardId) === BOARD_A ? bookA : bookB;
      for (const [key, value] of Object.entries(drafts)) book.drafts[key] = value;
      book.rev += 1;
      return {
        drafts: { ...book.drafts },
        updatedAt: NOW,
        rev: book.rev,
        keyVersions: { ...book.keyVersions },
      };
    }) as never);

    store.setCardDraftInput("card_a", { text: "A 的新草稿" });
    const aFlush = store.flushDrafts();
    await settle();
    expect(draftCalls().length, "A 的草稿保存没有发出").toBe(1);

    // 切到 B 编辑并触发：同一时刻只允许一个草稿请求在飞，B 等 A 结束
    await store.selectBoard(BOARD_B);
    store.setCardDraftInput("card_b", { text: "B 的新草稿" });
    const bFlush = store.flushDrafts();
    await settle();
    expect(
      draftCalls().length,
      "A 的草稿请求还在飞就并发了 B 的请求",
    ).toBe(1);

    // A 的回执先到（rev 推进到 10）
    aFlight.resolve({
      drafts: { [KEY_A]: "A 的新草稿" },
      updatedAt: NOW,
      rev: 10,
      keyVersions: { [KEY_A]: 10 },
    } as never);
    bookA.drafts[KEY_A] = "A 的新草稿";
    bookA.rev = 10;
    bookA.keyVersions[KEY_A] = 10;
    await aFlush;
    await settle();
    await bFlush;
    await settle();

    const calls = draftCalls();
    const aCalls = draftCallsFor(BOARD_A);
    const bCalls = draftCallsFor(BOARD_B);
    expect(aCalls.length, "A 的草稿保存没有发出").toBeGreaterThanOrEqual(1);
    expect(bCalls.length, "B 的草稿保存没有发出").toBeGreaterThanOrEqual(1);

    // 每个请求只带自己板面的键；版本只能是它自己读到的（A: 9 起，B: 5），绝不串到对方
    for (const call of calls) {
      if (call.boardId === BOARD_A) {
        expect(Object.keys(call.drafts), "A 的请求带了 B 的草稿键").toEqual([KEY_A]);
        expect([9, 10], "A 的 baseRev 不是 A 自己的版本").toContain(call.baseRev);
        expect(
          call.keyVersions?.[KEY_B],
          "A 的请求带上了 B 的逐键版本",
        ).toBeUndefined();
        expect(
          call.keyVersions?.[KEY_A],
          "A 的逐键版本不是 A 自己读到的版本",
        ).toBe(call.baseRev);
      } else {
        expect(Object.keys(call.drafts), "B 的请求带了 A 的草稿键").toEqual([KEY_B]);
        expect(
          call.baseRev,
          "B 的 baseRev 用了 A 的存簿版本（B 实际读到的是 5）",
        ).toBe(5);
        expect(
          call.keyVersions?.[KEY_B],
          "B 的逐键版本不是 B 读到的版本",
        ).toBe(5);
        expect(
          call.keyVersions?.[KEY_A],
          "B 的请求带上了 A 的逐键版本",
        ).toBeUndefined();
      }
    }

    // 两块板面各自的草稿内容互不串
    expect(store.drafts[KEY_B]).toBe("B 的新草稿");
    expect(store.draftStateFor(KEY_B).status).toBe("saved");
    expect(store.draftConflictFor(KEY_B)).toBeNull();
    await store.selectBoard(BOARD_A);
    await settle();
    expect(store.drafts[KEY_A]).toBe("A 的新草稿");
    expect(store.draftConflictFor(KEY_A)).toBeNull();
    expect(store.draftStateFor(KEY_A).status).not.toBe("error");
  });
});

describe("#46 第七轮 B：正式保存后台成功清旧草稿", () => {
  it("B1 B 的正式保存后台成功：B 的旧草稿被清掉（登记消化 + 真的发 removed），A 的草稿保持", async () => {
    const cardA = textCard("card_a", "A 的正文");
    const cardB = textCard("card_b", "B 的正文");
    // 可被用例改动的服务端草稿存簿：清除落地后 fetch 必须读到「没有这份草稿」
    const bookA = mutableBook({ drafts: { [KEY_A]: "A 的草稿" }, rev: 1, keyVersions: { [KEY_A]: 1 } });
    const bookB = mutableBook({ drafts: { [KEY_B]: "B 的旧草稿" }, rev: 3, keyVersions: { [KEY_B]: 3 } });
    vi.mocked(api.fetchBoardState).mockImplementation((async (id: string) =>
      id === BOARD_A
        ? boardPayload(boardState(BOARD_A, [cardA], 3), bookA)
        : boardPayload(boardState(BOARD_B, [cardB], 7), bookB)) as never);

    // 服务端草稿存簿：removed 里的键真的被清掉，回执带 cleared
    const serverDrafts: Record<string, Record<string, string>> = {
      [BOARD_A]: { ...bookA.drafts },
      [BOARD_B]: { ...bookB.drafts },
    };
    vi.mocked(api.saveDrafts).mockImplementation((async (
      boardId: string,
      drafts: Record<string, string>,
      patch: { removed?: string[] } = {},
    ) => {
      const id = String(boardId);
      const book = (serverDrafts[id] ??= {});
      const cleared: string[] = [];
      for (const [key, value] of Object.entries(drafts)) book[key] = value;
      for (const key of patch.removed ?? []) {
        if (Object.prototype.hasOwnProperty.call(book, key)) {
          delete book[key];
          cleared.push(key);
        }
      }
      const target = id === BOARD_A ? bookA : bookB;
      target.drafts = { ...book };
      target.rev += 1;
      return { drafts: { ...book }, updatedAt: NOW, rev: target.rev, cleared } as never;
    }) as never);

    await store.selectBoard(BOARD_B);
    expect(store.drafts[KEY_B], "B 的服务器草稿没有读进来").toBe("B 的旧草稿");

    // 完成编辑：登记清除 + 正式保存（PUT 挂起）
    store.requestDraftClear(KEY_B);
    expect(store.pendingDraftClearKeys, "清除登记没有生效").toContain(KEY_B);
    store.commit(updateCard(store.board!, "card_b", { content: "B 的正式内容" }), "完成编辑");
    const putFlight = deferred<never>();
    vi.mocked(api.saveBoardState).mockImplementationOnce(() => putFlight.promise as never);
    const saving = store.saveNow();
    await settle();
    expect(vi.mocked(api.saveBoardState).mock.calls.length, "前提：B 的正式保存已发出").toBe(1);

    // 切到 A，再放行 B 的 PUT 成功（后台成功）
    await store.selectBoard(BOARD_A);
    await settle();
    putFlight.resolve({
      ok: true,
      seq: 8,
      savedAt: NOW,
      state: {
        ...boardState(BOARD_B, [cardB], 8),
        cards: [{ ...cardB, content: "B 的正式内容" }],
      },
    } as never);
    await saving;
    await settle();
    await settle();
    // 给清除请求一个真的发出去的机会（防抖 600ms）
    await waitForDraftDebounce();

    // B 的旧草稿必须被清掉：登记消化 + 真的向 B 发一次 removed
    const bClearCalls = draftCallsFor(BOARD_B).filter((call) => call.removed.includes(KEY_B));
    expect(
      bClearCalls.length,
      "后台成功之后没有真的向 B 发一次草稿清除（removed）",
    ).toBeGreaterThan(0);
    expect(draftCallsFor(BOARD_A).length, "B 的草稿清除发到了 A").toBe(0);

    // A 的草稿保持原状态（当前正在看 A）
    expect(store.drafts[KEY_A], "A 的草稿被 B 的清除改掉了").toBe("A 的草稿");
    expect(store.draftStateFor(KEY_A).status, "A 的草稿被 B 的清除标成了失败").not.toBe("error");

    // 回到 B：登记消化完、旧草稿不再出现（服务端已清、内存也清）
    await store.selectBoard(BOARD_B);
    await settle();
    expect(store.pendingDraftClearKeys, "回到 B 后清除登记还挂着").not.toContain(KEY_B);
    expect(store.drafts[KEY_B], "回到 B 后旧草稿又冒出来了").toBeUndefined();
    expect(serverDrafts[BOARD_B][KEY_B], "服务端上 B 的旧草稿没有被清掉").toBeUndefined();
  });

  it("B2 B 在回执前又输入新版本：新草稿保留，不被后台成功的旧回执清掉", async () => {
    const cardA = textCard("card_a", "A 的正文");
    const cardB = textCard("card_b", "B 的正文");
    const bookA = mutableBook({ drafts: {}, rev: 1, keyVersions: {} });
    const bookB = mutableBook({ drafts: { [KEY_B]: "B 的旧草稿" }, rev: 3, keyVersions: { [KEY_B]: 3 } });
    vi.mocked(api.fetchBoardState).mockImplementation((async (id: string) =>
      id === BOARD_A
        ? boardPayload(boardState(BOARD_A, [cardA], 3), bookA)
        : boardPayload(boardState(BOARD_B, [cardB], 7), bookB)) as never);
    vi.mocked(api.saveDrafts).mockImplementation((async (
      boardId: string,
      drafts: Record<string, string>,
    ) => {
      const book = String(boardId) === BOARD_A ? bookA : bookB;
      for (const [key, value] of Object.entries(drafts)) book.drafts[key] = value;
      book.rev += 1;
      return { drafts: { ...book.drafts }, updatedAt: NOW, rev: book.rev };
    }) as never);

    await store.selectBoard(BOARD_B);
    store.setCardDraftInput("card_b", { text: "第一版" });
    store.requestDraftClear(KEY_B);
    store.commit(updateCard(store.board!, "card_b", { content: "B 的正式内容" }), "完成编辑");
    const putFlight = deferred<never>();
    vi.mocked(api.saveBoardState).mockImplementationOnce(() => putFlight.promise as never);
    const saving = store.saveNow();
    await settle();
    expect(vi.mocked(api.saveBoardState).mock.calls.length, "前提：B 的正式保存已发出").toBe(1);

    // 回执到达之前用户又输入了更新的一版
    store.setCardDraftInput("card_b", { text: "第二版" });
    await store.selectBoard(BOARD_A);
    await settle();
    putFlight.resolve({
      ok: true,
      seq: 8,
      savedAt: NOW,
      state: {
        ...boardState(BOARD_B, [cardB], 8),
        cards: [{ ...cardB, content: "B 的正式内容" }],
      },
    } as never);
    await saving;
    await settle();
    await settle();
    await waitForDraftDebounce();

    // 旧回执不许把回执前的新输入清掉
    await store.selectBoard(BOARD_B);
    await settle();
    expect(store.drafts[KEY_B], "旧回执把回执前的新输入清掉了").toBe("第二版");
    expect(
      store.pendingDraftClearKeys,
      "登记之后又输入的新版本没有被版本守卫作废",
    ).not.toContain(KEY_B);
    expect(
      draftCallsFor(BOARD_B).filter((call) => call.removed.includes(KEY_B)).length,
      "旧回执仍然把这份草稿向服务器发起了清除",
    ).toBe(0);
  });
});