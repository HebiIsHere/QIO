/**
 * #46 第七轮（任务书 四.8 / 四.9）：**附件状态机**与**批量导入**的真实状态区分。
 *
 * 已证实的残留缺陷（基线 0dbc387f）：
 * - 单卡关联：`finishImportAttempt()` 把「关联已写入板面」当成成功，只有「原板就是当前板面
 *   且有未保存改动」时才降级成 processing。保存回执失败后 `settleCardImportSaveForBoard()`
 *   会把它改成 failed —— 这条链路本身对，但**重试关联**时 `awaitingSave` 的判定依赖
 *   `saveStatus`，一旦上一次保存停在 error，重试的关联会被直接标成 succeeded（保存回执还没到）；
 * - 批量导入：`finishImportAttempt()` 对批次项**只看关联结果**，完全不看保存回执 ——
 *   写入（PUT）失败时批次项照样标「已接收」，界面于是显示成功，而板面并没有保存成功。
 *
 * 本文件锁死的行为（断言落在 store 对外可见状态与真实 API 调用参数上）：
 * 1. 上传成功但关联保存失败：状态区分「关联待保存 / 关联保存失败 / 关联成功」，
 *    失败保留附件 ID 与可重试入口状态，且**不发成功提示**；
 * 2. 重试关联只调用 1 次上传、复用同一 attachmentId，保存回执成功后才变成成功；
 * 3. 批量导入写入失败：批次项不得标成功；重试复用附件 ID、不重复建卡、同批其他项不变；
 *    成功回执后对应项才成功。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { flushPromises } from "@vue/test-utils";
import { useInteractiveStore } from "../interactive";
import * as api from "../../services/interactive";
import * as attachments from "../../services/attachments";
import { addCard } from "../../interactive/board";
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

function fileCard(id: string, name: string): BoardState["cards"][number] {
  return {
    id,
    kind: "file",
    content: "",
    meta: { name },
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

/** 单卡导入事实（按 attemptId 升序；界面读的就是这一份）。 */
function cardJobs(store: ReturnType<typeof useInteractiveStore>): CardImportJobSnapshot[] {
  return Object.values(store.cardImportJobs).sort((a, b) => a.attemptId - b.attemptId);
}

function latestCardJob(store: ReturnType<typeof useInteractiveStore>): CardImportJobSnapshot {
  const jobs = cardJobs(store);
  expect(jobs.length, "store 里没有任何单卡导入事实").toBeGreaterThan(0);
  return jobs[jobs.length - 1];
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

/**
 * 保存回执的可控开关（只 mock 服务边界，不 mock store 逻辑）：
 * - "ok"：正常成功；
 * - "fail"：一直失败（写入失败的真实形状：500 + 原因）；
 * - "hold"：挂起，直到 `releaseSave()` 放行 —— 用来精确制造「关联已写入、保存回执未到」的窗口。
 */
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
  saveMode = "ok";
  saveGate = null;
  localStorage.clear();
  vi.resetAllMocks();
  setActivePinia(createPinia());
  store = useInteractiveStore();
  vi.mocked(api.fetchBoards).mockResolvedValue({
    boards: [{ id: BOARD_A, title: "板面 A" }],
  } as never);
  vi.mocked(api.fetchBoardState).mockImplementation((async (id: string) =>
    boardPayload(boardState(id, [fileCard("card_a", "A 的文件.txt")]))) as never);
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
  vi.mocked(attachments.uploadAttachment).mockImplementation(async (upload: File) =>
    attachment(upload.name));
  vi.mocked(attachments.waitUntilSettled).mockImplementation(async (item: AttachmentRef) => item);
});

afterEach(() => {
  vi.useRealTimers();
  localStorage.clear();
});

