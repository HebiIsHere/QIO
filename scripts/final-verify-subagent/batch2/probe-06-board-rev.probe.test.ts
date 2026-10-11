/**
 * 第二批独立对抗性探针 · 06：板面旧回执不许覆盖新候选（单元/状态）。
 *
 * 与仓库内 final-lead-m1.test.ts 的差别（为什么另写一个）：
 *   - 被测对象只用「公开动作 + 受控 services」，不读任何 store 私有量；
 *   - 第一版 PUT 回执**受控挂起**，在飞期间完成第二版；旧回执返回的 state 就是
 *     第一版内容 —— 只要去掉版本记账，旧回执会立刻把板面与 dirty 一起改回第一版；
 *   - 显式断言「第二版才是当前事实」「dirty 仍为真」「saveStatus 仍为 saving」，
 *     并在第二版保存收敛后再断言落地的仍是第二版。
 *
 * 契约（docs/interactive-final-closure-contract.md M1 / 收尾项 06 反例 A）：
 *   保存回执落地前必须比较候选版本，旧回执不得覆盖新候选、不得清 dirty。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { flushPromises } from "@vue/test-utils";
import { useInteractiveStore } from "../../../frontend/src/stores/interactive";
import * as api from "../../../frontend/src/services/interactive";
import type { BoardState } from "../../../frontend/src/interactive/types";

vi.mock("../../../frontend/src/services/interactive", () => ({
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

function stateOf(seq: number, cards: BoardState["cards"]): BoardState {
  return { boardId: "board_default", seq, updatedAt: "2026-10-09T10:00:00Z", cards, groups: [], links: [], selection: [] };
}

function boardPayload(seq = 3, cards: BoardState["cards"] = []) {
  return {
    board: { id: "board_default", title: "默认板面" },
    state: stateOf(seq, cards),
    seq,
    baseline: null,
    submissions: [],
    drafts: { drafts: {}, updatedAt: null },
  } as Awaited<ReturnType<typeof api.fetchBoardState>>;
}

beforeEach(() => {
  localStorage.clear();
  setActivePinia(createPinia());
  vi.mocked(api.fetchBoardState).mockResolvedValue(boardPayload(3) as never);
  vi.mocked(api.saveDrafts).mockResolvedValue({ drafts: {}, updatedAt: "t" } as never);
  vi.mocked(api.fetchVisibleRange).mockResolvedValue({ visibleRange: { cards: [], groups: [], links: [], selection: [], empty: true, notVisibleCount: 0 } } as never);
  vi.mocked(api.checkMaterialImpact).mockResolvedValue({ ok: true, checkId: "chk_none", stateVersion: 3, affected: [], impactConfirmationRequired: false } as never);
  vi.mocked(api.previewMaterialImpact).mockResolvedValue({ affected: [] } as never);
  vi.mocked(api.fetchIntents).mockResolvedValue({ intents: [], conflicts: [], batchAvailable: false, recovery: { paused: [] } } as never);
});

afterEach(() => {
  vi.clearAllMocks();
});

describe("B2-06 板面候选版本记账", () => {
  it("尖锐用例：第一版回执在飞期间完成第二版 → 旧回执不得覆盖第二版、不得清 dirty", async () => {
    const store = useInteractiveStore();
    await store.load();

    // 第一版保存：回执受控挂起（模拟「已发出、还没返回」）
    let resolveFirst!: (v: unknown) => void;
    vi.mocked(api.saveBoardState).mockImplementationOnce(
      () => new Promise((res) => { resolveFirst = res; }) as never,
    );
    store.commit(stateOf(3, [card("c1", "第一版")]), "第一版");
    const firstSave = store.saveNow();
    await flushPromises();

    // 回执在飞期间：用户完成了第二版（板面是第二版，dirty 为真）
    store.commit(stateOf(3, [card("c2", "第二版")]), "第二版");
    expect(store.board!.cards.map((c) => c.id), "第二版必须先成为当前板面").toEqual(["c2"]);

    // 旧回执返回的正是第一版内容
    resolveFirst({ ok: true, seq: 4, savedAt: "t", state: stateOf(4, [card("c1", "第一版")]) });
    await firstSave;
    await flushPromises();

    expect(store.board!.cards.map((c) => c.id), "旧回执把板面换回了第一版（新候选被覆盖）").toEqual(["c2"]);
    expect(store.board!.cards[0].content, "旧回执把板面正文换回了第一版").toBe("第二版");
    expect(store.dirty, "旧回执不得清掉第二版的 dirty").toBe(true);
    expect(store.saveStatus, "第二版还没保存成功，状态不能显示为 saved").toBe("saving");

    // 第二版保存才真正收敛：载荷必须是第二版
    const calls = vi.mocked(api.saveBoardState).mock.calls;
    expect(calls.length, "第二版必须真的再发一次保存").toBeGreaterThanOrEqual(1);
    vi.mocked(api.saveBoardState).mockImplementation((async (_id: string, state: BoardState) => ({
      ok: true, seq: 5, savedAt: "t2", state,
    })) as never);
    await store.saveNow();
    await flushPromises();
    const lastSent = vi.mocked(api.saveBoardState).mock.calls.at(-1)?.[1] as BoardState;
    expect(lastSent.cards.map((c) => c.id), "收敛保存发的必须是第二版").toEqual(["c2"]);
    expect(store.dirty).toBe(false);
    expect(store.saveStatus).toBe("saved");
  });
});
