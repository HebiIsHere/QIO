/**
 * #46 第七轮回归（任务书三.6）：撤回未被服务器确认时的重试与响应式读取。
 *
 * 背景（F1 新口径）：取消影响确认时**先同步**把板面退回「上一次与服务器一致」的事实，
 * 回读只负责把撤回落到服务器事实上确认。回读失败 → 本地撤回仍生效 + cancelRecovery 说明
 * 「撤回还没被服务器确认」+ retryCancelRecovery() 重试入口；重试成功后必须收敛为已确认。
 *
 * 本文件锁死的行为：
 * 1. 回读失败 → 重试成功：cancelRecovery.active 必须变成 false、reason 清空
 *    （通过 store.cancelRecovery 这个**响应式公开读取**断言，而不是内部 Map）；
 * 2. 同板失败后重试仍失败：如实更新真实原因（不假装成功），再重试成功才收敛；
 * 3. B 的撤回失败晚于 A 的撤回开始：A 的撤回结果不被 B 的迟到失败覆盖；
 * 4. 组件层：挂载 ImpactConfirmLayer，取消后回读失败 → 显示「撤回还没有被服务器事实确认」
 *    与可点的重试入口；点它重试成功后重试入口消失。
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
const SERVER_TEXT = "服务器原文";
const CANCELLED_TEXT = "被取消掉的新正文";
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

function liveCards(store: ReturnType<typeof useInteractiveStore>) {
  return (store.board?.cards ?? []).filter((card) => !card.deleted);
}

async function settle(times = 8): Promise<void> {
  for (let i = 0; i < times; i += 1) await flushPromises();
}

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
        : boardState(BOARD_B, [textCard("card_b", SERVER_TEXT)], 7),
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
  vi.mocked(api.checkMaterialImpact).mockResolvedValue(affectedCheck("chk_b", 7) as never);
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
  await new Promise((resolve) => setTimeout(resolve, 700));
  await settle();
  localStorage.clear();
});

/** 前提装置：B 上编辑出影响确认（保存被影响门拦下）。 */
async function armPendingImpactOnB() {
  await store.selectBoard(BOARD_A);
  await store.selectBoard(BOARD_B);
  store.commit(boardState(BOARD_B, [textCard("card_b", CANCELLED_TEXT)], 7), "改正文");
  await store.saveNow();
  expect(store.pendingImpact, "前提：B 出现了影响确认").not.toBeNull();
  expect(store.pendingImpact!.boardId).toBe(BOARD_B);
}

