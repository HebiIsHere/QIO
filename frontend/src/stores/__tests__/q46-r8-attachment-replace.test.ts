/**
 * #46 第八轮（任务书 四.9 / 四.10）：**替换附件的重试必须处理用户刚选的文件**，
 * 以及**旧保存回执只结算它请求里包含的关联**。
 *
 * 基线 c9fedc60 上已证实的残留缺陷：
 * - 四.9：单卡「可复用附件」缓存 settledAttachmentByCard 只按「板面 + 卡片」键，
 *   不记录它属于哪一次尝试。卡片先关联过 old.txt（缓存里留着 att_old.txt）后，
 *   用户选 new.txt 上传失败 → 点「重试关联」时 retryCardAttachment() 直接复用
 *   att_old.txt（**根本不重传 new.txt**），并把 old 的附件写回卡片、提示「关联成功」。
 *   同名不同内容也一样：缓存只比文件名/卡片，无法区分同为 same.txt 的两次尝试。
 *   另外旧上传回执迟到时 settledAttachmentByCard.set() 在身份核对之前就覆盖缓存，
 *   于是之后的重试会拿**迟到的旧上传**的附件 ID 去覆盖更新的关联。
 * - 四.10：settleCardImportSaveForBoard(boardId, false, ...) 失败结算时遍历该板
 *   **所有** awaitingSave 的单卡/批次关联，不管它们是否包含在这次失败请求的候选里。
 *   于是一份不含新附件的旧保存失败，会把刚关联上、只等后续保存的新附件标成失败，
 *   之后真正包含新附件的保存成功也不会清除这个失败状态。
 *
 * 本文件锁死的行为（断言只落在 store 公开状态 + 真实 API mock 的调用参数/次数）：
 * 1. 替换上传失败后，重试重传**本次尝试的文件源**（新文件/新内容），成功后卡片与
 *    保存候选里的 attachmentId 都是新上传回执；替换成功前卡片保留旧附件；
 * 2. 同名不同内容的两次尝试可区分（旧缓存不得复用）；
 * 3. 旧上传回执晚于新尝试返回时不得覆盖新尝试，也不得成为后续重试的附件来源；
 * 4. 单卡与批量的旧保存失败都不结算不含它的新关联；只有包含它的保存成功后才成功。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { flushPromises } from "@vue/test-utils";
import { useInteractiveStore } from "../interactive";
import * as api from "../../services/interactive";
import * as attachments from "../../services/attachments";
import { updateCard } from "../../interactive/board";
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

/** 每个 File 对象对应的上传回执 id（同名不同内容用不同 File 对象区分）。 */
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

/** 测试文件：内容写的是回执 id，保证同名不同内容确实是两份不同的字节。 */
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

/** 单卡导入事实（按 attemptId 升序；界面读的就是这一份）。 */
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

