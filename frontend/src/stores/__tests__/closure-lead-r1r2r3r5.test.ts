/**
 * 本轮 Lead 侧反例（R1/R2/R3/R5）的会话层测试。
 *
 * R1 取消影响确认后正式板面必须撤回到已保存状态（新输入仍在草稿里）；
 * R2 迟到的 GET 不许覆盖已成功保存的新正式板面（含两次 GET 乱序与普通最新读取对照）；
 * R3 第一版保存未返回时完成第二版：服务器接受第一版是「版本事实」，第二版候选必须保留并能继续保存；
 * R5 服务端兜底影响确认必须补取 checkId，确认后才真的能存进去（不允许无条件保存）。
 *
 * 标注：【会话层】只依赖 stores/interactive.ts 的公开状态与动作；
 *      【模拟乱序/模拟 409】用受控的 services/interactive mock 制造挂起、乱序与服务端拒绝。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { flushPromises } from "@vue/test-utils";
import { useInteractiveStore } from "../interactive";
import * as api from "../../services/interactive";
import type { BoardState } from "../../interactive/types";

vi.mock("../../services/interactive", () => ({
  fetchBoardState: vi.fn(),
  saveBoardState: vi.fn(),
  saveDrafts: vi.fn(),
  fetchVisibleRange: vi.fn(),
  previewMaterialImpact: vi.fn(),
  checkMaterialImpact: vi.fn(),
  submitBoard: vi.fn(),
  fetchIntents: vi.fn(),
}));

function card(id: string, content: string): BoardState["cards"][number] {
  return {
    id,
    kind: "text",
    content,
    meta: {},
    x: 0,
    y: 0,
    w: 1,
    h: 1,
    checked: false,
    hidden: false,
    folded: false,
    bookmarked: false,
    deleted: false,
    createdAt: "",
    updatedAt: "",
  };
}

function boardPayload(
  seq = 3,
  drafts: Record<string, string> = {},
  cards: BoardState["cards"] = [card("c1", "已保存的第一版")],
): Awaited<ReturnType<typeof api.fetchBoardState>> {
  return {
    board: { id: "board_default", title: "默认板面" },
    state: {
      boardId: "board_default",
      seq,
      updatedAt: "2026-10-09T10:00:00Z",
      cards,
      groups: [],
      links: [],
      selection: [],
    },
    seq,
    baseline: null,
    submissions: [],
    drafts: { drafts, updatedAt: "2026-10-09T10:00:00Z" },
  } as Awaited<ReturnType<typeof api.fetchBoardState>>;
}

function savedResponse(seq: number, cards: BoardState["cards"]) {
  return { ok: true, seq, savedAt: "t" + seq, state: boardPayload(seq, {}, cards).state } as never;
}

/** 受控的 PUT 挂起：测试自己决定它什么时候返回 */
function deferredSave() {
  let resolve!: (value: unknown) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

function lastSaveCall() {
  const calls = vi.mocked(api.saveBoardState).mock.calls;
  return calls[calls.length - 1];
}

function conflictError(error: string, payload: Record<string, unknown>) {
  return Object.assign(new Error(error), { status: 409, payload: { error, ...payload } });
}

function withCardContent(store: ReturnType<typeof useInteractiveStore>, content: string): BoardState {
  const next = JSON.parse(JSON.stringify(store.board)) as BoardState;
  next.cards = next.cards.map((item) => (item.id === "c1" ? { ...item, content } : item));
  return next;
}

/** 一条执行中的任务：影响确认分支只有在「前端知道有执行中任务」时才会主动预判 */
function runningIntents() {
  return {
    intents: [
      {
        id: "A",
        boardId: "board_default",
        title: "任务A",
        status: "running",
        kind: "",
        materialRefs: ["c1"],
        preview: {},
        progress: { done: 1, total: 3, text: "进行中" },
        reason: "",
        createdAt: "",
        updatedAt: "",
      },
    ],
    conflicts: [],
    batchAvailable: false,
    recovery: { paused: [] },
  } as never;
}

/** 覆盖影响检查的返回值（默认实现会被 once 队列串味，所以统一走这里） */
function impactCheck(value: Record<string, unknown>) {
  vi.mocked(api.checkMaterialImpact).mockResolvedValue(value as never);
}

function noImpact() {
  impactCheck({ ok: true, checkId: "chk_none", stateVersion: 3, affected: [], impactConfirmationRequired: false });
}

function oneRunningAffected(checkId: string) {
  impactCheck({
    ok: true,
    checkId,
    stateVersion: 3,
    affected: [{ intentId: "A", title: "任务A", materials: ["材料"], consequence: "会暂停" }],
    impactConfirmationRequired: true,
  });
}

beforeEach(() => {
  vi.resetAllMocks();
  localStorage.clear();
  setActivePinia(createPinia());
  vi.mocked(api.fetchBoardState).mockResolvedValue(boardPayload(3));
  vi.mocked(api.saveBoardState).mockResolvedValue(savedResponse(4, [card("c1", "已保存的第一版")]));
  vi.mocked(api.saveDrafts).mockResolvedValue({ drafts: {}, updatedAt: "t" } as never);
  vi.mocked(api.fetchVisibleRange).mockResolvedValue({
    visibleRange: { cards: [], groups: [], links: [], selection: [], empty: true, notVisibleCount: 0 },
  } as never);
  vi.mocked(api.previewMaterialImpact).mockResolvedValue({ affected: [] } as never);
  noImpact();
  vi.mocked(api.fetchIntents).mockResolvedValue({
    intents: [],
    conflicts: [],
    batchAvailable: false,
    recovery: { paused: [] },
  } as never);
});

afterEach(() => {
  vi.clearAllMocks();
});

describe("R1 取消影响确认后正式板面必须撤回", () => {
  it("取消后板面回到已保存状态，草稿里的新输入还在，且不会被后续无关操作偷偷带回保存", async () => {
    vi.mocked(api.fetchIntents).mockResolvedValue(runningIntents());
    const store = useInteractiveStore();
    await store.load();
    store.setDraft("card:c1", "草稿里的新输入");
    store.commit(withCardContent(store, "被取消的新正文"), "编辑材料");
    oneRunningAffected("chk_A");
    await store.saveNow();
    expect(store.pendingImpact, "没有进入影响确认，本用例前提不成立").toBeTruthy();
    expect(store.board?.cards[0].content, "确认框出现时板面已经是新正文").toBe("被取消的新正文");

    vi.mocked(api.fetchBoardState).mockResolvedValue(boardPayload(3, {}, [card("c1", "已保存的第一版")]));
    await store.cancelImpact();
    await flushPromises();

    expect(store.pendingImpact, "取消后仍停在影响确认里").toBeNull();
    expect(store.board?.cards[0].content, "取消后正式板面没有撤回：仍显示被取消的新正文").toBe("已保存的第一版");
    expect(store.dirty, "取消后板面仍是未保存候选").toBe(false);
    expect(store.saveStatus, "取消并回读成功后保存状态不对").toBe("saved");
    expect(store.drafts["card:c1"], "取消把草稿里的新输入弄丢了").toBe("草稿里的新输入");

    // 之后完成一次无关操作：被取消的正文不许被重新带入保存（无关操作本身不受影响）
    noImpact();
    store.commit(withCardContent(store, "无关操作后的正文"), "无关操作");
    await store.saveNow();
    const lastPut = lastSaveCall()?.[1] as BoardState;
    expect(JSON.stringify(lastPut.cards), "被取消的改动被后续保存重新带上了").not.toContain("被取消的新正文");
  });

  it("取消时回读失败：如实提示原因、候选恢复为未保存，不假装撤回成功", async () => {
    vi.mocked(api.fetchIntents).mockResolvedValue(runningIntents());
    const store = useInteractiveStore();
    await store.load();
    store.commit(withCardContent(store, "被取消的新正文"), "编辑材料");
    oneRunningAffected("chk_A");
    await store.saveNow();
    expect(store.pendingImpact).toBeTruthy();

    vi.mocked(api.fetchBoardState).mockRejectedValueOnce(new Error("读取板面失败：连接中断"));
    await store.cancelImpact();

    expect(store.pendingImpact, "取消后仍停在影响确认里").toBeNull();
    expect(store.dirty, "回读失败却把候选说成已保存").toBe(true);
    expect(store.saveStatus, "回读失败没有如实标成失败").toBe("error");
    expect(store.saveError ?? "").toContain("连接中断");
  });
});

describe("R2 迟到的 GET 不许覆盖已保存成功的新正式板面", () => {
  it("旧 GET 在飞期间保存成功：旧 GET 返回后板面仍是新正文", async () => {
    const store = useInteractiveStore();
    await store.load();

    const gate = deferredSave();
    vi.mocked(api.fetchBoardState).mockImplementationOnce(async () => {
      await gate.promise; // 模拟旧 GET 晚到
      return boardPayload(3, {}, [card("c1", "旧的服务端正文")]);
    });
    const reading = store.refreshBoardFromServer();

    // 读取在飞期间：用户完成新编辑并保存成功（服务器 seq 4 = 新正文）
    store.commit(withCardContent(store, "已保存的新正文"), "编辑");
    vi.mocked(api.saveBoardState).mockResolvedValueOnce(savedResponse(4, [card("c1", "已保存的新正文")]));
    await store.saveNow();
    expect(store.saveStatus, "本用例前提：保存必须成功").toBe("saved");

    gate.resolve(undefined);
    await reading;
    await flushPromises();

    expect(store.board?.cards[0].content, "迟到的旧 GET 把已保存的新正式板面回退了").toBe("已保存的新正文");
    expect(store.dirty).toBe(false);
    expect(store.saveStatus).toBe("saved");
  });

  it("没有任何并发改动时，普通的最新读取仍然照常采用（不是永久拒绝刷新）", async () => {
    const store = useInteractiveStore();
    await store.load();
    vi.mocked(api.fetchBoardState).mockResolvedValue(boardPayload(7, {}, [card("c1", "服务器上的最新正文")]));
    await store.refreshBoardFromServer();
    expect(store.board?.cards[0].content).toBe("服务器上的最新正文");
    expect(store.board?.seq).toBe(7);
  });

  it("两次 GET 乱序：后发起的先返回并被采用，先发起的迟到返回不得覆盖", async () => {
    const store = useInteractiveStore();
    await store.load();

    const first = deferredSave();
    vi.mocked(api.fetchBoardState).mockImplementationOnce(async () => {
      await first.promise;
      return boardPayload(3, {}, [card("c1", "先发起的旧正文")]);
    });
    const older = store.refreshBoardFromServer();

    vi.mocked(api.fetchBoardState).mockResolvedValueOnce(boardPayload(6, {}, [card("c1", "后发起的新正文")]));
    await store.refreshBoardFromServer();
    expect(store.board?.cards[0].content).toBe("后发起的新正文");

    first.resolve(undefined);
    await older;
    await flushPromises();
    expect(store.board?.cards[0].content, "迟到的旧 GET 覆盖了更新的读取结果").toBe("后发起的新正文");
  });
});

describe("R3 第一版保存成功是版本事实，第二版候选必须能继续保存", () => {
  it("第一版 PUT 在飞时完成第二版：吸收第一版的 seq，第二版仍能正常保存", async () => {
    const store = useInteractiveStore();
    await store.load();

    const put = deferredSave();
    vi.mocked(api.saveBoardState).mockImplementationOnce(async () => gateResolve(put) as never);
    store.commit(withCardContent(store, "第一版正文"), "第一版");
    const pending = store.saveNow();

    store.commit(withCardContent(store, "第二版正文"), "第二版");
    put.resolve(savedResponse(4, [card("c1", "第一版正文")]));
    await pending;
    await flushPromises();

    expect(store.board?.cards[0].content, "第二版候选被第一版回执覆盖了").toBe("第二版正文");
    expect(store.dirty, "第二版候选没有被当成未保存").toBe(true);
    expect(store.board?.seq, "第一版保存成功的版本事实没有被吸收：候选仍带旧 seq").toBe(4);

    vi.mocked(api.saveBoardState).mockResolvedValueOnce(savedResponse(5, [card("c1", "第二版正文")]));
    await store.saveNow();
    const secondPut = lastSaveCall()?.[1] as BoardState;
    expect(secondPut.seq, "第二版保存仍带旧 seq").toBe(4);
    expect(secondPut.cards[0].content).toBe("第二版正文");
    expect(store.saveStatus, "第二版没有保存成功，停在保存中/失败").toBe("saved");
    expect(store.dirty).toBe(false);
  });
});

function gateResolve(put: { promise: Promise<unknown> }) {
  return put.promise;
}

describe("R5 服务端兜底影响确认必须补取 checkId", () => {
  it("服务端 409 兜底：补取 checkId 后，用户确认才真的保存成功", async () => {
    const store = useInteractiveStore();
    await store.load();
    // 前端任务清单滞后（没有执行中任务）：保存前的预判被跳过，直接撞上服务端门
    vi.mocked(api.fetchIntents).mockResolvedValue({
      intents: [],
      conflicts: [],
      batchAvailable: false,
      recovery: { paused: [] },
    } as never);
    vi.mocked(api.saveBoardState).mockRejectedValueOnce(
      conflictError("impact_confirmation_required", {
        affectedTasks: [{ intentId: "A", title: "任务A", materials: ["材料"], consequence: "会暂停" }],
      }) as never,
    );
    impactCheck({
      ok: true,
      checkId: "chk_fallback",
      stateVersion: 3,
      affected: [{ intentId: "A", title: "任务A", materials: ["材料"], consequence: "会暂停" }],
      impactConfirmationRequired: true,
    });

    store.commit(withCardContent(store, "改了运行任务依赖的材料"), "编辑材料");
    await store.saveNow();

    expect(store.pendingImpact, "服务端兜底没有给出影响确认框").toBeTruthy();
    expect(store.pendingImpact?.checkId, "兜底分支没有补取到 checkId：确认后必然再被拒一次").toBe("chk_fallback");
    expect(store.pendingImpact?.affected.map((item) => item.intentId)).toEqual(["A"]);

    vi.mocked(api.saveBoardState).mockResolvedValueOnce(savedResponse(4, [card("c1", "改了运行任务依赖的材料")]));
    await store.confirmImpact();
    const confirmed = lastSaveCall()?.[3] as
      | { checkId?: string; stateVersion?: number }
      | undefined;
    expect(confirmed?.checkId, "确认保存没有带上确认句柄").toBe("chk_fallback");
    expect(confirmed?.stateVersion).toBe(3);
    expect(store.saveStatus, "确认后仍然没有保存成功").toBe("saved");
    expect(store.pendingImpact, "确认保存成功后仍停在确认框").toBeNull();
  });

  it("补取到的范围与刚才的服务端说明不同：先显示变化，由用户按新范围确认", async () => {
    const store = useInteractiveStore();
    await store.load();
    vi.mocked(api.saveBoardState).mockRejectedValueOnce(
      conflictError("impact_confirmation_required", {
        affectedTasks: [{ intentId: "A", title: "任务A", materials: ["材料"], consequence: "会暂停" }],
      }) as never,
    );
    impactCheck({
      ok: true,
      checkId: "chk_wide",
      stateVersion: 3,
      affected: [
        { intentId: "A", title: "任务A", materials: ["材料"], consequence: "会暂停" },
        { intentId: "B", title: "任务B", materials: ["材料"], consequence: "会暂停" },
      ],
      impactConfirmationRequired: true,
    });

    store.commit(withCardContent(store, "改了材料"), "编辑材料");
    await store.saveNow();

    expect(store.pendingImpact?.affected.map((item) => item.intentId)).toEqual(["A", "B"]);
    expect(store.pendingImpact?.note ?? "", "范围变化没有被说明出来").toContain("不同");
  });
});
