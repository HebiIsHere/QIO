/**
 * 【final-A3 / 综合 07】草稿清除必须绑定「正式变更真实成功」，不能在点击时先清。
 *
 * 反例（_final-closure-task.md 07 / 验收矩阵 07）：
 *   运行任务依赖的卡片，改正文后点「完成编辑」→ 板面变更进入影响确认（尚未保存）；
 *   旧行为：点击时就 clearDraft（删内存候选 + 写 cleared 本机依据 + 排草稿清除请求），
 *   用户取消影响确认后旧板面回来，新输入连恢复来源都没了。
 *
 * 正确行为（约定 07）：组件点击时只登记 requestDraftClear(key)，不删候选、不写 cleared、
 * 不发草稿清除请求；store 只在承载这次变更的候选被服务器成功接受后按登记版本执行；
 * 等待确认 / 取消 / 保存失败期间候选与本机恢复来源都保留；登记后又有更新版本则作废。
 *
 * 标注：【组件/DOM】真实 BoardCard.vue + 真实 store；影响检查与保存响应为**受控模拟**。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { mount } from "@vue/test-utils";
import BoardCard from "../BoardCard.vue";
import { useInteractiveStore } from "../../../stores/interactive";
import { cardDraftKey, readCardLocalDraft } from "../../../interactive/drafts";
import { updateCard } from "../../../interactive/board";
import { emptyBoardState, type BoardCard as BoardCardType, type BoardState, type Intent } from "../../../interactive/types";
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

const OLD_CONTENT = "旧的正式正文";
const NEW_CONTENT = "用户刚写完的新正文";
const NEWER_CONTENT = "确认期间又补写的一段";

function textCard(content = OLD_CONTENT): BoardCardType {
  return {
    id: "c1",
    kind: "text",
    x: 40,
    y: 40,
    w: 240,
    h: 140,
    content,
    checked: true,
    hidden: false,
    folded: false,
    bookmarked: false,
    deleted: false,
    createdAt: "2026-10-08T00:00:00.000Z",
    updatedAt: "2026-10-08T00:00:00.000Z",
  } as unknown as BoardCardType;
}

function runningIntent(): Intent {
  return {
    id: "i1",
    boardId: "board_default",
    submissionId: null,
    title: "执行中的任务",
    summary: "依赖 c1",
    status: "running",
    preview: { cards: [], groups: [], links: [], reason: "" },
    impact: { materials: ["c1"], consequence: "会让这个任务暂停" },
    dependsOn: [],
    conflictsWith: [],
    conflictKey: "",
    materialRefs: ["c1"],
    progress: { done: 0, total: 1, text: "" },
    reason: "",
    demo: false,
    createdAt: "2026-10-08T00:00:00.000Z",
    updatedAt: "2026-10-08T00:00:00.000Z",
  } as unknown as Intent;
}

function boardState(card: BoardCardType): BoardState {
  return { ...emptyBoardState("board_default"), seq: 1, cards: [card], selection: [card.id] };
}

function boardResponse(card: BoardCardType, draftText: string) {
  return {
    board: { id: "board_default", title: "板面" },
    state: boardState(card),
    seq: 1,
    baseline: null,
    submissions: [],
    drafts: { drafts: { [cardDraftKey(card.id)]: draftText }, updatedAt: "2026-10-08T00:00:00.000Z" },
  } as never;
}

function mountCard(card = textCard()) {
  const store = useInteractiveStore();
  store.board = boardState(card);
  // 有一项执行中的任务 → 保存会先做影响预判
  store.intents = [runningIntent()];
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
  return { wrapper, store, card, key: cardDraftKey(card.id) };
}

/** 按真实链路走一遍「完成编辑」：点按钮 → 把 patch 交给 store.commit（等价 BoardCanvas 的接线）→ 保存 */
async function clickDoneEditing(wrapper: ReturnType<typeof mount>, store: ReturnType<typeof useInteractiveStore>) {
  const done = wrapper.findAll("button").find((b) => b.text() === "完成编辑");
  expect(done, "编辑态里没有「完成编辑」按钮").toBeTruthy();
  await done!.trigger("click");
  await wrapper.vm.$nextTick();
  const emitted = wrapper.emitted("patch") as [string, Partial<BoardCardType>, string][] | undefined;
  expect(emitted && emitted.length, "「完成编辑」没有把正式变更交给板面").toBeTruthy();
  const patch = emitted![emitted!.length - 1];
  store.commit(updateCard(store.board as BoardState, patch[0], patch[1]), patch[2]);
  await store.saveNow();
}