function lastUploadFile(): File | undefined {
  const calls = uploadCalls();
  return calls[calls.length - 1]?.[0] as File | undefined;
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

/** 等防抖保存真的发出（store 的防抖是 450ms）。 */
async function waitForDebouncedSave(): Promise<void> {
  await new Promise((resolve) => setTimeout(resolve, 600));
  await settle();
}

let store: ReturnType<typeof useInteractiveStore>;

/** 每块板面当前由 fetchBoardState 返回的卡片（用例内按需改）。 */
let cardsA: BoardState["cards"] = [];
let cardsB: BoardState["cards"] = [];

/**
 * 保存回执的可控开关（只 mock 服务边界）：
 * - "ok"：正常成功；
 * - "fail"：一直失败（写入失败的真实形状：500 + 原因）；
 * - "hold"：挂起，直到 releaseSaves() 放行 —— 用来精确制造「旧保存在飞、关联已写入」的窗口。
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

describe("#46 R8 四.9 替换文件失败后的重试必须处理刚选的文件", () => {
  it("不同文件名：重试重传本次尝试的 new.txt，成功后卡片与保存候选都是 new 的附件 ID（替换前保留旧附件）", async () => {
    cardsA = [fileCard("card_a", "old.txt", "att_old.txt")];
    await store.selectBoard(BOARD_A);
    await settle();
    expect(linkedAttachmentId(store, "card_a"), "前提：卡片原本关联 old.txt").toBe("att_old.txt");

    // 前提：old.txt 曾上传成功并被关联保存过（旧实现会把它的附件 ID 留作该卡的可复用事实）
    const oldFile = file("old.txt", "att_old.txt");
    await store.startCardAttachment({ cardId: "card_a", source: { file: oldFile } }).done;
    await waitForDebouncedSave();
    expect(store.saveStatus, "前提：old.txt 的关联已保存").toBe("saved");
    expect(uploadCalls().length, "前提：old.txt 上传了一次").toBe(1);

    // 用户替换成 new.txt：上传失败
    const newFile = file("new.txt", "att_new.txt");
    vi.mocked(attachments.uploadAttachment).mockRejectedValueOnce(new Error("上传失败：网络中断"));
    const failed = await store.startCardAttachment({
      cardId: "card_a",
      source: { file: newFile },
    }).done;
    await settle();

    expect(failed.ok, "前提：新文件上传确实失败了").toBe(false);
    expect((failed.error ?? ""), "失败原因不是上传失败").toContain("上传失败");
    expect(uploadCalls().length, "前提：new.txt 也发起过上传").toBe(2);
    expect(latestCardJob(store).phase, "上传失败后单卡导入不是失败终态").toBe("failed");
    expect(
      linkedAttachmentId(store, "card_a"),
      "替换成功前卡片没有保留原有附件",
    ).toBe("att_old.txt");

    // 点「重试关联」：必须重传用户刚选的 new.txt，而不是复用 old.txt 的附件 ID
    expect(store.retryCardAttachment("card_a"), "重试关联入口不可用").toBe(true);
    await settle();
    await waitForDebouncedSave();
    await settle();

    expect(
      uploadCalls().length,
      "重试关联没有重新上传用户刚选的 new.txt（复用了旧附件 ID）",
    ).toBe(3);
    expect(lastUploadFile(), "重试上传的不是本次尝试的文件源").toBe(newFile);
    expect(
      linkedAttachmentId(store, "card_a"),
      "重试成功后卡片关联的不是本次上传的新文件",
    ).toBe("att_new.txt");
    const job = latestCardJob(store);
    expect(job.attachment?.id, "重试后的单卡导入事实不是新上传的附件").toBe("att_new.txt");
    expect(job.phase, "保存回执成功后重试关联没有变成成功").toBe("succeeded");
    expect(
      cardAttachmentIn(lastSavedCandidate(), "card_a"),
      "保存请求的候选里不是新上传的附件",
    ).toBe("att_new.txt");
  });

  it("同名不同内容：同一张卡两次 same.txt 尝试可区分，重试不得复用第一次的附件 ID", async () => {
    cardsA = [fileCard("card_a", "same.txt", "att_same_v1")];
    await store.selectBoard(BOARD_A);
    await settle();
    expect(linkedAttachmentId(store, "card_a"), "前提：卡片原本关联 same.txt 第 1 版").toBe("att_same_v1");

    const v1 = file("same.txt", "att_same_v1");
    await store.startCardAttachment({ cardId: "card_a", source: { file: v1 } }).done;
    await waitForDebouncedSave();
    expect(uploadCalls().length, "前提：第 1 版上传了一次").toBe(1);

    // 用户重新选了同名但内容不同的第 2 版：上传失败
    const v2 = file("same.txt", "att_same_v2");
    vi.mocked(attachments.uploadAttachment).mockRejectedValueOnce(new Error("上传失败：写入被拒绝"));
    const failed = await store.startCardAttachment({ cardId: "card_a", source: { file: v2 } }).done;
    await settle();
    expect(failed.ok, "前提：第 2 版上传确实失败了").toBe(false);
    expect(linkedAttachmentId(store, "card_a"), "失败后卡片没有保留第 1 版").toBe("att_same_v1");

    // 重试：同名但内容不同 → 必须重新上传第 2 版，不能复用第 1 版的附件 ID
    expect(store.retryCardAttachment("card_a"), "重试关联入口不可用").toBe(true);
    await settle();
    await waitForDebouncedSave();
    await settle();

    expect(
      uploadCalls().length,
      "同名文件重试复用了第 1 版的附件 ID（两次尝试没有被区分）",
    ).toBe(3);
    expect(lastUploadFile(), "重试上传的不是本次选择的第 2 版文件").toBe(v2);
    expect(
      linkedAttachmentId(store, "card_a"),
      "同名替换后卡片关联的还是第 1 版的附件",
    ).toBe("att_same_v2");
    expect(latestCardJob(store).attachment?.id, "重试事实里的附件仍是第 1 版").toBe("att_same_v2");
  });

  it("旧上传回执晚于新尝试返回：不覆盖新关联，也不得成为之后重试的附件来源", async () => {
    cardsA = [fileCard("card_a", "卡")];
    await store.selectBoard(BOARD_A);
    await settle();

    // 第一次上传（旧尝试）挂起：它的回执晚于新尝试返回
    const lateGate = deferred<AttachmentRef>();
    const lateOldFile = file("late-old.txt", "att_late_old");
    vi.mocked(attachments.uploadAttachment).mockReturnValueOnce(lateGate.promise);
    const lateOld = store.startCardAttachment({ cardId: "card_a", source: { file: lateOldFile } });
    const lateAttemptId = lateOld.attemptId;
    await settle();
    expect(uploadCalls().length, "前提：旧尝试的上传已发出").toBe(1);

    // 同一张卡上更新的尝试：上传成功、关联保存成功
    const newFile = file("new.txt", "att_new.txt");
    const fresh = await store.startCardAttachment({ cardId: "card_a", source: { file: newFile } }).done;
    await waitForDebouncedSave();
    await settle();
    expect(fresh.ok, "前提：新尝试关联成功").toBe(true);
    expect(linkedAttachmentId(store, "card_a"), "前提：卡片已关联新文件").toBe("att_new.txt");

    // 旧上传回执迟到：被更新的尝试取代，不覆盖板面
    lateGate.resolve(attachment("late-old.txt", { id: "att_late_old" }));
    const lateResult = await lateOld.done;
    await settle();
    expect(lateResult.superseded, "旧上传回执没有被判定为已被取代").toBe(true);
    expect(linkedAttachmentId(store, "card_a"), "旧上传回执覆盖了更新的关联").toBe("att_new.txt");
    expect(
      store.cardImportJobs[String(lateAttemptId)]?.phase,
      "迟到的旧上传回执被当成了新的成功",
    ).not.toBe("succeeded");

    // 用户又选了 z.txt，上传失败 → 重试必须重传 z.txt；
    // 旧实现会把「迟到的旧上传」写进可复用缓存，重试于是拿 att_late_old 覆盖新关联
    const zFile = file("z.txt", "att_z.txt");
    vi.mocked(attachments.uploadAttachment).mockRejectedValueOnce(new Error("上传失败：连接被重置"));
    await store.startCardAttachment({ cardId: "card_a", source: { file: zFile } }).done;
    await settle();
    expect(linkedAttachmentId(store, "card_a"), "前提：z.txt 失败后仍保留新关联").toBe("att_new.txt");
    expect(latestCardJob(store).phase, "前提：z.txt 上传失败").toBe("failed");
    const uploadsBeforeRetry = uploadCalls().length;

    expect(store.retryCardAttachment("card_a"), "重试关联入口不可用").toBe(true);
    await settle();
    await waitForDebouncedSave();
    await settle();

    expect(
      uploadCalls().length,
      "重试用了迟到旧上传的附件 ID（没有重传本次尝试的 z.txt）",
    ).toBe(uploadsBeforeRetry + 1);
    expect(lastUploadFile(), "重试上传的不是本次尝试的 z.txt").toBe(zFile);
    expect(
      linkedAttachmentId(store, "card_a"),
      "迟到的旧上传附件 ID 覆盖了更新的关联",
    ).toBe("att_z.txt");
  });
});

describe("#46 R8 四.10 旧保存只结算它请求里包含的关联", () => {
  it("单卡：旧保存失败不把不含它的新关联标失败；包含新附件的保存成功后才成功", async () => {
    cardsA = [fileCard("card_a", "A 的文件.txt")];
    cardsB = [fileCard("card_b", "B 的文件.txt")];
    await store.selectBoard(BOARD_B);
    await settle();

    // 先让一次「不含新附件」的 B 保存真的在飞
    holdSaves();
    store.commit(updateCard(store.board!, "card_b", { content: "B 的编辑" }), "编辑 B 卡片");
    await waitForDebouncedSave();
    expect(store.saveStatus, "前提：B 的旧保存请求已经发出、回执未到").toBe("saving");
    expect(saveCalls().length, "前提：只发出了这一次旧保存").toBe(1);
    expect(
      cardAttachmentIn(saveCalls()[0][1] as BoardState, "card_b"),
      "前提：这次旧保存的候选不含新附件",
    ).toBe("");

    // 旧保存在飞期间关联新附件：上传完成、关联写入板面，保存还没发
    const newFile = file("new-b.txt", "att_new_b");
    const started = await store.startCardAttachment({ cardId: "card_b", source: { file: newFile } }).done;
    await settle();
    expect(started.ok, "关联没有写进板面：" + String(started.error)).toBe(true);
    const pendingJob = latestCardJob(store);
    expect(pendingJob.phase, "前提：关联已写入、保存未确认").toBe("processing");
    expect(pendingJob.awaitingSave, "前提：没有标出关联待保存").toBe(true);
    expect(linkedAttachmentId(store, "card_b"), "前提：板面上已有关联").toBe("att_new_b");

    // 切到 A，再让 B 的旧保存返回失败
    await store.selectBoard(BOARD_A);
    await settle();
    expect(store.boardId, "前提：当前在 A").toBe(BOARD_A);
    releaseSaves("fail");
    await settle();
    await settle();

    const afterOldFailure = latestCardJob(store);
    expect(
      afterOldFailure.phase,
      "旧保存失败把不含它的新关联标成了失败",
    ).toBe("processing");
    expect(afterOldFailure.awaitingSave, "旧保存失败后丢了「关联待保存」").toBe(true);
    expect(afterOldFailure.error, "旧保存失败给新关联写了原因").toBeNull();

    // 切回 B：包含新附件的保存成功后才显示成功
    saveMode = "ok";
    await store.selectBoard(BOARD_B);
    await settle();
    await waitForDebouncedSave();
    await settle();

    expect(store.saveStatus, "包含新附件的保存没有成功").toBe("saved");
    const doneJob = latestCardJob(store);
    expect(doneJob.phase, "包含新附件的保存成功后单卡导入没有变成成功").toBe("succeeded");
    expect(doneJob.awaitingSave, "成功后仍标着关联待保存").toBe(false);
    expect(doneJob.attachment?.id, "成功后附件事实变了").toBe("att_new_b");
    expect(linkedAttachmentId(store, "card_b"), "板面上的附件 ID 变了").toBe("att_new_b");
    const finalCandidate = lastSavedCandidate();
    expect(finalCandidate.boardId, "保存请求写到了别的板面").toBe(BOARD_B);
    expect(
      cardAttachmentIn(finalCandidate, "card_b"),
      "触发成功的保存候选里没有新附件",
    ).toBe("att_new_b");
    expect(
      saveCalls().length,
      "包含新附件的保存没有真的再发一次",
    ).toBeGreaterThanOrEqual(2);
  });

  it("批量：旧保存失败不把不含它的批次项标失败；包含新附件的保存成功后才标已接收", async () => {
    cardsA = [fileCard("card_a", "A 的文件.txt")];
    cardsB = [fileCard("card_b", "B 的文件.txt")];
    await store.selectBoard(BOARD_B);
    await settle();

    holdSaves();
    store.commit(updateCard(store.board!, "card_b", { content: "B 的编辑" }), "编辑 B 卡片");
    await waitForDebouncedSave();
    expect(store.saveStatus, "前提：B 的旧保存请求已经发出、回执未到").toBe("saving");
    expect(
      cardAttachmentIn(saveCalls()[0][1] as BoardState, "card_b"),
      "前提：这次旧保存的候选不含新附件",
    ).toBe("");

    // 旧保存在飞期间：批次项上传完成、关联写入 card_b，保存还没发
    const batch = store.beginImportBatch([{ name: "批次-b.txt", cardId: "card_b" }]);
    const itemKey = batch.items[0].key;
    const batchFile = file("批次-b.txt", "att_batch_b");
    store.setImportItemSource(batch.id, itemKey, { file: batchFile });
    const run = store.runImportItem(batch.id, itemKey);
    await settle();
    await settle();
    const item = () => store.importBatchById(batch.id)!.items[0];
    expect(item().status, "前提：关联已写入、保存未确认（关联待保存）").toBe("awaiting_save");
    expect(item().attachmentId, "前提：批次项记下了附件 ID").toBe("att_batch_b");
    expect(linkedAttachmentId(store, "card_b"), "前提：板面上已有关联").toBe("att_batch_b");

    // 切到 A，旧保存失败回执到达
    await store.selectBoard(BOARD_A);
    await settle();
    releaseSaves("fail");
    await settle();
    await settle();

    expect(
      item().status,
      "旧保存失败把不含它的批次项标成了失败",
    ).toBe("awaiting_save");
    expect(item().error, "旧保存失败给批次项写了原因").toBeNull();

    // 切回 B：包含新附件的保存成功后才标「已接收」
    saveMode = "ok";
    await store.selectBoard(BOARD_B);
    await settle();
    await waitForDebouncedSave();
    await settle();
    await run;
    await settle();

    expect(store.saveStatus, "包含新附件的保存没有成功").toBe("saved");
    expect(item().status, "包含新附件的保存成功后批次项没有变成已接收").toBe("succeeded");
    expect(item().attachmentId, "成功后批次项的附件 ID 变了").toBe("att_batch_b");
    expect(store.currentImportBatchCounts.received, "汇总没有把这一项算成已接收").toBe(1);
    expect(
      cardAttachmentIn(lastSavedCandidate(), "card_b"),
      "触发成功的保存候选里没有新附件",
    ).toBe("att_batch_b");
  });
});