describe("#46 R7 四.8 单卡附件状态机", () => {
  it("上传成功但关联保存失败：状态区分待保存/失败/成功，失败保留附件 ID 且不发成功提示", async () => {
    await store.selectBoard(BOARD_A);
    await settle();
    expect(liveCards(store).map((card) => card.id)).toEqual(["card_a"]);

    // 保存回执挂起：关联已写入板面，但保存还没确认
    holdSaves();

    const phases: string[] = [];
    const stop = store.onImportJob((event) => phases.push(event.phase));

    const started = store.startCardAttachment({
      cardId: "card_a",
      source: { file: file("A 的文件.txt") },
    });
    const result = await started.done;
    await settle();
    expect(result.ok, "关联本身没有成功：" + String(result.error)).toBe(true);
    expect(attachments.uploadAttachment, "上传被重复调用").toHaveBeenCalledTimes(1);

    // --- 关联待保存：既不是成功，也不是失败 ---
    const pendingJob = latestCardJob(store);
    expect(pendingJob.phase, "关联已写入但保存未确认时被标成了终态").toBe("processing");
    expect(pendingJob.awaitingSave, "没有标出「关联待保存」").toBe(true);
    expect(pendingJob.attachment?.id, "待保存状态丢了附件事实").toBe("att_A 的文件.txt");
    expect(
      phases.includes("succeeded"),
      "保存回执还没到就发了成功提示：" + JSON.stringify(phases),
    ).toBe(false);
    expect(
      store.activeCardImportForBoard?.phase,
      "返回页面时看不到「正在处理」的真实进度",
    ).toBe("processing");
    expect(
      store.unacknowledgedCardImportForBoard(BOARD_A),
      "保存回执还没到就把它当成已落定的终态展示",
    ).toBeNull();

    // 等这次候选的保存真的发出（被 hold 住）：此时仍是「关联待保存」
    await waitForDebouncedSave();
    expect(store.saveStatus, "前提：保存请求已经发出、回执未到").toBe("saving");
    expect(latestCardJob(store).phase, "保存在飞期间单卡导入不是「待保存」").toBe("processing");

    // --- 保存失败：状态变成失败，附件 ID 保留，仍然没有成功提示 ---
    releaseSaves("fail");
    await settle();
    await settle();

    expect(store.saveStatus, "前提：这次保存确实失败了").toBe("error");
    const failedJob = latestCardJob(store);
    expect(failedJob.phase, "保存失败后单卡导入仍显示成功").toBe("failed");
    expect(failedJob.awaitingSave, "失败后仍标着「关联待保存」").toBe(false);
    expect(failedJob.error ?? "", "失败原因不是保存失败的真实原因").toContain("保存失败");
    expect(failedJob.attachment?.id, "失败后丢了附件 ID（重试会重新上传）").toBe("att_A 的文件.txt");
    expect(failedJob.pendingAttachment?.id, "失败后没有保留可复用的附件事实").toBe("att_A 的文件.txt");
    expect(
      phases.includes("succeeded"),
      "保存失败却发了成功提示：" + JSON.stringify(phases),
    ).toBe(false);
    expect(
      store.unacknowledgedCardImportForBoard(BOARD_A)?.phase,
      "返回页面时会把失败展示成成功",
    ).toBe("failed");
    // 板面上附件关联仍在（失败保留附件 ID）
    expect(linkedAttachmentId(store, "card_a")).toBe("att_A 的文件.txt");
    stop();
  });

  it("重试关联：只上传 1 次、复用同一 attachmentId，保存回执成功后才变成成功", async () => {
    await store.selectBoard(BOARD_A);
    await settle();

    // 第一次：关联成功、保存失败
    saveMode = "fail";
    const started = store.startCardAttachment({
      cardId: "card_a",
      source: { file: file("A 的文件.txt") },
    });
    await started.done;
    await waitForDebouncedSave();
    expect(store.saveStatus, "前提：第一次保存失败").toBe("error");
    expect(latestCardJob(store).phase, "前提：第一次关联失败").toBe("failed");
    expect(attachments.uploadAttachment, "前提：只上传过一次").toHaveBeenCalledTimes(1);

    // 重试关联：保存回执挂起，先看「待保存」而不是「成功」
    holdSaves();
    const phases: string[] = [];
    const stop = store.onImportJob((event) => phases.push(event.phase));

    expect(store.retryCardAttachment("card_a"), "重试关联入口不可用").toBe(true);
    await settle();
    await settle();

    expect(attachments.uploadAttachment, "重试关联又上传了一次（没有复用附件 ID）").toHaveBeenCalledTimes(1);
    const retryJob = latestCardJob(store);
    expect(retryJob.attachment?.id, "重试关联没有复用第一次的附件 ID").toBe("att_A 的文件.txt");
    expect(retryJob.phase, "保存回执还没到就标成了成功").toBe("processing");
    expect(retryJob.awaitingSave, "重试关联没有标出「关联待保存」").toBe(true);
    expect(phases.includes("succeeded"), "保存回执还没到就发了成功提示").toBe(false);

    // 等重试后的保存真的发出（被 hold 住）
    await waitForDebouncedSave();
    expect(store.saveStatus, "前提：重试后的保存请求已经发出、回执未到").toBe("saving");

    // 保存回执成功：这时才算成功
    releaseSaves("ok");
    await settle();
    await settle();

    expect(store.saveStatus, "重试后的保存没有成功").toBe("saved");
    const doneJob = latestCardJob(store);
    expect(doneJob.phase, "保存回执成功后单卡导入没有变成成功").toBe("succeeded");
    expect(doneJob.awaitingSave, "成功后仍标着「关联待保存」").toBe(false);
    expect(doneJob.error, "成功后仍带着失败原因").toBeNull();
    expect(linkedAttachmentId(store, "card_a"), "板面上的附件 ID 变了").toBe("att_A 的文件.txt");
    expect(attachments.uploadAttachment, "整段只应上传一次").toHaveBeenCalledTimes(1);
    stop();
  });
});

