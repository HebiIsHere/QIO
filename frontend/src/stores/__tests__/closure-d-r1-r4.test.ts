/**
 * 【独立验收 D · closure-d】反例 R1 / R2 / R3 / R4（第①层：状态与单元）。
 *
 * 本文件断言的都是**正确行为**。冻结时（集成候选 SHA 之前）它们在基线上必须失败，
 * 失败信息就是「反例在基线上成立」的证据；修复后同一份文件必须全部通过。
 * 因此本文件**不允许**用条件判断跳过断言，也不允许把断言写弱到基线也能过。
 *
 * 每个用例都刻意构造真实时序：
 *  - R1：服务端门 409 要求影响确认 → 用户取消 → 板面必须回到服务器已保存内容；
 *  - R2：保存先成功、更早发起的 GET 后返回 → 已保存的新正式板面不许被旧读取换回；
 *  - R3：第一版保存成功（回执在飞时出现第二版）→ 第二版仍必须能继续保存到服务器；
 *  - R4：本机清除依据写失败 + 服务器清除已确认 → 重试不许把最新旧稿写成 cleared。
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
    id, kind: "text", content, meta: {}, x: 0, y: 0, w: 1, h: 1,
    checked: false, hidden: false, folded: false, bookmarked: false, deleted: false,
    createdAt: "", updatedAt: "",
  };
}

function state(seq: number, cards: BoardState["cards"]): BoardState {
  return { boardId: "board_default", seq, updatedAt: "2026-10-10T00:00:00Z", cards, groups: [], links: [], selection: [] };
}

function payload(seq: number, cards: BoardState["cards"], drafts: Record<string, string> = {}): never {
  return {
    board: { id: "board_default", title: "默认板面" },
    state: state(seq, cards),
    seq,
    baseline: null,
    submissions: [],
    drafts: { drafts, updatedAt: drafts ? "t" : null },
  } as never;
}

function intent(id: string, status = "running"): never {
  return {
    id, boardId: "board_default", submissionId: null, title: "任务-" + id, summary: "", status,
    preview: { kind: "task" }, impact: { materials: ["c1"], consequence: "暂停并保留进度" },
    dependsOn: [], conflictsWith: [], conflictKey: "", materialRefs: ["c1"],
    progress: { done: 0, total: 1, text: "" }, reason: "", demo: true, createdAt: "", updatedAt: "",
  } as never;
}

/**
 * 真实的 409 响应体（FastAPI HTTPException(detail=<dict>)）：
 * services/interactive.ts 会把 body.detail 提成 payload，所以替身必须用这个形状，
 * 否则替身行为与线上不同，会给出假的「已确认」/假的拒绝。
 */
function gateError(affectedTasks: unknown[]): Error {
  return Object.assign(new Error("409"), {
    status: 409,
    // 与 services/interactive.ts 解出的 payload 同形状（FastAPI 的 { detail: {...} }）
    payload: { error: "impact_confirmation_required", reason: "这次保存会改动正在执行任务依赖的材料", affectedTasks },
  });
}

beforeEach(() => {
  localStorage.clear();
  setActivePinia(createPinia());
  vi.mocked(api.fetchBoardState).mockResolvedValue(payload(3, [card("c1", "服务器原文")]));
  vi.mocked(api.saveBoardState).mockResolvedValue({
    ok: true, seq: 4, savedAt: "t", state: state(4, [card("c1", "服务器原文")]),
  } as never);
  vi.mocked(api.saveDrafts).mockResolvedValue({ drafts: {}, updatedAt: "t" } as never);
  vi.mocked(api.fetchVisibleRange).mockResolvedValue({
    visibleRange: { cards: [], groups: [], links: [], selection: [], empty: true, notVisibleCount: 0 },
  } as never);
  vi.mocked(api.checkMaterialImpact).mockResolvedValue({
    ok: true, checkId: "chk_none", stateVersion: 3, affected: [], impactConfirmationRequired: false,
  } as never);
  vi.mocked(api.previewMaterialImpact).mockResolvedValue({ affected: [] } as never);
  vi.mocked(api.submitBoard).mockResolvedValue({ status: "empty" } as never);
  vi.mocked(api.fetchIntents).mockResolvedValue({
    intents: [intent("A")], conflicts: [], batchAvailable: false, recovery: { paused: [] },
  } as never);
});

afterEach(() => {
  vi.clearAllMocks();
});

describe("R1 取消影响确认后，正式板面必须回到服务器已保存的内容", () => {
  it("服务端门要求确认 → 取消：板面回到服务器原文、dirty 与保存状态如实、草稿候选保留", async () => {
    const store = useInteractiveStore();
    await store.load();
    expect(store.board!.cards.map((c) => c.content)).toEqual(["服务器原文"]);

    store.setDraft("card:c1", "取消前的编辑候选");
    store.commit(state(3, [card("c1", "被取消掉的新正文")]), "改正文");
    vi.mocked(api.saveBoardState).mockRejectedValue(
      gateError([{ intentId: "A", title: "任务-A", materials: ["c1"], consequence: "暂停" }]),
    );
    await store.saveNow();
    expect(store.pendingImpact).not.toBeNull();
    expect(store.board!.cards.map((c) => c.content)).toEqual(["被取消掉的新正文"]);

    await store.cancelImpact();
    await flushPromises();

    // 正确行为：取消 = 这次板面变更不生效 → 板面是服务器已保存内容（不是被取消掉的候选）
    expect(store.board!.cards.map((c) => c.content)).toEqual(["服务器原文"]);
    // 取消不删编辑草稿候选与恢复来源
    expect(store.drafts["card:c1"]).toBe("取消前的编辑候选");
    // 取消落地后不是「有待保存的脏候选」
    expect(store.dirty).toBe(false);
  });
});

