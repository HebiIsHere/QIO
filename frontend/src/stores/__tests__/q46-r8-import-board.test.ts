/**
 * #46 第八轮（任务书 四.11 / 四.12）：**批量导入的反馈和重试入口归原板**，
 * 以及**撤销附件关联后及时结束失效的等待**。
 *
 * 基线 c9fedc60 上已证实的残留缺陷：
 * - 四.11：批次反馈只有全局一份（latestImportBatchId / currentImportBatch / counts /
 *   hasImportBatchInFlight），不按板面取。在 B 拖入文件后切到 A，A 上照样显示 B 的批次项、
 *   失败与「重试」入口；点重试也以 B 的批次身份继续，界面归属与反馈都是错的。
 *   B 的批次状态本身是按 boardId 记在批次对象里的（数据层没错），缺的是「当前板面应该看哪一批」。
 * - 四.12：关联已写入板面、等保存回执时会登记 awaitingSave（单卡 job / 批次项）。用户撤销
 *   这次关联（公开 undo 把板面恢复成关联前的样子）后，板面上已经没有这个关联，但 awaitingSave
 *   永远不会被重新核对 —— 它只由「保存回执」结算：撤销后的保存回执不包含该关联时被跳过，
 *   于是附件提示一直停在「处理中 / 关联待保存」；而撤销前发出、撤销后才返回的旧保存回执
 *   （快照里带着该关联）又会把它结算成「已关联附件」，与用户撤销后的板面矛盾。
 *   用户重做关联后，已上传的附件应可复用，并且只在包含该关联的保存回执成功后显示成功。
 *
 * 本文件锁死的行为（断言只落在 store 公开状态 + 真实 API mock 的调用参数/次数）：
 * 1. B 的批次在上传期间切到 A：A 看不到 B 的批次项 / 汇总 / 进行中状态 / 重试入口；
 *    B 的批次按原板维护，回 B 可见并恢复真实进度；切板期间的上传回执不写进 A；
 * 2. 重试沿用发起时的板面 / 批次 / 项 / 目标卡：点击后切板仍归 B，只 B 建卡、上传不重复、
 *    重试不重复建卡、同批其他项状态不变；
 * 3. 单卡与批量：撤销关联后等待状态立即结束（不留 processing / awaiting_save），
 *    撤销前的旧保存成功回执迟到不复活它；撤销后的保存确认板面已无该关联；
 * 4. 重做关联复用已上传附件（不重复上传），只在包含该关联的保存回执成功后显示成功。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { flushPromises } from "@vue/test-utils";
import { useInteractiveStore } from "../interactive";
import * as api from "../../services/interactive";
import * as attachments from "../../services/attachments";
import type { AttachmentRef } from "../../services/attachments";
import type { CardImportJobSnapshot } from "../interactive";
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

const fileReceiptId = new WeakMap<File, string>();

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

function file(name: string, receiptId = "att_" + name): File {
  const created = new File([receiptId], name, { type: "text/plain" });
  fileReceiptId.set(created, receiptId);
  return created;
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

function liveCards(store: ReturnType<typeof useInteractiveStore>) {
  return (store.board?.cards ?? []).filter((card) => !card.deleted);
}

function linkedAttachmentId(store: ReturnType<typeof useInteractiveStore>, cardId: string): string {
  const card = liveCards(store).find((item) => item.id === cardId);
  const block = (card?.meta ?? {}).file as { attachmentId?: string } | undefined;
  return block?.attachmentId ?? "";
}

function cardAttachmentIn(state: BoardState, cardId: string): string {
  const card = (state.cards ?? []).find((item) => item.id === cardId);
  const block = (card?.meta ?? {}).file as { attachmentId?: string } | undefined;
  return block?.attachmentId ?? "";
}

function cardJobs(store: ReturnType<typeof useInteractiveStore>): CardImportJobSnapshot[] {
  return Object.values(store.cardImportJobs).sort((a, b) => a.attemptId - b.attemptId);
}

function latestCardJob(store: ReturnType<typeof useInteractiveStore>): CardImportJobSnapshot {
  const jobs = cardJobs(store);
  expect(jobs.length, "store 里没有任何单卡导入事实").toBeGreaterThan(0);
  return jobs[jobs.length - 1];
}

function uploadCalls() {
  return vi.mocked(attachments.uploadAttachment).mock.calls;
}

function saveCalls() {
  return vi.mocked(api.saveBoardState).mock.calls;
}

function lastSavedCandidate(): BoardState {
  const calls = saveCalls();
  expect(calls.length, "没有任何保存请求").toBeGreaterThan(0);
  return calls[calls.length - 1][1] as BoardState;
}

async function settle(times = 8): Promise<void> {
  for (let i = 0; i < times; i += 1) {
    await flushPromises();
  }
}

async function waitForDebouncedSave(): Promise<void> {
  await new Promise((resolve) => setTimeout(resolve, 600));
  await settle();
}

let store: ReturnType<typeof useInteractiveStore>;

let cardsA: BoardState["cards"] = [];
let cardsB: BoardState["cards"] = [];

let saveMode: "ok" | "fail" | "hold" = "ok";
let saveGate: { promise: Promise<void>; resolve: () => void } | null = null;

function holdSaves(): void {
  saveMode = "hold";
  let resolve!: () => void;
  const promise = new Promise<void>((res) => {
    resolve = res;
  });
  saveGate = { promise, resolve };
}

function releaseSaves(nextMode: "ok" | "fail" = "ok"): void {
  saveMode = nextMode;
  saveGate?.resolve();
  saveGate = null;
}

beforeEach(() => {
  cardsA = [];
  cardsB = [];
  saveMode = "ok";
  saveGate = null;
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
    boardPayload(boardState(id, id === BOARD_B ? cardsB : cardsA))) as never);
  vi.mocked(api.saveBoardState).mockImplementation((async (_id: string, candidate: BoardState) => {
    if (saveMode === "hold" && saveGate) await saveGate.promise;
    if (saveMode === "fail") {
      throw Object.assign(new Error("保存失败：服务端拒绝"), { status: 500 });
    }
    return {
      ok: true,
      seq: Number(candidate?.seq ?? 3) + 1,
      savedAt: NOW,
      state: candidate,
    };
  }) as never);
  vi.mocked(api.saveDrafts).mockResolvedValue({ drafts: {}, updatedAt: NOW } as never);
  vi.mocked(api.fetchVisibleRange).mockResolvedValue({ visibleRange: emptySnapshot() } as never);
  vi.mocked(api.fetchIntentViews).mockResolvedValue({ available: true, source: "service", intents: [] } as never);
  vi.mocked(api.fetchIntents).mockResolvedValue({
    intents: [], conflicts: [], batchAvailable: false, recovery: { paused: [] },
  } as never);
  vi.mocked(api.fetchExecutions).mockResolvedValue({ executions: [] } as never);
  vi.mocked(api.fetchUnderstanding).mockResolvedValue({ understandings: [] } as never);
  vi.mocked(api.checkMaterialImpact).mockResolvedValue({
    ok: true, stateVersion: 3, affected: [], impactConfirmationRequired: false,
  } as never);
  vi.mocked(api.previewMaterialImpact).mockResolvedValue({ affected: [] } as never);
  vi.mocked(attachments.uploadAttachment).mockImplementation(async (upload: File) =>
    attachment(upload.name, { id: fileReceiptId.get(upload) ?? "att_" + upload.name }));
  vi.mocked(attachments.waitUntilSettled).mockImplementation(async (item: AttachmentRef) => item);
});

afterEach(() => {
  vi.useRealTimers();
  localStorage.clear();
});

describe("#46 R8 四.11 批量导入的反馈和重试入口归原板", () => {
  it("上传期间切到 A：A 看不到 B 的批次项/汇总/重试入口；B 的批次按原板维护，回 B 可见并可重试", async () => {
    cardsA = [fileCard("card_a", "A 的文件.txt")];
    cardsB = [fileCard("card_b", "B 的文件.txt")];
    await store.selectBoard(BOARD_B);
    await settle();

    const batch = store.beginImportBatch([{ name: "b1.txt" }, { name: "b2.txt" }]);
    const [item0, item1] = batch.items;
    store.setImportItemSource(batch.id, item0.key, { file: file("b1.txt", "att_b1") });
    store.setImportItemSource(batch.id, item1.key, { file: file("b2.txt", "att_b2") });

    // 第一份上传先挂起，制造「批次在上传期间切板」的窗口
    const firstUpload = deferred<AttachmentRef>();
    vi.mocked(attachments.uploadAttachment).mockReturnValueOnce(firstUpload.promise);
    const running = store.runImportBatch(batch.id);
    await settle();
    expect(
      store.importBatchById(batch.id)!.items[0].status,
      "前提：第一份正在上传",
    ).toBe("uploading");
    expect(store.currentImportBatch?.id, "前提：B 上能看到这一批").toBe(batch.id);

    // 上传期间切到 A
    await store.selectBoard(BOARD_A);
    await settle();
    expect(store.boardId, "前提：当前在 A").toBe(BOARD_A);
    expect(
      store.currentImportBatch,
      "A 上显示了 B 的批次（反馈没有按板面归属）",
    ).toBeNull();
    expect(store.currentImportBatchCounts, "A 上显示了 B 的批次汇总").toEqual({
      total: 0,
      received: 0,
      failed: 0,
      pending: 0,
      unreceived: 0,
      uploading: 0,
    });
    expect(store.hasImportBatchInFlight, "A 上显示 B 的批次仍在进行中").toBe(false);
    // B 的批次事实本身仍按原板维护（不是被丢掉）
    const keptBatch = store.importBatchById(batch.id);
    expect(keptBatch?.boardId, "B 的批次归属丢了").toBe(BOARD_B);
    expect(keptBatch?.items[0].status, "B 的批次项没有继续维护真实进度").toBe("uploading");

    // 放行上传：回执送达时当前在 A，不许写进 A、也不许把 A 显示成有批次反馈
    firstUpload.resolve(attachment("b1.txt", { id: "att_b1" }));
    await running;
    await settle();
    expect(
      liveCards(store).map((card) => card.id),
      "切板期间的上传在 A 上建了卡",
    ).toEqual(["card_a"]);
    expect(
      store.importBatchById(batch.id)!.items.every((item) => item.status !== "succeeded"),
      "切板期间的上传在别的板面上被标成了已接收",
    ).toBe(true);
    expect(store.currentImportBatch, "A 上仍然显示 B 的批次").toBeNull();

    // 回 B：批次可见、进度可恢复
    await store.selectBoard(BOARD_B);
    await settle();
    expect(store.currentImportBatch?.id, "回 B 后看不到原板的批次").toBe(batch.id);
    expect(store.currentImportBatchCounts.failed, "回 B 后看不到真实的失败项").toBe(2);
    expect(store.hasImportBatchInFlight, "回 B 后把已落定的批次当成仍在进行中").toBe(false);

    // 重试第 1 项：复用已上传附件（不重复上传），只在 B 建一张卡，同批其他项不变
    const uploadsBeforeRetry = uploadCalls().length;
    const cardsBeforeRetry = liveCards(store).length;
    await store.runImportItem(batch.id, item0.key);
    await waitForDebouncedSave();
    await settle();
    expect(
      uploadCalls().length,
      "重试又上传了一次（没有复用已上传的附件）",
    ).toBe(uploadsBeforeRetry);
    expect(store.importBatchById(batch.id)!.items[0].status, "重试成功后第 1 项没有标已接收").toBe(
      "succeeded",
    );
    expect(
      store.importBatchById(batch.id)!.items[0].attachmentId,
      "重试成功后第 1 项的附件 ID 变了",
    ).toBe("att_b1");
    expect(liveCards(store).length, "重试没有在 B 上建卡或建了重复卡").toBe(cardsBeforeRetry + 1);
    const retriedCard = liveCards(store).find(
      (card) => linkedAttachmentId(store, card.id) === "att_b1",
    );
    expect(retriedCard, "B 上没有一张卡关联到这次重试的附件").toBeTruthy();
    const other = store.importBatchById(batch.id)!.items[1];
    expect(other.status, "重试第 1 项改动了同批第 2 项的状态").toBe("failed");
    expect(other.attachmentId, "重试第 1 项改动了同批第 2 项的附件").toBe("att_b2");
    expect(lastSavedCandidate().boardId, "重试的保存写到了别的板面").toBe(BOARD_B);
  });

  it("重试核对发起时的板面/批次/项/目标卡：点击后切板仍归 B，不写 A、不重复上传/建卡", async () => {
    cardsA = [fileCard("card_a", "A 的文件.txt")];
    cardsB = [fileCard("card_b", "B 的文件.txt")];
    await store.selectBoard(BOARD_B);
    await settle();

    const batch = store.beginImportBatch([{ name: "b1.txt" }]);
    const itemKey = batch.items[0].key;
    store.setImportItemSource(batch.id, itemKey, { file: file("b1.txt", "att_b1") });

    // 第一次上传失败：B 上出现失败项与重试入口
    vi.mocked(attachments.uploadAttachment).mockRejectedValueOnce(new Error("上传失败：网络中断"));
    await store.runImportItem(batch.id, itemKey);
    await settle();
    const item = () => store.importBatchById(batch.id)!.items[0];
    expect(item().status, "前提：第 1 次上传失败").toBe("failed");

    // 点重试（上传挂起）后立刻切到 A：重试仍归 B，A 不出现批次反馈
    const retryUpload = deferred<AttachmentRef>();
    vi.mocked(attachments.uploadAttachment).mockReturnValueOnce(retryUpload.promise);
    const retryRun = store.runImportItem(batch.id, itemKey);
    await settle();
    expect(item().status, "前提：重试已开始上传").toBe("uploading");
    await store.selectBoard(BOARD_A);
    await settle();
    expect(
      store.currentImportBatch,
      "点击重试后切到 A：A 上出现了 B 批次的重试入口",
    ).toBeNull();
    expect(store.hasImportBatchInFlight, "A 上显示 B 的重试仍在进行中").toBe(false);

    retryUpload.resolve(attachment("b1.txt", { id: "att_b1" }));
    await retryRun;
    await settle();
    expect(
      liveCards(store).map((card) => card.id),
      "点击重试后切板：回执在 A 上建了卡",
    ).toEqual(["card_a"]);
    expect(item().status, "点击重试后切板：批次项被标成了已接收").not.toBe("succeeded");
    expect(store.currentImportBatch, "A 上仍然显示 B 的批次").toBeNull();

    // 回 B 再重试：复用已上传附件、只在 B 建一张卡
    await store.selectBoard(BOARD_B);
    await settle();
    expect(store.currentImportBatch?.id, "回 B 后看不到原板的批次").toBe(batch.id);
    const uploadsBefore = uploadCalls().length;
    const cardsBefore = liveCards(store).length;
    await store.runImportItem(batch.id, itemKey);
    await waitForDebouncedSave();
    await settle();
    expect(uploadCalls().length, "回 B 重试又上传了一次（没有复用附件）").toBe(uploadsBefore);
    expect(item().status, "回 B 重试成功后没有标已接收").toBe("succeeded");
    expect(liveCards(store).length, "回 B 重试没有建卡或建了重复卡").toBe(cardsBefore + 1);
    expect(lastSavedCandidate().boardId, "回 B 重试的保存写到了别的板面").toBe(BOARD_B);
  });
});

describe("#46 R8 四.12 撤销附件关联后结束失效的等待", () => {
  it("单卡：撤销后等待状态立即结束，撤销前的旧保存成功回执不复活它；重做复用附件且只在包含关联的保存成功后成功", async () => {
    cardsA = [fileCard("card_a", "A 的文件.txt")];
    cardsB = [fileCard("card_b", "B 的文件.txt")];
    await store.selectBoard(BOARD_B);
    await settle();

    // 关联与保存在飞：上传完成、关联写入板面、保存回执未到
    holdSaves();
    const bFile = file("b.txt", "att_b");
    const started = await store.startCardAttachment({ cardId: "card_b", source: { file: bFile } }).done;
    await settle();
    expect(started.ok, "前提：关联写入板面：" + String(started.error)).toBe(true);
    const pendingJob = latestCardJob(store);
    expect(pendingJob.phase, "前提：关联已写入、保存未确认").toBe("processing");
    expect(pendingJob.awaitingSave, "前提：没有标出关联待保存").toBe(true);
    expect(linkedAttachmentId(store, "card_b"), "前提：板面上已有关联").toBe("att_b");
    await waitForDebouncedSave();
    expect(store.saveStatus, "前提：关联的保存在飞").toBe("saving");
    expect(saveCalls().length, "前提：发出了关联的保存").toBe(1);
    expect(cardAttachmentIn(saveCalls()[0][1] as BoardState, "card_b"), "前提：这次保存包含该关联").toBe(
      "att_b",
    );

    // 用户撤销这次关联（公开 undo）：板面回到关联前，等待状态必须立刻结束
    store.undo();
    await settle();
    expect(linkedAttachmentId(store, "card_b"), "撤销后卡片仍关联着附件").toBe("");
    expect(
      cardJobs(store).every(
        (job) => !job.awaitingSave && job.phase !== "processing" && job.phase !== "uploading",
      ),
      "撤销关联后附件提示仍停在处理中（不会结束）",
    ).toBe(true);
    expect(
      store.activeCardImportForBoard,
      "撤销关联后返回页面仍显示「正在处理」",
    ).toBeNull();

    // 撤销前发出的旧保存成功回执迟到：快照里带着该关联，但用户已经撤销 → 不得复活成成功
    releaseSaves("ok");
    await settle();
    await settle();
    expect(
      cardJobs(store).some((job) => job.phase === "succeeded"),
      "撤销前的旧保存成功回执把已撤销的关联复活成了成功",
    ).toBe(false);
    expect(linkedAttachmentId(store, "card_b"), "旧保存回执把撤销的关联写回了板面").toBe("");

    // 撤销后的保存落地：顶栏显示已保存，回读的候选里没有该关联
    await waitForDebouncedSave();
    await settle();
    expect(store.saveStatus, "撤销后的保存没有完成").toBe("saved");
    expect(
      cardAttachmentIn(lastSavedCandidate(), "card_b"),
      "撤销后的保存候选里仍有已撤销的关联",
    ).toBe("");
    expect(
      cardJobs(store).every((job) => job.phase !== "processing"),
      "撤销后仍留着一个处理中的附件提示",
    ).toBe(true);

    // 用户重做关联：已上传的附件可复用（不重复上传）；
    // 只有包含该关联的保存回执成功后，才显示关联成功
    holdSaves();
    store.redo();
    await settle();
    expect(linkedAttachmentId(store, "card_b"), "重做后板面上没有恢复该关联").toBe("att_b");
    await waitForDebouncedSave();
    expect(store.saveStatus, "前提：重做后的保存在飞").toBe("saving");
    expect(
      cardJobs(store).some((job) => job.phase === "succeeded"),
      "保存回执还没到就把重做的关联标成了成功",
    ).toBe(false);
    expect(uploadCalls().length, "重做又上传了一次（没有复用已上传附件）").toBe(1);

    releaseSaves("ok");
    await settle();
    await settle();
    expect(store.saveStatus, "重做后的保存没有成功").toBe("saved");
    const doneJob = cardJobs(store).find(
      (job) => job.phase === "succeeded" && job.attachment?.id === "att_b",
    );
    expect(doneJob, "包含该关联的保存成功后没有显示关联成功").toBeTruthy();
    expect(linkedAttachmentId(store, "card_b"), "重做成功后的板面附件变了").toBe("att_b");
    expect(
      cardAttachmentIn(lastSavedCandidate(), "card_b"),
      "重做后的保存候选里没有该关联",
    ).toBe("att_b");
    expect(uploadCalls().length, "整段只应上传一次").toBe(1);
  });

  it("批量：撤销后批次项等待立即结束，旧保存成功回执不复活；重做复用附件且只在包含关联的保存成功后成功", async () => {
    cardsA = [fileCard("card_a", "A 的文件.txt")];
    cardsB = [fileCard("card_b", "B 的文件.txt")];
    await store.selectBoard(BOARD_B);
    await settle();

    holdSaves();
    const batch = store.beginImportBatch([{ name: "批次-b.txt", cardId: "card_b" }]);
    const itemKey = batch.items[0].key;
    const batchFile = file("批次-b.txt", "att_batch_b");
    store.setImportItemSource(batch.id, itemKey, { file: batchFile });
    const run = store.runImportItem(batch.id, itemKey);
    await settle();
    await settle();
    const item = () => store.importBatchById(batch.id)!.items[0];
    expect(item().status, "前提：关联已写入、保存未确认（关联待保存）").toBe("awaiting_save");
    expect(linkedAttachmentId(store, "card_b"), "前提：板面上已有关联").toBe("att_batch_b");
    await waitForDebouncedSave();
    expect(store.saveStatus, "前提：关联的保存在飞").toBe("saving");
    expect(
      cardAttachmentIn(saveCalls()[0][1] as BoardState, "card_b"),
      "前提：这次保存包含该关联",
    ).toBe("att_batch_b");

    // 用户撤销这次关联：批次项的等待必须立刻结束
    store.undo();
    await settle();
    await run;
    await settle();
    expect(linkedAttachmentId(store, "card_b"), "撤销后卡片仍关联着附件").toBe("");
    expect(
      item().status,
      "撤销关联后批次项仍停在「关联待保存 / 上传中」",
    ).not.toBe("awaiting_save");
    expect(item().status, "撤销关联后批次项仍显示上传中").not.toBe("uploading");
    expect(store.hasImportBatchInFlight, "撤销关联后批次仍显示有文件在途").toBe(false);

    // 撤销前的旧保存成功回执迟到：不得把已撤销的批次关联复活成成功
    releaseSaves("ok");
    await settle();
    await settle();
    expect(
      item().status,
      "撤销前的旧保存成功回执把已撤销的批次关联复活成了已接收",
    ).not.toBe("succeeded");
    expect(linkedAttachmentId(store, "card_b"), "旧保存回执把撤销的关联写回了板面").toBe("");
    await waitForDebouncedSave();
    expect(store.saveStatus, "撤销后的保存没有完成").toBe("saved");
    expect(
      cardAttachmentIn(lastSavedCandidate(), "card_b"),
      "撤销后的保存候选里仍有已撤销的关联",
    ).toBe("");

    // 用户重做关联：重跑这一项，复用已上传的附件；只有包含该关联的保存回执成功后才标已接收
    holdSaves();
    const uploadsBeforeRedo = uploadCalls().length;
    const redoRun = store.runImportItem(batch.id, itemKey);
    await settle();
    await settle();
    expect(linkedAttachmentId(store, "card_b"), "重做后板面上没有恢复该关联").toBe("att_batch_b");
    expect(uploadCalls().length, "重做又上传了一次（没有复用已上传附件）").toBe(uploadsBeforeRedo);
    await waitForDebouncedSave();
    expect(store.saveStatus, "前提：重做后的保存在飞").toBe("saving");
    expect(
      item().status,
      "保存回执还没到就把重做的批次项标成了已接收",
    ).not.toBe("succeeded");

    releaseSaves("ok");
    await settle();
    await settle();
    await redoRun;
    await settle();
    expect(store.saveStatus, "重做后的保存没有成功").toBe("saved");
    expect(item().status, "包含该关联的保存成功后批次项没有标已接收").toBe("succeeded");
    expect(item().attachmentId, "重做成功后批次项的附件 ID 变了").toBe("att_batch_b");
    expect(
      cardAttachmentIn(lastSavedCandidate(), "card_b"),
      "重做后的保存候选里没有该关联",
    ).toBe("att_batch_b");
    expect(uploadCalls().length, "整段只应上传一次").toBe(1);
  });
});
