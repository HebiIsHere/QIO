/**
 * P1：提交时把「未点完成编辑」的草稿提升为正式正文（本轮契约 1–6）。
 *
 * 根因（真实用户库）：board_drafts 里有 card:c_mv4io4hf1uuka="为什么链路不可用？"，
 * 但 board_states 里那张卡 content=""；后端只读 card.content（selectable_cards /
 * materials._inline_entry），于是提交快照里 requirement.text 为空、卡片标签「（无文字）」，
 * 模型输出 no_action —— 用户勾了注释、写了字，QIO 却什么都没看到。
 *
 * 契约（缺一条即未完成）：
 * 1) 提升发生在 submit() 里、**在 saveNow() 之前**：把每张卡片当前未确认草稿正文写进该卡
 *    content 形成候选板面，再走既有保存 → 提交链路；
 * 2) 存在未决草稿冲突 / 正文内容冲突的卡片**不提**，也不许静默丢用户内容，并给出可读原因；
 * 3) 提升后旧草稿不再复活：走既有 requestDraftClear + saveNow 清除语义（刷新不盖回正文）；
 * 4) 用户可见「N 条未点完成编辑的文字已随提交生效」；没有草稿可提升时文案逐字不变；
 * 5) 关闭态显示正式正文（提升后即正文），不出现「正文为空但编辑框里有字」的错位；
 * 6) 不改后端协议、不改 board 提交载荷形状。
 *
 * 标注：【状态】只依赖 store 公开状态与动作；【组件】挂载真实组件；【模拟】受控接口替身。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { flushPromises, mount } from "@vue/test-utils";
import { useInteractiveStore } from "../interactive";
import * as imApi from "../../services/interactive";
import { cardLocalDraftStorageKey, readCardLocalDraft, writeCardLocalDraft } from "../../interactive/drafts";
import SubmitCluster from "../../components/interactive/SubmitCluster.vue";
import type { BoardCard, BoardState, Snapshot } from "../../interactive/types";

vi.mock("../../services/interactive", () => ({
  fetchBoardState: vi.fn(),
  fetchVisibleRange: vi.fn(),
  saveBoardState: vi.fn(),
  saveDrafts: vi.fn(),
  checkMaterialImpact: vi.fn(),
  previewMaterialImpact: vi.fn(),
  submitBoard: vi.fn(),
  fetchAcceptance: vi.fn(),
  fetchIntents: vi.fn(),
  approveIntent: vi.fn(),
  rejectIntent: vi.fn(),
  batchDecide: vi.fn(),
  createDemoIntents: vi.fn(),
  advanceIntent: vi.fn(),
  updateIntentPreview: vi.fn(),
}));

const BOARD_ID = "board_default";
const CARD_KEY = "card:c1";

function card(id: string, content: string, checked = true): BoardCard {
  return {
    id,
    kind: "text",
    content,
    meta: {},
    x: 0,
    y: 0,
    w: 240,
    h: 120,
    checked,
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

function stateOf(seq: number, cards: BoardCard[]): BoardState {
  return {
    boardId: BOARD_ID,
    seq,
    updatedAt: "2026-10-11T00:00:00.000Z",
    cards,
    groups: [],
    links: [],
    selection: [],
  };
}

/**
 * 受控「服务器」：板面写入真的被记住（提升后的正文进快照），草稿按逐键语义保存。
 * 这样 (a) 才能同时断言「保存载荷里的正文」与「提交快照里的卡片」。
 */
