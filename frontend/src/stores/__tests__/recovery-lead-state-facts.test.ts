/**
 * 【本轮 Lead / F1·F3·N1·N5】版本事实与请求生命周期的开发测试。
 *
 * 全部是 store 层反例：只断言用户看得见的真实后果（板面正文 / 位置 / 勾选 / 发出的请求集合）。
 * 标注：【状态/单元】真实 store；【模拟】仅网络响应的到达顺序与延迟（受控 promise）。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { useInteractiveStore } from "../interactive";
import * as imApi from "../../services/interactive";
import { emptyBoardState, type BoardCard, type BoardState, type BoardStateResponse } from "../../interactive/types";
import {
  cardDraftKey,
  cardLocalDraftStorageKey,
  readCardLocalDraft,
  writeCardLocalDraft,
} from "../../interactive/drafts";

vi.mock("../../services/interactive", () => ({
  fetchBoardState: vi.fn(),
  fetchVisibleRange: vi.fn(),
  saveBoardState: vi.fn(),
  saveDrafts: vi.fn(),
  previewMaterialImpact: vi.fn(),
  checkMaterialImpact: vi.fn(),
  submitBoard: vi.fn(),
  fetchIntents: vi.fn(),
  approveIntent: vi.fn(),
  rejectIntent: vi.fn(),
  batchDecide: vi.fn(),
  createDemoIntents: vi.fn(),
  advanceIntent: vi.fn(),
  updateIntentPreview: vi.fn(),
}));

function card(over: Partial<BoardCard> = {}): BoardCard {
  return {
    id: "c1",
    kind: "text",
    content: "正式正文",
    meta: {},
    x: 10,
    y: 10,
    w: 100,
    h: 60,
    checked: false,
    hidden: false,
    folded: false,
    bookmarked: false,
    deleted: false,
    createdAt: "2026-10-10T00:00:00.000Z",
    updatedAt: "2026-10-10T00:00:00.000Z",
    ...over,
  };
}

function state(seq: number, cards: BoardCard[]): BoardState {
  return { ...emptyBoardState("board_default"), seq, updatedAt: "2026-10-10T00:00:00.000Z", cards };
}

function response(st: BoardState): BoardStateResponse {
  return {
    board: { id: "board_default", title: "板面" },
    state: st,
    seq: st.seq,
    baseline: null,
    submissions: [],
    drafts: { drafts: {}, updatedAt: null },
  } as unknown as BoardStateResponse;
}

function runningIntent(id = "i1") {
  return {
    id,
    boardId: "board_default",
    submissionId: null,
    title: "演示任务",
    summary: "",
    status: "running",
    preview: { cards: [], groups: [], links: [] },
    impact: { objects: [], tasks: [], consequences: [] },
    dependsOn: [],
    conflictsWith: [],
    conflictKey: "",
    materialRefs: [],
    progress: { done: 0, total: 1, text: "" },
    reason: "",
    demo: true,
    createdAt: "2026-10-10T00:00:00.000Z",
    updatedAt: "2026-10-10T00:00:00.000Z",
  } as never;
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

/** 把已排队的微任务跑完（用来精确控制「预保存结束、提交请求已发出」这一刻） */
async function flushMicrotasks(rounds = 60): Promise<void> {
  for (let index = 0; index < rounds; index += 1) await Promise.resolve();
}

function apiError(status: number, payload: Record<string, unknown>): Error {
  return Object.assign(new Error(String(payload.reason ?? payload.error ?? status)), { status, payload });
}

const AFFECTED = [{ intentId: "i1", title: "演示任务", materials: [], consequence: "会暂停" }];

beforeEach(() => {
  vi.useFakeTimers();
  localStorage.clear();
  vi.resetAllMocks();
  vi.mocked(imApi.saveDrafts).mockResolvedValue({ drafts: {}, updatedAt: null } as never);
  vi.mocked(imApi.fetchVisibleRange).mockResolvedValue({ visibleRange: {} } as never);
  vi.mocked(imApi.fetchIntents).mockResolvedValue({
    intents: [],
    conflicts: [],
    batchAvailable: false,
    recovery: { paused: [] },
  } as never);
  setActivePinia(createPinia());
});

afterEach(() => {
  vi.clearAllTimers();
  vi.useRealTimers();
  vi.restoreAllMocks();
  localStorage.clear();
});

