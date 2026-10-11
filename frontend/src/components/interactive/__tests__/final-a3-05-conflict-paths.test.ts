/**
 * 【final-A3 / 综合 05】未决草稿冲突：改一个字不许自动解除，四条路径分别验。
 *
 * 反例（_final-closure-task.md 05 / 验收矩阵 05）：
 *   无法判定新旧的草稿冲突已登记 → 用户在冲突卡片上改一个字 →
 *   旧行为：冲突被无条件清掉，本机新文字被当成正常候选排保存（「输入就是接管」）。
 *
 * 正确行为：用户没点选择按钮之前，改字只更新**本机候选**；两份来源与可操作入口都保留，
 * 也不按本机版本覆盖服务器那份；用户仍要明确选择。
 * 四条路径分别验：选本机 / 选服务器 / 继续编辑 / 取消（关闭编辑器不替用户做选择）。
 *
 * 标注：【组件/DOM】挂载真实 BoardCard.vue + CardDraftHint + 真实 store；
 * 冲突按真实路径产生（本机恢复记录 vs 服务器草稿），不用内部字段直接造。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { mount } from "@vue/test-utils";
import BoardCard from "../BoardCard.vue";
import { useInteractiveStore } from "../../../stores/interactive";
import { cardDraftKey, readCardLocalDraft, writeCardLocalDraft } from "../../../interactive/drafts";
import { emptyBoardState, type BoardCard as BoardCardType, type BoardStateResponse } from "../../../interactive/types";
import * as imApi from "../../../services/interactive";

vi.mock("../../../services/interactive", () => ({
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

/** 本机那份候选（用户上次没成功同步的编辑） */
const LOCAL = "本机那份候选";
/** 服务器那份草稿 */
const SERVER = "服务器那份草稿";
const LOCAL_EDITED = "本机那份候选，用户又改了一个字";
const LOCAL_EDITED_2 = "本机那份候选，用户继续改到第二版";
const CARD_CONTENT = "卡片正式正文";

function textCard(): BoardCardType {
  return {
    id: "c1",
    kind: "text",
    x: 40,
    y: 40,
    w: 240,
    h: 140,
    content: CARD_CONTENT,
    checked: true,
    hidden: false,
    folded: false,
    bookmarked: false,
    deleted: false,
    createdAt: "2026-10-08T00:00:00.000Z",
    updatedAt: "2026-10-08T00:00:00.000Z",
  } as unknown as BoardCardType;
}

function boardResponse(card: BoardCardType, draftText: string): BoardStateResponse {
  return {
    board: { id: "board_default", title: "板面" },
    state: { ...emptyBoardState("board_default"), seq: 1, cards: [card], selection: [card.id] },
    seq: 1,
    baseline: null,
    submissions: [],
    drafts: { drafts: { [cardDraftKey(card.id)]: draftText }, updatedAt: "2026-10-08T00:00:00.000Z" },
  } as unknown as BoardStateResponse;
}

function editorValue(wrapper: ReturnType<typeof mount>): string {
  return (wrapper.find('[data-im="card-editor"]').element as HTMLTextAreaElement).value;
}

function cancelButton(wrapper: ReturnType<typeof mount>) {
  return wrapper.findAll("button").find((b) => b.text() === "取消");
}

/**
 * 按真实路径造出未决冲突：本机恢复记录与服务器草稿正文不同 → restoreLocalCardDrafts
 * 登记冲突，两份都保留（内存草稿先显示本机候选）。
 */
async function seedConflict() {
  const card = textCard();
  const store = useInteractiveStore();
  store.board = { ...emptyBoardState("board_default"), cards: [card], selection: [card.id] };
  writeCardLocalDraft(card.id, LOCAL, { boardId: "board_default", seq: 1 });
  const wrapper = mount(BoardCard, {
    attachTo: document.body,
    props: {
      card,
      selected: true,
      highlight: false,
      dragging: false,
      x: 40,
      y: 40,
      groupName: null,
      groups: [],
      toolbarLeft: 40,
      toolbarTop: 200,
      multi: false,
      connecting: false,
    },
  });
  vi.mocked(imApi.fetchBoardState).mockResolvedValue(boardResponse(card, SERVER));
  await store.refreshBoardFromServer();
  await wrapper.vm.$nextTick();
  return { wrapper, store, card, key: cardDraftKey(card.id) };
}

beforeEach(() => {
  localStorage.clear();
  vi.resetAllMocks();
  vi.mocked(imApi.saveDrafts).mockResolvedValue({ drafts: {}, updatedAt: "2026-10-08T00:00:00.000Z" } as never);
  setActivePinia(createPinia());
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.useRealTimers();
  localStorage.clear();
  document.body.innerHTML = "";
});

