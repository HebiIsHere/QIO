/**
 * #46 第七轮回归（任务书二.4 / 二.5）：影响预判乱序 + 确认结果不串板。
 *
 * 二.4（预判乱序）：B 的影响预判请求 1 挂起，候选变更后请求 2 也发出；
 *   以三种顺序返回（旧成功先到 / 新成功先到 / 旧先失败），断言：
 *   - pendingImpact 始终对应**最新候选**（boardId / checkId / stateVersion / candidateKey）；
 *   - 旧响应不形成第二份确认、不触发额外预判、不触发额外 PUT；
 *   - 点 confirmImpact 后 saveBoardState 的 boardId / 候选 / checkId / stateVersion 与请求 2 一致。
 *   另测 409 恢复中的重新预判：旧响应迟到不许清理新恢复计划（否则保存不会继续）。
 *
 * 二.5（确认结果只在 B 展示）：B 有影响确认并点击确认后切到 A，让 B 的保存失败回执到达；
 *   - store 层：A 的 saveStatus / saveError / pendingImpact / 卡片都不受 B 的失败影响；
 *   - 组件层：挂载 ImpactConfirmLayer，A 上渲染不出 B 的失败文案与重试按钮；切回 B 后可见，
 *     点重试只向 B 发请求。
 *
 * 标注：【状态/单元】真实 store；【模拟】仅网络响应的到达顺序与延迟（受控 promise）。
 * 只依赖 store 公开 API 与组件公开的 data-im 钩子。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { flushPromises, mount, type VueWrapper } from "@vue/test-utils";
import { nextTick } from "vue";
import { useInteractiveStore } from "../interactive";
import * as api from "../../services/interactive";
import { addCard } from "../../interactive/board";
import type { BoardState } from "../../interactive/types";
import ImpactConfirmLayer from "../../components/interactive/v2/ImpactConfirmLayer.vue";

vi.mock("../../services/interactive", () => ({
  fetchBoardState: vi.fn(),
  fetchBoards: vi.fn(),
  saveBoardState: vi.fn(),
  saveDrafts: vi.fn(),
  fetchVisibleRange: vi.fn(),
  previewMaterialImpact: vi.fn(),
  checkMaterialImpact: vi.fn(),
  submitBoard: vi.fn(),
  fetchIntents: vi.fn(),
  fetchAcceptance: vi.fn(),
  fetchUnderstanding: vi.fn(),
  retryUnderstanding: vi.fn(),
  fetchExecutions: vi.fn(),
  fetchIntentViews: vi.fn(),
  createIntents: vi.fn(),
}));

const NOW = "2026-11-15T00:00:00Z";
const BOARD_A = "board_a";
const BOARD_B = "board_b";
const TASK_ID = "task_impact_1";
const AFFECTED_TASK = {
  intentId: TASK_ID,
  title: "任务-1",
  materials: ["card_b"],
  consequence: "暂停并保留进度",
};

function textCard(id: string, content: string): BoardState["cards"][number] {
  return {
    id,
    kind: "text",
    content,
    meta: {},
    x: 0,
    y: 0,
    w: 200,
    h: 120,
    checked: false,
    hidden: false,
    folded: false,
    bookmarked: false,
    deleted: false,
    createdAt: NOW,
    updatedAt: NOW,
  } as BoardState["cards"][number];
}

function boardState(boardId: string, cards: BoardState["cards"], seq = 3): BoardState {
  return { boardId, seq, updatedAt: NOW, cards, groups: [], links: [], selection: [] };
}

function boardPayload(state: BoardState) {
  return {
    board: { id: state.boardId, title: state.boardId },
    state,
    seq: state.seq ?? 3,
    baseline: null,
    submissions: [],
    drafts: { drafts: {}, updatedAt: null },
  };
}

function deferred<T = never>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

function apiError(status: number, message: string, payload: Record<string, unknown> = {}): Error {
  return Object.assign(new Error(message), { status, payload });
}

function runningIntent(boardId: string) {
  return {
    id: TASK_ID,
    boardId,
    submissionId: null,
    title: "任务-1",
    summary: "",
    status: "running",
    preview: { kind: "task" },
    impact: { materials: ["card_b"], consequence: "暂停并保留进度" },
    dependsOn: [],
    conflictsWith: [],
    conflictKey: "",
    materialRefs: ["card_b"],
    progress: { done: 0, total: 1, text: "" },
    reason: "",
    demo: true,
    createdAt: NOW,
    updatedAt: NOW,
  };
}

function affectedCheck(checkId: string, stateVersion: number) {
  return {
    ok: true,
    checkId,
    stateVersion,
    affected: [AFFECTED_TASK],
    impactConfirmationRequired: true,
  } as never;
}

/** 不需要影响确认：没有 checkId，store 才会按「可以继续保存」处理。 */
function cleanCheck(stateVersion: number) {
  return { ok: true, stateVersion, affected: [], impactConfirmationRequired: false } as never;
}