describe("F1 取消恢复", () => {
  it("【状态/单元】取消后等待回读期间移动卡片：不携带被取消正文，独立移动保留，随后保存也不会落库被取消内容", async () => {
    const store = useInteractiveStore();
    vi.mocked(imApi.fetchBoardState).mockResolvedValue(response(state(3, [card()])));
    await store.refreshBoardFromServer();
    expect(store.board?.cards[0].content).toBe("正式正文");

    store.intents = [runningIntent()];
    vi.mocked(imApi.checkMaterialImpact).mockResolvedValue({
      ok: true,
      checkId: "chk1",
      stateVersion: 3,
      impactConfirmationRequired: true,
      affected: AFFECTED,
    } as never);

    // 用户编辑正文 → 出现影响确认
    store.commit(state(3, [card({ content: "被取消的正文" })]), "编辑正文");
    await store.saveNow();
    expect(store.pendingImpact, "编辑后没有出现影响确认").toBeTruthy();

    // 取消：回读故意很慢
    const slow = deferred<BoardStateResponse>();
    vi.mocked(imApi.fetchBoardState).mockReturnValue(slow.promise);
    const cancelling = store.cancelImpact();

    // 等待回读期间：用户做了一个**独立**的移动
    store.commit(state(3, [card({ x: 400 })]), "移动卡片");

    slow.resolve(response(state(3, [card()])));
    await cancelling;

    expect(store.board?.cards[0].content, "被取消的正文仍在候选里").toBe("正式正文");
    expect(store.board?.cards[0].x, "等待期间的独立移动被丢掉了").toBe(400);
    expect(store.cancelRecovery?.active, "服务器事实已经落地，撤回应当被确认").toBe(false);

    // 随后真的保存一次：落库的正文必须是恢复后的正式正文，位置是用户的独立移动
    const puts: BoardState[] = [];
    vi.mocked(imApi.checkMaterialImpact).mockResolvedValue({
      ok: true,
      affected: [],
      impactConfirmationRequired: false,
    } as never);
    vi.mocked(imApi.saveBoardState).mockImplementation(async (_boardId: string, st: BoardState) => {
      puts.push(st);
      return { ok: true, seq: 4, savedAt: "2026-10-10T00:00:02.000Z", state: st } as never;
    });
    await store.saveNow();
    expect(puts.length, "没有真的发出保存").toBeGreaterThan(0);
    expect(puts[puts.length - 1].cards[0].content, "被取消的正文被重新落库").toBe("正式正文");
    expect(puts[puts.length - 1].cards[0].x).toBe(400);
  });

  it("【状态/单元】取消后的回读失败：如实说明撤回未被确认，并提供重试", async () => {
    const store = useInteractiveStore();
    vi.mocked(imApi.fetchBoardState).mockResolvedValue(response(state(3, [card()])));
    await store.refreshBoardFromServer();
    store.intents = [runningIntent()];
    vi.mocked(imApi.checkMaterialImpact).mockResolvedValue({
      ok: true,
      checkId: "chk1",
      stateVersion: 3,
      impactConfirmationRequired: true,
      affected: AFFECTED,
    } as never);
    store.commit(state(3, [card({ content: "被取消的正文" })]), "编辑正文");
    await store.saveNow();

    vi.mocked(imApi.fetchBoardState).mockRejectedValue(new Error("网络中断"));
    await store.cancelImpact();

    expect(store.board?.cards[0].content, "本地没有先撤回被取消的正文").toBe("正式正文");
    expect(store.cancelRecovery?.active, "回读失败却宣称撤回已确认").toBe(true);
    expect(store.cancelRecovery?.reason ?? "").toContain("网络中断");

    // 重试入口：服务器恢复后事实落地
    vi.mocked(imApi.fetchBoardState).mockResolvedValue(response(state(3, [card()])));
    const retried = await store.retryCancelRecovery();
    expect(retried.ok).toBe(true);
    expect(store.cancelRecovery?.active).toBe(false);
  });
});