describe("05 未决冲突的组件行为", () => {
  it("【组件/DOM】改一个字不清另一份候选、不按本机版本覆盖服务器", async () => {
    const { wrapper, store, card, key } = await seedConflict();

    // 冲突已登记：关闭态卡片上就能看到两份来源与明确选择入口（打开编辑器不是选择）
    expect(store.draftConflictFor(card.id), "基线路径没造出冲突，本用例无效").toEqual({ local: LOCAL, server: SERVER });
    expect(wrapper.find('[data-im="card-draft-conflict"]').exists()).toBe(true);
    expect(wrapper.find('[data-im="card-draft-keep-local"]').exists()).toBe(true);
    expect(wrapper.find('[data-im="card-draft-keep-server"]').exists()).toBe(true);

    await wrapper.find('[data-im="card-edit"]').trigger("click");
    expect(editorValue(wrapper), "打开编辑器应显示本机候选").toBe(LOCAL);

    // 用户没点选择按钮，只在本机候选上改了一个字
    await wrapper.find('[data-im="card-editor"]').setValue(LOCAL_EDITED);
    await wrapper.vm.$nextTick();

    expect(store.draftConflictFor(card.id), "改一个字就把未决冲突消掉了").toEqual({
      local: LOCAL_EDITED,
      server: SERVER,
    });
    expect(wrapper.find('[data-im="card-draft-conflict"]').exists(), "冲突说明不见了").toBe(true);
    expect(wrapper.find('[data-im="card-draft-keep-local"]').exists(), "本机来源的入口不见了").toBe(true);
    expect(wrapper.find('[data-im="card-draft-keep-server"]').exists(), "服务器来源的入口不见了").toBe(true);
    expect(editorValue(wrapper)).toBe(LOCAL_EDITED);

    // 未决期间写回服务器的必须仍是服务器那份，不能按本机版本覆盖
    vi.mocked(imApi.saveDrafts).mockClear();
    await store.flushDrafts();
    const calls = vi.mocked(imApi.saveDrafts).mock.calls;
    expect(calls.length, "未决冲突期间根本没有写回服务器，无法判断有没有覆盖").toBeGreaterThan(0);
    expect(calls[calls.length - 1][1][key], "未决冲突期间把本机候选写上了服务器").toBe(SERVER);
  });

  it("【组件/DOM】选本机：本机候选成为草稿、冲突消失、编辑框用本机那份", async () => {
    const { wrapper, store, card, key } = await seedConflict();

    await wrapper.find('[data-im="card-draft-keep-local"]').trigger("click");
    await wrapper.vm.$nextTick();

    expect(store.draftConflictFor(card.id), "选了本机冲突还在").toBeNull();
    expect(store.cardDraftText(card.id), "选本机后草稿不是本机那份").toBe(LOCAL);
    expect(wrapper.find('[data-im="card-draft-conflict"]').exists()).toBe(false);

    await wrapper.find('[data-im="card-edit"]').trigger("click");
    expect(editorValue(wrapper)).toBe(LOCAL);

    vi.mocked(imApi.saveDrafts).mockClear();
    await store.flushDrafts();
    const calls = vi.mocked(imApi.saveDrafts).mock.calls;
    expect(calls.length).toBeGreaterThan(0);
    expect(calls[calls.length - 1][1][key], "选了本机却没把本机那份写回服务器").toBe(LOCAL);
  });

  it("【组件/DOM】选服务器：草稿与编辑框都用服务器那份，本机候选按已确认版本清掉", async () => {
    const { wrapper, store, card } = await seedConflict();

    // 编辑器先打开（这时正文是本机候选），再明确选服务器
    await wrapper.find('[data-im="card-edit"]').trigger("click");
    expect(editorValue(wrapper)).toBe(LOCAL);
    await wrapper.find('[data-im="card-draft-keep-server"]').trigger("click");
    await wrapper.vm.$nextTick();

    expect(store.draftConflictFor(card.id)).toBeNull();
    expect(store.cardDraftText(card.id), "选服务器后草稿不是服务器那份").toBe(SERVER);
    expect(editorValue(wrapper), "选服务器后真实编辑框没有跟随").toBe(SERVER);
    expect(wrapper.find('[data-im="card-draft-conflict"]').exists()).toBe(false);
    expect(readCardLocalDraft(card.id), "选了服务器，本机那份候选仍留着（以后会再冒出来）").toBeNull();
  });

  it("【组件/DOM】继续编辑与取消：不选就不替用户做决定，两份来源与入口一直在", async () => {
    const { wrapper, store, card } = await seedConflict();

    await wrapper.find('[data-im="card-edit"]').trigger("click");
    const editor = wrapper.find('[data-im="card-editor"]');
    await editor.setValue(LOCAL_EDITED);
    await editor.setValue(LOCAL_EDITED_2);
    await wrapper.vm.$nextTick();

    // 继续编辑：冲突仍在，本机候选跟着最新输入走，服务器候选保持原样
    expect(store.draftConflictFor(card.id)).toEqual({ local: LOCAL_EDITED_2, server: SERVER });

    // 取消：关闭编辑器不等于选择，冲突与两份候选都保留
    const cancel = cancelButton(wrapper);
    expect(cancel, "编辑态里没有「取消」按钮").toBeTruthy();
    await cancel!.trigger("click");
    await wrapper.vm.$nextTick();

    expect(wrapper.find('[data-im="card-draft-conflict"]').exists(), "取消后冲突说明消失").toBe(true);
    expect(wrapper.find('[data-im="card-draft-keep-server"]').exists()).toBe(true);
    expect(store.draftConflictFor(card.id), "取消把未决冲突一并处理了").toEqual({
      local: LOCAL_EDITED_2,
      server: SERVER,
    });

    // 之后仍能明确选服务器那份：拿到的必须是真正的服务器正文
    await wrapper.find('[data-im="card-draft-keep-server"]').trigger("click");
    await wrapper.vm.$nextTick();
    expect(store.cardDraftText(card.id), "本机后续输入覆盖了服务器那份候选").toBe(SERVER);
    await store.flushDrafts();
  });
});