describe("三.6 撤回未被服务器确认：重试与响应式读取", () => {
  it("回读失败 → 重试成功：cancelRecovery.active 变 false、reason 清空（响应式公开读取）", async () => {
    await armPendingImpactOnB();

    // 取消：回读失败（真实原因必须留下）
    vi.mocked(api.fetchBoardState).mockRejectedValueOnce(
      apiError(503, "服务端暂时不可用（503）", { error: "read_failed", reason: "服务端暂时不可用（503）" }),
    );
    await store.cancelImpact();
    await settle();

    // 本地撤回立刻生效：被取消的正文不许回到板面
    expect(
      liveCards(store).find((card) => card.id === "card_b")!.content,
      "回读失败又把被取消的正文放回了板面",
    ).toBe(SERVER_TEXT);
    expect(store.pendingImpact, "取消后仍停在影响确认里").toBeNull();

    // 第一次读取 cancelRecovery（响应式公开读取）：建立 computed 缓存
    const recoveryBefore = store.cancelRecovery;
    expect(recoveryBefore?.boardId, "撤回恢复状态不属于 B").toBe(BOARD_B);
    expect(recoveryBefore?.active, "回读失败却宣称撤回已确认").toBe(true);
    expect(String(recoveryBefore?.reason ?? ""), "回读失败没有留下真实原因").toContain("503");

    // 重试：服务器恢复，回读成功
    vi.mocked(api.fetchBoardState).mockResolvedValue(
      boardPayload(boardState(BOARD_B, [textCard("card_b", SERVER_TEXT)], 7)) as never,
    );
    const retried = await store.retryCancelRecovery();
    expect(retried.ok, "重试入口没有取到最新事实").toBe(true);

    // 核心断言：重试成功后，响应式读取必须收敛为「已确认」
    expect(
      store.cancelRecovery?.active,
      "重试成功后 cancelRecovery 仍是 active=true（响应式读取没有随状态更新）",
    ).toBe(false);
    expect(store.cancelRecovery?.reason, "重试成功后仍留着旧的失败原因").toBeNull();
    expect(store.cancelRecovery?.boardId, "收敛后的撤回状态不属于 B").toBe(BOARD_B);
  });

  it("同板失败后重试仍失败：如实更新真实原因（不假装成功），再重试成功才收敛", async () => {
    await armPendingImpactOnB();

    vi.mocked(api.fetchBoardState).mockRejectedValueOnce(apiError(503, "第一次读取失败（503）"));
    await store.cancelImpact();
    await settle();
    expect(store.cancelRecovery?.active, "回读失败却宣称撤回已确认").toBe(true);
    expect(String(store.cancelRecovery?.reason ?? "")).toContain("503");

    // 第一次重试仍失败：原因换成这次的真实原因，仍然 active
    vi.mocked(api.fetchBoardState).mockRejectedValueOnce(apiError(502, "第二次读取失败（502）"));
    const firstRetry = await store.retryCancelRecovery();
    expect(firstRetry.ok, "重试失败却报告成功").toBe(false);
    expect(store.cancelRecovery?.active, "重试失败却宣称撤回已确认").toBe(true);
    expect(String(store.cancelRecovery?.reason ?? ""), "重试失败没有更新真实原因").toContain("502");

    // 第二次重试成功：收敛
    vi.mocked(api.fetchBoardState).mockResolvedValue(
      boardPayload(boardState(BOARD_B, [textCard("card_b", SERVER_TEXT)], 7)) as never,
    );
    const secondRetry = await store.retryCancelRecovery();
    expect(secondRetry.ok, "第二次重试没有取到最新事实").toBe(true);
    expect(store.cancelRecovery?.active, "重试成功后仍在说撤回未被确认").toBe(false);
    expect(store.cancelRecovery?.reason).toBeNull();
  });

  it("B 的撤回失败晚于 A 的撤回开始：A 的撤回结果不被 B 的迟到失败覆盖", async () => {
    // B 上发起撤回，回读挂起
    await store.selectBoard(BOARD_A);
    await store.selectBoard(BOARD_B);
    const bCancelRead = deferred<never>();
    vi.mocked(api.fetchBoardState).mockImplementationOnce(() => bCancelRead.promise as never);
    const bCancel = store.cancelImpact();
    await settle();
    expect(store.cancelRecovery?.boardId, "B 的撤回状态没有建立").toBe(BOARD_B);
    expect(store.cancelRecovery?.active).toBe(true);

    // 读取还没回来就切到 A，A 自己也发起一次撤回（读取挂起）
    await store.selectBoard(BOARD_A);
    const aCancelRead = deferred<never>();
    vi.mocked(api.fetchBoardState).mockImplementationOnce(() => aCancelRead.promise as never);
    const aCancel = store.cancelImpact();
    await settle();

    // A 的撤回读取成功：A 的撤回结果收敛
    aCancelRead.resolve(boardPayload(boardState(BOARD_A, [textCard("card_a", "A 的内容")], 3)) as never);
    await aCancel;
    await settle();
    expect(store.boardId).toBe(BOARD_A);
    expect(store.cancelRecovery?.boardId, "A 的撤回状态不属于 A").toBe(BOARD_A);
    expect(store.cancelRecovery?.active, "A 的撤回没有被确认").toBe(false);
    expect(store.cancelRecovery?.reason).toBeNull();

    // B 的撤回读取迟到失败：不许改写 A 的撤回结果
    bCancelRead.reject(apiError(500, "B 撤回读取失败"));
    await bCancel;
    await settle();
    expect(store.boardId).toBe(BOARD_A);
    expect(store.cancelRecovery?.boardId, "B 的迟到失败改写了 A 的撤回状态").toBe(BOARD_A);
    expect(store.cancelRecovery?.active, "A 的撤回结果被 B 的失败覆盖").toBe(false);
    expect(store.cancelRecovery?.reason, "B 的失败原因显示在 A 上").toBeNull();

    // 回到 B：B 自己的撤回状态由 B 的事实决定（成功重读后收敛，不显示旧失败原因）
    await store.selectBoard(BOARD_B);
    await settle();
    expect(store.cancelRecovery?.boardId).toBe(BOARD_B);
    expect(store.cancelRecovery?.active, "回到 B 的成功读取没有把撤回落到服务器事实").toBe(false);
    expect(store.cancelRecovery?.reason ?? "").not.toContain("B 撤回读取失败");
  });

  it("组件层：取消后回读失败显示重试入口，点它重试成功后入口消失", async () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    store = useInteractiveStore();
    await armPendingImpactOnB();

    const wrapper = mountLayer(pinia);
    await nextTick();
    await flushPromises();
    expect(wrapper.get('[data-im="impact-confirm"]').attributes("data-impact-mode")).toBe("impact");

    // 取消：回读失败
    vi.mocked(api.fetchBoardState).mockRejectedValueOnce(apiError(503, "服务端暂时不可用（503）"));
    await wrapper.get('[data-im="impact-confirm-cancel"]').trigger("click");
    await settle();

    // 界面如实说明「撤回还没有被服务器事实确认」，并给出可点的重试入口
    const outcome = wrapper.get('[data-im="impact-confirm-outcome"]');
    expect(outcome.attributes("data-outcome"), "回读失败却显示成已取消").toBe("cancelled_unconfirmed");
    expect(outcome.text(), "界面没有说明撤回未被服务器确认").toContain("撤回还没有被服务器事实确认");
    expect(outcome.text(), "界面没有给出真实原因").toContain("503");
    const retry = wrapper.get('[data-im="impact-confirm-recovery-retry"]');
    expect(retry.attributes("disabled"), "重试入口不可点").toBeUndefined();

    // 点重试：服务器恢复，回读成功 → 重试入口消失
    vi.mocked(api.fetchBoardState).mockResolvedValue(
      boardPayload(boardState(BOARD_B, [textCard("card_b", SERVER_TEXT)], 7)) as never,
    );
    await retry.trigger("click");
    await settle();
    await nextTick();

    expect(
      wrapper.find('[data-im="impact-confirm-recovery-retry"]').exists(),
      "重试成功后界面仍留着「重新核对服务器事实」入口",
    ).toBe(false);
    expect(
      wrapper.get('[data-im="impact-confirm-outcome"]').attributes("data-outcome"),
      "重试成功后界面没有收敛为已取消",
    ).toBe("cancelled");
  });
});
