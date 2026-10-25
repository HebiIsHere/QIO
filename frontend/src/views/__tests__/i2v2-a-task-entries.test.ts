/**
 * F03 行为回归：任务 / 批量入口必须接到**新页面有效界面**，不能只改内部开关。
 *
 * 基线缺陷：HUD「任务」按钮只切 store.tasksOpen，而承接它的 IntentStatusPopover 未挂载；
 * 提交区的「打开批量审批」只切 store.batchOpen，IntentBatchTray 同样未挂载。
 * 于是两个入口点了没有任何可见反应。运行中的任务也没有暂停 / 取消 / 继续入口。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";

const mocks = vi.hoisted(() => ({
  api: {
    fetchBoardState: vi.fn(),
    fetchVisibleRange: vi.fn(),
    fetchIntents: vi.fn(),
    fetchIntentViews: vi.fn(),
    fetchUnderstanding: vi.fn(),
    fetchExecutions: vi.fn(),
    fetchDrafts: vi.fn(),
    saveBoardState: vi.fn(),
    checkMaterialImpact: vi.fn(),
    submitBoard: vi.fn(),
    fetchAcceptance: vi.fn(),
    createIntents: vi.fn(),
    saveDrafts: vi.fn(),
    createDemoIntents: vi.fn(),
    approveIntent: vi.fn(),
    rejectIntent: vi.fn(),
    batchDecide: vi.fn(),
    updateIntentPreview: vi.fn(),
    advanceIntent: vi.fn(),
    pauseIntent: vi.fn(),
    resumeIntent: vi.fn(),
    cancelIntent: vi.fn(),
    previewMaterialImpact: vi.fn(),
    retryUnderstanding: vi.fn(),
  },
}));
vi.mock("../../services/interactive", () => mocks.api);

import InteractiveView from "../InteractiveView.vue";
import { useInteractiveStore } from "../../stores/interactive";
import { recordIntentBatch } from "../../interactive/approval";
import { VIEW_STUBS, byIm, textOf } from "./i2v2-a-harness";

const STAMP = "2026-10-25T00:00:00.000Z";

function emptyPayload() {
  return {
    board: { id: "board_default", title: "互动板面", seq: 1 },
    seq: 1,
    state: { boardId: "board_default", seq: 1, cards: [], groups: [], links: [], updatedAt: STAMP },
    submissions: [],
    drafts: { drafts: {}, updatedAt: null, rev: 1, keyVersions: {} },
  };
}

function view(overrides: Record<string, unknown> = {}) {
  return {
    intent_id: "i1",
    board_id: "board_default",
    version: 1,
    status: "proposed",
    title: "整理材料",
    depends_on: [],
    conflicts_with: [],
    authorization_valid: true,
    created_at: STAMP,
    updated_at: STAMP,
    ...overrides,
  };
}

function legacyIntent(overrides: Record<string, unknown> = {}) {
  return {
    id: "i1",
    boardId: "board_default",
    title: "整理材料",
    status: "pending",
    version: 1,
    createdAt: STAMP,
    updatedAt: STAMP,
    ...overrides,
  };
}

function mountView() {
  const stubs: Record<string, unknown> = { ...VIEW_STUBS };
  delete stubs.BoardToolbar; // 批量入口在真实工具栏里，这里必须挂真组件
  return mount(InteractiveView, { attachTo: document.body, global: { stubs: stubs as never } });
}

beforeEach(() => {
  // resetAllMocks：连 mockImplementationOnce 的排队一起清掉，避免上一个用例的
  // 未决 promise 被下一个用例的页面加载吃掉（跨用例串扰会让正确实现看起来像卡住）
  vi.resetAllMocks();
  setActivePinia(createPinia());
  document.body.innerHTML = "";
  Object.defineProperty(document, "visibilityState", { value: "visible", configurable: true });
  localStorage.clear();
  const api = mocks.api;
  api.fetchBoardState.mockResolvedValue(emptyPayload());
  api.fetchVisibleRange.mockResolvedValue({ visibleRange: { cardIds: [], groupIds: [] } });
  api.fetchIntents.mockResolvedValue({ intents: [], conflicts: [], batchAvailable: false, recovery: { paused: [] } });
  api.fetchIntentViews.mockResolvedValue({ available: true, source: "service", intents: [] });
  api.fetchUnderstanding.mockResolvedValue({ understandings: [] });
  api.fetchExecutions.mockResolvedValue({ executions: [] });
  api.fetchDrafts.mockResolvedValue({ drafts: {}, updatedAt: null, rev: 1, keyVersions: {} });
  api.saveDrafts.mockResolvedValue({ drafts: {}, updatedAt: null, rev: 1, keyVersions: {} });
  api.createIntents.mockResolvedValue({ created: [], demo: false });
  api.saveBoardState.mockResolvedValue({ ok: true, seq: 2, savedAt: STAMP, state: emptyPayload().state });
  api.checkMaterialImpact.mockResolvedValue({ ok: true, affected: [], impactConfirmationRequired: false });
});

describe("F03 任务入口：真实界面 + 准确空态", () => {
  it("点「任务」打开真实任务面板，没有任务时给出准确空态", async () => {
    const wrapper = mountView();
    await flushPromises();
    expect(byIm(wrapper, "tasks-entry").length).toBe(1);
    await byIm(wrapper, "tasks-entry")[0].trigger("click");
    await flushPromises();
    expect(byIm(wrapper, "task-center").length, "任务入口必须打开真实面板（基线只切开关）").toBe(1);
    expect(textOf(wrapper, "task-empty")).toContain("没有");
    // 面板自己会重新核实服务端事实
    expect(mocks.api.fetchIntentViews.mock.calls.length).toBeGreaterThanOrEqual(2);
    wrapper.unmount();
  });

  it("任务入口带上真实数量，并随服务端事实更新", async () => {
    mocks.api.fetchIntentViews.mockResolvedValue({
      available: true,
      source: "service",
      intents: [view({ intent_id: "i-run", status: "running", title: "写结论" })],
    });
    const wrapper = mountView();
    await flushPromises();
    expect(byIm(wrapper, "tasks-entry")[0].text()).toContain("1");
    wrapper.unmount();
  });
});

describe("F03 任务控制：暂停 / 继续 / 取消", () => {
  it("运行中任务可暂停；暂停后出现继续入口（复用现有执行控制接口）", async () => {
    let status = "running";
    const api = mocks.api;
    api.fetchIntentViews.mockImplementation(async () => ({
      available: true,
      source: "service",
      intents: [view({ intent_id: "i-run", status, title: "写结论" })],
    }));
    api.pauseIntent.mockImplementation(async () => {
      status = "paused";
      return { ok: true, execution: { id: "e1", intentId: "i-run", status: "paused", attempt: 1, createdAt: STAMP, updatedAt: STAMP } };
    });
    api.resumeIntent.mockImplementation(async () => {
      status = "running";
      return { ok: true, execution: { id: "e1", intentId: "i-run", status: "running", attempt: 2, createdAt: STAMP, updatedAt: STAMP } };
    });
    const wrapper = mountView();
    await flushPromises();
    await byIm(wrapper, "tasks-entry")[0].trigger("click");
    await flushPromises();

    const row = byIm(wrapper, "task-row");
    expect(row.length).toBe(1);
    expect(row[0].attributes("data-status")).toBe("running");
    await byIm(wrapper, "task-pause")[0].trigger("click");
    await flushPromises();
    expect(api.pauseIntent).toHaveBeenCalledWith("i-run");
    expect(byIm(wrapper, "task-row")[0].attributes("data-status")).toBe("paused");
    expect(byIm(wrapper, "task-resume").length).toBe(1);

    await byIm(wrapper, "task-resume")[0].trigger("click");
    await flushPromises();
    expect(api.resumeIntent).toHaveBeenCalledWith("i-run");
    expect(byIm(wrapper, "task-row")[0].attributes("data-status")).toBe("running");
    wrapper.unmount();
  });

  it("取消是「取消这条互动任务」，与聊天停止不是同一件事；失败有明确反馈", async () => {
    const api = mocks.api;
    api.fetchIntentViews.mockResolvedValue({
      available: true,
      source: "service",
      intents: [view({ intent_id: "i-run", status: "running", title: "写结论" })],
    });
    api.cancelIntent.mockRejectedValueOnce(new Error("服务端拒绝了这次取消"));
    const wrapper = mountView();
    await flushPromises();
    await byIm(wrapper, "tasks-entry")[0].trigger("click");
    await flushPromises();
    const cancelButton = byIm(wrapper, "task-cancel")[0];
    expect(cancelButton.text()).toContain("取消任务");
    await cancelButton.trigger("click");
    await flushPromises();
    expect(api.cancelIntent).toHaveBeenCalledWith("i-run");
    expect(textOf(wrapper, "task-notice")).toContain("服务端拒绝了这次取消");
    wrapper.unmount();
  });

  it("任务列表显示真实执行记录状态（不需要等下一次提交或整页刷新）", async () => {
    const api = mocks.api;
    api.fetchIntentViews.mockResolvedValue({
      available: true,
      source: "service",
      intents: [view({ intent_id: "i-run", status: "approved", title: "写结论" })],
    });
    api.fetchExecutions.mockResolvedValue({
      executions: [{ id: "e1", intentId: "i-run", status: "running", attempt: 1, createdAt: STAMP, updatedAt: STAMP }],
    });
    const wrapper = mountView();
    await flushPromises();
    await byIm(wrapper, "tasks-entry")[0].trigger("click");
    await flushPromises();
    expect(byIm(wrapper, "task-row")[0].attributes("data-execution")).toBe("running");
    expect(byIm(wrapper, "task-row")[0].text()).toContain("执行中");
    wrapper.unmount();
  });
});

describe("F03 批量入口与重复操作", () => {
  it("旧提交区的「打开批量审批」接到真实待确认界面（唯一状态是 tasksOpen）", async () => {
    recordIntentBatch("batch-a", ["i1", "i2", "i3", "i4"]);
    // 旧链的「本次理解产出的待审批」按 understandingId 归属；批量列表要求同一批 ≥4 条
    mocks.api.fetchIntents.mockResolvedValue({
      intents: ["i1", "i2", "i3", "i4"].map((id) =>
        legacyIntent({ id, title: "任务 " + id, understandingId: "u1" }),
      ),
      conflicts: [],
      batchAvailable: true,
      recovery: { paused: [] },
    });
    const wrapper = mountView();
    await flushPromises();
    const store = useInteractiveStore();
    store.understanding = { id: "u1", status: "understood" } as never;
    await flushPromises();
    const batchEntry = byIm(wrapper, "submit-open-batch");
    expect(batchEntry.length).toBe(1);
    await batchEntry[0].trigger("click");
    await flushPromises();
    expect(byIm(wrapper, "task-center").length).toBe(1);
    expect(byIm(wrapper, "task-pending").length).toBe(1);
    expect(byIm(wrapper, "task-pending-row").length).toBe(4);
    expect(byIm(wrapper, "task-batch-approve").length).toBe(1);
    // C02：可见承接界面由 tasksOpen 决定，批量入口不再写旧的 batchOpen 通道
    expect(store.tasksOpen).toBe(true);
    expect(store.taskCenterFocus).toBe("pending");
    expect(store.batchOpen).toBe(false);
    wrapper.unmount();
  });

  it("新链已有意图视图时，旧提交区不再叠同一对象的批准 / 拒绝按钮", async () => {
    mocks.api.fetchIntentViews.mockResolvedValue({
      available: true,
      source: "service",
      intents: [view({ intent_id: "i1", status: "proposed", title: "整理材料" })],
    });
    mocks.api.fetchIntents.mockResolvedValue({
      intents: [legacyIntent({ id: "i1" })],
      conflicts: [],
      batchAvailable: false,
      recovery: { paused: [] },
    });
    const wrapper = mountView();
    await flushPromises();
    expect(byIm(wrapper, "virtual-intent").length).toBe(1);
    expect(byIm(wrapper, "submit-approvals").length, "同一意图不能同时有两套审批按钮").toBe(0);
    wrapper.unmount();
  });

  it("拒绝能力保留在新意图详情里（不是被删掉）", async () => {
    mocks.api.fetchIntentViews.mockResolvedValue({
      available: true,
      source: "legacy",
      intents: [view({ intent_id: "i1", status: "proposed", raw_legacy_status: "pending" })],
    });
    mocks.api.rejectIntent.mockResolvedValue({ ok: true, intent: { id: "i1", status: "rejected" } });
    const wrapper = mountView();
    await flushPromises();
    const card = byIm(wrapper, "virtual-intent")[0];
    card.element.dispatchEvent(new MouseEvent("pointerdown", { clientX: 10, clientY: 10, bubbles: true }));
    window.dispatchEvent(new MouseEvent("pointerup", { clientX: 10, clientY: 10, bubbles: true }));
    await flushPromises();
    const reject = byIm(wrapper, "intent-reject");
    expect(reject.length, "新意图详情必须保留拒绝能力").toBe(1);
    await reject[0].trigger("click");
    await flushPromises();
    expect(mocks.api.rejectIntent).toHaveBeenCalledWith("i1");
    expect(textOf(wrapper, "intent-layer-notice")).toContain("拒绝");
    wrapper.unmount();
  });
});