function fakeServer(initialCards: BoardCard[], initialDrafts: Record<string, string> = {}) {
  const drafts: Record<string, string> = { ...initialDrafts };
  let seq = 3;
  let state = stateOf(seq, initialCards);
  const view = {
    drafts,
    get state() {
      return state;
    },
  };
  vi.mocked(imApi.fetchBoardState).mockImplementation(async (): Promise<never> => ({
    board: { id: BOARD_ID, title: "板面" },
    state,
    seq,
    baseline: null,
    submissions: [],
    drafts: { drafts: { ...drafts }, updatedAt: "2026-10-11T00:00:00.000Z", rev: 1 },
    pending: { expressions: [] },
  }) as never);
  vi.mocked(imApi.saveBoardState).mockImplementation(async (_boardId: string, next: BoardState): Promise<never> => {
    seq += 1;
    state = { ...next, seq, updatedAt: "2026-10-11T00:00:01.000Z" };
    return { ok: true, seq, savedAt: state.updatedAt, state } as never;
  });
  vi.mocked(imApi.saveDrafts).mockImplementation(async (_boardId: string, patch: Record<string, string>, meta?: { removed?: string[] }): Promise<never> => {
    Object.assign(drafts, patch);
    for (const key of meta?.removed ?? []) delete drafts[key];
    return { drafts: { ...drafts }, updatedAt: "2026-10-11T00:00:01.000Z", cleared: [...(meta?.removed ?? [])], rev: seq + 1 } as never;
  });
  /**
   * 提交：服务端按**已保存的板面**取快照（后端的真实语义）。
   * 所以「提升有没有进 QIO 的视野」= 这次 before 里那张卡的 content。
   */
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
  vi.mocked(imApi.fetchIntents).mockResolvedValue({ intents: [], conflicts: [], batchAvailable: false, recovery: { paused: [] } } as never);
  vi.mocked(imApi.checkMaterialImpact).mockResolvedValue({
    ok: true,
    checkId: "chk_none",
    stateVersion: seq,
    affected: [],
    impactConfirmationRequired: false,
  } as never);
  return view;
}

function savePayloads(): BoardState[] {
  return vi.mocked(imApi.saveBoardState).mock.calls.map((call) => call[1] as BoardState);
}

function lastSavePayload(): BoardState {
  const payloads = savePayloads();
  return payloads[payloads.length - 1];
}

beforeEach(() => {
  localStorage.clear();
  vi.resetAllMocks();
  setActivePinia(createPinia());
});

afterEach(() => {
  localStorage.clear();
});

/* ==========================================================================
 * (a) 有草稿 + 已勾选 → 提交请求里的板面正文是该草稿文字、且该卡出现在提交快照
 * ======================================================================== */
describe("(a) 提交时把未确认草稿提升为正文", () => {
  it("【状态】提升发生在 saveNow 之前：PUT 的正文是草稿文字，提交快照里 QIO 读得到它", async () => {
    const server = fakeServer([card("c1", "")]);
    const store = useInteractiveStore();
    await store.load();
    await flushPromises();

    // 用户在已勾选的注释里写了字，但**没有**点「完成编辑」
    store.setDraft(CARD_KEY, "为什么链路不可用？");
    await flushPromises();
    expect(store.board?.cards[0].content, "前提：提交前正式正文仍是空的").toBe("");

    await store.submit();
    await flushPromises();

    const puts = savePayloads();
    expect(puts.length, "提交必须走既有保存链路（saveNow 的 PUT）").toBeGreaterThan(0);
    expect(
      puts[puts.length - 1].cards.find((item) => item.id === "c1")?.content,
      "PUT 的板面正文不是草稿文字：提升没有发生在 saveNow 之前",
    ).toBe("为什么链路不可用？");
    expect(puts[puts.length - 1].cards.some((item) => item.id === "c1"), "该卡没有出现在保存载荷里").toBe(true);

    // QIO 实际拿到的快照（后端按已保存板面取 selectable_cards）
    const submitted = vi.mocked(imApi.submitBoard).mock.results[0]?.value as Promise<{
      before: Snapshot;
    }>;
    const before = (await submitted).before;
    expect(
      before.cards.find((item) => item.id === "c1")?.content,
      "提交快照里这张卡仍是空正文：QIO 还是看不到用户的字",
    ).toBe("为什么链路不可用？");
    expect(store.promotedDraftsCount, "本次有 1 条未点完成编辑的文字随提交生效").toBe(1);
    expect(server.state.cards[0].content).toBe("为什么链路不可用？");
  });

  it("【状态】未勾选的注释草稿不提升（不在 QIO 可见范围，不偷偷改变它的正文）", async () => {
    fakeServer([card("c1", "", false)]);
    const store = useInteractiveStore();
    await store.load();
    await flushPromises();

    store.setDraft(CARD_KEY, "还没勾选的字");
    await flushPromises();
    await store.submit();
    await flushPromises();

    expect(
      lastSavePayload().cards.find((item) => item.id === "c1")?.content,
      "未勾选的注释被提升进正文：越过了「勾选才允许 QIO 查看」的边界",
    ).toBe("");
    expect(store.cardDraftText("c1"), "草稿必须原样保留，留给用户自己决定").toBe("还没勾选的字");
    expect(store.promotedDraftsCount, "没有提升就不该声称有文字随提交生效").toBe(0);
  });
});

