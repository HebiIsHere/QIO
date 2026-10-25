/**
 * F02 行为回归：提交 → 理解 → 意图确认 → 执行的前端同步。
 *
 * 基线缺陷：意图视图由页面本地 intentViews 维护（仅在 lastSubmission.submission.id 变化时刷新），
 * store 的意图清单是另一份 —— 理解延迟返回时新意图不会出现（必须重进页面），
 * 乱序响应没有代次保护，页面卸载后 store 的理解轮询仍在跑。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises } from "@vue/test-utils";
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

import { useInteractiveStore } from "../../stores/interactive";
import { mountInteractiveView, byIm, setVisibility } from "./i2v2-a-harness";

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

function intentView(overrides: Record<string, unknown> = {}) {
  return {
    intent_id: "i1",
    board_id: "board_default",
    version: 1,
    status: "proposed",
    title: "整理材料",
    goal: "整理成清单",
    expected: "得到清单",
    depends_on: [],
    conflicts_with: [],
    authorization_valid: true,
    created_at: STAMP,
    updated_at: STAMP,
    ...overrides,
  };
}

function understanding(status = "understood", submissionId = "s1") {
  return {
    understanding: { id: "u1", status, submissionId, createdAt: STAMP },
    understandings: [{ id: "u1", status, submissionId, createdAt: STAMP }],
  };
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
  api.previewMaterialImpact.mockResolvedValue({ affected: [] });
  api.checkMaterialImpact.mockResolvedValue({ ok: true, affected: [], impactConfirmationRequired: false });
  api.saveBoardState.mockResolvedValue({ ok: true, seq: 2, savedAt: STAMP, state: emptyPayload().state });
});

afterEach(() => {
  vi.useRealTimers();
});

describe("F02 延迟理解：新意图自动出现在页面上（不需要重新进入）", () => {
  it("理解迟到返回时，意图卡自动出现，且没有重复创建任务", async () => {
    vi.useFakeTimers();
    const wrapper = mountInteractiveView();
    await flushPromises();
    const store = useInteractiveStore();
    store.submitStatus = "succeeded";
    store.lastSubmission = { submission: { id: "s1" }, status: "succeeded" } as never;
    store.understandingStatus = "accepted";
    mocks.api.fetchUnderstanding.mockResolvedValue(understanding("understood", "s1"));
    mocks.api.fetchIntents.mockResolvedValue({
      intents: [{ id: "i1", title: "整理材料", status: "pending", version: 1 }],
      conflicts: [],
      batchAvailable: false,
      recovery: { paused: [] },
    });
    mocks.api.fetchIntentViews.mockResolvedValue({ available: true, source: "service", intents: [intentView()] });

    void store.startUnderstandingPolling();
    await vi.advanceTimersByTimeAsync(1200);
    await flushPromises();

    expect(store.understandingStatus).toBe("understood");
    expect(byIm(wrapper, "virtual-intent").length, "理解迟到返回后意图卡必须出现").toBe(1);
    expect(mocks.api.createIntents).toHaveBeenCalledTimes(1);
    expect(store.intentViews.map((view) => view.intent_id)).toEqual(["i1"]);
    wrapper.unmount();
  });

  it("手动刷新理解也接回同一套状态更新：意图卡出现，不需要再一次提交", async () => {
    const wrapper = mountInteractiveView();
    await flushPromises();
    const store = useInteractiveStore();
    store.submitStatus = "succeeded";
    store.lastSubmission = { submission: { id: "s1" }, status: "succeeded" } as never;
    store.understandingStatus = "accepted";
    await flushPromises();
    mocks.api.fetchUnderstanding.mockResolvedValue(understanding("understood", "s1"));
    mocks.api.fetchIntents.mockResolvedValue({
      intents: [{ id: "i1", title: "整理材料", status: "pending", version: 1 }],
      conflicts: [],
      batchAvailable: false,
      recovery: { paused: [] },
    });
    mocks.api.fetchIntentViews.mockResolvedValue({ available: true, source: "service", intents: [intentView()] });

    // 用户点 HUD 上的「刷新理解状态」（与自动轮询同一条状态更新链路）
    const refresh = byIm(wrapper, "understanding-refresh");
    expect(refresh.length).toBe(1);
    await refresh[0].trigger("click");
    await flushPromises();

    expect(store.understandingStatus).toBe("understood");
    expect(byIm(wrapper, "virtual-intent").length).toBe(1);
    expect(mocks.api.submitBoard).not.toHaveBeenCalled();
    // 既有契约：刷新不会重复创建意图
    expect(mocks.api.createIntents).not.toHaveBeenCalled();
    wrapper.unmount();
  });
});

describe("F02 意图状态唯一维护位置与乱序保护", () => {
  it("意图视图由 store 维护：页面显示的意图来自 store.intentViews", async () => {
    const wrapper = mountInteractiveView();
    await flushPromises();
    const store = useInteractiveStore();
    mocks.api.fetchIntentViews.mockResolvedValue({ available: true, source: "service", intents: [intentView()] });
    await store.loadIntentViews();
    await flushPromises();
    expect(store.intentViews.length).toBe(1);
    expect(byIm(wrapper, "virtual-intent").length).toBe(1);
    wrapper.unmount();
  });

  it("同一时刻只允许一个在飞读取；旧板面的迟到回执不能写进当前板面", async () => {
    const store = useInteractiveStore();
    let resolveOld: (value: unknown) => void = () => undefined;
    mocks.api.fetchIntentViews.mockImplementationOnce(
      () => new Promise((resolve) => { resolveOld = resolve as (value: unknown) => void; }),
    );
    const first = store.loadIntentViews();
    const second = store.loadIntentViews();
    await flushPromises();
    // 连点 / 多条路径同时刷新：复用同一份在飞读取，不打出第二个请求
    expect(mocks.api.fetchIntentViews).toHaveBeenCalledTimes(1);

    // 板面切走：旧板面的回执落地时代次与身份都对不上，必须丢弃
    store.boardId = "board_other";
    resolveOld({ available: true, source: "service", intents: [intentView({ title: "旧板面" })] });
    await Promise.all([first, second]);
    await flushPromises();
    expect(store.intentViews).toEqual([]);
    expect(store.intentViewsLoadState).not.toBe("ok");
  });
});

describe("F02 恢复与清理：回前台重新核实、卸载停止轮询、不重复创建/执行", () => {
  it("窗口回到前台时重新核实事实，但不重复创建任务或发起执行", async () => {
    const wrapper = mountInteractiveView();
    await flushPromises();
    const store = useInteractiveStore();
    store.submitStatus = "succeeded";
    store.lastSubmission = { submission: { id: "s1" }, status: "succeeded" } as never;
    store.understandingStatus = "understood";
    const before = mocks.api.fetchIntentViews.mock.calls.length;
    mocks.api.fetchIntentViews.mockClear();

    setVisibility("hidden");
    setVisibility("visible");
    await flushPromises();
    expect(mocks.api.fetchIntentViews.mock.calls.length).toBeGreaterThanOrEqual(1);
    expect(before).toBeGreaterThanOrEqual(0);
    expect(mocks.api.createIntents).not.toHaveBeenCalled();
    expect(mocks.api.pauseIntent).not.toHaveBeenCalled();
    expect(mocks.api.cancelIntent).not.toHaveBeenCalled();
    wrapper.unmount();
  });

  it("页面卸载后不再继续轮询理解状态（计时器清理）", async () => {
    vi.useFakeTimers();
    const wrapper = mountInteractiveView();
    await flushPromises();
    const store = useInteractiveStore();
    store.submitStatus = "succeeded";
    store.lastSubmission = { submission: { id: "s1" }, status: "succeeded" } as never;
    store.understandingStatus = "accepted";
    mocks.api.fetchUnderstanding.mockResolvedValue(understanding("understanding", "s1"));
    void store.startUnderstandingPolling();
    await vi.advanceTimersByTimeAsync(1200);
    await flushPromises();
    const callsBeforeUnmount = mocks.api.fetchUnderstanding.mock.calls.length;
    expect(callsBeforeUnmount).toBeGreaterThan(0);

    wrapper.unmount();
    await vi.advanceTimersByTimeAsync(5000);
    await flushPromises();
    expect(mocks.api.fetchUnderstanding.mock.calls.length).toBe(callsBeforeUnmount);
  });
});
