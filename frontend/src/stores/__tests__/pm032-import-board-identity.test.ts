/**
 * #46 返修（PM-QIO032 集成验收）：上传回执必须与**已加载的原板面**匹配。
 *
 * 已证实的残留缺陷：`selectBoard()` 先更新 boardId 再异步 `load()`。
 * 切回 A 但 A 还没读到时，boardId 已经是 A、内存里的 board 仍是 B；
 * 此时 A 的附件回执能通过「只比 boardId」的校验，把含 B 卡片的对象交给 commit()。
 *
 * 本文件锁死的行为（全部走 store 的公开动作，不 mock store 内部）：
 * 1. 通过 selectBoard() 复现 A 上传 → 切到 B → 切回 A 并延迟 A 的读取 → 上传先成功：
 *    A 加载前 commit 与保存请求都收不到 B 的内容；A 加载后关联只发生一次，
 *    保存请求的板面 ID / 文件卡 / 附件都属于 A；
 * 2. 原卡删除、换绑、旧回执晚到、批次新建卡（无预建卡）四条补测；
 * 3. 刷新 / 重读两块板：B 内容完整、A 没有混入 B 的卡片。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { flushPromises } from "@vue/test-utils";
import { useInteractiveStore } from "../interactive";
import * as api from "../../services/interactive";
import * as attachments from "../../services/attachments";
import { addCard, emptyState, removeCard, updateCard } from "../../interactive/board";
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

/** 保存请求里出现过的板面 ID（PUT 的第一个参数）。 */
function savedBoardIds(): string[] {
  return vi.mocked(api.saveBoardState).mock.calls.map((call) => String(call[0]));
}

/** 保存请求里出现过的卡片 id（PUT 的第二个参数）。 */
function savedCardIds(): string[] {
  const ids: string[] = [];
  for (const call of vi.mocked(api.saveBoardState).mock.calls) {
    const state = call[1] as BoardState;
    for (const card of state?.cards ?? []) ids.push(card.id);
  }
  return ids;
}

