/**
 * 【final-A3 / 综合 01 组件侧】迟到读取不得把**真实编辑框**回退到旧正文。
 *
 * 反例时序（_final-closure-task.md 01 / 验收矩阵 01）：
 *   1. 用户在卡片编辑框输入新稿（GET 开始前就已输入，或 GET 在飞期间继续输入）；
 *   2. 草稿 PUT 与板面 GET 同时在飞；
 *   3. PUT 先成功（这一版已经是「已保存事实」，本机副本随之清理）；
 *   4. 旧 GET 后返回，正文是服务器旧稿。
 *
 * 正确行为（M1）：任何 GET/PUT 响应落地前，都要与**当前内存候选的版本**比较；
 * 响应读到的版本落后于本地当前候选（含刚保存成功的新版本）时不许整体覆盖。
 * 本文件只断言用户看得见的事实：**真实 textarea 里的文字不许变回旧稿**，
 * 以及内存候选与「取消后重开编辑器」拿到的仍是新稿。
 *
 * 标注：【组件/DOM】挂载真实 BoardCard.vue + 真实 store；【模拟】仅网络响应乱序
 * （受控 promise 决定 PUT/GET 到达顺序），不改任何产品代码。
 * 基线对照：把 store 的「GET 读基线」（savedNow > readSaved）去掉后，
 * 用例 1 会以「expected '服务器上的旧正文' to be 'GET 开始前已经输入的新稿'」失败。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { mount } from "@vue/test-utils";
import BoardCard from "../BoardCard.vue";
import { useInteractiveStore } from "../../../stores/interactive";
import { cardDraftKey } from "../../../interactive/drafts";
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

const SERVER_OLD = "服务器上的旧正文";
const LOCAL_NEW = "GET 开始前已经输入的新稿";
const LOCAL_NEWER = "GET 在飞期间又输入的第二版";

function textCard(content = SERVER_OLD): BoardCardType {
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

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((r) => {
    resolve = r;
  });
  return { promise, resolve };
}

/** GET 返回的板面正文：卡片还是旧正文，草稿也是旧稿 */
function boardResponse(card: BoardCardType, draftText: string): BoardStateResponse {
  return {
    board: { id: "board_default", title: "板面" },
    // 刷新回来的板面仍保留用户当前选中的那张卡（真实服务端会保存 selection），
    // 否则卡片局部工具栏会随选中消失，后续「取消再打开编辑器」就没入口了。
    state: { ...emptyBoardState("board_default"), seq: 1, cards: [card], selection: [card.id] },
    seq: 1,
    baseline: null,
    submissions: [],
    drafts: { drafts: { [cardDraftKey(card.id)]: draftText }, updatedAt: "2026-10-08T00:00:00.000Z" },
  } as unknown as BoardStateResponse;
}

function mountCard() {
  const card = textCard();
  const store = useInteractiveStore();
  store.board = { ...emptyBoardState("board_default"), cards: [card], selection: [card.id] };
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
  return { wrapper, store, card };
}