describe("F3 跨页面保存后的重新确认", () => {
  it("【状态/单元】另一页面保存到 4 后本页确认被拒：接收最新事实、保留本页正文、按新版本重新确认并保存", async () => {
    const store = useInteractiveStore();
    vi.mocked(imApi.fetchBoardState).mockResolvedValue(response(state(3, [card()])));
    await store.refreshBoardFromServer();
    store.intents = [runningIntent()];

    vi.mocked(imApi.checkMaterialImpact).mockResolvedValue({
      ok: true,
      checkId: "chk1",
      stateVersion: 3,
      impactConfirmationRequired: true,
      affected: AFFECTED,
    } as never);
    store.commit(state(3, [card({ content: "本页新正文" })]), "编辑正文");
    await store.saveNow();
    expect(store.pendingImpact?.checkId).toBe("chk1");

    // 确认时服务端已经有版本 4（另一页面独立移动了卡片）
    vi.mocked(imApi.fetchBoardState).mockResolvedValue(response(state(4, [card({ x: 999 })])));
    vi.mocked(imApi.checkMaterialImpact).mockResolvedValue({
      ok: true,
      checkId: "chk2",
      stateVersion: 4,
      impactConfirmationRequired: true,
      affected: AFFECTED,
    } as never);
    vi.mocked(imApi.saveBoardState).mockRejectedValue(
      apiError(409, { error: "stale_check", reason: "板面版本已变化" }),
    );

    const first = await store.confirmImpact();
    expect(first.outcome, "第一次确认被拒后应当给出新的确认说明").toBe("needs_confirm");
    expect(store.board?.cards[0].content, "本页候选正文被服务器事实覆盖").toBe("本页新正文");
    expect(store.board?.cards[0].x, "另一页面的独立移动没有被协调进来").toBe(999);
    expect(store.board?.seq, "仍然拿着旧 seq 预判").toBe(4);
    expect(store.pendingImpact?.stateVersion, "新确认仍然绑定旧版本").toBe(4);
    expect(store.pendingImpact?.checkId).toBe("chk2");
    expect(store.impactCheckError, "给出新说明后还留着旧的预判失败").toBeNull();

    // 用户按新说明确认：这次必须真的落库（seq=4）
    const puts: BoardState[] = [];
    vi.mocked(imApi.saveBoardState).mockImplementation(async (_boardId: string, st: BoardState) => {
      puts.push(st);
      return { ok: true, seq: 5, savedAt: "2026-10-10T00:00:03.000Z", state: st } as never;
    });
    const second = await store.confirmImpact();
    expect(second.outcome).toBe("saved");
    expect(puts.length).toBe(1);
    expect(puts[0].seq).toBe(4);
    expect(puts[0].cards[0].content).toBe("本页新正文");
    expect(puts[0].cards[0].x).toBe(999);
  });

  it("【状态/单元】服务端确认被拒（impact_confirmation_required）时也先接收最新事实再补取说明", async () => {
    const store = useInteractiveStore();
    vi.mocked(imApi.fetchBoardState).mockResolvedValue(response(state(3, [card()])));
    await store.refreshBoardFromServer();
    store.intents = [];

    store.commit(state(3, [card({ content: "本页新正文" })]), "编辑正文");
    vi.mocked(imApi.saveBoardState).mockRejectedValue(
      apiError(409, { error: "impact_confirmation_required", reason: "会影响运行中的任务", affectedTasks: AFFECTED }),
    );
    vi.mocked(imApi.fetchBoardState).mockResolvedValue(response(state(4, [card({ x: 999 })])));
    vi.mocked(imApi.checkMaterialImpact).mockResolvedValue({
      ok: true,
      checkId: "chk9",
      stateVersion: 4,
      impactConfirmationRequired: true,
      affected: AFFECTED,
    } as never);

    await store.saveNow();
    expect(store.pendingImpact?.checkId, "兜底分支没有拿到有效的新确认").toBe("chk9");
    expect(store.board?.seq, "兜底分支没有接收最新版本事实").toBe(4);
    expect(store.board?.cards[0].content, "本页候选被覆盖").toBe("本页新正文");
    expect(store.board?.cards[0].x, "另一页面的独立移动被丢掉").toBe(999);
  });
});

