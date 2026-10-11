/**
 * 第二批独立对抗性探针 · 07：登记式草稿清除必须绑定「正式变更成功」（单元/状态）。
 *
 * 与仓库内 final-lead-m07-clear-binding.test.ts 的差别（为什么另写一个）：
 *   - 覆盖任务点名的**两个时点**：①等待影响确认（保存根本没发出）②保存失败；
 *     两个时点都断言「内存候选 + 本机恢复来源 + 登记项」三者都在；
 *   - 明确断言「只有保存回执对应当前候选时才清」，并覆盖成功后的正向清理（这一条
 *     正是 saveNow 里消化 pendingDraftClears 的那段在承重）；
 *   - 附带版本守卫：登记之后又输入更新版本 → 成功也不许清。
 *
 * 契约（docs/interactive-final-closure-contract.md 07 / §12.2）：
 *   组件只 requestDraftClear(key)；未确认 / 失败 / 取消期间候选与恢复来源都在；
 *   正式变更真的成功、且回执对应当前候选时才按登记版本清除。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { flushPromises } from "@vue/test-utils";
import { useInteractiveStore } from "../../../frontend/src/stores/interactive";
import * as api from "../../../frontend/src/services/interactive";
import { readCardLocalDraft } from "../../../frontend/src/interactive/drafts";
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
    seq, baseline: null, submissions: [], drafts: { drafts: {}, updatedAt: null },
  } as Awaited<ReturnType<typeof api.fetchBoardState>>;
}
const KEY = "card:c1";

beforeEach(() => {
  localStorage.clear();
  setActivePinia(createPinia());
  vi.mocked(api.fetchBoardState).mockResolvedValue(boardPayload(3, [card("c1", "")]) as never);
  vi.mocked(api.saveDrafts).mockResolvedValue({ drafts: {}, updatedAt: "t" } as never);
  vi.mocked(api.fetchVisibleRange).mockResolvedValue({ visibleRange: { cards: [], groups: [], links: [], selection: [], empty: true, notVisibleCount: 0 } } as never);
  vi.mocked(api.previewMaterialImpact).mockResolvedValue({ affected: [] } as never);
  vi.mocked(api.checkMaterialImpact).mockResolvedValue({ ok: true, checkId: "chk_none", stateVersion: 3, affected: [], impactConfirmationRequired: false } as never);
  vi.mocked(api.fetchIntents).mockResolvedValue({ intents: [], conflicts: [], batchAvailable: false, recovery: { paused: [] } } as never);
  vi.mocked(api.saveBoardState).mockImplementation((async (_id: string, state: BoardState) => ({ ok: true, seq: 4, savedAt: "t", state })) as never);
});

afterEach(() => { vi.clearAllMocks(); });

describe("B2-07 登记式清除绑定正式变更", () => {
  it("时点①等待影响确认：保存没发出，候选/恢复来源/登记项三者都在；确认后才清", async () => {
    vi.mocked(api.fetchIntents).mockResolvedValue({
      intents: [{ id: "t1", title: "任务", status: "running" }],
      conflicts: [], batchAvailable: false, recovery: { paused: [] },
    } as never);
    vi.mocked(api.checkMaterialImpact).mockResolvedValue({
      ok: true, checkId: "chk_A", stateVersion: 3,
      affected: [{ intentId: "t1", title: "任务", materials: ["c1"], consequence: "暂停" }],
      impactConfirmationRequired: true,
    } as never);

    const store = useInteractiveStore();
    await store.load();
    store.setDraft(KEY, "候选草稿");
    store.requestDraftClear(KEY);
    store.commit(stateOf(3, [card("c1", "材料改动")]), "改材料");

    await store.saveNow();
    await flushPromises();

    expect(api.saveBoardState, "等待影响确认时保存不得落库").not.toHaveBeenCalled();
    expect(store.pendingImpact, "必须停在「等待影响确认」").not.toBeNull();
    expect(store.drafts[KEY], "等待确认期间内存候选必须还在").toBe("候选草稿");
    expect(readCardLocalDraft("c1")?.text, "等待确认期间本机恢复来源必须还在").toBe("候选草稿");
    expect(readCardLocalDraft("c1")?.kind).toBe("draft");
    expect(store.hasCardDraft("c1")).toBe(true);
    expect(store.pendingDraftClearKeys, "等待确认期间登记项必须还在（等正式变更成功）").toContain(KEY);

    // 用户点确认 → 这次候选真的保存成功 → 才执行登记清除
    await store.confirmImpact();
    await flushPromises();
    expect(api.saveBoardState, "确认后必须真的发出保存").toHaveBeenCalledTimes(1);
    expect(store.drafts[KEY], "正式变更成功后候选应当被清掉").toBeUndefined();
    expect(store.hasCardDraft("c1"), "正式变更成功后恢复来源应当不再可恢复").toBe(false);
    expect(store.pendingDraftClearKeys, "消化完的登记不许留在表里").not.toContain(KEY);
  });

  it("时点②保存失败：候选/恢复来源/登记项都保留；重试成功后才清", async () => {
    const store = useInteractiveStore();
    await store.load();
    store.setDraft(KEY, "候选草稿");
    store.requestDraftClear(KEY);
    store.commit(stateOf(3, [card("c1", "材料改动")]), "删除卡片");

    vi.mocked(api.saveBoardState).mockRejectedValue(new Error("网络中断"));
    await store.saveNow();
    await flushPromises();

    expect(store.saveStatus).toBe("error");
    expect(store.drafts[KEY], "保存失败期间内存候选必须还在").toBe("候选草稿");
    expect(readCardLocalDraft("c1")?.text, "保存失败期间本机恢复来源必须还在").toBe("候选草稿");
    expect(store.pendingDraftClearKeys, "保存失败期间登记项必须还在").toContain(KEY);

    // 重试成功：才清
    vi.mocked(api.saveBoardState).mockImplementation((async (_id: string, state: BoardState) => ({ ok: true, seq: 4, savedAt: "t", state })) as never);
    await store.saveNow();
    await flushPromises();
    expect(store.drafts[KEY], "重试成功后候选应当被清掉").toBeUndefined();
    expect(store.pendingDraftClearKeys).not.toContain(KEY);
  });

  it("版本守卫：登记之后又输入更新版本 → 成功也不许清后来输入", async () => {
    const store = useInteractiveStore();
    await store.load();
    store.setDraft(KEY, "第一版");
    store.requestDraftClear(KEY);
    store.setDraft(KEY, "登记之后的新输入");
    store.commit(stateOf(3, [card("c1", "材料改动")]), "删除卡片");
    await store.saveNow();
    await flushPromises();
    expect(store.drafts[KEY], "登记之后的新输入被旧登记误清了").toBe("登记之后的新输入");
  });
});