describe("R2 迟到的 GET 不许把已成功保存的新正式板面换回旧正文", () => {
  it("保存先落地、更早发起的 GET 后返回：板面仍是新正文，不许回退", async () => {
    const store = useInteractiveStore();
    await store.load();

    let resolveGet!: (v: unknown) => void;
    vi.mocked(api.fetchBoardState).mockImplementation(
      () => new Promise((res) => { resolveGet = res; }) as never,
    );
    const refresh = store.refreshBoardFromServer();
    await Promise.resolve();

    // 用户提交新正文并保存成功（服务器接受为 seq=4）
    store.commit(state(3, [card("c1", "已保存成功的新正文")]), "改正文");
    vi.mocked(api.saveBoardState).mockResolvedValue({
      ok: true, seq: 4, savedAt: "t", state: state(4, [card("c1", "已保存成功的新正文")]),
    } as never);
    await store.saveNow();
    expect(store.board!.cards.map((c) => c.content)).toEqual(["已保存成功的新正文"]);

    // 更早发起的读取现在才返回：它读到的是旧正文
    resolveGet(payload(3, [card("c1", "旧正文")]));
    await refresh;
    await flushPromises();

    // 正确行为：已保存成功的新正式板面不被更早发起的读取回退（迟到读取只能作废，不许落地）
    expect(store.board!.cards.map((c) => c.content)).toEqual(["已保存成功的新正文"]);
  });
});

describe("R3 保住第二版正文，也必须保住第一版保存成功的版本事实", () => {
  it("第二版必须真的能继续保存到服务器（把服务器最新 seq 带上），而不是永远卡在未保存", async () => {
    const store = useInteractiveStore();
    await store.load();

    let resolveFirst!: (v: unknown) => void;
    vi.mocked(api.saveBoardState).mockImplementation(
      () => new Promise((res) => { resolveFirst = res; }) as never,
    );
    store.commit(state(3, [card("c1", "第一版")]), "第一版");
    const first = store.saveNow();
    await flushPromises();

    // 第一版回执在飞时用户完成第二版
    store.commit(state(3, [card("c1", "第二版")]), "第二版");
    resolveFirst({ ok: true, seq: 4, savedAt: "t1", state: state(4, [card("c1", "第一版")]) });
    await first;
    await flushPromises();

    // 第二版候选保留、仍是未保存
    expect(store.board!.cards.map((c) => c.content)).toEqual(["第二版"]);
    expect(store.dirty).toBe(true);

    // 第二版继续保存：必须携带服务器最新版本事实 seq=4（否则服务器按旧基准拒绝，第二版永远存不上）
    let secondSeq: number | null = null;
    vi.mocked(api.saveBoardState).mockImplementation(async (_id, putState) => {
      secondSeq = putState.seq;
      return { ok: true, seq: 5, savedAt: "t2", state: state(5, []) } as never;
    });
    await store.saveNow();
    await flushPromises();

    expect(secondSeq).toBe(4);
    expect(store.dirty).toBe(false);
  });
});

describe("R4 本机清除依据写失败：重试不许把最新旧稿写成 cleared", () => {
  it("重试后本机记录仍是编辑稿 kind=draft，正文原样保留", async () => {
    localStorage.setItem(
      "qio.draft.card.local-c1",
      JSON.stringify({ text: "旧稿-v1", updatedAt: Date.now(), seq: 1, kind: "draft", version: 1, boardId: "board_default" }),
    );
    const store = useInteractiveStore();
    await store.load();
    expect(store.drafts["card:c1"]).toBe("旧稿-v1");

    // 用户清除该草稿，但本机清除依据写失败（setItem 抛错）；磁盘上仍是 kind=draft 的旧稿
    const setItem = Storage.prototype.setItem;
    Storage.prototype.setItem = function () { throw new Error("模拟本机写入失败"); };
    store.clearDraft("card:c1");
    Storage.prototype.setItem = setItem;
    expect(store.draftRemovalStates["card:c1"].status).toBe("error");

    // 服务器清除成功（草稿集合变空），随后用户点重试
    vi.mocked(api.saveDrafts).mockResolvedValue({ drafts: {}, updatedAt: "t" } as never);
    await store.flushDrafts();
    await store.retryDraftSave();

    const disk = JSON.parse(localStorage.getItem("qio.draft.card.local-c1") ?? "null") as { kind?: string; text?: string } | null;
    expect(disk).not.toBeNull();
    // 正确行为：本机记录仍是编辑稿、正文原样（不许被写成 kind=cleared 的「整份清除」）
    expect(disk!.kind).toBe("draft");
    expect(disk!.text).toBe("旧稿-v1");
  });
});