/* ==========================================================================
 * (b) 未决冲突 → 不提升、不提交、给出明确原因
 * ======================================================================== */
describe("(b) 未决冲突的卡片不提", () => {
  it("【状态】未决草稿冲突：正文不动、保存与提交都不发出、原因可读", async () => {
    const server = fakeServer([card("c1", "服务器上的正式正文")], { [CARD_KEY]: "服务器上的草稿" });
    // 旧格式本机记录（与本机已保存副本不一致 → 恢复时登记冲突）
    writeCardLocalDraft("c1", "本机上的版本", { boardId: BOARD_ID, seq: 1 });

    const store = useInteractiveStore();
    await store.load();
    await flushPromises();
    expect(store.draftConflictFor("c1"), "前提：装载后已登记草稿冲突").not.toBeNull();

    // 只看**提交这一刻之后**有没有发生板面写入与提交
    vi.mocked(imApi.saveBoardState).mockClear();
    vi.mocked(imApi.submitBoard).mockClear();

    await store.submit();
    await flushPromises();

    expect(imApi.saveBoardState, "有未决冲突却仍然保存了板面").not.toHaveBeenCalled();
    expect(imApi.submitBoard, "有未决冲突却仍然提交了").not.toHaveBeenCalled();
    expect(store.submitStatus).toBe("failed");
    expect(store.submitError ?? "", "没有说明为什么这次没有生效").toMatch(/冲突/);
    expect(store.submitError ?? "", "没有说明涉事卡片").toContain("c1");
    // 界面读的是 submitError（提交区失败区），这里核对它确实拿到了可读原因
    expect(store.draftsNotPromoted.map((item) => item.cardId), "没有登记「哪张卡这次没提」").toEqual(["c1"]);
    expect(server.state.cards[0].content, "服务器正文被改掉了").toBe("服务器上的正式正文");
    expect(store.board?.cards[0].content, "本页候选正文被改掉了").toBe("服务器上的正式正文");
    expect(store.cardDraftText("c1"), "冲突未决的草稿被静默丢掉了").toBe("本机上的版本");
  });

  it("【状态】未决正文内容冲突：不提也不提交，草稿与两份正文都还在", async () => {
    const store = useInteractiveStore();
    vi.mocked(imApi.fetchBoardState).mockResolvedValue({
      board: { id: BOARD_ID, title: "板面" },
      state: stateOf(3, [card("c1", "正式正文")]),
      seq: 3,
      baseline: null,
      submissions: [],
      drafts: { drafts: {}, updatedAt: null },
      pending: { expressions: [] },
    } as never);
    vi.mocked(imApi.saveDrafts).mockResolvedValue({ drafts: {}, updatedAt: null } as never);
    vi.mocked(imApi.fetchVisibleRange).mockResolvedValue({ visibleRange: null } as never);
    vi.mocked(imApi.fetchIntents).mockResolvedValue({ intents: [], conflicts: [], batchAvailable: false } as never);
    await store.load();
    await flushPromises();

    // 本页改了正文，服务器上同一张卡也被另一页面改了 → 登记内容冲突（F3 的既有门）
    store.commit(stateOf(3, [card("c1", "本页正文")]), "编辑正文");
    vi.mocked(imApi.saveBoardState).mockRejectedValue(
      Object.assign(new Error("板面版本已变化"), { status: 409, payload: { error: "stale_check", reason: "板面版本已变化" } }),
    );
    vi.mocked(imApi.fetchBoardState).mockResolvedValue({
      board: { id: BOARD_ID, title: "板面" },
      state: stateOf(4, [card("c1", "另一页正文")]),
      seq: 4,
      baseline: null,
      submissions: [],
      drafts: { drafts: {}, updatedAt: null },
      pending: { expressions: [] },
    } as never);
    await store.saveNow();
    expect(store.boardContentConflictFor("c1"), "前提：已登记正文内容冲突").not.toBeNull();
    store.setDraft(CARD_KEY, "冲突期间写的字");
    vi.mocked(imApi.saveBoardState).mockClear();
    vi.mocked(imApi.submitBoard).mockClear();
    vi.mocked(imApi.saveBoardState).mockImplementation(async (_boardId: string, next: BoardState): Promise<never> => ({
      ok: true, seq: 5, savedAt: "2026-10-11T00:00:03.000Z", state: { ...next, seq: 5 },
    }) as never);

    await store.submit();
    await flushPromises();

    expect(imApi.saveBoardState, "有未决内容冲突却仍然保存了板面").not.toHaveBeenCalled();
    expect(imApi.submitBoard, "有未决内容冲突却仍然提交了").not.toHaveBeenCalled();
    expect(store.submitStatus).toBe("failed");
    expect(store.board?.cards[0].content, "本页正文被静默覆盖").toBe("本页正文");
    expect(store.cardDraftText("c1"), "冲突期间写的字被丢掉了").toBe("冲突期间写的字");
  });
});

