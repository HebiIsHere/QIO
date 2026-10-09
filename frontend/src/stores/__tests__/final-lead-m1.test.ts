/**
 * Lead 收尾轮 M1 反例（docs/interactive-final-closure-contract.md M1）：
 *
 * 01 迟到读取回退已保存的新卡片草稿：
 *   PUT 与 GET 的相对顺序乱掉时，已保存成功的新正文不许被旧 GET 回退。
 * 06 反例 A：保存回执在飞期间的新候选不被旧回执覆盖，dirty 不被清掉。
 * 06 反例 B：审批收尾保存失败后回读服务器，本地候选保留。
 * 08 路径1：影响预判失败不落库，真实原因可见，候选保留。
 * 08 路径3：等待影响确认时不发出提交；提交携带本次候选版本。
 *
 * 标注：【会话层】只依赖 stores/interactive.ts 的公开状态与动作；
 *      【模拟失败/模拟乱序】用受控的 services/interactive mock 制造挂起与乱序回执。
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

function boardPayload(seq = 3, drafts: Record<string, string> = {}): Awaited<ReturnType<typeof api.fetchBoardState>> {
  return {
    board: { id: "board_default", title: "默认板面" },
    state: {
      boardId: "board_default",
      seq,
      updatedAt: "2026-10-09T10:00:00Z",
      cards: [],
      groups: [],
      links: [],
      selection: [],
    },
    seq,
    baseline: null,
    submissions: [],
    drafts: { drafts, updatedAt: drafts ? "2026-10-09T10:00:00Z" : null },
  } as Awaited<ReturnType<typeof api.fetchBoardState>>;
}

beforeEach(() => {
  setActivePinia(createPinia());
  vi.mocked(api.fetchBoardState).mockResolvedValue(boardPayload(3));
  vi.mocked(api.saveBoardState).mockResolvedValue({ ok: true, seq: 4, savedAt: "t", state: boardPayload(4).state } as never);
  vi.mocked(api.saveDrafts).mockResolvedValue({ drafts: {}, updatedAt: "t" } as never);
  vi.mocked(api.fetchVisibleRange).mockResolvedValue({ visibleRange: { cards: [], groups: [], links: [], selection: [], empty: true, notVisibleCount: 0 } } as never);
  vi.mocked(api.previewMaterialImpact).mockResolvedValue({ affected: [] } as never);
  vi.mocked(api.submitBoard).mockResolvedValue({
    status: "empty", submission: { id: "s1", seq: 4, status: "empty", createdAt: "" }, before: { cards: [], groups: [], links: [], selection: [], empty: true }, after: { cards: [], groups: [], links: [], selection: [], empty: true }, expressions: [], baseline: { updated: false }, delivery: { delivered: false, reason: "", detail: "" }, visibleRange: { cards: [], groups: [], links: [], selection: [], empty: true }, checkedCleared: [],
  } as never);
  vi.mocked(api.fetchIntents).mockResolvedValue({ intents: [], conflicts: [], batchAvailable: false, recovery: { paused: [] } } as never);
});

afterEach(() => {
  vi.clearAllMocks();
});

describe("01 迟到读取不许回退已保存的新卡片草稿", () => {
  it("GET 在飞期间 flushDrafts 先成功：旧 GET 返回后编辑草稿仍是新正文", async () => {
    const store = useInteractiveStore();
    await store.load();
    const key = "card:c1";
    store.setDraft(key, "新正文-v2");
    // 让 GET 返回的正文比「GET 结束时刻的已保存版本」旧：GET 在飞时先推进保存事实，
    // 再返回一份「不含这份草稿」的旧服务端正文 —— 相当于旧 GET 晚到。
    vi.mocked(api.fetchBoardState).mockImplementation(async () => {
      // GET 在飞期间：flushDrafts 的服务端保存成功
      await store.flushDrafts();
      return boardPayload(3, {}) as never; // 旧正文：服务器上还没有这份草稿
    });
    await store.refreshBoardFromServer();
    await flushPromises();
    expect(store.drafts[key]).toBe("新正文-v2");
    expect(store.draftStateFor(key).status).toBe("saved");
  });
});

describe("06 正式板面的版本保护", () => {
  it("反例A：保存回执在飞期间新提交的候选不被旧回执覆盖、dirty 不被清", async () => {
    const store = useInteractiveStore();
    await store.load();
    let resolveSave!: (v: unknown) => void;
    vi.mocked(api.saveBoardState).mockImplementation(
      () => new Promise((res) => { resolveSave = res; }) as never,
    );
    store.commit(
      { ...store.board!, seq: 99, cards: [card("c1", "v1")] } as BoardState,
      "第一版",
    );
    const savePromise = store.saveNow();
    await flushPromises();
    // 回执在飞时，用户完成第二版
    store.commit(
      { ...store.board!, seq: 99, cards: [card("c2", "v2")] } as BoardState,
      "第二版",
    );
    resolveSave({ ok: true, seq: 4, savedAt: "t", state: { boardId: "board_default", seq: 4, updatedAt: "t", cards: [card("c1", "v1")], groups: [], links: [], selection: [] } });
    await savePromise;
    await flushPromises();
    expect(store.board!.cards.map((c) => c.id)).toEqual(["c2"]);
    expect(store.dirty).toBe(true);
    // 第二版的保存真的会发出去并收敛
    vi.mocked(api.saveBoardState).mockResolvedValue({ ok: true, seq: 5, savedAt: "t2", state: store.board! } as never);
    await store.saveNow();
    await flushPromises();
    expect(store.dirty).toBe(false);
    expect(store.saveStatus).toBe("saved");
  });

  it("反例B：候选未保存时审批收尾回读服务器，候选保留", async () => {
    const store = useInteractiveStore();
    await store.load();
    store.commit({ ...store.board!, seq: 99, cards: [card("c9", "未保存编辑")] } as BoardState, "编辑");
    vi.mocked(api.saveBoardState).mockRejectedValue(new Error("保存失败"));
    await store.saveNow();
    await flushPromises();
    expect(store.saveStatus).toBe("error");
    expect(store.dirty).toBe(true);
    await store.refreshBoardFromServer();
    await flushPromises();
    expect(store.board!.cards.map((c) => c.content)).toEqual(["未保存编辑"]);
    expect(store.dirty).toBe(true);
  });
});

describe("08 影响确认约束保存与提交", () => {
  it("路径1：影响预判失败不落库，原因可见，候选保留", async () => {
    const store = useInteractiveStore();
    await store.load();
    vi.mocked(api.fetchIntents).mockResolvedValue({
      intents: [{ id: "t1", title: "任务", status: "running" } as never],
      conflicts: [],
      batchAvailable: false,
      recovery: { paused: [] },
    } as never);
    await store.loadIntents();
    vi.mocked(api.previewMaterialImpact).mockRejectedValue(new Error("后端预判不可用"));
    store.commit({ ...store.board!, seq: 99, cards: [card("c1", "改动")] } as BoardState, "依赖材料的改动");
    await store.saveNow();
    await flushPromises();
    expect(api.saveBoardState).not.toHaveBeenCalled();
    expect(store.impactCheckError).toContain("影响预判");
    expect(store.dirty).toBe(true);
  });

  it("路径3：等待影响确认时不发出提交；确认后提交携带本次候选版本", async () => {
    const store = useInteractiveStore();
    await store.load();
    vi.mocked(api.fetchIntents).mockResolvedValue({
      intents: [{ id: "t1", title: "任务", status: "running" } as never],
      conflicts: [],
      batchAvailable: false,
      recovery: { paused: [] },
    } as never);
    await store.loadIntents();
    vi.mocked(api.previewMaterialImpact).mockResolvedValue({
      affected: [{ intentId: "t1", title: "任务", materials: ["c1"], consequence: "暂停" }],
    } as never);
    store.commit({ ...store.board!, seq: 99, cards: [card("c1", "改动")] } as BoardState, "改动");
    await store.saveNow();
    await flushPromises();
    expect(store.pendingImpact).not.toBeNull();
    const r = await store.submit();
    expect(r).toBeNull();
    expect(api.submitBoard).not.toHaveBeenCalled();
    vi.mocked(api.previewMaterialImpact).mockResolvedValue({ affected: [] } as never);
    await store.confirmImpact();
    await flushPromises();
    await store.submit();
    expect(api.submitBoard).toHaveBeenCalledTimes(1);
    expect((api.submitBoard as ReturnType<typeof vi.fn>).mock.calls[0][3]).toBeTypeOf("number");
  });
});
