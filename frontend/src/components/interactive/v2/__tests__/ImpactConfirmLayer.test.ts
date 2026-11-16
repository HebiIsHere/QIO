/**
 * 旧 ImpactConfirmDialog 的有效能力迁移到 v2/ImpactConfirmLayer —— 定向行为测试。
 *
 * 迁移清单（COMMON §5 B-CP1）：
 * - pendingDecision：每项决定的状态（原因 / 影响）在真实页面上可见；
 * - 剩余撤回 pendingReverts / dismissedReverts：失败或取消后未处理的项自动提示，结束提示后仍可「再次查看」；
 * - 明确 decisionIds：只提交这次界面展示的项，不顺带处理没看到的项；
 * - 关闭不撤回：取消只结束提示，不执行任何撤回；
 * - 失败重试：服务端真实原因可见、按钮恢复可点；
 * - 部分完成反馈：处理完展示项但仍有剩余时说清楚还剩多少；
 * - 与统一审批共用一个阻塞面：登记进 modalArbiter，被更高优先级窗口挡住时让位但不丢状态。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount, type VueWrapper } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { nextTick } from "vue";
import ImpactConfirmLayer from "../ImpactConfirmLayer.vue";
import { useInteractiveStore } from "../../../../stores/interactive";
import { useModalArbiterStore } from "../../../../stores/modalArbiter";
import type { Intent } from "../../../../interactive/types";
import * as api from "../../../../services/interactive";

vi.mock("../../../../services/interactive", () => ({
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

function pending(id: string, reason: string, impact = "会影响其他工作") {
  return { id, reason, impact };
}

function intentWithRevert(
  id: string,
  decisions: { id: string; reason: string; impact: string }[],
  status = "failed",
): Intent {
  return {
    id,
    boardId: "board_t",
    submissionId: null,
    title: "任务 " + id,
    summary: "",
    status,
    preview: null,
    impact: { objects: [], tasks: [], consequences: [] },
    dependsOn: [],
    conflictsWith: [],
    conflictKey: "",
    materialRefs: [],
    progress: { done: 0, total: 1, text: "" },
    reason: "",
    demo: false,
    createdAt: "2026-10-25T00:00:00.000Z",
    updatedAt: "2026-10-25T00:00:00.000Z",
    revert: {
      reverted: [],
      kept: [],
      pendingDecision: decisions,
      reasonText: "已经撤回不受影响的部分",
    },
  } as unknown as Intent;
}

let wrapper: VueWrapper | null = null;

function mountLayer() {
  const pinia = createPinia();
  setActivePinia(pinia);
  wrapper = mount(ImpactConfirmLayer, { global: { plugins: [pinia] }, attachTo: document.body });
  return { wrapper, store: useInteractiveStore(), arbiter: useModalArbiterStore() };
}

/* ── 第十轮：撤回「重新核对」的结果归属与最终事实 ────────────────── */
const BOARD_A = "board_a";
const BOARD_B = "board_b";
const B_SERVER_SEQ = 7;
const NOW = "2026-11-16T00:00:00Z";

function boardPayload(boardId: string, seq = B_SERVER_SEQ) {
  return {
    board: { id: boardId, title: boardId },
    state: { boardId, seq, updatedAt: NOW, cards: [], groups: [], links: [], selection: [] },
    seq,
    baseline: null,
    submissions: [],
    drafts: { drafts: {}, updatedAt: null },
  };
}