/* ==========================================================================
 * (c) 提升后草稿被登记清除，刷新不复活
 * ======================================================================== */
describe("(c) 提升后的旧草稿不再复活", () => {
  it("【状态】清除登记生效 + 本机依据为 cleared + 重读板面不把草稿盖回正文", async () => {
    fakeServer([card("c1", "")]);
    const store = useInteractiveStore();
    await store.load();
    await flushPromises();
    store.setDraft(CARD_KEY, "随提交生效的字");
    await flushPromises();

    await store.submit();
    await flushPromises();
    expect(store.cardDraftText("c1"), "提升后草稿还留在编辑器来源里：刷新就会把草稿盖回正文").toBe("");
    expect(
      Object.prototype.hasOwnProperty.call(store.drafts, CARD_KEY),
      "提升后内存草稿没有清掉",
    ).toBe(false);
    expect(
      JSON.parse(localStorage.getItem(cardLocalDraftStorageKey("c1")) ?? "{}").kind,
      "本机没有留下 cleared 依据：刷新后这份旧草稿会复活",
    ).toBe("cleared");
    expect(readCardLocalDraft("c1")?.text ?? "", "本机记录里还留着草稿正文").toBe("");

    // 清除必须真的同步给服务器（既有 requestDraftClear → saveNow 清除语义）
    await store.flushDrafts();
    await flushPromises();
    const removals = vi
      .mocked(imApi.saveDrafts)
      .mock.calls.flatMap((call) => (call[2] as { removed?: string[] } | undefined)?.removed ?? []);
    expect(removals, "旧草稿没有被登记清除给服务器").toContain(CARD_KEY);

    // 刷新：服务器上没有这份草稿、正文是提升后的文字 → 不出现「正文为空但编辑框里有字」
    await store.refreshBoardFromServer();
    await flushPromises();
    expect(store.board?.cards[0].content, "重读后正文被回退").toBe("随提交生效的字");
    expect(store.cardDraftText("c1"), "重读后草稿复活了").toBe("");
  });
});

/* ==========================================================================
 * (c2) 提交结果未知：提升带来的草稿清除不许把用户内容变成「说不清的消失」
 * ======================================================================== */
describe("(c2) 没有拿到回执（结果未知）", () => {
  it("【状态】不声称「已随提交生效」，且未点完成编辑的文字仍能被找到", async () => {
    fakeServer([card("c1", "")]);
    const store = useInteractiveStore();
    await store.load();
    await flushPromises();
    store.setDraft(CARD_KEY, "还没提交的文字");
    await flushPromises();

    vi.mocked(imApi.submitBoard).mockRejectedValue(new Error("网络中断，没有收到回执"));
    // 按身份的精确查证：服务端没有这条记录
    vi.mocked(imApi.fetchAcceptance).mockResolvedValue({ acceptance: null, reason: "没有记录" } as never);

    await store.submit();
    await flushPromises();

    expect(store.submitStatus, "查不到不是失败结论").toBe("unknown");
    expect(store.promotedDraftsCount, "结果未知却声称已随提交生效").toBe(0);
    // 提升后的正文已经保存（内容不丢），同时草稿候选放回去：用户还能找到并改这段字
    expect(store.board?.cards[0].content, "提升后的正文在结果未知时被丢了").toBe("还没提交的文字");
    expect(store.cardDraftText("c1"), "结果未知时草稿候选没有放回去").toBe("还没提交的文字");
  });
});