describe("F2 本机记录与服务器草稿", () => {
  function responseWithDrafts(st: BoardState, drafts: Record<string, string>): BoardStateResponse {
    return { ...response(st), drafts: { drafts, updatedAt: "2026-10-10T00:00:00.000Z" } } as unknown as BoardStateResponse;
  }

  it("【状态/单元】本页保存的成功回执不许删掉另一页面写下的新稿（正版本记录）", async () => {
    const store = useInteractiveStore();
    writeCardLocalDraft("c1", "本机旧稿", { boardId: "board_default", seq: 1 });
    vi.mocked(imApi.fetchBoardState).mockResolvedValue(
      responseWithDrafts(state(3, [card()]), { [cardDraftKey("c1")]: "服务器正文" }),
    );
    await store.load();
    expect(store.draftConflictFor("c1"), "没有识别出「本机 / 服务器」两份候选").not.toBeNull();

    // 用户明确选择服务器那一份：本机旧副本被清理
    store.resolveDraftConflict("c1", "server");
    expect(readCardLocalDraft("c1"), "选服务器本该清掉那条旧副本").toBeNull();

    // 同一浏览器另一个页面为同一张卡片写下了新稿（本页并不知道）
    writeCardLocalDraft("c1", "另一页面的新稿", { boardId: "board_default", seq: 2 });
    // 本页另一张卡片有未保存输入 → 触发一次草稿保存（成功回执不得删 c1 的新稿）
    store.setDraft(cardDraftKey("c2"), "另一张卡的新输入");
    vi.mocked(imApi.saveDrafts).mockResolvedValue({ drafts: {}, updatedAt: null } as never);
    await store.flushDrafts();

    expect(readCardLocalDraft("c1")?.text, "另一页面写下的新稿被本页的成功回执删掉了").toBe("另一页面的新稿");
  });

  it("【状态/单元】旧格式（没有 version）记录同样不许被误删", async () => {
    const store = useInteractiveStore();
    // 旧格式记录：没有 version 字段
    localStorage.setItem(
      cardLocalDraftStorageKey("c1"),
      JSON.stringify({ text: "更早的旧稿", updatedAt: 1, seq: 1, kind: "draft", boardId: "board_default" }),
    );
    vi.mocked(imApi.fetchBoardState).mockResolvedValue(
      responseWithDrafts(state(3, [card()]), { [cardDraftKey("c1")]: "服务器正文" }),
    );
    await store.load();
    store.draftConflictFor("c1");
    // 另一页面把它换成了新稿（仍然没有 version：跨页面写入的旧格式兼容形态）
    localStorage.setItem(
      cardLocalDraftStorageKey("c1"),
      JSON.stringify({ text: "另一页面的新稿", updatedAt: 2, seq: 2, kind: "draft", boardId: "board_default" }),
    );
    store.setDraft(cardDraftKey("c2"), "另一张卡的新输入");
    vi.mocked(imApi.saveDrafts).mockResolvedValue({ drafts: {}, updatedAt: null } as never);
    await store.flushDrafts();
    expect(readCardLocalDraft("c1")?.text, "旧格式新稿被误删").toBe("另一页面的新稿");
  });
});

describe("F3 正文内容冲突", () => {
  it("【状态/单元】两边都改了同一张卡片的正文：记成内容冲突、阻断保存，用户选择服务器版后才继续", async () => {
    const store = useInteractiveStore();
    vi.mocked(imApi.fetchBoardState).mockResolvedValue(response(state(3, [card()])));
    await store.refreshBoardFromServer();
    store.intents = [];

    store.commit(state(3, [card({ content: "本页正文" })]), "编辑正文");
    vi.mocked(imApi.saveBoardState).mockRejectedValue(
      apiError(409, { error: "stale_check", reason: "板面版本已变化" }),
    );
    vi.mocked(imApi.fetchBoardState).mockResolvedValue(response(state(4, [card({ content: "另一页正文" })])));
    vi.mocked(imApi.checkMaterialImpact).mockResolvedValue({
      ok: true,
      affected: [],
      impactConfirmationRequired: false,
    } as never);

    await store.saveNow();
    expect(store.boardContentConflictFor("c1"), "正文两边都改了却没有登记内容冲突").not.toBeNull();
    expect(store.board?.cards[0].content, "本页正文被静默覆盖").toBe("本页正文");
    expect(store.saveStatus, "有未决定的内容冲突却像正常一样").toBe("error");
    expect(store.saveError ?? "", "没有说明为什么不能保存").toContain("正文");

    // 用户明确选择「用服务器上的」：那时才允许继续保存，且保存的就是服务器那一版
    const puts: BoardState[] = [];
    vi.mocked(imApi.saveBoardState).mockImplementation(async (_boardId: string, st: BoardState) => {
      puts.push(st);
      return { ok: true, seq: 5, savedAt: "2026-10-10T00:00:09.000Z", state: st } as never;
    });
    store.resolveBoardContentConflict("c1", "server");
    expect(store.boardContentConflictFor("c1")).toBeNull();
    expect(store.board?.cards[0].content).toBe("另一页正文");
    await store.saveNow();
    expect(puts.length).toBeGreaterThan(0);
    expect(puts[puts.length - 1].cards[0].content).toBe("另一页正文");
  });

  it("【状态/单元】用户选择「用本页」：本页正文被保存，冲突清除", async () => {
    const store = useInteractiveStore();
    vi.mocked(imApi.fetchBoardState).mockResolvedValue(response(state(3, [card()])));
    await store.refreshBoardFromServer();
    store.intents = [];
    store.commit(state(3, [card({ content: "本页正文" })]), "编辑正文");
    vi.mocked(imApi.saveBoardState).mockRejectedValue(
      apiError(409, { error: "stale_state", reason: "旧版本" }),
    );
    vi.mocked(imApi.fetchBoardState).mockResolvedValue(response(state(4, [card({ content: "另一页正文" })])));
    await store.saveNow();
    expect(store.boardContentConflictFor("c1")).not.toBeNull();

    const puts: BoardState[] = [];
    vi.mocked(imApi.saveBoardState).mockImplementation(async (_boardId: string, st: BoardState) => {
      puts.push(st);
      return { ok: true, seq: 5, savedAt: "2026-10-10T00:00:10.000Z", state: st } as never;
    });
    store.resolveBoardContentConflict("c1", "local");
    await store.saveNow();
    expect(puts[puts.length - 1].cards[0].content).toBe("本页正文");
  });
});