function localRecord(card: BoardCardType) {
  return readCardLocalDraft(card.id);
}

beforeEach(() => {
  localStorage.clear();
  vi.resetAllMocks();
  vi.mocked(imApi.saveDrafts).mockResolvedValue({ drafts: {}, updatedAt: "2026-10-08T00:00:00.000Z" } as never);
  vi.mocked(imApi.fetchVisibleRange).mockResolvedValue({ visibleRange: { cards: [], groups: [], links: [], selection: [], empty: true } } as never);
  vi.mocked(imApi.fetchIntents).mockResolvedValue({ intents: [], conflicts: [], batchAvailable: false, recovery: { paused: [] } } as never);
  vi.mocked(imApi.checkMaterialImpact).mockResolvedValue({
    ok: true,
    checkId: "chk-1",
    stateVersion: 1,
    affected: [{ intentId: "i1", title: "执行中的任务", status: "running", materials: ["c1"], consequence: "会让这个任务暂停并保留进度" }],
    impactConfirmationRequired: true,
  } as never);
  vi.mocked(imApi.saveBoardState).mockImplementation(async (_boardId, state) => ({
    ok: true,
    seq: (state as BoardState).seq + 1,
    savedAt: "2026-10-08T00:01:00.000Z",
    state: state as BoardState,
  }) as never);
  setActivePinia(createPinia());
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.useRealTimers();
  localStorage.clear();
  document.body.innerHTML = "";
});