function liveCards(store: ReturnType<typeof useInteractiveStore>) {
  return (store.board?.cards ?? []).filter((card) => !card.deleted);
}

/** 每次影响预判的「目标板面 + 依据版本 + 候选」。 */
function impactChecks() {
  return vi.mocked(api.checkMaterialImpact).mock.calls.map((call) => {
    const candidate = call[2] as BoardState;
    return {
      boardId: String(call[0]),
      stateVersion: Number(call[1]),
      candidate,
      cardIds: (candidate?.cards ?? []).map((card) => card.id),
    };
  });
}

/** 每次 PUT 的「目标板面 + 候选 + 确认句柄」。 */
function puts() {
  return vi.mocked(api.saveBoardState).mock.calls.map((call) => {
    const candidate = call[1] as BoardState;
    return {
      boardId: String(call[0]),
      state: candidate,
      cardIds: (candidate?.cards ?? []).map((card) => card.id),
      confirm: call[3] as { checkId?: string; stateVersion?: number } | undefined,
    };
  });
}

/** 与 store 内部 candidateComparable 同口径（只比会进 PUT 正文的字段）。 */
function comparable(state: BoardState): string {
  return JSON.stringify({
    boardId: state.boardId ?? "",
    seq: state.seq ?? 0,
    cards: state.cards ?? [],
    groups: state.groups ?? [],
    links: state.links ?? [],
    selection: state.selection ?? [],
  });
}

async function settle(times = 8): Promise<void> {
  for (let i = 0; i < times; i += 1) await flushPromises();
}

/** 等防抖保存真的发出（store 的防抖是 450ms）。 */
async function waitForDebouncedSave(): Promise<void> {
  await new Promise((resolve) => setTimeout(resolve, 600));
  await settle();
}

let store: ReturnType<typeof useInteractiveStore>;
let wrappers: VueWrapper[] = [];

function mountLayer(pinia: ReturnType<typeof createPinia>): VueWrapper {
  const wrapper = mount(ImpactConfirmLayer, {
    global: { plugins: [pinia] },
    attachTo: document.body,
  }) as VueWrapper;
  wrappers.push(wrapper);
  return wrapper;
}

beforeEach(() => {
  localStorage.clear();
  vi.resetAllMocks();
  setActivePinia(createPinia());
  store = useInteractiveStore();
  vi.mocked(api.fetchBoards).mockResolvedValue({
    boards: [
      { id: BOARD_A, title: "板面 A" },
      { id: BOARD_B, title: "板面 B" },
    ],
  } as never);
  vi.mocked(api.fetchBoardState).mockImplementation((async (id: string) =>
    boardPayload(
      id === BOARD_A
        ? boardState(BOARD_A, [textCard("card_a", "A 的内容")], 3)
        : boardState(BOARD_B, [textCard("card_b", "B 的内容")], 7),
    )) as never);
  vi.mocked(api.saveBoardState).mockImplementation((async (id: string, candidate: BoardState) => ({
    ok: true,
    seq: Number(candidate?.seq ?? 3) + 1,
    savedAt: NOW,
    state: { ...candidate, boardId: id, seq: Number(candidate?.seq ?? 3) + 1 },
  })) as never);
  vi.mocked(api.saveDrafts).mockResolvedValue({ drafts: {}, updatedAt: NOW } as never);
  vi.mocked(api.fetchVisibleRange).mockResolvedValue({
    visibleRange: { cards: [], groups: [], links: [], selection: [], empty: true, notVisibleCount: 0 },
  } as never);
  vi.mocked(api.fetchIntents).mockImplementation((async (id: string) => ({
    intents: id === BOARD_B ? [runningIntent(id)] : [],
    conflicts: [],
    batchAvailable: false,
    recovery: { paused: [] },
  })) as never);
  vi.mocked(api.fetchIntentViews).mockResolvedValue({ intents: [], source: "service" } as never);
  vi.mocked(api.fetchExecutions).mockResolvedValue({ executions: [] } as never);
  vi.mocked(api.fetchUnderstanding).mockResolvedValue({ understandings: [] } as never);
  vi.mocked(api.fetchAcceptance).mockResolvedValue({ acceptance: null } as never);
  vi.mocked(api.checkMaterialImpact).mockResolvedValue(cleanCheck(7) as never);
  vi.mocked(api.previewMaterialImpact).mockResolvedValue({ affected: [] } as never);
  vi.mocked(api.submitBoard).mockResolvedValue({ status: "empty" } as never);
});