/* ==========================================================================
 * (d) 无草稿时与今天逐字一致（回归）
 * ======================================================================== */
describe("(d) 没有草稿可提升时行为不变", () => {
  it("【状态】板面原样保存并提交，不声称有任何文字随提交生效", async () => {
    fakeServer([card("c1", "正式正文")]);
    const store = useInteractiveStore();
    await store.load();
    await flushPromises();

    await store.submit();
    await flushPromises();

    expect(lastSavePayload().cards.find((item) => item.id === "c1")?.content).toBe("正式正文");
    expect(imApi.submitBoard).toHaveBeenCalledTimes(1);
    expect(store.promotedDraftsCount, "没有草稿却声称有文字随提交生效").toBe(0);
    expect(store.promotedDrafts).toEqual([]);
  });
});

/* ==========================================================================
 * (4) 组件级：用户可见文案
 * ======================================================================== */
describe("(4) 提交区的用户可见说明", () => {
  async function mountCluster(): Promise<ReturnType<typeof mount>> {
    const wrapper = mount(SubmitCluster, { attachTo: document.body });
    await flushPromises();
    return wrapper;
  }

  it("【组件】有草稿随提交生效时给出条数与解释；没有草稿时这句不出现", async () => {
    fakeServer([card("c1", "")]);
    const store = useInteractiveStore();
    await store.load();
    await flushPromises();
    store.setDraft(CARD_KEY, "为什么链路不可用？");
    await flushPromises();

    const wrapper = await mountCluster();
    // 提交前不该提前声称「已随提交生效」
    expect(wrapper.find('[data-im="submit-draft-promoted"]').exists()).toBe(false);

    await wrapper.find('[data-im="submit"]').trigger("click");
    await flushPromises();

    const promoted = wrapper.find('[data-im="submit-draft-promoted"]');
    expect(promoted.exists(), "提交后没有说明有文字随提交生效：用户会以为自己的输入没了").toBe(true);
    expect(promoted.text(), "没有说清条数").toContain("1");
    expect(promoted.text()).toContain("未点完成编辑");
    expect(promoted.text()).toMatch(/已(随|在)本次提交生效/);
    wrapper.unmount();
  });

  it("【组件】没有草稿可提升时，提交区文案与今天逐字一致", async () => {
    fakeServer([card("c1", "正式正文")]);
    const store = useInteractiveStore();
    await store.load();
    await flushPromises();

    const wrapper = await mountCluster();
    // 提交前：没有任何「提升」字样
    expect(wrapper.find('[data-im="submit-draft-promoted"]').exists()).toBe(false);
    expect(wrapper.find('[data-im="submit-draft-not-promoted"]').exists()).toBe(false);

    await wrapper.find('[data-im="submit"]').trigger("click");
    await flushPromises();

    expect(store.promotedDraftsCount, "没有草稿就不该有任何提升记录").toBe(0);
    expect(wrapper.find('[data-im="submit-draft-promoted"]').exists()).toBe(false);
    expect(wrapper.find('[data-im="submit-draft-not-promoted"]').exists()).toBe(false);
    // 提交区里所有既有可见文案逐字保留（提交结果本身照常变化，不在比较范围内）
    expect(wrapper.find('[data-im="visible-range"]').text()).toBe("本次可见：材料 0 项 · 注释 1 条");
    expect(wrapper.find('[data-im="submit-cluster"]').text()).toContain("已提交");
    expect(wrapper.find('[data-im="submit"]').text()).toBe("提交给 QIO");
    wrapper.unmount();
  });
});