function apiError(status: number, message: string): Error {
  return Object.assign(new Error(message), { status, payload: {} });
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

/** 挂载后把当前板面设成 B（组件只按 store.boardId 决定结果属于哪块板）。 */
async function mountOnBoardB() {
  const mounted = mountLayer();
  mounted.store.boardId = BOARD_B;
  await nextTick();
  return mounted;
}

/**
 * 经真实 UI 走一次「取消本次改动」：回读失败 → 结果面板停在「撤回还没有被服务器事实确认」，
 * 并渲染「重新核对服务器事实」入口。断言只落在组件 DOM + 真实 store 状态上。
 */
async function armUnconfirmedCancel(w: VueWrapper, store: ReturnType<typeof useInteractiveStore>, message: string) {
  store.pendingImpact = {
    affected: [{ intentId: "intent_b_1", title: "任务-B-1", materials: ["card_b"], consequence: "暂停并保留进度" }],
    previewRev: 1,
    stateVersion: 0,
    checkId: "chk_b",
  } as never;
  await nextTick();
  vi.mocked(api.fetchBoardState).mockRejectedValueOnce(apiError(502, message));
  await w.get('[data-im="impact-confirm-cancel"]').trigger("click");
  await flushPromises();
  expect(store.cancelRecovery?.boardId, "前提：B 的撤回转回等待服务器确认").toBe(BOARD_B);
  expect(store.cancelRecovery?.active, "前提：撤回还没有被服务器事实确认").toBe(true);
  expect(w.get('[data-im="impact-confirm-outcome"]').attributes("data-outcome")).toBe(
    "cancelled_unconfirmed",
  );
}

beforeEach(() => {
  localStorage.clear();
});

afterEach(() => {
  wrapper?.unmount();
  wrapper = null;
  document.body.innerHTML = "";
});

describe("剩余撤回：真实展示 pendingDecision（每项决定状态）", () => {
  it("任务失败/取消后仍留着待决定项：自动出现确认框，逐项写原因与影响", async () => {
    const { wrapper: w, store } = mountLayer();
    store.intents = [
      intentWithRevert("i1", [pending("d1", "会改写任务 A 依赖的材料"), pending("d2", "会删除任务 B 的结果卡")]),
    ];
    await nextTick();

    const dialog = w.get('[data-im="impact-confirm"]');
    expect(dialog.attributes("data-impact-mode")).toBe("revert");
    expect(dialog.attributes("role")).toBe("alertdialog");
    expect(w.findAll('[data-im="impact-revert-item"]')).toHaveLength(1);
    expect(w.findAll('[data-im="impact-revert-pending-item"]')).toHaveLength(2);
    const text = dialog.text();
    expect(text).toContain("会改写任务 A 依赖的材料");
    expect(text).toContain("会删除任务 B 的结果卡");
    expect(text).toContain("不会顺带处理你没有看到的项");
    expect(text).toContain("已经撤回不受影响的部分");
  });

  it("已结束提示的任务不再自动打断，但可在「再次查看」里重新打开", async () => {
    const { wrapper: w, store } = mountLayer();
    store.intents = [intentWithRevert("i1", [pending("d1", "会改写材料")])];
    await nextTick();
    await w.get('[data-im="impact-confirm-cancel"]').trigger("click");
    await flushPromises();

    // v-show：窗口 DOM 保留、状态不丢，只是不再显示
    expect(w.get('[data-im="impact-confirm-scrim"]').isVisible()).toBe(false);
    const dismissed = w.get('[data-im="impact-revert-dismissed"]');
    expect(dismissed.text()).toContain("还有撤回没有决定");
    expect(dismissed.text()).toContain("任务 i1");

    await w.get('[data-im="impact-revert-reopen"]').trigger("click");
    await nextTick();
    expect(w.find('[data-im="impact-confirm"]').exists()).toBe(true);
    expect(w.find('[data-im="impact-revert-dismissed"]').exists()).toBe(false);
  });
});

describe("剩余撤回：明确 decisionIds / 关闭不撤回 / 失败重试 / 部分完成", () => {
  it("「继续」只提交这次界面明确展示的 decisionIds（不顺带处理没看到的项）", async () => {
    const { wrapper: w, store } = mountLayer();
    store.intents = [
      intentWithRevert("i1", [pending("d1", "会改写材料 A")]),
      intentWithRevert("i2", [pending("d2", "会改写材料 B")]),
    ];
    await nextTick();
    const spy = vi.spyOn(store, "continueRevertDecision").mockResolvedValue({ ok: true } as never);
    const buttons = w.findAll('[data-im="impact-revert-continue"]');
    expect(buttons).toHaveLength(2);
    await buttons[0].trigger("click");
    await flushPromises();

    expect(spy).toHaveBeenCalledTimes(1);
    expect(spy).toHaveBeenCalledWith("i1", ["d1"]);
    // 另一个任务的决定项没有被顺带处理
    expect(spy).not.toHaveBeenCalledWith("i2", expect.anything());
  });

  it("关闭不撤回：取消只结束本次提示，不调用任何撤回接口", async () => {
    const { wrapper: w, store } = mountLayer();
    store.intents = [intentWithRevert("i1", [pending("d1", "会改写材料")])];
    await nextTick();
    const spy = vi.spyOn(store, "continueRevertDecision").mockResolvedValue({ ok: true } as never);
    await w.get('[data-im="impact-confirm-cancel"]').trigger("click");
    await flushPromises();

    expect(spy).not.toHaveBeenCalled();
    expect(store.revertDecisionDismissed["i1"]).toBe(true);
    expect(store.intents[0].revert?.pendingDecision).toHaveLength(1);
  });

  it("失败重试：服务端真实原因可见、按钮恢复可点，再次点击真的重试", async () => {
    const { wrapper: w, store } = mountLayer();
    store.intents = [intentWithRevert("i1", [pending("d1", "会改写材料")])];
    await nextTick();
    const spy = vi
      .spyOn(store, "continueRevertDecision")
      .mockResolvedValue({ ok: false, reason: "服务端拒绝：内容已经变化" } as never);

    const button = w.get('[data-im="impact-revert-continue"]');
    await button.trigger("click");
    await flushPromises();

    expect(w.get('[data-im="impact-revert-item-error"]').text()).toContain("服务端拒绝：内容已经变化");
    const again = w.get('[data-im="impact-revert-continue"]');
    expect(again.attributes("disabled")).toBeUndefined();
    await again.trigger("click");
    await flushPromises();
    expect(spy).toHaveBeenCalledTimes(2);
  });

  it("部分完成：处理完展示项但仍有剩余 → 说明还剩多少；全部处理完 → 结果活过对话框关闭", async () => {
    const { wrapper: w, store } = mountLayer();
    store.intents = [intentWithRevert("i1", [pending("d1", "会改写材料 A"), pending("d2", "会改写材料 B")])];
    await nextTick();
    const spy = vi.spyOn(store, "continueRevertDecision");
    // 服务端逐项摘要：请求了两项，其中一项因为之后又有变化需要重新决定
    spy.mockImplementationOnce(async () => {
      store.intents = [intentWithRevert("i1", [pending("d2", "会改写材料 B")])];
      return {
        ok: true,
        decision: {
          requested: ["d1", "d2"],
          processed: ["d1"],
          reconfirmed: [],
          skipped: [],
          remaining: ["d2"],
          gone: [],
        },
      } as never;
    });
    spy.mockImplementationOnce(async () => {
      store.intents = [];
      return {
        ok: true,
        decision: {
          requested: ["d2"],
          processed: ["d2"],
          reconfirmed: [],
          skipped: [],
          remaining: [],
          gone: [],
        },
      } as never;
    });

    await w.get('[data-im="impact-revert-continue"]').trigger("click");
    await flushPromises();
    // 框还开着：部分完成说明在框内，且按服务端 processed / remaining 说，不按展示条数猜
    expect(w.find('[data-im="impact-confirm"]').exists()).toBe(true);
    expect(w.get('[data-im="impact-revert-notice"]').text()).toContain("已按你的决定撤回 1 项");
    expect(w.get('[data-im="impact-revert-notice"]').text()).toContain("还有 1 项需要你决定");
    // F05：同一条部分完成信息不在框里重复出现第二次（标题式 outcome 不再叠进来）
    const dialogText = w.get('[data-im="impact-confirm"]').text();
    expect(dialogText).not.toContain("还有剩余撤回项等你决定");
    expect((dialogText.match(/还有 1 项需要你决定/g) ?? []).length).toBe(1);

    await w.get('[data-im="impact-revert-continue"]').trigger("click");
    await flushPromises();
    // 全部处理完：框不再显示（DOM 保留），真实结果留在页面上
    expect(w.get('[data-im="impact-confirm-scrim"]').isVisible()).toBe(false);
    const outcome = w.get('[data-im="impact-confirm-outcome"]');
    expect(outcome.attributes("data-outcome")).toBe("revert_done");
    expect(outcome.text()).toContain("已按你的决定撤回 1 项");
    expect(outcome.text()).toContain("剩下的改动已经处理完");
    expect(spy).toHaveBeenCalledTimes(2);
  });
});

describe("键盘与阻塞面规则", () => {
  it("Escape 在撤回模式 = 关闭提示（不撤回）；打开时焦点落在第一个按钮", async () => {
    const { wrapper: w, store } = mountLayer();
    store.intents = [intentWithRevert("i1", [pending("d1", "会改写材料")])];
    await nextTick();
    await flushPromises();
    const cancel = w.get('[data-im="impact-confirm-cancel"]');
    expect(document.activeElement).toBe(cancel.element);

    window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    await flushPromises();
    expect(w.get('[data-im="impact-confirm-scrim"]').isVisible()).toBe(false);
    expect(store.revertDecisionDismissed["i1"]).toBe(true);
  });

  it("Escape 在影响确认模式不取消改动：只有「确认改动 / 取消本次改动」两个按钮能作答", async () => {
    const { wrapper: w, store } = mountLayer();
    store.pendingImpact = {
      affected: [{ intentId: "i1", title: "整理材料", materials: ["a.pdf"], consequence: "会暂停" }],
      previewRev: 1,
      stateVersion: 0,
      checkId: "chk",
    } as never;
    await nextTick();
    await flushPromises();
    expect(w.get('[data-im="impact-confirm"]').attributes("data-impact-mode")).toBe("impact");
    const spy = vi.spyOn(store, "cancelImpact").mockResolvedValue(undefined as never);
    window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    await flushPromises();
    // Esc 不能顺手撤销：框还在，保存决定没有被做出，也没有任何「已取消」的假象
    expect(spy).not.toHaveBeenCalled();
    expect(w.find('[data-im="impact-confirm"]').exists()).toBe(true);
    expect(w.find('[data-im="impact-confirm-cancel"]').exists()).toBe(true);
    expect(w.find('[data-im="impact-confirm-submit"]').exists()).toBe(true);
  });

  it("外部点击遮罩不关闭影响确认（点击不算作答）", async () => {
    const { wrapper: w, store } = mountLayer();
    store.pendingImpact = {
      affected: [{ intentId: "i1", title: "整理材料", materials: ["a.pdf"], consequence: "会暂停" }],
      previewRev: 1,
      stateVersion: 0,
      checkId: "chk",
    } as never;
    await nextTick();
    await flushPromises();
    await w.get('[data-im="impact-confirm-scrim"]').trigger("click");
    await flushPromises();
    expect(w.find('[data-im="impact-confirm"]').exists()).toBe(true);
  });

  it("关闭询问挂起（suspendAll）时只隐藏不卸载，恢复后自动回来", async () => {
    const { wrapper: w, store, arbiter } = mountLayer();
    store.intents = [intentWithRevert("i1", [pending("d1", "会改写材料")])];
    await nextTick();
    await flushPromises();
    expect(w.get('[data-im="impact-confirm-scrim"]').isVisible()).toBe(true);

    arbiter.suspendAll("close-ask");
    await nextTick();
    // v-show：DOM 保留（状态/滚动位置不丢），但不再显示、也不再接收键盘
    expect(w.find('[data-im="impact-confirm-scrim"]').exists()).toBe(true);
    expect(w.get('[data-im="impact-confirm-scrim"]').isVisible()).toBe(false);
    expect(w.get('[data-im="impact-confirm-deferred"]').text()).toContain("撤回");

    arbiter.resumeAll();
    await nextTick();
    expect(w.get('[data-im="impact-confirm-scrim"]').isVisible()).toBe(true);
    expect(w.find('[data-im="impact-confirm-deferred"]').exists()).toBe(false);
  });

  it("已有更高优先级中央弹窗（引导 / 关闭询问）时不强行显示；它关闭后自动显示", async () => {
    const { wrapper: w, store, arbiter } = mountLayer();
    const opened = arbiter.open({ id: "onboarding", kind: "onboarding", dismissible: false });
    expect(opened).toBe(true);

    store.pendingImpact = {
      affected: [{ intentId: "i1", title: "整理材料", materials: ["a.pdf"], consequence: "会暂停" }],
      previewRev: 1,
      stateVersion: 0,
      checkId: "chk",
    } as never;
    await nextTick();
    await flushPromises();
    expect(w.get('[data-im="impact-confirm-scrim"]').isVisible()).toBe(false);
    expect(w.get('[data-im="impact-confirm-deferred"]').text()).toContain("影响确认");

    arbiter.close("onboarding");
    await nextTick();
    await flushPromises();
    expect(w.get('[data-im="impact-confirm-scrim"]').isVisible()).toBe(true);
  });

  it("影响确认登记为中央弹窗（kind=impact），关闭后从仲裁里移除", async () => {
    const { store, arbiter } = mountLayer();
    store.pendingImpact = {
      affected: [],
      previewRev: 1,
      stateVersion: 0,
      checkId: "chk",
    } as never;
    await nextTick();
    await flushPromises();
    expect(arbiter.isOpen("impact-confirm")).toBe(true);
    expect(arbiter.active?.kind).toBe("impact");

    store.pendingImpact = null;
    await nextTick();
    expect(arbiter.isOpen("impact-confirm")).toBe(false);
  });
});

/**
 * #46（第十轮，Task 3）：「重新核对」的结果必须跟随**发起这次操作的原板**与**最终服务器事实**。
 *
 * 反例（基线 70e475c2）：
 * - retryCancelRecovery() 在 await 之后才读 store.boardId 写 outcome：B 点重新核对后切到 A，
 *   A 上会显示 B 的撤回提示，而 B 上看不到结果；
 * - 同一次撤回已被别的采纳读取确认（cancelRecovery.active=false）后，这次请求失败会无条件
 *   重新显示「撤回还没有被服务器事实确认」，把已确认说回等待/失败；
 * - 同板又开始新的撤回后，旧操作的迟到结果覆盖新操作的提示。
 */
describe("第十轮：重新核对的结果跟随原板与最终服务器事实", () => {
  beforeEach(() => {
    vi.resetAllMocks();
    vi.mocked(api.fetchBoardState).mockImplementation((async (id: string) =>
      boardPayload(String(id))) as never);
  });

  it("B 点「重新核对」后切到 A：A 不出现 B 的撤回提示，切回 B 才显示这次结果", async () => {
    const { wrapper: w, store } = await mountOnBoardB();
    await armUnconfirmedCancel(w, store, "撤回第一次回读失败：服务端 502");

    // 点「重新核对」：回读挂起
    const retryRead = deferred<never>();
    vi.mocked(api.fetchBoardState).mockImplementationOnce(() => retryRead.promise as never);
    await w.get('[data-im="impact-confirm-recovery-retry"]').trigger("click");
    await flushPromises();

    // 结果回来前切到 A：这块板不是这次撤回的归属
    store.boardId = BOARD_A;
    await nextTick();
    expect(
      w.find('[data-im="impact-confirm-outcome"]').exists(),
      "A 上出现了 B 的撤回提示",
    ).toBe(false);

    // B 的回读这时返回：已经切到 A，这次读取没有被采纳
    retryRead.resolve(boardPayload(BOARD_B) as never);
    await flushPromises();
    await flushPromises();
    expect(store.boardId).toBe(BOARD_A);
    expect(
      w.find('[data-im="impact-confirm-outcome"]').exists(),
      "B 的迟到重试结果写进了 A 的页面",
    ).toBe(false);
    expect(store.cancelRecovery, "B 的撤回重试在 A 上建立了撤回状态").toBeNull();

    // 切回 B：这次撤回的真实结果在 B 上显示（仍未确认，可再次核对）
    store.boardId = BOARD_B;
    await nextTick();
    const outcome = w.get('[data-im="impact-confirm-outcome"]');
    expect(outcome.attributes("data-board-id"), "结果没有归属原板 B").toBe(BOARD_B);
    expect(outcome.attributes("data-outcome")).toBe("cancelled_unconfirmed");
    expect(outcome.text()).toContain("撤回还没有被服务器事实确认");
    expect(store.cancelRecovery?.active, "前提：B 的撤回仍未确认").toBe(true);
  });

  it("同一次撤回已被同板采纳读取确认：迟到的失败重试不重新显示等待/失败", async () => {
    const { wrapper: w, store } = await mountOnBoardB();
    await armUnconfirmedCancel(w, store, "撤回第一次回读失败：服务端 502");

    // 受控的「重新核对」返回值/时序（真实 store 状态仍由下面的真实采纳读取改变）
    const retryResult = deferred<{ ok: boolean; error?: string }>();
    const retrySpy = vi
      .spyOn(store, "retryCancelRecovery")
      .mockImplementation(() => retryResult.promise as never);
    await w.get('[data-im="impact-confirm-recovery-retry"]').trigger("click");
    await flushPromises();
    expect(retrySpy).toHaveBeenCalledTimes(1);

    // 同板另一次真正被采纳的读取先确认这次撤回（版本到达撤回基线）
    const adoptedRead = deferred<never>();
    vi.mocked(api.fetchBoardState).mockImplementationOnce(() => adoptedRead.promise as never);
    const confirming = store.refreshBoardFromServer(BOARD_B);
    await flushPromises();
    adoptedRead.resolve(boardPayload(BOARD_B) as never);
    await confirming;
    await flushPromises();
    expect(store.cancelRecovery?.active, "前提：同板采纳读取已确认这次撤回").toBe(false);

    // 这次重试这时才返回失败：结果必须以 store 的服务器事实为准
    retryResult.resolve({ ok: false, error: "撤回还没有被服务器事实确认：重试迟到失败" });
    await flushPromises();

    const outcome = w.get('[data-im="impact-confirm-outcome"]');
    expect(
      outcome.attributes("data-outcome"),
      "已确认的撤回被迟到的失败重试重新显示成等待/失败",
    ).toBe("cancelled");
    expect(outcome.text(), "已确认的撤回仍显示「撤回还没有被服务器事实确认」").not.toContain(
      "撤回还没有被服务器事实确认",
    );
    expect(store.cancelRecovery?.active, "迟到的失败重试重新打开了已确认的撤回").toBe(false);
    expect(store.cancelRecovery?.reason).toBeNull();
  });

  it("重试在飞时同板又开始新的撤回：旧结果被丢弃，不覆盖新一次撤回的提示", async () => {
    const { wrapper: w, store } = await mountOnBoardB();
    await armUnconfirmedCancel(w, store, "第一次撤回回读失败：服务端 502");

    // 旧的「重新核对」：回读挂起
    const oldRetryRead = deferred<never>();
    vi.mocked(api.fetchBoardState).mockImplementationOnce(() => oldRetryRead.promise as never);
    await w.get('[data-im="impact-confirm-recovery-retry"]').trigger("click");
    await flushPromises();

    // 同板又开始一次新的撤回（新的 opSeq）：它自己的回读失败 → 新的提示
    store.pendingImpact = {
      affected: [],
      previewRev: 2,
      stateVersion: 0,
      checkId: "chk_b_2",
    } as never;
    await nextTick();
    vi.mocked(api.fetchBoardState).mockRejectedValueOnce(apiError(502, "第二次撤回回读失败：服务端 502"));
    await w.get('[data-im="impact-confirm-cancel"]').trigger("click");
    await flushPromises();
    expect(store.cancelRecovery?.active, "前提：第二次撤回也在等待服务器确认").toBe(true);
    expect(String(store.cancelRecovery?.reason ?? "")).toContain("第二次撤回回读失败");

    // 旧重试这时才失败回来：不得把旧操作的原因写成当前提示
    oldRetryRead.reject(apiError(503, "旧重试迟到失败：服务端 503"));
    await flushPromises();

    const outcome = w.get('[data-im="impact-confirm-outcome"]');
    expect(outcome.text(), "旧撤回的迟到重试覆盖了新一次撤回的提示").not.toContain(
      "旧重试迟到失败",
    );
    expect(outcome.text()).toContain("第二次撤回回读失败");
    expect(store.cancelRecovery?.active, "旧重试结算了新撤回的等待").toBe(true);
  });
});