afterEach(async () => {
  for (const wrapper of wrappers) {
    try {
      wrapper.unmount();
    } catch {
      // 已卸载
    }
  }
  wrappers = [];
  document.body.innerHTML = "";
  // 排空上一用例留下的防抖保存（450ms 真实计时器），避免污染下一个用例的请求记录
  await new Promise((resolve) => setTimeout(resolve, 700));
  await settle();
  localStorage.clear();
});

describe("二.4 影响预判乱序：确认始终对应最新候选", () => {
  /**
   * 前提装置：B 有执行中任务；先发一次预判（旧候选，挂起），候选变更后再发一次（新候选，挂起）。
   * 返回两个受控 promise，由用例决定返回顺序。
   */
  async function armTwoChecks() {
    await store.selectBoard(BOARD_A);
    await store.selectBoard(BOARD_B);
    const first = deferred<never>();
    const second = deferred<never>();
    vi.mocked(api.checkMaterialImpact)
      .mockReturnValueOnce(first.promise as never)
      .mockReturnValueOnce(second.promise as never);

    store.commit(addCard(store.board!, { kind: "text", content: "第一版" }), "新增卡片");
    await waitForDebouncedSave();
    expect(impactChecks().length, "前提：旧候选的预判已经发出").toBe(1);
    expect(impactChecks()[0].boardId, "预判没有发往 B").toBe(BOARD_B);

    store.commit(addCard(store.board!, { kind: "text", content: "第二版" }), "新增卡片");
    await waitForDebouncedSave();
    expect(impactChecks().length, "前提：候选变更后发出了第二次预判").toBe(2);
    expect(impactChecks()[1].boardId, "第二次预判没有发往 B").toBe(BOARD_B);
    expect(impactChecks()[1].cardIds.length, "第二次预判用的不是最新候选").toBe(
      impactChecks()[0].cardIds.length + 1,
    );
    return { first, second };
  }

  /** 最新候选形成唯一一份确认；确认后只发一次 B 的 PUT，且带最新 checkId / stateVersion。 */
  async function expectLatestConfirmAndSave() {
    const latestCandidate = impactChecks()[1].candidate;
    const pending = store.pendingImpact;
    expect(pending, "最新候选没有形成确认").not.toBeNull();
    expect(pending!.boardId, "确认不属于 B").toBe(BOARD_B);
    expect(pending!.checkId, "确认不是最新那次预判的").toBe("chk_new");
    expect(pending!.stateVersion, "确认没有使用最新预判的服务端版本").toBe(8);
    expect(pending!.candidateKey, "确认对应的不是最新候选正文").toBe(comparable(latestCandidate));
    expect(puts().length, "预判阶段就发出了 PUT").toBe(0);
    expect(impactChecks().length, "旧响应又触发了新的预判").toBe(2);

    await store.confirmImpact();
    await settle();
    await waitForDebouncedSave();

    const bPuts = puts().filter((item) => item.boardId === BOARD_B);
    expect(bPuts.length, "确认后没有只发一次 B 的保存").toBe(1);
    expect(bPuts[0].confirm, "保存没有带上最新确认的句柄与版本").toEqual({
      checkId: "chk_new",
      stateVersion: 8,
    });
    expect(bPuts[0].cardIds.length, "保存的候选不是最新候选").toBe(
      (latestCandidate.cards ?? []).length,
    );
    expect(
      comparable(bPuts[0].state),
      "确认后保存的候选正文与请求 2 的候选不一致（确认放行了旧候选）",
    ).toBe(comparable(latestCandidate));
    expect(puts().filter((item) => item.boardId === BOARD_A).length, "向 A 发出了 B 的保存").toBe(0);
    expect(impactChecks().length, "确认后又发了新的预判").toBe(2);
  }

  it("旧预判先成功返回：旧响应被丢弃，确认仍对应最新候选", async () => {
    const { first, second } = await armTwoChecks();

    first.resolve(affectedCheck("chk_old", 8) as never);
    await settle();
    expect(store.pendingImpact, "旧响应形成了确认").toBeNull();
    expect(store.impactCheckError, "旧响应被当成了当前预判失败").toBeNull();

    second.resolve(affectedCheck("chk_new", 8) as never);
    await settle();
    await expectLatestConfirmAndSave();
  });

  it("新预判先成功返回、旧预判迟到：旧响应不覆盖较新的确认", async () => {
    const { first, second } = await armTwoChecks();

    second.resolve(affectedCheck("chk_new", 8) as never);
    await settle();
    expect(store.pendingImpact?.checkId, "最新候选没有形成确认").toBe("chk_new");

    first.resolve(affectedCheck("chk_old", 8) as never);
    await settle();
    await settle();
    expect(store.pendingImpact?.checkId, "旧响应覆盖了较新的确认").toBe("chk_new");
    await expectLatestConfirmAndSave();
  });

  it("旧预判先失败返回：旧失败不写成当前预判失败，最新候选仍可确认", async () => {
    const { first, second } = await armTwoChecks();

    first.reject(apiError(500, "旧预判失败"));
    await settle();
    expect(store.impactCheckError, "旧预判的失败写成了当前预判失败").toBeNull();
    expect(store.pendingImpact, "旧预判失败后出现了确认").toBeNull();

    second.resolve(affectedCheck("chk_new", 8) as never);
    await settle();
    await expectLatestConfirmAndSave();
  });

  it("409 恢复中的重新预判：旧响应迟到不许清理新恢复计划（保存必须继续）", async () => {
    await store.selectBoard(BOARD_A);
    await store.selectBoard(BOARD_B);

    // 1) B 的 PUT 被服务端要求影响确认（PUT 挂起，稍后 409）
    const putFlight = deferred<void>();
    vi.mocked(api.saveBoardState).mockImplementationOnce(async () => {
      await putFlight.promise;
      throw apiError(409, "需要影响确认", {
        error: "impact_confirmation_required",
        reason: "需要影响确认",
        affectedTasks: [AFFECTED_TASK],
      });
    });
    store.commit(addCard(store.board!, { kind: "text", content: "B 新增的卡" }), "新增卡片");
    await waitForDebouncedSave();
    expect(puts().filter((item) => item.boardId === BOARD_B).length, "前提：B 的 PUT 已发出").toBe(1);
    // 这次保存自己的保存前预判（默认 cleanCheck，不需要确认）
    const checksAfterFirstSave = impactChecks().filter((item) => item.boardId === BOARD_B).length;
    expect(checksAfterFirstSave, "前提：B 的保存前预判已经发出").toBe(1);

    // 2) 409 到达时已经切到 A：恢复计划留在 B（不在这里重读）
    await store.selectBoard(BOARD_A);
    await settle();
    putFlight.resolve();
    await settle();
    await settle();
    expect(store.pendingImpact, "B 的待确认状态落到了 A").toBeNull();

    // 3) 回到 B：load 落定后开始重新预判（旧步骤，挂起）
    const recoveryRead = deferred<never>();
    const oldCheck = deferred<never>();
    const newCheck = deferred<never>();
    vi.mocked(api.fetchBoardState).mockImplementationOnce(() => recoveryRead.promise as never);
    vi.mocked(api.checkMaterialImpact)
      .mockReturnValueOnce(oldCheck.promise as never)
      .mockReturnValueOnce(newCheck.promise as never);
    const back = store.selectBoard(BOARD_B);
    await settle();
    recoveryRead.resolve(boardPayload(boardState(BOARD_B, [textCard("card_b", "B 的内容")], 8)) as never);
    await back;
    await settle();
    const checksAfterReturn = impactChecks().filter((item) => item.boardId === BOARD_B).length;
    expect(checksAfterReturn, "回到 B 后没有发出重新预判").toBe(checksAfterFirstSave + 1);

    // 4) 旧预判在飞时修改 B：按最新候选开启新步骤（第二次重新预判）
    store.commit(addCard(store.board!, { kind: "text", content: "第二版" }), "新增卡片");
    await waitForDebouncedSave();
    const checksAfterEdit = impactChecks().filter((item) => item.boardId === BOARD_B).length;
    expect(checksAfterEdit, "候选变化后没有按最新事实重新预判").toBe(checksAfterReturn + 1);

    // 5) 旧响应先回：它只属于旧步骤，不许清理新步骤的恢复计划
    oldCheck.resolve(affectedCheck("chk_old", 8) as never);
    await settle();
    await settle();
    expect(store.pendingImpact, "旧响应形成了确认").toBeNull();

    // 6) 新响应回（这次不需要影响确认）：保存必须继续（计划还在 → 重排保存）
    newCheck.resolve(cleanCheck(8) as never);
    await settle();
    await waitForDebouncedSave();

    const bPuts = puts().filter((item) => item.boardId === BOARD_B);
    expect(
      bPuts.length,
      "旧响应清掉了新恢复计划：重新预判返回「可以保存」后保存没有继续",
    ).toBe(2);
    expect(bPuts[bPuts.length - 1].state.seq, "恢复后的保存没有使用最新服务端版本").toBe(8);
    expect(puts().filter((item) => item.boardId === BOARD_A).length, "向 A 发出了 B 的保存").toBe(0);
  });
});