function editorValue(wrapper: ReturnType<typeof mountCard>["wrapper"]): string {
  const el = wrapper.find('[data-im="card-editor"]').element as HTMLTextAreaElement;
  return el.value;
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

describe("01 迟到读取不得回退真实编辑框", () => {
  it("【组件/DOM】PUT 先成功、旧 GET 后返回：真实 textarea 仍是新稿，取消后重开也还是新稿", async () => {
    const { wrapper, store, card } = mountCard();
    await wrapper.find('[data-im="card-edit"]').trigger("click");
    expect(editorValue(wrapper)).toBe(SERVER_OLD);

    // 1. 新稿在 GET 开始前已经输入（真实编辑框 → onDraftInput → store.setDraft）
    await wrapper.find('[data-im="card-editor"]').setValue(LOCAL_NEW);
    const key = cardDraftKey(card.id);
    expect(store.cardDraftText(card.id)).toBe(LOCAL_NEW);

    // 2. 草稿 PUT 与板面 GET 同时在飞（到达顺序由本用例决定）
    const put = deferred<unknown>();
    const get = deferred<BoardStateResponse>();
    vi.mocked(imApi.saveDrafts).mockReturnValue(put.promise as never);
    vi.mocked(imApi.fetchBoardState).mockReturnValue(get.promise);

    const flushing = store.flushDrafts();
    const refreshing = store.refreshBoardFromServer();

    // 3. PUT 先成功：这一版已经是「已保存事实」，本机恢复副本随之清理
    put.resolve({ drafts: { [key]: LOCAL_NEW }, updatedAt: "2026-10-08T00:00:01.000Z" } as never);
    await flushing;
    expect(store.drafts[key], "PUT 成功后内存候选不该丢").toBe(LOCAL_NEW);

    // 4. 旧 GET 后返回，正文是服务器旧稿
    get.resolve(boardResponse(textCard(), SERVER_OLD));
    await refreshing;
    await wrapper.vm.$nextTick();

    expect(editorValue(wrapper), "迟到的读取把用户真实编辑框里的新稿回退成了旧正文").toBe(LOCAL_NEW);
    expect(store.cardDraftText(card.id), "内存草稿候选被旧响应覆盖").toBe(LOCAL_NEW);

    // 5. 退出编辑再打开：拿到的是新稿候选，不是服务器旧正文
    const cancel = wrapper.findAll("button").find((b) => b.text() === "取消");
    expect(cancel, "编辑态里没有「取消」按钮").toBeTruthy();
    await cancel!.trigger("click");
    await wrapper.vm.$nextTick();
    await wrapper.find('[data-im="card-edit"]').trigger("click");
    await wrapper.vm.$nextTick();
    expect(editorValue(wrapper), "重开编辑器又冒出服务器旧正文").toBe(LOCAL_NEW);
  });

  it("【组件/DOM】旧 GET 先返回、PUT 后成功：真实 textarea 仍是新稿", async () => {
    const { wrapper, store, card } = mountCard();
    await wrapper.find('[data-im="card-edit"]').trigger("click");
    await wrapper.find('[data-im="card-editor"]').setValue(LOCAL_NEW);

    const put = deferred<unknown>();
    const get = deferred<BoardStateResponse>();
    vi.mocked(imApi.saveDrafts).mockReturnValue(put.promise as never);
    vi.mocked(imApi.fetchBoardState).mockReturnValue(get.promise);

    const flushing = store.flushDrafts();
    const refreshing = store.refreshBoardFromServer();

    get.resolve(boardResponse(textCard(), SERVER_OLD));
    await refreshing;
    await wrapper.vm.$nextTick();
    expect(editorValue(wrapper), "旧正文先到就把新稿冲掉了").toBe(LOCAL_NEW);

    put.resolve({ drafts: { [cardDraftKey(card.id)]: LOCAL_NEW }, updatedAt: "2026-10-08T00:00:02.000Z" } as never);
    await flushing;
    await wrapper.vm.$nextTick();
    expect(editorValue(wrapper), "PUT 回执落地后又把编辑框改了").toBe(LOCAL_NEW);
    expect(store.cardDraftText(card.id)).toBe(LOCAL_NEW);
  });

  it("【组件/DOM】GET 在飞期间又输入第二版：旧响应不许回退第二版", async () => {
    const { wrapper, store, card } = mountCard();
    await wrapper.find('[data-im="card-edit"]').trigger("click");
    await wrapper.find('[data-im="card-editor"]').setValue(LOCAL_NEW);

    const put = deferred<unknown>();
    const get = deferred<BoardStateResponse>();
    vi.mocked(imApi.saveDrafts).mockReturnValue(put.promise as never);
    vi.mocked(imApi.fetchBoardState).mockReturnValue(get.promise);

    const flushing = store.flushDrafts();
    const refreshing = store.refreshBoardFromServer();

    // 用户在请求在飞期间又改了（这正是「响应到达时还没保存吗」不足够的场景）
    await wrapper.find('[data-im="card-editor"]').setValue(LOCAL_NEWER);

    get.resolve(boardResponse(textCard(), SERVER_OLD));
    await refreshing;
    await wrapper.vm.$nextTick();
    expect(editorValue(wrapper), "旧响应回退了在飞期间的第二版").toBe(LOCAL_NEWER);

    put.resolve({ drafts: { [cardDraftKey(card.id)]: LOCAL_NEW }, updatedAt: "2026-10-08T00:00:03.000Z" } as never);
    await flushing;
    await wrapper.vm.$nextTick();
    expect(editorValue(wrapper), "旧版本回执把第二版改回去了").toBe(LOCAL_NEWER);
    expect(store.cardDraftText(card.id)).toBe(LOCAL_NEWER);
  });
});
