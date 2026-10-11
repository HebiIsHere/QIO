/**
 * 收尾轮 07（Lead 侧证据）：删除卡片触发的草稿清除必须绑定「正式变更成功」。
 *
 * - 登记后、板面变更还没保存成功（等待影响确认 / 保存失败 / 取消）时：
 *   内存草稿与本机恢复来源都必须留着；
 * - 只有保存回执对应当前候选时才按登记版本真正清除；
 * - 登记之后用户又输入更新版本：该次登记作废，绝不误清后来的输入。
 *
 * 标注：【组件/DOM】挂载真实 BoardCanvas + 真实 store（保存响应受控）；
 *      【会话层】store.requestDraftClear 的登记与消化。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { flushPromises, mount } from "@vue/test-utils";
import BoardCanvas from "../../components/interactive/BoardCanvas.vue";
import BoardCard from "../../components/interactive/BoardCard.vue";
import { useInteractiveStore } from "../../stores/interactive";
import * as api from "../../services/interactive";
import * as board from "../board";
import { writeCardLocalDraft } from "../drafts";
import type { BoardState } from "../types";

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

beforeEach(() => {
  localStorage.clear();
  setActivePinia(createPinia());
  vi.mocked(api.saveBoardState).mockImplementation((async (_id: string, state: BoardState) => ({
    ok: true,
    seq: 9,
    savedAt: "t",
    state,
  })) as never);
  vi.mocked(api.saveDrafts).mockResolvedValue({ drafts: {}, updatedAt: "t" } as never);
  vi.mocked(api.fetchVisibleRange).mockResolvedValue({ visibleRange: { cards: [], groups: [], links: [], selection: [], empty: true } } as never);
  vi.mocked(api.fetchIntents).mockResolvedValue({ intents: [], conflicts: [], batchAvailable: false, recovery: { paused: [] } } as never);
  vi.mocked(api.fetchBoardState).mockResolvedValue({
    board: { id: "board_default", title: "默认板面" },
    state: { boardId: "board_default", seq: 3, updatedAt: "t", cards: [], groups: [], links: [], selection: [] },
    seq: 3,
    baseline: null,
    submissions: [],
    drafts: { drafts: {}, updatedAt: null },
  } as never);
});

afterEach(() => {
  localStorage.clear();
});

describe("07 删除卡片的草稿清除绑定正式变更", () => {
  it("【会话层】登记后未保存成功不清；保存成功后才按登记版本清", async () => {
    const store = useInteractiveStore();
    await store.load();
    const key = "card:c1";
    store.setDraft(key, "要删掉的卡片草稿");
    writeCardLocalDraft("c1", "要删掉的卡片草稿", { boardId: "board_default", seq: 1 });
    store.requestDraftClear(key);
    // 只是登记：候选与恢复来源都还在
    expect(store.pendingDraftClearKeys).toContain(key);
    expect(store.drafts[key]).toBe("要删掉的卡片草稿");
    expect(store.hasCardDraft("c1")).toBe(true);

    // 正式变更落地：保存成功 → 才真正清除
    store.commit({ ...store.board!, seq: 3, cards: [], groups: [], links: [], selection: [] } as BoardState, "删除卡片");
    await store.saveNow();
    await flushPromises();
    expect(store.pendingDraftClearKeys).not.toContain(key);
    expect(store.drafts[key]).toBeUndefined();
    expect(store.hasCardDraft("c1")).toBe(false);
  });

  it("【会话层】登记之后用户又输入更新版本：保存成功也不许清掉后来输入", async () => {
    const store = useInteractiveStore();
    await store.load();
    const key = "card:c1";
    store.setDraft(key, "第一版");
    store.requestDraftClear(key);
    store.setDraft(key, "登记之后的新输入");
    store.commit({ ...store.board!, seq: 3, cards: [], groups: [], links: [], selection: [] } as BoardState, "删除卡片");
    await store.saveNow();
    await flushPromises();
    expect(store.drafts[key]).toBe("登记之后的新输入");
  });

  it("【组件/DOM】点删除卡片：草稿候选先保留（不提前清）", async () => {
    const store = useInteractiveStore();
    store.board = board.normalizeState(
      board.addCard(board.emptyState("board_default"), { kind: "text", content: "材料" }),
    ) as BoardState;
    const cardId = store.board.cards[0].id;
    store.setDraft("card:" + cardId, "编辑器里的草稿");
    const wrapper = mount(BoardCanvas, { attachTo: document.body });
    await wrapper.vm.$nextTick();
    // 卡片的删除入口在卡片自己的工具栏里（工具栏只在悬停/选中时可见），
    // 组件级这里直接触发卡片抛出的 remove 事件 —— 与用户点「删除」走同一条处理路径。
    const card = wrapper.findComponent(BoardCard);
    expect(card.exists()).toBe(true);
    card.vm.$emit("remove", cardId);
    await wrapper.vm.$nextTick();
    // 登记了，但候选还在（保存还没成功）
    expect(store.pendingDraftClearKeys).toContain("card:" + cardId);
    expect(store.drafts["card:" + cardId]).toBe("编辑器里的草稿");
    wrapper.unmount();
  });
});