describe("N1 保存生命周期", () => {
  it("【状态/单元】被替代的影响检查不再发出旧候选（旧写入不许覆盖已保存的新版）", async () => {
    const store = useInteractiveStore();
    vi.mocked(imApi.fetchBoardState).mockResolvedValue(response(state(3, [card()])));
    await store.refreshBoardFromServer();
    store.intents = [runningIntent()];

    const firstCheck = deferred<never>();
    vi.mocked(imApi.checkMaterialImpact).mockReturnValueOnce(firstCheck.promise as never);
    const puts: BoardState[] = [];
    vi.mocked(imApi.saveBoardState).mockImplementation(async (_boardId: string, st: BoardState) => {
      puts.push(st);
      return { ok: true, seq: 9, savedAt: "2026-10-10T00:00:04.000Z", state: st } as never;
    });

    // 第一版：影响检查在飞（第一次 checkMaterialImpact 被挂起）
    store.commit(state(3, [card({ content: "第一版" })]), "第一版");
    const savingFirst = store.saveNow();

    // 第二版：检查先结束并保存成功
    vi.mocked(imApi.checkMaterialImpact).mockResolvedValue({
      ok: true,
      affected: [],
      impactConfirmationRequired: false,
    } as never);
    store.commit(state(3, [card({ content: "第二版" })]), "第二版");
    await store.saveNow();
    expect(puts.length, "第二版没有保存").toBe(1);
    expect(puts[0].cards[0].content).toBe("第二版");

    // 第一版的检查迟到返回：不许再把第一版写出去
    firstCheck.resolve({ ok: true, affected: [], impactConfirmationRequired: false } as never);
    await savingFirst;
    await vi.waitFor(() => expect(puts.length).toBe(1));
    expect(puts.map((item) => item.cards[0].content), "旧候选被保存，覆盖了已保存的新版").toEqual(["第二版"]);
  });

  it("【状态/单元】服务端拒绝旧版本写入（409 stale_state）后：接收最新事实并用最新版本重试", async () => {
    const store = useInteractiveStore();
    vi.mocked(imApi.fetchBoardState).mockResolvedValue(response(state(3, [card()])));
    await store.refreshBoardFromServer();
    store.intents = [];

    store.commit(state(3, [card({ x: 300 })]), "移动卡片");
    vi.mocked(imApi.saveBoardState).mockRejectedValueOnce(
      apiError(409, { error: "stale_state", reason: "服务端已有更新的已保存版本", currentSeq: 4 }),
    );
    vi.mocked(imApi.fetchBoardState).mockResolvedValue(response(state(4, [card({ x: 10, content: "另一页写的正文" })])));
    const puts: BoardState[] = [];
    vi.mocked(imApi.saveBoardState).mockImplementation(async (_boardId: string, st: BoardState) => {
      puts.push(st);
      return { ok: true, seq: 5, savedAt: "2026-10-10T00:00:05.000Z", state: st } as never;
    });

    await store.saveNow();
    await vi.waitFor(() => expect(puts.length).toBeGreaterThan(0));
    expect(puts[puts.length - 1].seq, "重试没有带最新版本").toBe(4);
    expect(puts[puts.length - 1].cards[0].x, "本页独立移动没有被保留").toBe(300);
    expect(puts[puts.length - 1].cards[0].content, "另一页面的正文成果被覆盖").toBe("另一页写的正文");
  });
});

