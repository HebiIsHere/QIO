/**
 * P1 契约 5：提升之后，**关闭态卡片显示的就是正式正文**。
 *
 * 真实缺陷的反面：提升前正文为空、编辑框里有字 —— 用户看到「卡片是空的」，
 * 提交后 QIO 也读到空文本（no_action）。提升后必须只剩一种事实：正文就是那段文字。
 *
 * 标注：【组件】挂载真实 BoardCard；【状态】用真实 store 的提交链路产生提升。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { flushPromises, mount } from "@vue/test-utils";
import { useInteractiveStore } from "../../../stores/interactive";
import * as imApi from "../../../services/interactive";
import BoardCard from "../BoardCard.vue";
import type { BoardCard as BoardCardModel, BoardState, Snapshot } from "../../../interactive/types";

vi.mock("../../../services/interactive", () => ({
  fetchBoardState: vi.fn(),
  fetchVisibleRange: vi.fn(),
  saveBoardState: vi.fn(),
  saveDrafts: vi.fn(),
  checkMaterialImpact: vi.fn(),
  previewMaterialImpact: vi.fn(),
  submitBoard: vi.fn(),
  fetchIntents: vi.fn(),
  approveIntent: vi.fn(),
  rejectIntent: vi.fn(),
  batchDecide: vi.fn(),
  createDemoIntents: vi.fn(),
  advanceIntent: vi.fn(),
  updateIntentPreview: vi.fn(),
}));

const CARD_ID = "c1";
const CARD_KEY = "card:c1";

function cardModel(content: string): BoardCardModel {
  return {
    id: CARD_ID,
    kind: "text",
    content,
    meta: {},
    x: 0,
    y: 0,
    w: 240,
    h: 120,
    checked: true,
    hidden: false,
    folded: false,
    bookmarked: false,
    deleted: false,
    createdAt: "2026-10-11T00:00:00.000Z",
    updatedAt: "2026-10-11T00:00:00.000Z",
  };
}

function snapshotOf(state: BoardState): Snapshot {
  return {
    cards: state.cards,
    groups: state.groups,
    links: state.links,
    selection: state.selection,
    empty: state.cards.length === 0,
  };
}

beforeEach(() => {
  localStorage.clear();
  vi.resetAllMocks();
  setActivePinia(createPinia());
});

describe("P1 契约 5：关闭态显示正式正文", () => {
  it("【组件】提升后卡片正文就是那段文字，编辑框不再另存一份", async () => {
    let seq = 3;
    let state: BoardState = {
      boardId: "board_default",
      seq,
      updatedAt: "2026-10-11T00:00:00.000Z",
      cards: [cardModel("")],
      groups: [],
      links: [],
      // 选中这张卡（工具栏只由选中里的最后一张渲染，与真实路径一致）
      selection: [CARD_ID],
    };
    vi.mocked(imApi.fetchBoardState).mockImplementation(async (): Promise<never> => ({
      board: { id: "board_default", title: "板面" },
      state,
      seq,
      baseline: null,
      submissions: [],
      drafts: { drafts: {}, updatedAt: null },
      pending: { expressions: [] },
    }) as never);
    vi.mocked(imApi.saveBoardState).mockImplementation(async (_boardId: string, next: BoardState): Promise<never> => {
      seq += 1;
      state = { ...next, seq };
      return { ok: true, seq, savedAt: state.updatedAt, state } as never;
    });
    vi.mocked(imApi.saveDrafts).mockResolvedValue({ drafts: {}, updatedAt: null } as never);
    vi.mocked(imApi.submitBoard).mockImplementation(async (): Promise<never> => ({
      status: "succeeded",
      submission: { id: "s1", seq, status: "succeeded", createdAt: "2026-10-11T00:00:02.000Z" },
      before: snapshotOf(state),
      after: snapshotOf(state),
      expressions: [],
      baseline: { updated: true, previousSeq: null, seq },
      delivery: { delivered: true, reason: "", detail: "", status: "accepted", understandingId: "u1" },
      visibleRange: { ...snapshotOf(state), notVisibleCount: 0 },
      checkedCleared: [],
    }) as never);
    vi.mocked(imApi.fetchVisibleRange).mockResolvedValue({ visibleRange: null } as never);
    vi.mocked(imApi.fetchIntents).mockResolvedValue({ intents: [], conflicts: [], batchAvailable: false } as never);
    vi.mocked(imApi.checkMaterialImpact).mockResolvedValue({
      ok: true, checkId: "chk_none", stateVersion: seq, affected: [], impactConfirmationRequired: false,
    } as never);

    const store = useInteractiveStore();
    await store.load();
    await flushPromises();
    store.setDraft(CARD_KEY, "为什么链路不可用？");
    await flushPromises();
    await store.submit();
    await flushPromises();

    const card = store.board?.cards[0] as BoardCardModel;
    const wrapper = mount(BoardCard, {
      props: {
        card,
        // 选中才有「编辑」入口（与用户真实路径一致：点卡片 → 工具栏 → 编辑）
        selected: true,
        highlight: false,
        dragging: false,
        x: 0,
        y: 0,
        groupName: null,
        groups: [],
        toolbarLeft: 0,
        toolbarTop: 120,
        multi: false,
        connecting: false,
      },
      attachTo: document.body,
    });
    await flushPromises();

    // 关闭态：显示正式正文（提升后的那段文字），而且没有任何草稿来源与它不同
    expect(wrapper.find(".content").text()).toContain("为什么链路不可用？");
    expect(store.cardDraftText(CARD_ID), "提升后还存在第二份草稿（刷新就会盖回正文）").toBe("");
    // 打开编辑器：编辑框显示的就是正文那份事实，不再出现「正文为空但编辑框里有字」
    await wrapper.find('[data-im="card-edit"]').trigger("click");
    await flushPromises();
    expect((wrapper.find('[data-im="card-editor"]').element as HTMLTextAreaElement).value).toBe("为什么链路不可用？");
    /**
     * 打开编辑器本身会把当前这一版登记成「未完成输入」（既有语义），这里如实收尾：
     * 不点完成编辑直接取消 → 草稿退回与正文一致，不会形成一份「和正文不一样的旧稿」。
     */
    await wrapper.find("button.btn").trigger("click");
    await flushPromises();
    expect(store.cardDraftText(CARD_ID), "取消后留下了与正文不一致的草稿").toBe("为什么链路不可用？");
    wrapper.unmount();
  });
});
