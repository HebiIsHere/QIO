/**
 * #46 第二轮返修：板面切换时隔离**待保存数据**与上传回执。
 *
 * 已复现的残留缺陷：上一轮修好了「回执只写进已加载的原板面」，但**待保存候选**仍然
 * 只有一份全局状态（board.value / dirty / boardLocalRev / cleanState / knownServerSeq）。
 * 于是：
 *   1. 在 A 发起上传（延迟回执）→ 切到 B 并新增一张卡（B 的候选还没保存）→
 *      趁自动保存未完成切回 A（A 的读取也延迟）→ 上传先成功、A 的读取后完成；
 *   2. A 的读取落地时 hasCandidate 为真，adoptServerFacts() 把 **B 的候选**合并进
 *      A 的服务端结果，再把合并结果标记成 A；
 *   3. 随后的保存请求目标是 A，但卡片集合里带着 B 的原有卡片与新增卡片。
 *
 * 本文件锁死的行为（全部走 store 的公开动作）：
 * 1. A 加载前没有错误写入；A 加载后 A 只收到 A 的卡片与对应附件；
 * 2. B 的修改只归 B（B 的保存请求目标是 B、卡片是 B 的）；
 * 3. 上传只调用一次；
 * 4. 覆盖「B 的保存尚未发出」「B 的保存已发出但回执迟到」「原文件卡关联」「批次新建卡」；
 * 5. 重读两板核对最终数据。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { flushPromises } from "@vue/test-utils";
import { useInteractiveStore } from "../interactive";
import * as api from "../../services/interactive";
import * as attachments from "../../services/attachments";
import { addCard, emptyState } from "../../interactive/board";
import type { AttachmentRef } from "../../services/attachments";
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

function attachment(name: string, over: Partial<AttachmentRef> = {}): AttachmentRef {
  return {
    id: "att_" + name,
    name,
    sizeBytes: 16,
    kind: "copy",
    display: "已保存副本",
    state: "ready",
    error: null,
    ...over,
  };
}

function file(name: string): File {
  return new File(["QIO"], name, { type: "text/plain" });
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

function fileCard(id: string, name: string, attachmentId = ""): BoardState["cards"][number] {
  return {
    id,
    kind: "file",
    content: "",
    meta: { name, ...(attachmentId ? { file: { attachmentId, name } } : {}) },
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

function liveCards(store: ReturnType<typeof useInteractiveStore>) {
  return (store.board?.cards ?? []).filter((card) => !card.deleted);
}

function linkedAttachmentId(store: ReturnType<typeof useInteractiveStore>, cardId: string): string {
  const card = liveCards(store).find((item) => item.id === cardId);
  const block = (card?.meta ?? {}).file as { attachmentId?: string } | undefined;
  return block?.attachmentId ?? "";
}

/** 每个保存请求的「目标板面 + 卡片 id 集合」。 */
function saveRequests(): { boardId: string; cardIds: string[] }[] {
  return vi.mocked(api.saveBoardState).mock.calls.map((call) => {
    const state = call[1] as BoardState;
    return {
      boardId: String(call[0]),
      cardIds: (state?.cards ?? []).map((card) => card.id),
    };
  });
}

function requestsFor(boardId: string) {
  return saveRequests().filter((item) => item.boardId === boardId);
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
    boardPayload(boardState(id))) as never);
  vi.mocked(api.saveBoardState).mockImplementation((async (id: string, candidate: BoardState) => ({
    ok: true,
    seq: Number(candidate?.seq ?? 3) + 1,
    savedAt: NOW,
    state: { ...candidate, boardId: id },
  })) as never);
  vi.mocked(api.saveDrafts).mockResolvedValue({ drafts: {}, updatedAt: NOW } as never);
  vi.mocked(api.fetchVisibleRange).mockResolvedValue({ visibleRange: emptySnapshot() } as never);
  vi.mocked(api.fetchIntents).mockResolvedValue({
    intents: [],
    conflicts: [],
    batchAvailable: false,
    recovery: { paused: [] },
  } as never);
  vi.mocked(api.fetchIntentViews).mockResolvedValue({ intents: [], source: "service" } as never);
  vi.mocked(api.fetchExecutions).mockResolvedValue({ executions: [] } as never);
  vi.mocked(api.fetchUnderstanding).mockResolvedValue({ understandings: [] } as never);
  vi.mocked(attachments.uploadAttachment).mockImplementation(async (upload: File) =>
    attachment(upload.name),
  );
  vi.mocked(attachments.waitUntilSettled).mockImplementation(async (item: AttachmentRef) => item);
  vi.mocked(attachments.registerSourceAttachment).mockImplementation(async (path: string) =>
    attachment(path.split(/[\\/]/).pop() || "source"),
  );
});

afterEach(async () => {
  /**
   * 排空上一用例留下的防抖保存：store 的防抖是 450ms 的真实计时器，
   * 不排空的话它会在**下一个用例**里发出 PUT，把那个用例的请求记录污染成 2 次。
   */
  await new Promise((resolve) => setTimeout(resolve, 700));
  await settle();
  vi.useRealTimers();
  localStorage.clear();
});