describe("N5 提交清理与勾选版本", () => {
  it("【状态/单元】提交成功清掉勾选：等待期间的移动不把旧勾选写回", async () => {
    const store = useInteractiveStore();
    vi.mocked(imApi.fetchBoardState).mockResolvedValue(response(state(3, [card({ checked: true })])));
    await store.refreshBoardFromServer();
    vi.mocked(imApi.checkMaterialImpact).mockResolvedValue({
      ok: true,
      affected: [],
      impactConfirmationRequired: false,
    } as never);
    vi.mocked(imApi.saveBoardState).mockImplementation(async (_boardId: string, st: BoardState) => ({
      ok: true,
      seq: 3,
      savedAt: "2026-10-10T00:00:06.000Z",
      state: st,
    }) as never);

    const submission = deferred<never>();
    vi.mocked(imApi.submitBoard).mockReturnValue(submission.promise as never);
    const submitting = store.submit();
    // 先让「提交前的保存」真的完成、提交请求真的发出去
    await flushMicrotasks();
    expect(vi.mocked(imApi.submitBoard).mock.calls.length, "提交请求没有发出").toBe(1);

    // 等待返回期间：用户只移动了卡片（勾选没有重新选择）
    store.commit(state(3, [card({ checked: true, x: 500 })]), "移动卡片");

    vi.mocked(imApi.fetchBoardState).mockResolvedValue(response(state(4, [card({ checked: false, x: 500 })])));
    submission.resolve({
      status: "succeeded",
      submission: { id: "s1" },
      before: {},
      after: {},
      expressions: [],
      baseline: { updated: true, previousSeq: 3, seq: 4 },
      delivery: { delivered: false, reason: "未接入", detail: "" },
      visibleRange: {},
      checkedCleared: ["c1"],
    } as never);
    await submitting;

    expect(store.board?.cards[0].checked, "用户没有重新选择，注释却又被勾上了").toBe(false);
    expect(store.board?.cards[0].x, "等待期间的移动丢了").toBe(500);

    const puts: BoardState[] = [];
    vi.mocked(imApi.saveBoardState).mockImplementation(async (_boardId: string, st: BoardState) => {
      puts.push(st);
      return { ok: true, seq: 5, savedAt: "2026-10-10T00:00:07.000Z", state: st } as never;
    });
    await store.saveNow();
    expect(puts.length).toBeGreaterThan(0);
    expect(puts[puts.length - 1].cards[0].checked, "旧勾选被写回服务器").toBe(false);
    expect(puts[puts.length - 1].cards[0].x).toBe(500);
  });

  it("【状态/单元】用户在提交等待期间重新勾选同一条：旧回执不许取消他/她的新选择", async () => {
    const store = useInteractiveStore();
    vi.mocked(imApi.fetchBoardState).mockResolvedValue(response(state(3, [card({ checked: true })])));
    await store.refreshBoardFromServer();
    vi.mocked(imApi.checkMaterialImpact).mockResolvedValue({
      ok: true,
      affected: [],
      impactConfirmationRequired: false,
    } as never);
    vi.mocked(imApi.saveBoardState).mockImplementation(async (_boardId: string, st: BoardState) => ({
      ok: true,
      seq: 3,
      savedAt: "2026-10-10T00:00:08.000Z",
      state: st,
    }) as never);

    const submission = deferred<never>();
    vi.mocked(imApi.submitBoard).mockReturnValue(submission.promise as never);
    const submitting = store.submit();
    await flushMicrotasks();
    expect(vi.mocked(imApi.submitBoard).mock.calls.length, "提交请求没有发出").toBe(1);

    // 用户先取消勾选、又主动重新勾选同一条
    store.commit(state(3, [card({ checked: false, x: 500 })]), "取消勾选");
    store.commit(state(3, [card({ checked: true, x: 500 })]), "重新勾选");

    vi.mocked(imApi.fetchBoardState).mockResolvedValue(response(state(4, [card({ checked: false, x: 500 })])));
    submission.resolve({
      status: "succeeded",
      submission: { id: "s2" },
      before: {},
      after: {},
      expressions: [],
      baseline: { updated: true, previousSeq: 3, seq: 4 },
      delivery: { delivered: false, reason: "未接入", detail: "" },
      visibleRange: {},
      checkedCleared: ["c1"],
    } as never);
    await submitting;

    expect(store.board?.cards[0].checked, "用户重新做的勾选被旧回执取消了").toBe(true);
  });
});