describe("07 草稿清除绑定正式变更", () => {
  it("【组件/DOM】影响确认等待期间：候选与本机恢复来源都保留，不发草稿清除", async () => {
    const { wrapper, store, card, key } = mountCard();
    await wrapper.find('[data-im="card-edit"]').trigger("click");
    await wrapper.find('[data-im="card-editor"]').setValue(NEW_CONTENT);

    await clickDoneEditing(wrapper, store);

    expect(store.pendingImpact, "没有进入影响确认，本用例前提不成立").toBeTruthy();
    expect(store.hasCardDraft(card.id), "等待确认期间草稿候选已经没了").toBe(true);
    expect(store.drafts[key], "等待确认期间内存候选被清掉").toBe(NEW_CONTENT);
    const record = localRecord(card);
    expect(record, "等待确认期间本机恢复记录不见了").toBeTruthy();
    expect(record!.kind ?? "draft", "等待确认期间恢复来源被写成了「已清除」").not.toBe("cleared");
    expect(record!.text, "本机恢复记录里的正文不是用户最新的输入").toBe(NEW_CONTENT);

    vi.mocked(imApi.saveDrafts).mockClear();
    await store.flushDrafts();
    const calls = vi.mocked(imApi.saveDrafts).mock.calls;
    expect(calls.length, "等待确认期间连一份草稿都没保住").toBeGreaterThan(0);
    for (const call of calls) {
      expect(call[1][key], "板面还没保存，服务器那份草稿就被独立清掉了").toBe(NEW_CONTENT);
    }
  });

  it("【组件/DOM】用户取消影响确认：候选与恢复来源仍在，编辑器能取回新输入", async () => {
    const { wrapper, store, card, key } = mountCard();
    await wrapper.find('[data-im="card-edit"]').trigger("click");
    await wrapper.find('[data-im="card-editor"]').setValue(NEW_CONTENT);
    await clickDoneEditing(wrapper, store);
    expect(store.pendingImpact).toBeTruthy();
    vi.mocked(imApi.saveBoardState).mockClear();

    // 取消：服务器上还是旧板面（卡片正文旧、草稿是用户刚落的那份）
    vi.mocked(imApi.fetchBoardState).mockResolvedValue(boardResponse(textCard(), NEW_CONTENT));
    await store.cancelImpact();
    await wrapper.vm.$nextTick();

    expect(store.pendingImpact, "取消后还停在影响确认里").toBeNull();
    expect(vi.mocked(imApi.saveBoardState).mock.calls.length, "取消不许发出板面保存").toBe(0);
    expect(store.drafts[key], "取消后内存候选没了").toBe(NEW_CONTENT);
    expect(store.hasCardDraft(card.id), "取消后编辑器取不回新输入").toBe(true);
    const record = localRecord(card);
    expect(record, "取消后本机恢复来源没了").toBeTruthy();
    expect(record!.kind ?? "draft", "取消后恢复来源变成了「已清除」").not.toBe("cleared");
    expect(record!.text, "取消后本机恢复记录不是新输入").toBe(NEW_CONTENT);

    // 重新打开编辑器：新输入确实取回得到
    await wrapper.find('[data-im="card-edit"]').trigger("click");
    expect((wrapper.find('[data-im="card-editor"]').element as HTMLTextAreaElement).value).toBe(NEW_CONTENT);
  });

  it("【组件/DOM】确认并保存成功后：才按登记版本清草稿（本机清除依据 + 待同步清除）", async () => {
    const { wrapper, store, card, key } = mountCard();
    await wrapper.find('[data-im="card-edit"]').trigger("click");
    await wrapper.find('[data-im="card-editor"]').setValue(NEW_CONTENT);
    await clickDoneEditing(wrapper, store);
    expect(store.pendingImpact).toBeTruthy();

    await store.confirmImpact();
    await wrapper.vm.$nextTick();

    expect(store.saveStatus, "确认后板面没有保存成功，本用例前提不成立").toBe("saved");
    expect(store.hasCardDraft(card.id), "正式变更保存成功后草稿候选还在").toBe(false);
    expect(store.drafts[key], "正式变更保存成功后内存候选还在").toBeUndefined();
    const record = localRecord(card);
    expect(record, "保存成功后没有留下本机清除依据（重开后旧稿会复活）").toBeTruthy();
    expect(record!.kind, "保存成功后本机记录不是「已清除」").toBe("cleared");

    vi.mocked(imApi.saveDrafts).mockClear();
    await store.flushDrafts();
    const calls = vi.mocked(imApi.saveDrafts).mock.calls;
    expect(calls.length, "没有把这次清除同步给服务器").toBeGreaterThan(0);
    expect(Object.prototype.hasOwnProperty.call(calls[calls.length - 1][1], key), "清除后服务器那份草稿仍会被保留").toBe(false);
  });

  it("【组件/DOM】保存失败：草稿候选与恢复来源都不许清，原因可见", async () => {
    const { wrapper, store, card, key } = mountCard();
    await wrapper.find('[data-im="card-edit"]').trigger("click");
    await wrapper.find('[data-im="card-editor"]').setValue(NEW_CONTENT);
    await clickDoneEditing(wrapper, store);
    expect(store.pendingImpact).toBeTruthy();

    vi.mocked(imApi.saveBoardState).mockRejectedValue(new Error("服务器没有接受这次保存（模拟）"));
    await store.confirmImpact();
    await wrapper.vm.$nextTick();

    expect(store.saveStatus, "保存失败却显示成功").toBe("error");
    expect(store.saveError, "保存失败没有真实原因").toBeTruthy();
    expect(store.drafts[key], "保存失败却把草稿候选清了").toBe(NEW_CONTENT);
    expect(store.hasCardDraft(card.id)).toBe(true);
    const record = localRecord(card);
    expect(record!.kind ?? "draft", "保存失败却写下了「已清除」依据").not.toBe("cleared");
    expect(record!.text).toBe(NEW_CONTENT);
  });

  it("【组件/DOM】登记之后又输入更新版本：保存成功也不许清掉后来输入", async () => {
    const { wrapper, store, card, key } = mountCard();
    await wrapper.find('[data-im="card-edit"]').trigger("click");
    await wrapper.find('[data-im="card-editor"]').setValue(NEW_CONTENT);
    await clickDoneEditing(wrapper, store);
    expect(store.pendingImpact).toBeTruthy();

    // 等待确认期间用户又打开编辑器补写了一段（更新版本）
    await wrapper.find('[data-im="card-edit"]').trigger("click");
    await wrapper.find('[data-im="card-editor"]').setValue(NEWER_CONTENT);
    expect(store.drafts[key]).toBe(NEWER_CONTENT);

    await store.confirmImpact();
    await wrapper.vm.$nextTick();

    expect(store.saveStatus).toBe("saved");
    expect(store.drafts[key], "正式变更成功后把后来输入的更新版本一起清掉了").toBe(NEWER_CONTENT);
    expect(store.hasCardDraft(card.id), "更新版本没有恢复来源").toBe(true);
    const record = localRecord(card);
    expect(record!.kind ?? "draft", "更新版本被写成「已清除」").not.toBe("cleared");
    expect(record!.text).toBe(NEWER_CONTENT);
  });
});
