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

afterEach(() => {
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