describe("#46 R7 四.9 批量导入写入失败", () => {
  it("关联已写入但保存回执未到：批次项不得标成功；保存失败后仍不得成功", async () => {
    await store.selectBoard(BOARD_A);
    await settle();

    const batch = store.beginImportBatch([{ name: "批次-1.txt" }]);
    const itemKey = batch.items[0].key;
    store.setImportItemSource(batch.id, itemKey, { file: file("批次-1.txt") });

    holdSaves();

    const run = store.runImportItem(batch.id, itemKey);
    await settle();
    await settle();

    const item = () => store.importBatchById(batch.id)!.items[0];
    expect(attachments.uploadAttachment, "上传没有只调用一次").toHaveBeenCalledTimes(1);
    expect(
      item().status,
      "保存回执还没到就把批次项标成了「已接收」",
    ).not.toBe("succeeded");
    expect(item().attachmentId, "批次项没有记下附件 ID").toBe("att_批次-1.txt");

    // 等这次候选的保存真的发出（被 hold 住）
    await waitForDebouncedSave();
    expect(store.saveStatus, "前提：保存请求已经发出、回执未到").toBe("saving");

    // 保存失败：批次项仍然不得成功
    releaseSaves("fail");
    await run;
    await settle();
    await settle();

    expect(store.saveStatus, "前提：这次写入确实失败了").toBe("error");
    expect(
      item().status,
      "写入失败后批次项仍标「已接收」（界面会显示成功）",
    ).not.toBe("succeeded");
    expect(item().attachmentId, "写入失败后丢了附件 ID").toBe("att_批次-1.txt");
    expect(store.currentImportBatchCounts.received, "汇总把失败项算成了已接收").toBe(0);
  });

  it("重试复用附件 ID、不重复建卡、同批其他项不变；成功回执后对应项才成功", async () => {
    await store.selectBoard(BOARD_A);
    await settle();
    const cardsBefore = liveCards(store).length;

    const batch = store.beginImportBatch([{ name: "批次-1.txt" }, { name: "批次-2.txt" }]);
    const [item0, item1] = batch.items;
    store.setImportItemSource(batch.id, item0.key, { file: file("批次-1.txt") });
    store.setImportItemSource(batch.id, item1.key, { file: file("批次-2.txt") });

    // 第 1 项：写入失败
    saveMode = "fail";
    await store.runImportItem(batch.id, item0.key);
    await waitForDebouncedSave();
    expect(store.saveStatus, "前提：第 1 项写入失败").toBe("error");
    const item0AfterFail = store.importBatchById(batch.id)!.items[0];
    expect(item0AfterFail.status, "写入失败的第 1 项被标成了成功").not.toBe("succeeded");
    expect(item0AfterFail.attachmentId).toBe("att_批次-1.txt");

    // 第 2 项：写入成功
    saveMode = "ok";
    await store.runImportItem(batch.id, item1.key);
    await waitForDebouncedSave();
    expect(store.saveStatus, "前提：第 2 项写入成功").toBe("saved");
    const item1AfterOk = store.importBatchById(batch.id)!.items[1];
    expect(item1AfterOk.status, "写入成功的第 2 项没有标成功").toBe("succeeded");
    expect(item1AfterOk.attachmentId).toBe("att_批次-2.txt");
    const uploadsAfterBoth = vi.mocked(attachments.uploadAttachment).mock.calls.length;
    expect(uploadsAfterBoth, "两项各上传一次").toBe(2);
    const cardsAfterBoth = liveCards(store).length;
    expect(cardsAfterBoth, "两项各建一张卡").toBe(cardsBefore + 2);

    // 重试第 1 项：保存回执挂起
    holdSaves();
    await store.runImportItem(batch.id, item0.key);
    await settle();
    await settle();

    expect(
      vi.mocked(attachments.uploadAttachment).mock.calls.length,
      "重试第 1 项又上传了一次（没有复用附件 ID）",
    ).toBe(uploadsAfterBoth);
    expect(
      store.importBatchById(batch.id)!.items[0].attachmentId,
      "重试没有复用第一次的附件 ID",
    ).toBe("att_批次-1.txt");
    expect(
      liveCards(store).length,
      "重试第 1 项重复建卡",
    ).toBe(cardsAfterBoth);
    expect(
      store.importBatchById(batch.id)!.items[0].status,
      "保存回执还没到就把重试项标成了成功",
    ).not.toBe("succeeded");
    // 同批其他项不变
    const item1Now = store.importBatchById(batch.id)!.items[1];
    expect(item1Now.status, "重试第 1 项改动了同批第 2 项的状态").toBe("succeeded");
    expect(item1Now.attachmentId, "重试第 1 项改动了同批第 2 项的附件").toBe("att_批次-2.txt");
    expect(item1Now.error, "重试第 1 项给同批第 2 项写了错误").toBeNull();

    // 等重试后的保存真的发出（被 hold 住）
    await waitForDebouncedSave();
    expect(store.saveStatus, "前提：重试后的保存请求已经发出、回执未到").toBe("saving");

    // 保存回执成功：这时第 1 项才算成功
    releaseSaves("ok");
    await settle();
    await settle();

    expect(store.saveStatus, "重试后的写入没有成功").toBe("saved");
    const item0Done = store.importBatchById(batch.id)!.items[0];
    expect(item0Done.status, "成功回执后第 1 项没有变成成功").toBe("succeeded");
    expect(item0Done.attachmentId, "成功后附件 ID 变了").toBe("att_批次-1.txt");
    expect(liveCards(store).length, "成功后卡片数量变了").toBe(cardsAfterBoth);
    expect(store.currentImportBatchCounts.received, "汇总没有把两项都算成已接收").toBe(2);
  });
});