async function settle(times = 6): Promise<void> {
  for (let i = 0; i < times; i += 1) {
    await flushPromises();
  }
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
  vi.mocked(api.saveBoardState).mockImplementation((async (_id: string, candidate: BoardState) => ({
    ok: true,
    seq: Number(candidate?.seq ?? 3) + 1,
    savedAt: NOW,
    state: candidate,
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

describe("#46 跨板回执只关联已加载的原板面", () => {
  it("A 上传 → 切到 B → 切回 A 且延迟 A 的读取：A 加载前不写板面，加载后只关联一次且都属于 A", async () => {
    // 板面 A：一张已有文件卡（上传目标）
    const cardA = fileCard("card_a", "A 的文件.txt");
    vi.mocked(api.fetchBoardState).mockImplementation((async (id: string) =>
      boardPayload(
        id === BOARD_A ? boardState(BOARD_A, [cardA]) : boardState(BOARD_B, [textCard("card_b", "B 的内容")]),
      )) as never);
    await store.load();
    // 切到 A（真实读取）
    await store.selectBoard(BOARD_A);
    expect(store.boardId).toBe(BOARD_A);
    expect(liveCards(store).map((card) => card.id)).toEqual(["card_a"]);

    // A 上发起上传：上传先挂起
    const flying = deferred<AttachmentRef>();
    vi.mocked(attachments.uploadAttachment).mockReturnValueOnce(flying.promise);
    const started = store.startCardAttachment({ cardId: "card_a", source: { file: file("A 的文件.txt") } });
    await settle();
    expect(attachments.uploadAttachment).toHaveBeenCalledTimes(1);

    // 切到 B：真实读取 B
    await store.selectBoard(BOARD_B);
    expect(store.boardId).toBe(BOARD_B);
    expect(liveCards(store).map((card) => card.id)).toEqual(["card_b"]);

    // 切回 A，但 A 的读取被挂起（A 还没读到，内存里仍是 B 的内容）
    const readingA = deferred<void>();
    vi.mocked(api.fetchBoardState).mockImplementationOnce(async () => {
      await readingA.promise;
      return boardPayload(boardState(BOARD_A, [cardA])) as never;
    });
    const selecting = store.selectBoard(BOARD_A);
    await settle();
    expect(store.boardId, "本用例前提：boardId 已经是 A").toBe(BOARD_A);
    expect(liveCards(store).map((card) => card.id), "本用例前提：内存里仍是 B 的内容").toEqual([
      "card_b",
    ]);

    // 上传先成功：A 还没加载 —— 不许写板面，也不许把 B 的内容交给 commit / 保存
    const saveCallsBefore = vi.mocked(api.saveBoardState).mock.calls.length;
    flying.resolve(attachment("A 的文件.txt", { id: "att_a" }));
    await settle();
    expect(
      vi.mocked(api.saveBoardState).mock.calls.length,
      "A 加载前就发出了保存请求",
    ).toBe(saveCallsBefore);
    expect(savedCardIds(), "保存请求里出现了 B 的卡片").not.toContain("card_b");
    expect(liveCards(store).map((card) => card.id), "A 加载前板面被写成了别的内容").toEqual([
      "card_b",
    ]);

    // A 读取落定：回执继续关联，只关联一次，且板面 ID / 文件卡 / 附件都属于 A
    readingA.resolve();
    await selecting;
    await settle();
    const result = await started.done;
    await settle();

    expect(result.ok, "A 加载后回执没有完成关联：" + String(result.error)).toBe(true);
    expect(result.boardId).toBe(BOARD_A);
    expect(result.cardId).toBe("card_a");
    expect(attachments.uploadAttachment, "回执被重复处理（重复上传）").toHaveBeenCalledTimes(1);

    expect(store.boardId).toBe(BOARD_A);
    expect(linkedAttachmentId(store, "card_a")).toBe("att_a");
    expect(liveCards(store).map((card) => card.id), "A 上出现了不属于它的卡片").toEqual(["card_a"]);

    // 保存请求：板面 ID 是 A，卡片是 A 的卡，附件是这次上传的附件
    // （关联走 store 的公开 commit → 排保存；等防抖真的发出这一次 PUT）
    await new Promise((resolve) => setTimeout(resolve, 600));
    await settle();
    const ids = savedBoardIds();
    expect(ids.length, "关联没有产生保存请求").toBeGreaterThan(0);
    expect(ids.every((id) => id === BOARD_A), "保存请求写到了别的板面：" + ids.join(",")).toBe(true);
    expect(savedCardIds()).toContain("card_a");
    expect(savedCardIds(), "保存请求里混进了 B 的卡片").not.toContain("card_b");
    const saveCalls = vi.mocked(api.saveBoardState).mock.calls;
    const lastPut = saveCalls[saveCalls.length - 1][1] as BoardState;
    expect(lastPut.boardId).toBe(BOARD_A);
    expect(linkedAttachmentId(store, "card_a")).toBe("att_a");
  });

  it("原板面读取失败（空占位）时回执不写板面，如实说明原因", async () => {
    const cardA = fileCard("card_a", "A 的文件.txt");
    vi.mocked(api.fetchBoardState).mockImplementation((async (id: string) =>
      boardPayload(id === BOARD_A ? boardState(BOARD_A, [cardA]) : boardState(BOARD_B))) as never);
    await store.selectBoard(BOARD_A);

    const flying = deferred<AttachmentRef>();
    vi.mocked(attachments.uploadAttachment).mockReturnValueOnce(flying.promise);
    const started = store.startCardAttachment({ cardId: "card_a", source: { file: file("A 的文件.txt") } });
    await settle();

    // 切走再切回 A，但这次 A 的读取失败（load 会补一块空占位）
    await store.selectBoard(BOARD_B);
    vi.mocked(api.fetchBoardState).mockRejectedValueOnce(new Error("读取超时") as never);
    const selecting = store.selectBoard(BOARD_A);
    await settle();
    flying.resolve(attachment("A 的文件.txt", { id: "att_a" }));
    await settle();
    await selecting;
    const result = await started.done;
    await settle();

    expect(result.ok, "读取失败的空占位被当成了已加载的原板面").toBe(false);
    expect(result.error || "").toContain("原板面");
    expect(savedCardIds(), "读取失败期间把回执写进了板面").not.toContain("card_a");
  });

  it("原卡已删除：迟到回执不重建卡、不写板面", async () => {
    const cardA = fileCard("card_a", "A 的文件.txt");
    vi.mocked(api.fetchBoardState).mockImplementation((async (id: string) =>
      boardPayload(id === BOARD_A ? boardState(BOARD_A, [cardA]) : boardState(BOARD_B))) as never);
    await store.selectBoard(BOARD_A);

    const flying = deferred<AttachmentRef>();
    vi.mocked(attachments.uploadAttachment).mockReturnValueOnce(flying.promise);
    const started = store.startCardAttachment({ cardId: "card_a", source: { file: file("A 的文件.txt") } });
    await settle();

    // 用户删掉原卡（真实 commit 路径）
    store.commit(removeCard(store.board!, "card_a"), "删除卡片");
    await settle();

    flying.resolve(attachment("A 的文件.txt", { id: "att_a" }));
    const result = await started.done;
    await settle();

    expect(result.ok).toBe(false);
    expect(result.error || "").toContain("原文件卡已不存在");
    expect(liveCards(store), "迟到回执把删掉的卡重建了").toHaveLength(0);
  });

  it("原卡已换绑：迟到回执不覆盖新关联", async () => {
    const cardA = fileCard("card_a", "旧.txt", "att_old");
    vi.mocked(api.fetchBoardState).mockImplementation((async (id: string) =>
      boardPayload(id === BOARD_A ? boardState(BOARD_A, [cardA]) : boardState(BOARD_B))) as never);
    await store.selectBoard(BOARD_A);

    const flying = deferred<AttachmentRef>();
    vi.mocked(attachments.uploadAttachment).mockReturnValueOnce(flying.promise);
    const started = store.startCardAttachment({ cardId: "card_a", source: { file: file("新.txt") } });
    await settle();

    store.commit(
      updateCard(store.board!, "card_a", {
        meta: { name: "别的.txt", file: { attachmentId: "att_other", name: "别的.txt" } },
      }),
      "换附件",
    );
    await settle();

    flying.resolve(attachment("新.txt", { id: "att_late" }));
    const result = await started.done;
    await settle();

    expect(result.ok).toBe(false);
    expect(result.error || "").toContain("已经被另一次操作替换");
    expect(linkedAttachmentId(store, "card_a"), "迟到回执覆盖了用户已换上的附件").toBe("att_other");
  });

  it("旧回执晚到（同一张卡上更新的尝试）：不覆盖新结果、不重复关联", async () => {
    const cardA = fileCard("card_a", "卡");
    vi.mocked(api.fetchBoardState).mockImplementation((async (id: string) =>
      boardPayload(id === BOARD_A ? boardState(BOARD_A, [cardA]) : boardState(BOARD_B))) as never);
    await store.selectBoard(BOARD_A);

    const first = deferred<AttachmentRef>();
    const second = deferred<AttachmentRef>();
    vi.mocked(attachments.uploadAttachment).mockReturnValueOnce(first.promise);
    vi.mocked(attachments.uploadAttachment).mockReturnValueOnce(second.promise);
    const firstStarted = store.startCardAttachment({ cardId: "card_a", source: { file: file("第一次.txt") } });
    const secondStarted = store.startCardAttachment({ cardId: "card_a", source: { file: file("第二次.txt") } });
    await settle();

    second.resolve(attachment("第二次.txt", { id: "att_second" }));
    const secondResult = await secondStarted.done;
    await settle();
    expect(secondResult.ok).toBe(true);

    first.resolve(attachment("第一次.txt", { id: "att_first" }));
    const firstResult = await firstStarted.done;
    await settle();

    expect(firstResult.superseded).toBe(true);
    expect(firstResult.supersededBy).toBe("card");
    expect(linkedAttachmentId(store, "card_a"), "旧回执覆盖了更新的关联").toBe("att_second");
  });

  it("批次新建卡（无预建卡）：跨板等待后只在原板面建一张卡，保存请求属于原板面", async () => {
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
    expect(store.importBatchById(batch.id)!.items[0].status).toBe("uploading");

    // 切到 B，再切回 A 且延迟 A 的读取
    await store.selectBoard(BOARD_B);
    const readingA = deferred<void>();
    vi.mocked(api.fetchBoardState).mockImplementationOnce(async () => {
      await readingA.promise;
      return boardPayload(boardState(BOARD_A)) as never;
    });
    const selecting = store.selectBoard(BOARD_A);
    await settle();

    flying.resolve(attachment("批次文件.txt", { id: "att_batch" }));
    await settle();
    expect(savedCardIds(), "A 加载前就在板面上建了卡").not.toContain("card_b");

    readingA.resolve();
    await selecting;
    const result = await run;
    await settle();

    expect(result.ok, "A 加载后批次没有完成建卡：" + String(result.error)).toBe(true);
    expect(result.boardId).toBe(BOARD_A);
    const live = liveCards(store);
    expect(live, "原板面没有建卡，或建了重复卡").toHaveLength(1);
    expect(live[0].kind).toBe("file");
    expect(linkedAttachmentId(store, live[0].id)).toBe("att_batch");
    expect(store.importBatchById(batch.id)!.items[0].status).toBe("succeeded");
    expect(savedBoardIds().every((id) => id === BOARD_A)).toBe(true);
    expect(savedCardIds()).not.toContain("card_b");
  });

  it("刷新 / 重读两块板：B 内容完整，A 没有混入 B 的卡片", async () => {
    const cardA = fileCard("card_a", "A 的文件.txt");
    vi.mocked(api.fetchBoardState).mockImplementation((async (id: string) =>
      boardPayload(
        id === BOARD_A
          ? boardState(BOARD_A, [cardA])
          : boardState(BOARD_B, [textCard("card_b", "B 的内容"), textCard("card_b2", "B 的第二张")]),
      )) as never);

    await store.selectBoard(BOARD_A);
    expect(liveCards(store).map((card) => card.id)).toEqual(["card_a"]);

    await store.selectBoard(BOARD_B);
    expect(liveCards(store).map((card) => card.id)).toEqual(["card_b", "card_b2"]);

    // 重读 B：内容完整
    await store.refreshBoardFromServer();
    await settle();
    expect(liveCards(store).map((card) => card.id)).toEqual(["card_b", "card_b2"]);

    // 重读 A：只有 A 的卡片，没有混入 B
    await store.selectBoard(BOARD_A);
    await store.refreshBoardFromServer();
    await settle();
    expect(liveCards(store).map((card) => card.id)).toEqual(["card_a"]);
    expect(store.board?.boardId).toBe(BOARD_A);
  });
});