describe("二.5 确认结果只在 B 展示，不串到 A", () => {
  /** 前提装置：B 有执行中任务，编辑后出现影响确认。 */
  async function armConfirmOnB() {
    await store.selectBoard(BOARD_A);
    const aStatus = store.saveStatus;
    const aCards = liveCards(store).map((card) => card.id);
    await store.selectBoard(BOARD_B);
    vi.mocked(api.checkMaterialImpact).mockResolvedValueOnce(affectedCheck("chk_b", 7) as never);
    store.commit(addCard(store.board!, { kind: "text", content: "B 新增的卡" }), "新增卡片");
    await waitForDebouncedSave();
    expect(store.pendingImpact, "前提：B 出现了影响确认").not.toBeNull();
    expect(store.pendingImpact!.boardId).toBe(BOARD_B);
    return { aStatus, aCards };
  }

  it("store 层：B 点确认后切到 A，B 的保存失败迟到只写 B", async () => {
    const { aStatus, aCards } = await armConfirmOnB();

    const putFlight = deferred<void>();
    vi.mocked(api.saveBoardState).mockImplementationOnce(async () => {
      await putFlight.promise;
      throw apiError(500, "B 板保存失败：服务端 500");
    });
    const confirming = store.confirmImpact();
    await settle();
    expect(puts().filter((item) => item.boardId === BOARD_B).length, "前提：B 的 PUT 已发出").toBe(1);

    // 确认还在飞时切到 A
    await store.selectBoard(BOARD_A);
    await settle();
    expect(store.boardId).toBe(BOARD_A);

    // B 的失败回执这时才到
    putFlight.reject(apiError(500, "B 板保存失败：服务端 500"));
    await confirming;
    await settle();
    await settle();

    expect(store.boardId, "B 的失败把当前板面切走了").toBe(BOARD_A);
    expect(store.saveStatus, "B 的失败写进了 A 的保存状态").toBe(aStatus);
    expect(store.saveError, "B 的失败原因显示在 A 上").toBeNull();
    expect(store.pendingImpact, "B 的待确认状态落到了 A").toBeNull();
    expect(liveCards(store).map((card) => card.id), "B 的失败改动了 A 的卡片").toEqual(aCards);
    expect(puts().filter((item) => item.boardId === BOARD_A).length, "向 A 发出了 B 的保存").toBe(0);

    // 切回 B：B 自己的失败与候选都在
    await store.selectBoard(BOARD_B);
    await settle();
    expect(store.boardId).toBe(BOARD_B);
    expect(store.saveError, "B 看不到自己的失败原因").toContain("B 板保存失败");
    expect(store.dirty, "B 的候选丢了").toBe(true);
    expect(liveCards(store).length, "B 的新增卡丢了").toBe(2);
  });

  it("组件层：A 上不渲染 B 的失败文案与重试按钮；B 上可见，点重试只向 B 发请求", async () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    store = useInteractiveStore();
    const { aStatus, aCards } = await armConfirmOnB();

    const wrapper = mountLayer(pinia);
    await nextTick();
    await flushPromises();
    expect(wrapper.get('[data-im="impact-confirm"]').attributes("data-impact-mode")).toBe("impact");

    const putFlight = deferred<void>();
    vi.mocked(api.saveBoardState).mockImplementationOnce(async () => {
      await putFlight.promise;
      throw apiError(500, "B 板保存失败：服务端 500");
    });
    await wrapper.get('[data-im="impact-confirm-submit"]').trigger("click");
    await settle();
    expect(puts().filter((item) => item.boardId === BOARD_B).length, "前提：B 的 PUT 已发出").toBe(1);

    // 确认还在飞时切到 A
    await store.selectBoard(BOARD_A);
    await settle();
    expect(store.boardId).toBe(BOARD_A);

    // B 的失败回执这时才到
    putFlight.reject(apiError(500, "B 板保存失败：服务端 500"));
    await settle();
    await settle();

    // store：A 完全不受影响
    expect(store.saveStatus, "B 的失败写进了 A 的保存状态").toBe(aStatus);
    expect(store.saveError, "B 的失败原因显示在 A 上").toBeNull();
    expect(store.pendingImpact, "B 的待确认状态落到了 A").toBeNull();
    expect(liveCards(store).map((card) => card.id), "B 的失败改动了 A 的卡片").toEqual(aCards);

    // 组件：A 上不许出现 B 的失败文案与重试入口
    expect(
      wrapper.text(),
      "A 的页面上渲染出了 B 的保存失败文案（确认结果没有按板面归属）",
    ).not.toContain("B 板保存失败");
    expect(
      wrapper.find('[data-im="impact-confirm-retry"]').exists(),
      "A 的页面上出现了 B 的「重新核实并保存」重试按钮",
    ).toBe(false);

    // 切回 B：B 的失败与重试入口可见、可点
    await store.selectBoard(BOARD_B);
    await settle();
    expect(wrapper.text(), "B 上看不到自己的失败文案").toContain("B 板保存失败");
    const retry = wrapper.get('[data-im="impact-confirm-retry"]');
    expect(retry.attributes("disabled"), "B 的重试按钮不可点").toBeUndefined();

    // 点重试：只向 B 发一次 PUT，A 一次都没有
    const bBefore = puts().filter((item) => item.boardId === BOARD_B).length;
    await retry.trigger("click");
    await settle();
    await waitForDebouncedSave();
    const bAfter = puts().filter((item) => item.boardId === BOARD_B).length;
    expect(bAfter, "点重试没有向 B 发出保存").toBe(bBefore + 1);
    expect(puts().filter((item) => item.boardId === BOARD_A).length, "重试发到了 A").toBe(0);
  });

  it("同板：较早的保存失败结果不覆盖较新的确认操作（重试成功后显示已保存）", async () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    store = useInteractiveStore();
    await armConfirmOnB();

    const wrapper = mountLayer(pinia);
    await nextTick();
    await flushPromises();

    // 第一次确认：保存失败，B 上如实显示失败结果 + 重试入口
    vi.mocked(api.saveBoardState).mockImplementationOnce(async () => {
      throw apiError(500, "第一次保存失败：服务端 500");
    });
    await wrapper.get('[data-im="impact-confirm-submit"]').trigger("click");
    await settle();
    await settle();
    expect(wrapper.get('[data-im="impact-confirm-outcome"]').attributes("data-outcome")).toBe(
      "save_failed",
    );
    expect(wrapper.text(), "B 看不到自己的失败文案").toContain("第一次保存失败");
    const retry = wrapper.get('[data-im="impact-confirm-retry"]');
    expect(retry.attributes("disabled"), "重试入口不可点").toBeUndefined();

    // 较新的确认操作（点「重新核实并保存」）：这次保存成功
    await retry.trigger("click");
    await settle();
    await settle();
    await waitForDebouncedSave();

    expect(
      wrapper.get('[data-im="impact-confirm-outcome"]').attributes("data-outcome"),
      "较早的失败结果覆盖了较新的确认操作（重试已成功却仍显示失败）",
    ).toBe("saved");
    expect(wrapper.text(), "较新的成功结果里仍带着上一次的失败文案").not.toContain("第一次保存失败");
    expect(
      puts().filter((item) => item.boardId === BOARD_B).length,
      "同板两次确认没有产生各自的保存",
    ).toBe(2);
    expect(puts().filter((item) => item.boardId === BOARD_A).length, "同板确认发到了 A").toBe(0);
  });
});