describe("#46 板面切换时隔离待保存数据", () => {
  it("B 的保存尚未发出就切回 A：A 的保存请求只含 A 的卡片，B 的候选只归 B", async () => {
    const cardA = fileCard("card_a", "A 的文件.txt");
    vi.mocked(api.fetchBoardState).mockImplementation((async (id: string) =>
      boardPayload(
        id === BOARD_A
          ? boardState(BOARD_A, [cardA])
          : boardState(BOARD_B, [textCard("card_b", "B 的内容")]),
      )) as never);
    await store.selectBoard(BOARD_A);
    expect(liveCards(store).map((card) => card.id)).toEqual(["card_a"]);

    // A 上发起上传：上传先挂起
    const flying = deferred<AttachmentRef>();
    vi.mocked(attachments.uploadAttachment).mockReturnValueOnce(flying.promise);
    const started = store.startCardAttachment({ cardId: "card_a", source: { file: file("A 的文件.txt") } });
    await settle();
    expect(attachments.uploadAttachment).toHaveBeenCalledTimes(1);

    // 切到 B，并在 B 上新增一张卡（B 的候选，防抖保存还没发出）
    await store.selectBoard(BOARD_B);
    expect(liveCards(store).map((card) => card.id)).toEqual(["card_b"]);
    store.commit(addCard(store.board!, { kind: "text", content: "B 新增的卡" }), "新增卡片");
    await settle();
    expect(store.dirty, "本用例前提：B 有未保存候选").toBe(true);

    // 趁 B 的自动保存还没发出，切回 A（A 的读取被挂起）
    const readingA = deferred<void>();
    vi.mocked(api.fetchBoardState).mockImplementationOnce(async () => {
      await readingA.promise;
      return boardPayload(boardState(BOARD_A, [cardA])) as never;
    });
    const selecting = store.selectBoard(BOARD_A);
    await settle();
    expect(store.boardId, "本用例前提：boardId 已经是 A").toBe(BOARD_A);

    // 上传先成功：A 还没加载 —— 不许写板面
    const savesBefore = vi.mocked(api.saveBoardState).mock.calls.length;
    flying.resolve(attachment("A 的文件.txt", { id: "att_a" }));
    await settle();
    expect(
      vi.mocked(api.saveBoardState).mock.calls.length,
      "A 加载前就发出了保存请求",
    ).toBe(savesBefore);

    // A 读取落定：回执继续关联
    readingA.resolve();
    await selecting;
    await settle();
    const result = await started.done;
    await settle();
    expect(result.ok, "A 加载后回执没有完成关联：" + String(result.error)).toBe(true);
    expect(attachments.uploadAttachment, "回执被重复处理（重复上传）").toHaveBeenCalledTimes(1);

    // 等 A 的关联保存真的发出
    await waitForDebouncedSave();

    // 核心断言：发往 A 的保存请求只含 A 的卡片
    const toA = requestsFor(BOARD_A);
    expect(toA.length, "A 的关联没有产生保存请求").toBeGreaterThan(0);
    for (const request of toA) {
      expect(request.cardIds, "发往 A 的保存请求里混进了 B 的卡片").not.toContain("card_b");
      expect(request.cardIds, "发往 A 的保存请求里混进了 B 的新增卡片").not.toContain(
        liveCards(store).find((card) => card.kind === "text")?.id ?? "card_b_new",
      );
    }
    expect(toA[toA.length - 1].cardIds).toContain("card_a");
    expect(linkedAttachmentId(store, "card_a")).toBe("att_a");

    // B 的候选只归 B：发往 B 的保存请求（如果有）只含 B 的卡片
    for (const request of requestsFor(BOARD_B)) {
      expect(request.cardIds, "发往 B 的保存请求里混进了 A 的卡片").not.toContain("card_a");
    }
  });

  it("B 的保存已发出但回执迟到：迟到回执不写进 A，A 的保存请求仍只含 A 的卡片", async () => {
    const cardA = fileCard("card_a", "A 的文件.txt");
    vi.mocked(api.fetchBoardState).mockImplementation((async (id: string) =>
      boardPayload(
        id === BOARD_A
          ? boardState(BOARD_A, [cardA])
          : boardState(BOARD_B, [textCard("card_b", "B 的内容")]),
      )) as never);
    await store.selectBoard(BOARD_A);

    const flying = deferred<AttachmentRef>();
    vi.mocked(attachments.uploadAttachment).mockReturnValueOnce(flying.promise);
    const started = store.startCardAttachment({ cardId: "card_a", source: { file: file("A 的文件.txt") } });
    await settle();

    // 切到 B 并新增一张卡，让 B 的保存真的发出（PUT 挂起 = 回执迟到）
    await store.selectBoard(BOARD_B);
    const bSave = deferred<void>();
    vi.mocked(api.saveBoardState).mockImplementationOnce(async (id: string, candidate: BoardState) => {
      await bSave.promise;
      return { ok: true, seq: Number(candidate?.seq ?? 3) + 1, savedAt: NOW, state: { ...candidate, boardId: id } } as never;
    });
    store.commit(addCard(store.board!, { kind: "text", content: "B 新增的卡" }), "新增卡片");
    await waitForDebouncedSave();
    const bRequests = requestsFor(BOARD_B);
    expect(bRequests.length, "本用例前提：B 的保存已经发出").toBeGreaterThan(0);
    expect(bRequests[0].cardIds).toContain("card_b");

    // 趁 B 的保存回执还没回来，切回 A（A 的读取挂起）
    const readingA = deferred<void>();
    vi.mocked(api.fetchBoardState).mockImplementationOnce(async () => {
      await readingA.promise;
      return boardPayload(boardState(BOARD_A, [cardA])) as never;
    });
    const selecting = store.selectBoard(BOARD_A);
    await settle();

    // 上传先成功，再让 A 的读取完成
    flying.resolve(attachment("A 的文件.txt", { id: "att_a" }));
    await settle();
    readingA.resolve();
    await selecting;
    await settle();
    const result = await started.done;
    await settle();
    expect(result.ok, "A 加载后回执没有完成关联：" + String(result.error)).toBe(true);

    // B 的迟到回执现在才回来：不许写进 A
    bSave.resolve();
    await settle();
    await waitForDebouncedSave();

    expect(store.boardId).toBe(BOARD_A);
    expect(liveCards(store).map((card) => card.id), "B 的迟到回执把卡片写进了 A").toEqual(["card_a"]);
    for (const request of requestsFor(BOARD_A)) {
      expect(request.cardIds, "发往 A 的保存请求里混进了 B 的卡片").not.toContain("card_b");
    }
    expect(linkedAttachmentId(store, "card_a")).toBe("att_a");
  });

  it("批次新建卡（无预建卡）：跨板等待后建卡与保存都归原板面", async () => {
    vi.mocked(api.fetchBoardState).mockImplementation((async (id: string) =>
      boardPayload(
        id === BOARD_A ? boardState(BOARD_A) : boardState(BOARD_B, [textCard("card_b", "B 的内容")]),
      )) as never);
    await store.selectBoard(BOARD_A);

    const batch = store.beginImportBatch([{ name: "批次文件.txt" }]);
    const itemKey = batch.items[0].key;
    store.setImportItemSource(batch.id, itemKey, { file: file("批次文件.txt") });
    const flying = deferred<AttachmentRef>();
    vi.mocked(attachments.uploadAttachment).mockReturnValueOnce(flying.promise);
    const run = store.runImportItem(batch.id, itemKey);
    await settle();

    // 切到 B 并新增一张卡（B 的候选）
    await store.selectBoard(BOARD_B);
    store.commit(addCard(store.board!, { kind: "text", content: "B 新增的卡" }), "新增卡片");
    await settle();

    // 切回 A 且延迟 A 的读取
    const readingA = deferred<void>();
    vi.mocked(api.fetchBoardState).mockImplementationOnce(async () => {
      await readingA.promise;
      return boardPayload(boardState(BOARD_A)) as never;
    });
    const selecting = store.selectBoard(BOARD_A);
    await settle();

    flying.resolve(attachment("批次文件.txt", { id: "att_batch" }));
    await settle();
    readingA.resolve();
    await selecting;
    const result = await run;
    await settle();
    await waitForDebouncedSave();

    expect(result.ok, "A 加载后批次没有完成建卡：" + String(result.error)).toBe(true);
    expect(result.boardId).toBe(BOARD_A);
    const live = liveCards(store);
    expect(live, "原板面没有建卡，或建了重复卡").toHaveLength(1);
    expect(live[0].kind).toBe("file");
    expect(linkedAttachmentId(store, live[0].id)).toBe("att_batch");
    for (const request of requestsFor(BOARD_A)) {
      expect(request.cardIds, "发往 A 的保存请求里混进了 B 的卡片").not.toContain("card_b");
    }
  });

  it("重读两板：A 只有 A 的卡片，B 只有 B 的卡片（含 B 的新增卡）", async () => {
    const cardA = fileCard("card_a", "A 的文件.txt");
    const cardB = textCard("card_b", "B 的内容");
    vi.mocked(api.fetchBoardState).mockImplementation((async (id: string) =>
      boardPayload(
        id === BOARD_A ? boardState(BOARD_A, [cardA]) : boardState(BOARD_B, [cardB]),
      )) as never);

    await store.selectBoard(BOARD_A);
    await store.selectBoard(BOARD_B);
    store.commit(addCard(store.board!, { kind: "text", content: "B 新增的卡" }), "新增卡片");
    await settle();
    await waitForDebouncedSave();

    // 切回 A 再切回 B：B 的新增卡仍在（B 的候选归 B）
    await store.selectBoard(BOARD_A);
    await settle();
    expect(liveCards(store).map((card) => card.id), "A 上出现了 B 的卡片").toEqual(["card_a"]);

    await store.selectBoard(BOARD_B);
    await settle();
    const bIds = liveCards(store).map((card) => card.id);
    expect(bIds, "B 的新增卡丢了").toContain("card_b");
    expect(bIds.length).toBe(2);
    expect(bIds, "B 上出现了 A 的卡片").not.toContain("card_a");
  });
});
describe("#46 第三轮：保存归属与在途请求", () => {
  /** 记录每次 PUT 的「目标板面 + 候选版本 + 卡片集合」。 */
  function putLog(): { boardId: string; seq: number; cardIds: string[] }[] {
    return vi.mocked(api.saveBoardState).mock.calls.map((call) => {
      const state = call[1] as BoardState;
      return {
        boardId: String(call[0]),
        seq: Number(state?.seq ?? 0),
        cardIds: (state?.cards ?? []).map((card) => card.id),
      };
    });
  }

  function pendingFor(store: ReturnType<typeof useInteractiveStore>) {
    return store.pendingSubmissionChanges;
  }

  it("反例 1：B 的保存回执迟到 —— 只更新 B，A 的待提交改动/基准/状态/卡片/版本保持为 A", async () => {
    const cardA = textCard("card_a", "A 的内容");
    const cardB = textCard("card_b", "B 的内容");
    vi.mocked(api.fetchBoardState).mockImplementation((async (id: string) =>
      boardPayload(
        id === BOARD_A ? boardState(BOARD_A, [cardA], 3) : boardState(BOARD_B, [cardB], 7),
      )) as never);

    await store.selectBoard(BOARD_A);
    await settle();
    expect(store.boardId).toBe(BOARD_A);

    // B 的保存挂起（回执迟到）
    const bSave = deferred<void>();
    vi.mocked(api.saveBoardState).mockImplementationOnce(async (id: string, candidate: BoardState) => {
      await bSave.promise;
      return {
        ok: true,
        seq: 99,
        savedAt: NOW,
        state: { ...candidate, boardId: id, seq: 99 },
        pending: {
          baselineSeq: 99,
          expressions: [{ id: "e_b", kind: "note_added", intentBearing: true, summary: "B 的改动", cardIds: ["card_b"], groupId: null, linkId: null }],
          updatedAt: NOW,
        },
      } as never;
    });

    await store.selectBoard(BOARD_B);
    await settle();
    store.commit(addCard(store.board!, { kind: "text", content: "B 新增的卡" }), "新增卡片");
    await waitForDebouncedSave();
    expect(putLog().filter((item) => item.boardId === BOARD_B).length, "前提：B 的保存已发出").toBe(1);

    // 切到 A，等 A 的待提交改动加载完成
    await store.selectBoard(BOARD_A);
    await settle();
    expect(store.boardId).toBe(BOARD_A);
    const aPendingBefore = pendingFor(store);
    const aBaselineBefore = store.submissionBaseline;
    const aSaveStatusBefore = store.saveStatus;
    const aCardsBefore = liveCards(store).map((card) => card.id);

    // B 的回执现在才回来（版本 99 远高于 A 的 3）
    bSave.resolve();
    await settle();
    await settle();

    expect(store.boardId, "界面仍在 A").toBe(BOARD_A);
    expect(pendingFor(store), "B 的回执把 A 的待提交改动换成了 B 的").toEqual(aPendingBefore);
    expect(store.submissionBaseline, "B 的回执改了 A 的提交基准").toEqual(aBaselineBefore);
    expect(store.saveStatus, "B 的回执改了 A 的保存状态").toBe(aSaveStatusBefore);
    expect(liveCards(store).map((card) => card.id), "B 的回执把卡片写进了 A").toEqual(aCardsBefore);
    expect(store.board?.seq, "B 的回执把 A 的版本改成了 B 的").toBe(3);

    /**
     * 让 B 的回执版本（99）远高于 A，再重读 A（seq 4）：
     * A 的有效响应必须仍能被采用 —— 若 B 的迟到回执推进了 A 的 knownServerSeq，
     * 这里会把它当成「过期读取」而拒绝落地。
     */
    const cardA2 = textCard("card_a2", "A 的第二次读取");
    vi.mocked(api.fetchBoardState).mockImplementation((async (id: string) =>
      boardPayload(
        id === BOARD_A ? boardState(BOARD_A, [cardA, cardA2], 4) : boardState(BOARD_B, [cardB], 99),
      )) as never);
    await store.refreshBoardFromServer();
    await settle();
    expect(store.boardId).toBe(BOARD_A);
    expect(store.board?.seq, "A 的有效读取被 B 的迟到回执判成了过期").toBe(4);
    expect(liveCards(store).map((card) => card.id), "A 的有效读取没有落地").toEqual(["card_a", "card_a2"]);

    // 切回 B：能看到 B 的正确结果
    await store.selectBoard(BOARD_B);
    await settle();
    expect(store.boardId).toBe(BOARD_B);
    const bIds = liveCards(store).map((card) => card.id);
    expect(bIds, "B 的保存结果丢了").toContain("card_b");
    expect(bIds, "B 上出现了 A 的卡片").not.toContain("card_a");
    expect(store.saveStatus, "切回 B 没有接回已保存状态").toBe("saved");
  });

  it("反例 2：B 的保存仍在途时切回 B —— 同一份候选只发一次 PUT", async () => {
    const cardA = textCard("card_a", "A 的内容");
    const cardB = textCard("card_b", "B 的内容");
    vi.mocked(api.fetchBoardState).mockImplementation((async (id: string) =>
      boardPayload(
        id === BOARD_A ? boardState(BOARD_A, [cardA], 3) : boardState(BOARD_B, [cardB], 7),
      )) as never);

    await store.selectBoard(BOARD_A);
    await settle();
    await store.selectBoard(BOARD_B);
    await settle();

    const bSave = deferred<void>();
    vi.mocked(api.saveBoardState).mockImplementationOnce(async (id: string, candidate: BoardState) => {
      await bSave.promise;
      return { ok: true, seq: 8, savedAt: NOW, state: { ...candidate, boardId: id, seq: 8 } } as never;
    });
    store.commit(addCard(store.board!, { kind: "text", content: "B 新增的卡" }), "新增卡片");
    await waitForDebouncedSave();
    const firstPuts = putLog().filter((item) => item.boardId === BOARD_B);
    expect(firstPuts.length, "前提：B 的首个保存已发出").toBe(1);

    // B → A → B，期间没有编辑，首个请求仍未返回
    await store.selectBoard(BOARD_A);
    await settle();
    await store.selectBoard(BOARD_B);
    await settle();
    await waitForDebouncedSave();

    const putsWhileInFlight = putLog().filter((item) => item.boardId === BOARD_B);
    expect(putsWhileInFlight.length, "切回 B 时对同一份候选又发了一次 PUT").toBe(1);

    // 原请求成功：B 不再发相同候选，界面从保存中转为已保存
    bSave.resolve();
    await settle();
    await waitForDebouncedSave();
    expect(putLog().filter((item) => item.boardId === BOARD_B).length, "原请求成功后 B 又发了相同候选").toBe(1);
    expect(store.boardId).toBe(BOARD_B);
    expect(store.saveStatus, "切回 B 后没有从保存中转为已保存").toBe("saved");
    expect(store.dirty, "B 保存成功后仍标未保存").toBe(false);
  });

  it("切回 B 后出现新编辑：首个回执保留新编辑，后续请求只发新候选", async () => {
    const cardB = textCard("card_b", "B 的内容");
    vi.mocked(api.fetchBoardState).mockImplementation((async (id: string) =>
      boardPayload(boardState(id, id === BOARD_B ? [cardB] : [], 7))) as never);

    await store.selectBoard(BOARD_B);
    await settle();

    const firstSave = deferred<void>();
    vi.mocked(api.saveBoardState).mockImplementationOnce(async (id: string, candidate: BoardState) => {
      await firstSave.promise;
      return { ok: true, seq: 8, savedAt: NOW, state: { ...candidate, boardId: id, seq: 8 } } as never;
    });
    store.commit(addCard(store.board!, { kind: "text", content: "第一版" }), "新增卡片");
    await waitForDebouncedSave();
    expect(putLog().filter((item) => item.boardId === BOARD_B).length).toBe(1);

    await store.selectBoard(BOARD_A);
    await settle();
    await store.selectBoard(BOARD_B);
    await settle();

    // 切回 B 后新增编辑
    store.commit(addCard(store.board!, { kind: "text", content: "第二版" }), "新增卡片");
    await settle();

    firstSave.resolve();
    await settle();
    await waitForDebouncedSave();

    const puts = putLog().filter((item) => item.boardId === BOARD_B);
    expect(puts.length, "首个回执之后没有按新候选再保存").toBe(2);
    expect(puts[1].seq, "后续请求的版本不是基于已确认的结果").toBe(8);
    expect(puts[1].cardIds.length, "后续请求没有带上新编辑").toBe(puts[0].cardIds.length + 1);
    expect(store.dirty, "新编辑被首个回执吞掉了").toBe(false);
    expect(store.saveStatus).toBe("saved");
  });


  it("B 的迟到回执遇到 B 的更新候选：只推进已保存事实，不覆盖候选，切回 B 后接着保存", async () => {
    const cardA = textCard("card_a", "A 的内容");
    const cardB = textCard("card_b", "B 的内容");
    vi.mocked(api.fetchBoardState).mockImplementation((async (id: string) =>
      boardPayload(
        id === BOARD_A ? boardState(BOARD_A, [cardA], 3) : boardState(BOARD_B, [cardB], 7),
      )) as never);

    await store.selectBoard(BOARD_A);
    await settle();
    await store.selectBoard(BOARD_B);
    await settle();

    const firstSave = deferred<void>();
    vi.mocked(api.saveBoardState).mockImplementationOnce(async (id: string, candidate: BoardState) => {
      await firstSave.promise;
      return { ok: true, seq: 9, savedAt: NOW, state: { ...candidate, boardId: id, seq: 9 } } as never;
    });
    store.commit(addCard(store.board!, { kind: "text", content: "第一版" }), "新增卡片");
    await waitForDebouncedSave();
    expect(putLog().filter((item) => item.boardId === BOARD_B).length).toBe(1);
    const candidateAfterFirstSave = store.board!.cards.length;

    // 切到 A，在 B 上继续编辑（B 的候选仍在内存里，等切回去）
    await store.selectBoard(BOARD_A);
    await settle();
    await store.selectBoard(BOARD_B);
    await settle();
    store.commit(addCard(store.board!, { kind: "text", content: "第二版" }), "新增卡片");
    await settle();
    expect(store.board!.cards.length).toBe(candidateAfterFirstSave + 1);

    // 首个回执迟到：只吸收版本事实，不覆盖 B 的新候选
    await store.selectBoard(BOARD_A);
    await settle();
    firstSave.resolve();
    await settle();
    await settle();
    expect(store.boardId, "界面仍在 A").toBe(BOARD_A);
    expect(liveCards(store).map((card) => card.id), "B 的回执写进了 A").toEqual(["card_a"]);

    // 切回 B：新候选仍在，并按最新候选继续保存（版本基于已确认结果）
    await store.selectBoard(BOARD_B);
    await settle();
    expect(liveCards(store).length, "B 的新候选被迟到回执吞掉了").toBe(candidateAfterFirstSave + 1);
    await waitForDebouncedSave();
    const bPuts = putLog().filter((item) => item.boardId === BOARD_B);
    expect(bPuts.length, "切回 B 后没有按最新候选再保存").toBe(2);
    expect(bPuts[1].cardIds.length, "后续请求没有带上 B 的新编辑").toBe(candidateAfterFirstSave + 1);
    expect(bPuts[1].seq, "后续请求的版本没有吸收已确认的结果").toBe(9);
  });

  it("两块板面各自有在飞保存：B 的回执只清 B 的在途记录，A 的请求不受影响", async () => {
    const cardA = textCard("card_a", "A 的内容");
    const cardB = textCard("card_b", "B 的内容");
    vi.mocked(api.fetchBoardState).mockImplementation((async (id: string) =>
      boardPayload(
        id === BOARD_A ? boardState(BOARD_A, [cardA], 3) : boardState(BOARD_B, [cardB], 7),
      )) as never);

    await store.selectBoard(BOARD_A);
    await settle();
    await store.selectBoard(BOARD_B);
    await settle();

    const bSave = deferred<void>();
    const aSave = deferred<void>();
    vi.mocked(api.saveBoardState)
      .mockImplementationOnce(async (id: string, candidate: BoardState) => {
        await bSave.promise;
        return { ok: true, seq: 8, savedAt: NOW, state: { ...candidate, boardId: id, seq: 8 } } as never;
      })
      .mockImplementationOnce(async (id: string, candidate: BoardState) => {
        await aSave.promise;
        return { ok: true, seq: 4, savedAt: NOW, state: { ...candidate, boardId: id, seq: 4 } } as never;
      });

    store.commit(addCard(store.board!, { kind: "text", content: "B 新增" }), "新增卡片");
    await waitForDebouncedSave();
    expect(putLog().filter((item) => item.boardId === BOARD_B).length).toBe(1);

    // 切到 A 并保存 A：B 的在途请求不许挡住 A
    await store.selectBoard(BOARD_A);
    await settle();
    store.commit(addCard(store.board!, { kind: "text", content: "A 新增" }), "新增卡片");
    await waitForDebouncedSave();
    const aPuts = putLog().filter((item) => item.boardId === BOARD_A);
    expect(aPuts.length, "A 的保存被 B 的在途请求挡住了").toBe(1);

    // B 的请求结算：只清 B 自己的在途记录
    bSave.resolve();
    await settle();
    await settle();
    aSave.resolve();
    await settle();
    await waitForDebouncedSave();

    expect(putLog().filter((item) => item.boardId === BOARD_A).length, "B 的回执清掉了 A 的在途记录，A 又发了一次").toBe(1);
    expect(putLog().filter((item) => item.boardId === BOARD_B).length, "B 又被发了第二次").toBe(1);
    expect(liveCards(store).map((card) => card.id), "B 的回执把卡片写进了 A").toContain("card_a");
    for (const request of putLog()) {
      if (request.boardId === BOARD_A) expect(request.cardIds, "发往 A 的请求混进了 B 的卡片").not.toContain("card_b");
      if (request.boardId === BOARD_B) expect(request.cardIds, "发往 B 的请求混进了 A 的卡片").not.toContain("card_a");
    }
  });

  it("B 回执失败时正在看 A：普通失败只作用于 B，A 的状态保持正确", async () => {
    const cardA = textCard("card_a", "A 的内容");
    const cardB = textCard("card_b", "B 的内容");
    vi.mocked(api.fetchBoardState).mockImplementation((async (id: string) =>
      boardPayload(
        id === BOARD_A ? boardState(BOARD_A, [cardA], 3) : boardState(BOARD_B, [cardB], 7),
      )) as never);

    await store.selectBoard(BOARD_A);
    await settle();
    const aStatus = store.saveStatus;
    const aError = store.saveError;

    const bSave = deferred<void>();
    vi.mocked(api.saveBoardState).mockImplementationOnce(async () => {
      await bSave.promise;
      throw Object.assign(new Error("保存失败"), { status: 500 });
    });
    await store.selectBoard(BOARD_B);
    await settle();
    store.commit(addCard(store.board!, { kind: "text", content: "B 新增的卡" }), "新增卡片");
    await waitForDebouncedSave();

    await store.selectBoard(BOARD_A);
    await settle();
    bSave.reject(Object.assign(new Error("保存失败"), { status: 500 }));
    await settle();
    await settle();

    expect(store.boardId).toBe(BOARD_A);
    expect(store.saveStatus, "B 的失败写进了 A 的保存状态").toBe(aStatus);
    expect(store.saveError, "B 的失败原因写进了 A").toBe(aError);

    // 切回 B：仍能看到 B 的待保存改动与失败原因，并按已有规则重试（重试只作用于 B）
    await store.selectBoard(BOARD_B);
    await settle();
    expect(store.boardId).toBe(BOARD_B);
    expect(store.dirty, "B 的候选丢了").toBe(true);
    expect(store.saveError, "B 的失败原因丢了").toContain("保存失败");
    expect(liveCards(store).map((card) => card.id).length, "B 的新增卡丢了").toBe(2);
    // A 没有因为 B 的失败收到任何保存请求
    expect(requestsFor(BOARD_A).length, "B 的恢复/重试发到了 A").toBe(0);
  });

  it("B 回执 409 版本冲突时正在看 A：A 不受影响，恢复只作用于 B", async () => {
    const cardA = textCard("card_a", "A 的内容");
    const cardB = textCard("card_b", "B 的内容");
    vi.mocked(api.fetchBoardState).mockImplementation((async (id: string) =>
      boardPayload(
        id === BOARD_A ? boardState(BOARD_A, [cardA], 3) : boardState(BOARD_B, [cardB], 7),
      )) as never);

    await store.selectBoard(BOARD_A);
    await settle();
    const aStatus = store.saveStatus;

    const bSave = deferred<void>();
    vi.mocked(api.saveBoardState).mockImplementationOnce(async () => {
      await bSave.promise;
      throw Object.assign(new Error("版本过期"), {
        status: 409,
        payload: { error: "stale_state", reason: "板面版本已经变化" },
      });
    });
    await store.selectBoard(BOARD_B);
    await settle();
    store.commit(addCard(store.board!, { kind: "text", content: "B 新增的卡" }), "新增卡片");
    await waitForDebouncedSave();

    await store.selectBoard(BOARD_A);
    await settle();
    bSave.reject(Object.assign(new Error("版本过期"), {
      status: 409,
      payload: { error: "stale_state", reason: "板面版本已经变化" },
    }));
    await settle();
    await settle();

    expect(store.boardId).toBe(BOARD_A);
    expect(store.saveStatus, "B 的 409 写进了 A 的保存状态").toBe(aStatus);
    expect(liveCards(store).map((card) => card.id), "B 的 409 恢复把 A 的卡片改了").toEqual(["card_a"]);
  });
});
describe("#46 第四轮：保存归属贯穿预判、写入与冲突恢复", () => {
  const TASK_ID = "task_impact_1";
  const AFFECTED_TASK = {
    intentId: TASK_ID,
    title: "任务-1",
    materials: ["card_b"],
    consequence: "暂停并保留进度",
  };

  function runningIntent(): never {
    return {
      id: TASK_ID,
      boardId: BOARD_B,
      submissionId: null,
      title: "任务-1",
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

  function withRunningTask(): void {
    vi.mocked(api.fetchIntents).mockResolvedValue({
      intents: [runningIntent()],
      conflicts: [],
      batchAvailable: false,
      recovery: { paused: [] },
    } as never);
  }

  /** 每次影响预判的「目标板面 + 依据版本 + 候选卡片」，用来核对请求归属。 */
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

  /** 每次 PUT 的「目标板面 + 候选 + 确认句柄」，用来核对最终写入。 */
  function puts(): {
    boardId: string;
    state: BoardState;
    cardIds: string[];
    confirm: { checkId?: string } | undefined;
  }[] {
    return vi.mocked(api.saveBoardState).mock.calls.map((call) => {
      const candidate = call[1] as BoardState;
      return {
        boardId: String(call[0]),
        state: candidate,
        cardIds: (candidate?.cards ?? []).map((card) => card.id),
        confirm: call[3] as { checkId?: string } | undefined,
      };
    });
  }

  function apiError(status: number, payload: Record<string, unknown>): Error {
    return Object.assign(new Error(String(status)), { status, payload });
  }

  function affectedCheck(checkId: string, stateVersion: number): never {
    return {
      ok: true,
      checkId,
      stateVersion,
      affected: [AFFECTED_TASK],
      impactConfirmationRequired: true,
    } as never;
  }

  function cleanCheck(stateVersion = 0): never {
    return { ok: true, stateVersion, affected: [], impactConfirmationRequired: false } as never;
  }

  function boardPayloadOf(boardId: string, seq: number, cards: BoardState["cards"]) {
    return boardPayload(boardState(boardId, cards, seq));
  }

  it("预判在飞时 B → A → B：同一份候选只发一次 B 的预判、一次 B 的 PUT", async () => {
    withRunningTask();
    await store.selectBoard(BOARD_A);
    await store.selectBoard(BOARD_B);

    const precheck = deferred<never>();
    vi.mocked(api.checkMaterialImpact).mockReturnValueOnce(precheck.promise as never);

    store.commit(addCard(store.board!, { kind: "text", content: "B 新增的卡" }), "新增卡片");
    const candidateSize = liveCards(store).length;
    await waitForDebouncedSave();
    expect(impactChecks().length, "前提：B 的预判已经发出").toBe(1);
    expect(impactChecks()[0].boardId, "预判没有发往 B").toBe(BOARD_B);
    expect(impactChecks()[0].cardIds.length).toBe(candidateSize);

    // 预判还没回来：B → A → B（期间没有新编辑）
    await store.selectBoard(BOARD_A);
    await store.selectBoard(BOARD_B);
    await waitForDebouncedSave();

    expect(impactChecks().length, "切回 B 时对同一份候选又发了一次预判").toBe(1);
    expect(puts().length, "预判还没回来就发出了 PUT").toBe(0);

    precheck.resolve(cleanCheck() as never);
    await settle();
    await waitForDebouncedSave();

    const bPuts = puts().filter((item) => item.boardId === BOARD_B);
    expect(bPuts.length, "同一份候选发了多次 PUT").toBe(1);
    expect(bPuts[0].cardIds.length, "PUT 的候选不是发起保存时的那一份").toBe(candidateSize);
    expect(bPuts[0].state.seq, "PUT 没有使用已确认的服务端版本").toBe(3);
    expect(puts().filter((item) => item.boardId === BOARD_A).length, "向 A 发出了 B 的保存").toBe(0);
    expect(store.boardId).toBe(BOARD_B);
    expect(store.saveStatus, "B 没有落到已保存").toBe("saved");
    expect(store.dirty, "B 保存成功后仍标未保存").toBe(false);
  });

  it("切回 B 后再编辑：旧预判结果不能放行旧候选", async () => {
    withRunningTask();
    await store.selectBoard(BOARD_A);
    await store.selectBoard(BOARD_B);

    const oldCheck = deferred<never>();
    const newCheck = deferred<never>();
    vi.mocked(api.checkMaterialImpact)
      .mockReturnValueOnce(oldCheck.promise as never)
      .mockReturnValueOnce(newCheck.promise as never);

    store.commit(addCard(store.board!, { kind: "text", content: "第一版" }), "新增卡片");
    await waitForDebouncedSave();
    expect(impactChecks().length, "前提：旧候选的预判已经发出").toBe(1);

    await store.selectBoard(BOARD_A);
    await store.selectBoard(BOARD_B);

    // 切回 B 后新增编辑：旧预判对应的候选已经不是当前候选
    store.commit(addCard(store.board!, { kind: "text", content: "第二版" }), "新增卡片");
    const latestSize = liveCards(store).length;
    await waitForDebouncedSave();

    // 旧预判现在才回来：不许放行旧候选
    oldCheck.resolve(cleanCheck() as never);
    await settle();
    expect(puts().filter((item) => item.boardId === BOARD_B).length, "旧预判结果放行了旧候选").toBe(0);

    const checks = impactChecks();
    expect(checks.length, "新候选没有重新预判").toBeGreaterThanOrEqual(2);
    const latest = checks[checks.length - 1];
    expect(latest.boardId, "重新预判没有发往 B").toBe(BOARD_B);
    expect(latest.cardIds.length, "重新预判用的不是最新候选").toBe(latestSize);
    expect(latest.stateVersion, "重新预判没有使用已确认的服务端版本").toBe(3);

    newCheck.resolve(cleanCheck() as never);
    await settle();
    await waitForDebouncedSave();
    const bPuts = puts().filter((item) => item.boardId === BOARD_B);
    expect(bPuts.length, "最新候选没有只保存一次").toBe(1);
    expect(bPuts[0].cardIds.length, "保存的不是最新候选").toBe(latestSize);
    expect(puts().filter((item) => item.boardId === BOARD_A).length, "向 A 发出了 B 的保存").toBe(0);
    expect(store.boardId).toBe(BOARD_B);
  });

  it("收到 409 后重读在途时切到 A、重读失败：A 不受影响，切回 B 可继续恢复", async () => {
    const cardA = textCard("card_a", "A 的内容");
    const cardB = textCard("card_b", "B 的内容");
    vi.mocked(api.fetchBoardState).mockImplementation((async (id: string) =>
      boardPayload(
        id === BOARD_A ? boardState(BOARD_A, [cardA], 3) : boardState(BOARD_B, [cardB], 7),
      )) as never);
    await store.selectBoard(BOARD_A);
    const aStatus = store.saveStatus;
    const aCards = liveCards(store).map((card) => card.id);
    await store.selectBoard(BOARD_B);

    const putFlight = deferred<void>();
    vi.mocked(api.saveBoardState).mockImplementationOnce(async () => {
      await putFlight.promise;
      throw apiError(409, { error: "stale_state", reason: "服务端已有更新的已保存版本", currentSeq: 8 });
    });
    store.commit(addCard(store.board!, { kind: "text", content: "B 新增的卡" }), "新增卡片");
    await waitForDebouncedSave();
    expect(puts().filter((item) => item.boardId === BOARD_B).length, "前提：B 的 PUT 已发出").toBe(1);

    // 409 后的重读挂起
    const recoveryRead = deferred<never>();
    vi.mocked(api.fetchBoardState).mockImplementationOnce(() => recoveryRead.promise as never);
    putFlight.resolve();
    await settle();
    expect(store.boardId, "前提：收到 409 时仍在 B").toBe(BOARD_B);

    // 重读在途时切到 A，然后重读失败
    await store.selectBoard(BOARD_A);
    recoveryRead.reject(new Error("读取最新事实失败"));
    await settle();
    await settle();

    expect(store.boardId).toBe(BOARD_A);
    expect(store.saveStatus, "B 的重读失败写进了 A 的保存状态").toBe(aStatus);
    expect(store.saveError, "B 的错误显示在 A 上").toBeNull();
    expect(store.dirty, "B 的未保存标记落到了 A").toBe(false);
    expect(liveCards(store).map((card) => card.id), "A 的卡片被改动").toEqual(aCards);
    expect(impactChecks().filter((item) => item.boardId === BOARD_A).length, "向 A 发出了 B 的影响预判").toBe(0);
    expect(puts().filter((item) => item.boardId === BOARD_A).length, "向 A 发出了 B 的保存").toBe(0);

    // 切回 B：候选完整、失败状态归 B；读取落定后继续恢复
    let bSeq = 7;
    const loadB = deferred<never>();
    vi.mocked(api.fetchBoardState).mockImplementationOnce(() => loadB.promise as never);
    const selectingB = store.selectBoard(BOARD_B);
    await settle();
    expect(store.boardId).toBe(BOARD_B);
    expect(liveCards(store).map((card) => card.id).length, "B 的候选丢了").toBe(2);
    expect(store.saveStatus, "B 的重读失败没有归 B").toBe("error");
    expect(store.saveError, "B 看不到自己的失败原因").toContain("读取最新事实失败");

    bSeq = 7;
    loadB.resolve(boardPayloadOf(BOARD_B, bSeq, [cardB]) as never);
    await selectingB;
    await settle();
    await waitForDebouncedSave();

    const bPuts = puts().filter((item) => item.boardId === BOARD_B);
    expect(bPuts.length, "回 B 后没有继续恢复保存").toBeGreaterThanOrEqual(2);
    expect(bPuts[bPuts.length - 1].cardIds.length, "恢复保存用的候选不完整").toBe(2);
    expect(store.boardId).toBe(BOARD_B);
  });

  it("收到 409 后重读在途时切到 A、重读成功：A 不受影响，回 B 用最新版本继续", async () => {
    const cardA = textCard("card_a", "A 的内容");
    const cardB = textCard("card_b", "B 的内容");
    let bSeq = 7;
    vi.mocked(api.fetchBoardState).mockImplementation((async (id: string) =>
      boardPayload(
        id === BOARD_A
          ? boardState(BOARD_A, [cardA], 3)
          : boardState(BOARD_B, [cardB], bSeq),
      )) as never);
    await store.selectBoard(BOARD_A);
    await store.selectBoard(BOARD_B);
    const aStatus = store.saveStatus;

    const putFlight = deferred<void>();
    vi.mocked(api.saveBoardState).mockImplementationOnce(async () => {
      await putFlight.promise;
      throw apiError(409, { error: "stale_state", reason: "服务端已有更新的已保存版本", currentSeq: 8 });
    });
    store.commit(addCard(store.board!, { kind: "text", content: "B 新增的卡" }), "新增卡片");
    await waitForDebouncedSave();

    const recoveryRead = deferred<never>();
    vi.mocked(api.fetchBoardState).mockImplementationOnce(() => recoveryRead.promise as never);
    putFlight.resolve();
    await settle();

    await store.selectBoard(BOARD_A);
    bSeq = 8;
    recoveryRead.resolve(boardPayloadOf(BOARD_B, 8, [cardB]) as never);
    await settle();
    await settle();

    expect(store.boardId).toBe(BOARD_A);
    expect(store.saveStatus, "B 的重读结果写进了 A").toBe(aStatus);
    expect(store.dirty, "B 的未保存标记落到了 A").toBe(false);
    expect(liveCards(store).map((card) => card.id), "A 的卡片被改动").toEqual(["card_a"]);
    expect(puts().filter((item) => item.boardId === BOARD_A).length, "向 A 发出了 B 的保存").toBe(0);

    // 回 B：按最新的服务端版本继续保存（不拿旧 seq 再撞一次）
    const loadB = deferred<never>();
    vi.mocked(api.fetchBoardState).mockImplementationOnce(() => loadB.promise as never);
    const selectingB = store.selectBoard(BOARD_B);
    await settle();
    expect(store.boardId).toBe(BOARD_B);
    expect(liveCards(store).map((card) => card.id).length, "B 的候选丢了").toBe(2);

    loadB.resolve(boardPayloadOf(BOARD_B, 8, [cardB]) as never);
    await selectingB;
    await settle();
    await waitForDebouncedSave();

    const bPuts = puts().filter((item) => item.boardId === BOARD_B);
    expect(bPuts.length, "回 B 后没有继续保存").toBeGreaterThanOrEqual(2);
    const finalPut = bPuts[bPuts.length - 1];
    expect(finalPut.cardIds.length, "恢复后的保存候选不完整").toBe(2);
    expect(finalPut.state.seq, "恢复后的保存没有使用已确认的服务端版本").toBe(8);
    expect(store.saveStatus).toBe("saved");
  });

  it("impact_confirmation_required 的重读已落、重新预判在飞时切到 A：确认归 B，回 B 重新核对后确认", async () => {
    const cardA = textCard("card_a", "A 的内容");
    const cardB = textCard("card_b", "B 的内容");
    let bSeq = 7;
    vi.mocked(api.fetchBoardState).mockImplementation((async (id: string) =>
      boardPayload(
        id === BOARD_A
          ? boardState(BOARD_A, [cardA], 3)
          : boardState(BOARD_B, [cardB], bSeq),
      )) as never);
    await store.selectBoard(BOARD_A);
    await store.selectBoard(BOARD_B);

    const putFlight = deferred<void>();
    vi.mocked(api.saveBoardState).mockImplementationOnce(async () => {
      await putFlight.promise;
      throw apiError(409, {
        error: "impact_confirmation_required",
        reason: "这次改动会影响正在运行的任务",
        affectedTasks: [AFFECTED_TASK],
      });
    });
    store.commit(addCard(store.board!, { kind: "text", content: "B 新增的卡" }), "新增卡片");
    await waitForDebouncedSave();

    // 重读在 B 上成功，重新预判挂起
    const recoveryRead = deferred<never>();
    const awayRecheck = deferred<never>();
    vi.mocked(api.fetchBoardState).mockImplementationOnce(() => recoveryRead.promise as never);
    vi.mocked(api.checkMaterialImpact).mockReturnValueOnce(awayRecheck.promise as never);
    putFlight.resolve();
    await settle();
    bSeq = 8;
    recoveryRead.resolve(boardPayloadOf(BOARD_B, 8, [cardB]) as never);
    await settle();
    const checksOnB = impactChecks().filter((item) => item.boardId === BOARD_B);
    expect(checksOnB.length, "前提：恢复后的重新预判已经发给 B").toBe(1);

    // 预判在飞时切到 A，然后放回 B 的预判结果
    await store.selectBoard(BOARD_A);
    awayRecheck.resolve(affectedCheck("chk_away", 8) as never);
    await settle();
    await settle();

    expect(store.boardId).toBe(BOARD_A);
    expect(store.pendingImpact, "B 的待确认状态落到了 A").toBeNull();
    expect(impactChecks().filter((item) => item.boardId === BOARD_A).length, "向 A 发出了 B 的影响预判").toBe(0);
    expect(puts().filter((item) => item.boardId === BOARD_A).length, "向 A 发出了保存").toBe(0);

    // 回 B：重新核对 B 的当前候选与任务范围，再显示可操作的确认
    const loadB = deferred<never>();
    const resumeRecheck = deferred<never>();
    vi.mocked(api.fetchBoardState).mockImplementationOnce(() => loadB.promise as never);
    vi.mocked(api.checkMaterialImpact).mockReturnValueOnce(resumeRecheck.promise as never);
    const selectingB = store.selectBoard(BOARD_B);
    await settle();
    loadB.resolve(boardPayloadOf(BOARD_B, 8, [cardB]) as never);
    await selectingB;
    await settle();

    const checksAfterReturn = impactChecks().filter((item) => item.boardId === BOARD_B);
    expect(checksAfterReturn.length, "回 B 后没有按当前候选重新核对").toBe(2);
    expect(checksAfterReturn[checksAfterReturn.length - 1].stateVersion).toBe(8);
    resumeRecheck.resolve(affectedCheck("chk_b2", 8) as never);
    await settle();
    await settle();

    const pending = store.pendingImpact;
    expect(pending, "回 B 后没有可操作的确认").not.toBeNull();
    expect(pending!.boardId, "确认记录不属于 B").toBe(BOARD_B);
    expect(pending!.checkId).toBe("chk_b2");
    expect(pending!.affected.map((item) => item.intentId)).toEqual([TASK_ID]);

    // 用测试替身走一次有效确认：最终 PUT 的目标板、候选与确认编号一致
    const putCandidateSize = liveCards(store).length;
    await store.confirmImpact();
    await settle();
    await waitForDebouncedSave();
    const bPuts = puts().filter((item) => item.boardId === BOARD_B);
    const finalPut = bPuts[bPuts.length - 1];
    expect(finalPut, "确认后没有发出保存").toBeTruthy();
    expect(finalPut.cardIds.length, "保存的候选不是 B 的当前候选").toBe(putCandidateSize);
    expect(finalPut.state.seq, "确认后的保存没有使用已确认的服务端版本").toBe(8);
    expect(finalPut.confirm?.checkId, "保存没有带上这次确认的编号").toBe("chk_b2");
    expect(puts().filter((item) => item.boardId === BOARD_A).length, "确认后保存发到了 A").toBe(0);
  });
});
describe("#46 第四轮：确认必须匹配板面、候选与任务范围", () => {
  const TASK_ID = "task_impact_1";
  const AFFECTED_TASK = {
    intentId: TASK_ID,
    title: "任务-1",
    materials: ["card_b"],
    consequence: "暂停并保留进度",
  };

  function runningIntent(): never {
    return {
      id: TASK_ID,
      boardId: BOARD_B,
      submissionId: null,
      title: "任务-1",
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

  function withRunningTask(): void {
    vi.mocked(api.fetchIntents).mockResolvedValue({
      intents: [runningIntent()],
      conflicts: [],
      batchAvailable: false,
      recovery: { paused: [] },
    } as never);
  }

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

  function puts(): {
    boardId: string;
    state: BoardState;
    cardIds: string[];
    confirm: { checkId?: string } | undefined;
  }[] {
    return vi.mocked(api.saveBoardState).mock.calls.map((call) => {
      const candidate = call[1] as BoardState;
      return {
        boardId: String(call[0]),
        state: candidate,
        cardIds: (candidate?.cards ?? []).map((card) => card.id),
        confirm: call[3] as { checkId?: string } | undefined,
      };
    });
  }

  function apiError(status: number, payload: Record<string, unknown>): Error {
    return Object.assign(new Error(String(status)), { status, payload });
  }

  function affectedCheck(checkId: string, stateVersion: number): never {
    return {
      ok: true,
      checkId,
      stateVersion,
      affected: [AFFECTED_TASK],
      impactConfirmationRequired: true,
    } as never;
  }

  function boardPayloadOf(boardId: string, seq: number, cards: BoardState["cards"]) {
    return boardPayload(boardState(boardId, cards, seq));
  }

  it("stale_check 的重读已落、重新预判在飞时切到 A：范围变化归 B，回 B 重新确认后保存", async () => {
    const cardA = textCard("card_a", "A 的内容");
    const cardB = textCard("card_b", "B 的内容");
    let bSeq = 7;
    vi.mocked(api.fetchBoardState).mockImplementation((async (id: string) =>
      boardPayload(
        id === BOARD_A
          ? boardState(BOARD_A, [cardA], 3)
          : boardState(BOARD_B, [cardB], bSeq),
      )) as never);
    await store.selectBoard(BOARD_A);
    await store.selectBoard(BOARD_B);

    const putFlight = deferred<void>();
    vi.mocked(api.saveBoardState).mockImplementationOnce(async () => {
      await putFlight.promise;
      throw apiError(409, {
        error: "stale_check",
        reason: "确认已经过期",
        scopeChanged: true,
        affectedTasks: [AFFECTED_TASK],
      });
    });
    store.commit(addCard(store.board!, { kind: "text", content: "B 新增的卡" }), "新增卡片");
    await waitForDebouncedSave();

    const recoveryRead = deferred<never>();
    const awayRecheck = deferred<never>();
    vi.mocked(api.fetchBoardState).mockImplementationOnce(() => recoveryRead.promise as never);
    vi.mocked(api.checkMaterialImpact).mockReturnValueOnce(awayRecheck.promise as never);
    putFlight.resolve();
    await settle();
    bSeq = 8;
    recoveryRead.resolve(boardPayloadOf(BOARD_B, 8, [cardB]) as never);
    await settle();
    expect(impactChecks().filter((item) => item.boardId === BOARD_B).length, "前提：重新预判已经发给 B").toBe(1);

    await store.selectBoard(BOARD_A);
    awayRecheck.resolve(affectedCheck("chk_stale_away", 8) as never);
    await settle();
    await settle();

    expect(store.boardId).toBe(BOARD_A);
    expect(store.pendingImpact, "B 的待确认状态落到了 A").toBeNull();
    expect(impactChecks().filter((item) => item.boardId === BOARD_A).length, "向 A 发出了 B 的重新预判").toBe(0);
    expect(puts().filter((item) => item.boardId === BOARD_A).length, "向 A 发出了保存").toBe(0);

    const loadB = deferred<never>();
    const resumeRecheck = deferred<never>();
    vi.mocked(api.fetchBoardState).mockImplementationOnce(() => loadB.promise as never);
    vi.mocked(api.checkMaterialImpact).mockReturnValueOnce(resumeRecheck.promise as never);
    const selectingB = store.selectBoard(BOARD_B);
    await settle();
    loadB.resolve(boardPayloadOf(BOARD_B, 8, [cardB]) as never);
    await selectingB;
    await settle();

    expect(impactChecks().filter((item) => item.boardId === BOARD_B).length, "回 B 后没有重新核对范围").toBe(2);
    resumeRecheck.resolve(affectedCheck("chk_stale_b2", 8) as never);
    await settle();
    await settle();

    const pending = store.pendingImpact;
    expect(pending, "回 B 后没有可操作的确认").not.toBeNull();
    expect(pending!.boardId, "确认记录不属于 B").toBe(BOARD_B);
    expect(pending!.checkId).toBe("chk_stale_b2");
    expect(pending!.affected.map((item) => item.intentId).sort()).toEqual([TASK_ID].sort());
    expect(pending!.note ?? "", "范围变化没有按既有机制重新说明").toMatch(/重新核对|重新确认/);

    const candidateSize = liveCards(store).length;
    await store.confirmImpact();
    await settle();
    await waitForDebouncedSave();
    const bPuts = puts().filter((item) => item.boardId === BOARD_B);
    const finalPut = bPuts[bPuts.length - 1];
    expect(finalPut.confirm?.checkId, "保存没有带上重新确认的编号").toBe("chk_stale_b2");
    expect(finalPut.cardIds.length, "保存的候选不是 B 的当前候选").toBe(candidateSize);
    expect(finalPut.state.seq, "重新确认后的保存没有使用已确认的服务端版本").toBe(8);
    expect(puts().filter((item) => item.boardId === BOARD_A).length).toBe(0);
  });

  it("确认已显示时切到 A 再回 B 继续编辑：A 消费不了 B 的确认，B 先重新核对再保存", async () => {
    withRunningTask();
    const cardA = textCard("card_a", "A 的内容");
    const cardB = textCard("card_b", "B 的内容");
    vi.mocked(api.fetchBoardState).mockImplementation((async (id: string) =>
      boardPayload(
        id === BOARD_A ? boardState(BOARD_A, [cardA], 3) : boardState(BOARD_B, [cardB], 7),
      )) as never);
    await store.selectBoard(BOARD_A);
    await store.selectBoard(BOARD_B);

    const firstCheck = deferred<never>();
    vi.mocked(api.checkMaterialImpact).mockReturnValueOnce(firstCheck.promise as never);
    store.commit(addCard(store.board!, { kind: "text", content: "第一版" }), "新增卡片");
    await waitForDebouncedSave();
    firstCheck.resolve(affectedCheck("chk_1", 7) as never);
    await settle();
    await settle();
    expect(store.pendingImpact, "前提：B 的确认已经显示").not.toBeNull();
    expect(store.pendingImpact!.boardId).toBe(BOARD_B);

    // 切到 A：B 的确认不允许被 A 消费
    await store.selectBoard(BOARD_A);
    expect(store.boardId).toBe(BOARD_A);
    expect(store.pendingImpact, "B 的确认显示到了 A").toBeNull();
    const aStatus = store.saveStatus;
    const outcome = await store.confirmImpact();
    await settle();
    await waitForDebouncedSave();
    expect(outcome.outcome, "A 消费了不属于它的确认").not.toBe("saved");
    expect(puts().filter((item) => item.boardId === BOARD_A).length, "A 因为 B 的确认发出了保存").toBe(0);
    expect(store.saveStatus).toBe(aStatus);

    // 回 B：确认记录取回，候选又有新编辑 → 必须先重新核对
    await store.selectBoard(BOARD_B);
    await settle();
    expect(store.pendingImpact?.checkId, "B 的确认记录丢了").toBe("chk_1");
    store.commit(addCard(store.board!, { kind: "text", content: "第二版" }), "新增卡片");
    const latestSize = liveCards(store).length;
    await settle();

    const secondCheck = deferred<never>();
    vi.mocked(api.checkMaterialImpact).mockReturnValueOnce(secondCheck.promise as never);
    const confirming = store.confirmImpact();
    await settle();
    const checks = impactChecks();
    const recheck = checks[checks.length - 1];
    expect(recheck.boardId, "重新核对没有发往 B").toBe(BOARD_B);
    expect(recheck.cardIds.length, "重新核对用的不是最新候选").toBe(latestSize);
    expect(recheck.stateVersion, "重新核对没有使用已确认的服务端版本").toBe(7);
    secondCheck.resolve(affectedCheck("chk_2", 7) as never);
    const confirmResult = await confirming;
    await settle();
    await settle();
    expect(confirmResult.outcome, "候选变了却直接保存了").toBe("needs_confirm");
    expect(store.pendingImpact?.checkId, "确认编号没有换成重新核对后的那份").toBe("chk_2");

    // 再确认一次：最终 PUT 的目标板、候选与确认编号一致
    await store.confirmImpact();
    await settle();
    await waitForDebouncedSave();
    const bPuts = puts().filter((item) => item.boardId === BOARD_B);
    const finalPut = bPuts[bPuts.length - 1];
    expect(finalPut.confirm?.checkId).toBe("chk_2");
    expect(finalPut.cardIds.length, "最终保存的候选不是最新候选").toBe(latestSize);
    expect(finalPut.state.seq, "最终保存没有使用已确认的服务端版本").toBe(7);
    expect(puts().filter((item) => item.boardId === BOARD_A).length).toBe(0);
    expect(store.saveStatus).toBe("saved");
  });
});
describe("#46 第五轮：整轮恢复次数与在途重新预判接回", () => {
  const TASK_ID = "task_impact_1";
  const AFFECTED_TASK = {
    intentId: TASK_ID,
    title: "任务-1",
    materials: ["card_b"],
    consequence: "暂停并保留进度",
  };

  function apiError(status: number, payload: Record<string, unknown>): Error {
    return Object.assign(new Error(String(status)), { status, payload });
  }

  /** 每次 PUT 的「目标板面 + 候选版本 + 卡片集合」。 */
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

  /** 每次重新预判的「目标板面 + 依据版本 + 候选卡片」。 */
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

  function affectedCheck(checkId: string, stateVersion: number): never {
    return {
      ok: true,
      checkId,
      stateVersion,
      affected: [AFFECTED_TASK],
      impactConfirmationRequired: true,
    } as never;
  }

  function wireBoardPayloads() {
    const cardA = textCard("card_a", "A 的内容");
    const cardB = textCard("card_b", "B 的内容");
    const state = { bSeq: 7 };
    vi.mocked(api.fetchBoardState).mockImplementation((async (id: string) =>
      boardPayload(
        boardState(id, id === BOARD_A ? [cardA] : [cardB], id === BOARD_A ? 3 : state.bSeq),
      )) as never);
    return { cardA, cardB, state };
  }

  /**
   * 前提装置：B 的 PUT 被服务端要求影响确认 → 恢复重读挂起 → 切到 A → 重读在 A 上成功返回。
   * 结束时恢复计划留在 B（A 上没有任何待确认状态），供后续「切回原板」用例使用。
   */
  async function leaveRecoveryPlanOnB(
    cardB: BoardState["cards"][number],
    state: { bSeq: number },
  ): Promise<void> {
    const putFlight = deferred<void>();
    vi.mocked(api.saveBoardState).mockImplementationOnce(async () => {
      await putFlight.promise;
      throw apiError(409, {
        error: "impact_confirmation_required",
        reason: "需要影响确认",
        affectedTasks: [AFFECTED_TASK],
      });
    });
    store.commit(addCard(store.board!, { kind: "text", content: "B 新增的卡" }), "新增卡片");
    await waitForDebouncedSave();
    expect(putLog().filter((item) => item.boardId === BOARD_B).length, "前提：B 的 PUT 已发出").toBe(1);

    const recoveryRead = deferred<never>();
    vi.mocked(api.fetchBoardState).mockImplementationOnce(() => recoveryRead.promise as never);
    putFlight.resolve();
    await settle();
    await store.selectBoard(BOARD_A);
    state.bSeq = 8;
    recoveryRead.resolve(boardPayload(boardState(BOARD_B, [cardB], 8)) as never);
    await settle();
    await settle();
    expect(store.boardId).toBe(BOARD_A);
    expect(store.pendingImpact, "前提：待确认状态没有留在 A").toBeNull();
  }

  it("连续 409（每次重读成功且版本推进）：首次 PUT 后最多 3 次自动重试，达到上限停止并保留候选", async () => {
    const { cardA, cardB } = wireBoardPayloads();
    let serverSeq = 7;
    vi.mocked(api.fetchBoardState).mockImplementation((async (id: string) =>
      boardPayload(
        boardState(id, id === BOARD_A ? [cardA] : [cardB], id === BOARD_A ? 3 : serverSeq),
      )) as never);
    const puts: { boardId: string; seq: number; cardIds: string[] }[] = [];
    /** 前 4 次 PUT 都 409（第 4 次触发上限）；之后按「还剩几次失败」控制重试。 */
    let failuresRemaining = 4;
    vi.mocked(api.saveBoardState).mockImplementation(async (id: string, candidate: BoardState) => {
      const state = candidate as BoardState;
      puts.push({
        boardId: String(id),
        seq: Number(state.seq ?? 0),
        cardIds: (state.cards ?? []).map((card) => card.id),
      });
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
    });

    await store.selectBoard(BOARD_A);
    const aStatus = store.saveStatus;
    await store.selectBoard(BOARD_B);

    vi.useFakeTimers();
    try {
      store.commit(addCard(store.board!, { kind: "text", content: "B 新增的卡" }), "新增卡片");
      await vi.advanceTimersByTimeAsync(4200);

      const autoPuts = puts.filter((item) => item.boardId === BOARD_B);
      expect(autoPuts.length, "首次 PUT 之后的自动重试超过了 3 次").toBe(4);
      expect(autoPuts.map((item) => item.seq), "重读推进的服务端版本没有被用上").toEqual([7, 8, 9, 10]);

      await vi.advanceTimersByTimeAsync(3000);
      expect(puts.filter((item) => item.boardId === BOARD_B).length, "达到上限后仍在自动重试").toBe(4);
      expect(store.boardId).toBe(BOARD_B);
      expect(store.saveStatus, "达到上限后没有退出保存中 / 失败态").toBe("error");
      expect(store.saveError, "没有给出可理解的上限原因").toContain("3 次");
      expect(store.dirty, "失败后候选被当成已保存").toBe(true);
      expect(liveCards(store).length, "原改动丢了").toBe(2);
      expect(store.pendingImpact).toBeNull();
      expect(puts.filter((item) => item.boardId === BOARD_A).length, "向 A 发出了 B 的保存").toBe(0);
      expect(store.saveStatus === "saved", "读取成功被显示成保存成功").toBe(false);

      // 用户点重试：从原板最新事实开始新一轮（先再撞一次 409 → 读最新版本 → 保存成功）
      failuresRemaining = 1;
      await store.saveNow();
      await vi.advanceTimersByTimeAsync(1500);

      const afterRetry = puts.filter((item) => item.boardId === BOARD_B);
      expect(afterRetry.length, "重试没有跑完一轮").toBe(6);
      const finalPut = afterRetry[afterRetry.length - 1];
      expect(finalPut.seq, "重试没有基于原板最新版本").toBe(serverSeq);
      expect(store.saveStatus).toBe("saved");
      expect(store.dirty).toBe(false);
    } finally {
      vi.useRealTimers();
    }
    expect(store.pendingImpact).toBeNull();
    expect(aStatus).toBe("saved");
  });

  it("自动恢复中切到 A 再切回 B：继续使用 B 已消耗的次数，A 的归属不被污染", async () => {
    const { cardA, cardB } = wireBoardPayloads();
    let serverSeq = 7;
    vi.mocked(api.fetchBoardState).mockImplementation((async (id: string) =>
      boardPayload(
        boardState(id, id === BOARD_A ? [cardA] : [cardB], id === BOARD_A ? 3 : serverSeq),
      )) as never);
    const puts: { boardId: string; seq: number }[] = [];
    vi.mocked(api.saveBoardState).mockImplementation(async (id: string, candidate: BoardState) => {
      const state = candidate as BoardState;
      puts.push({ boardId: String(id), seq: Number(state.seq ?? 0) });
      serverSeq += 1;
      throw apiError(409, {
        error: "stale_state",
        reason: "服务端已有更新的已保存版本",
        currentSeq: serverSeq,
      });
    });

    await store.selectBoard(BOARD_A);
    const aStatus = store.saveStatus;
    const aCards = liveCards(store).map((card) => card.id);
    await store.selectBoard(BOARD_B);

    vi.useFakeTimers();
    try {
      store.commit(addCard(store.board!, { kind: "text", content: "B 新增的卡" }), "新增卡片");
      await vi.advanceTimersByTimeAsync(1000);
      expect(puts.filter((item) => item.boardId === BOARD_B).length, "前提：B 已经消耗过一次自动恢复").toBe(2);

      // 自动恢复进行中切到 A：A 的候选 / 保存状态 / 待确认记录维持自己的归属
      await store.selectBoard(BOARD_A);
      await settle();
      expect(store.boardId).toBe(BOARD_A);
      expect(liveCards(store).map((card) => card.id)).toEqual(aCards);
      expect(store.saveStatus).toBe(aStatus);
      expect(store.dirty).toBe(false);
      expect(store.pendingImpact).toBeNull();
      expect(puts.filter((item) => item.boardId === BOARD_A).length, "向 A 发出了 B 的自动恢复").toBe(0);

      // 切回 B：继续使用已经消耗掉的次数（总共 4 次 PUT 就停，而不是从 1 重新数）
      await store.selectBoard(BOARD_B);
      await settle();
      await vi.advanceTimersByTimeAsync(3000);

      expect(puts.filter((item) => item.boardId === BOARD_B).length, "切板往返把 B 的重试次数清零了").toBe(4);
      expect(store.saveStatus).toBe("error");
      expect(store.saveError).toContain("3 次");
      expect(store.dirty).toBe(true);
      expect(liveCards(store).length).toBe(2);
      expect(store.pendingImpact).toBeNull();
    } finally {
      vi.useRealTimers();
    }
  });

  it("返回 B 后重新预判在途时再次 B → A → B：同板同计划同候选只发一次，只形成一份确认", async () => {
    const { cardB, state } = wireBoardPayloads();
    await store.selectBoard(BOARD_A);
    await store.selectBoard(BOARD_B);
    await leaveRecoveryPlanOnB(cardB, state);

    // 回到 B：load 落定后开始重新预判（挂起）
    const loadB1 = deferred<never>();
    vi.mocked(api.fetchBoardState).mockImplementationOnce(() => loadB1.promise as never);
    const check1 = deferred<never>();
    vi.mocked(api.checkMaterialImpact).mockReturnValueOnce(check1.promise as never);
    const back1 = store.selectBoard(BOARD_B);
    await settle();
    loadB1.resolve(boardPayload(boardState(BOARD_B, [cardB], 8)) as never);
    await back1;
    await settle();
    expect(impactChecks().filter((item) => item.boardId === BOARD_B).length, "回到 B 后没有发出重新预判").toBe(1);

    // 预判仍在途时再次 B → A → B：接回同一份在途操作，不重复发
    await store.selectBoard(BOARD_A);
    const loadB2 = deferred<never>();
    vi.mocked(api.fetchBoardState).mockImplementationOnce(() => loadB2.promise as never);
    const back2 = store.selectBoard(BOARD_B);
    await settle();
    loadB2.resolve(boardPayload(boardState(BOARD_B, [cardB], 8)) as never);
    await back2;
    await settle();
    await settle();
    expect(
      impactChecks().filter((item) => item.boardId === BOARD_B).length,
      "再次切回 B 时对同一份恢复又发了一次重新预判",
    ).toBe(1);

    // 响应回来：只形成一份有效确认
    check1.resolve(affectedCheck("chk_round5", 8) as never);
    await settle();
    await settle();
    const pending = store.pendingImpact;
    expect(pending, "没有形成可操作的确认").not.toBeNull();
    expect(pending!.boardId).toBe(BOARD_B);
    expect(pending!.checkId).toBe("chk_round5");
    expect(pending!.affected.map((item) => item.intentId)).toEqual([TASK_ID]);
    expect(impactChecks().filter((item) => item.boardId === BOARD_B).length).toBe(1);

    // 确认已显示后再切 A / 回 B：确认记录取回，不再发新的预判
    await store.selectBoard(BOARD_A);
    expect(store.pendingImpact, "B 的确认显示到了 A").toBeNull();
    await store.selectBoard(BOARD_B);
    await settle();
    expect(store.pendingImpact?.checkId).toBe("chk_round5");
    expect(impactChecks().filter((item) => item.boardId === BOARD_B).length).toBe(1);

    // 确认后保存：目标板面 / 候选 / 版本 / 确认编号一致，A 没有收到任何相关请求
    const candidateSize = liveCards(store).length;
    await store.confirmImpact();
    await settle();
    await waitForDebouncedSave();
    const bPuts = putLog().filter((item) => item.boardId === BOARD_B);
    const finalPut = bPuts[bPuts.length - 1];
    expect(finalPut, "确认后没有发出保存").toBeTruthy();
    expect(finalPut.cardIds.length).toBe(candidateSize);
    expect(finalPut.seq).toBe(8);
    expect(vi.mocked(api.saveBoardState).mock.calls[vi.mocked(api.saveBoardState).mock.calls.length - 1]?.[3]).toEqual({ checkId: "chk_round5", stateVersion: 8 });
    expect(putLog().filter((item) => item.boardId === BOARD_A).length, "向 A 发出了 B 的保存").toBe(0);
  });

  it("重新预判期间修改 B、新旧结果反序返回：最终确认属于最新有效候选", async () => {
    const { cardB, state } = wireBoardPayloads();
    await store.selectBoard(BOARD_A);
    await store.selectBoard(BOARD_B);
    await leaveRecoveryPlanOnB(cardB, state);

    const loadB = deferred<never>();
    vi.mocked(api.fetchBoardState).mockImplementationOnce(() => loadB.promise as never);
    const oldCheck = deferred<never>();
    const newCheck = deferred<never>();
    vi.mocked(api.checkMaterialImpact)
      .mockReturnValueOnce(oldCheck.promise as never)
      .mockReturnValueOnce(newCheck.promise as never);
    const back = store.selectBoard(BOARD_B);
    await settle();
    loadB.resolve(boardPayload(boardState(BOARD_B, [cardB], 8)) as never);
    await back;
    await settle();
    expect(impactChecks().filter((item) => item.boardId === BOARD_B).length, "回到 B 后没有发出重新预判").toBe(1);

    // 旧预判在途时修改 B，并触发保存：需要按最新候选重新核对（同一轮恢复、新步骤）
    store.commit(addCard(store.board!, { kind: "text", content: "第二版" }), "新增卡片");
    const latestSize = liveCards(store).length;
    await store.saveNow();
    await settle();
    const checks = impactChecks().filter((item) => item.boardId === BOARD_B);
    expect(checks.length, "候选变化后没有按最新事实重新预判").toBe(2);
    expect(checks[1].cardIds.length, "重新预判用的不是最新候选").toBe(latestSize);

    // 新响应先回：形成最新候选的确认
    newCheck.resolve(affectedCheck("chk_new", 8) as never);
    await settle();
    await settle();
    expect(store.pendingImpact?.checkId, "最新候选没有形成确认").toBe("chk_new");

    // 旧响应迟到：不许覆盖较新的确认，也不许再触发新的预判
    oldCheck.resolve(affectedCheck("chk_old", 8) as never);
    await settle();
    await settle();
    expect(store.pendingImpact?.checkId, "旧响应覆盖了较新的确认").toBe("chk_new");
    expect(impactChecks().filter((item) => item.boardId === BOARD_B).length, "旧响应又触发了新的预判").toBe(2);

    // 确认后保存：目标板、候选、版本与确认编号都属于最新有效候选
    const candidateSize = liveCards(store).length;
    await store.confirmImpact();
    await settle();
    await waitForDebouncedSave();
    const bPuts = putLog().filter((item) => item.boardId === BOARD_B);
    const finalPut = bPuts[bPuts.length - 1];
    expect(finalPut, "确认后没有发出保存").toBeTruthy();
    expect(finalPut.cardIds.length, "确认后保存的不是最新候选").toBe(candidateSize);
    expect(finalPut.seq).toBe(8);
    expect(vi.mocked(api.saveBoardState).mock.calls[vi.mocked(api.saveBoardState).mock.calls.length - 1]?.[3]).toEqual({ checkId: "chk_new", stateVersion: 8 });
    expect(putLog().filter((item) => item.boardId === BOARD_A).length, "向 A 发出了 B 的保存").toBe(0);
  });
});
