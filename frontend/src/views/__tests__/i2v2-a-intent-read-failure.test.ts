/**
 * F06 行为回归：意图读取失败时保留旧内容并可重试。
 *
 * 基线缺陷：InteractiveView.loadIntents 的 catch 直接把 intentViews 置空（既不提示也不保留），
 * 并把「读取失败」显示成空板面；板面读取失败后的「重试」只调 store.load()，不会重新读取意图。
 *
 * #07 口径（2026-11）：同一次任务读取失败**只在任务入口标记一次**，并到任务面板里重试；
 * 消息区不再重复一条同样的 intent-read-error 横幅。保留旧内容与「不回退成空」的断言不变。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
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
import { mountInteractiveView, byIm, textOf } from "./i2v2-a-harness";

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
});

describe("F06 意图读取失败：保留旧内容 + 明确提示 + 重试", () => {
  it("刷新失败保留最后一次成功内容，只在任务入口标记一次失败", async () => {
    mocks.api.fetchIntentViews.mockResolvedValueOnce({ available: true, source: "service", intents: [view()] });
    const wrapper = mountInteractiveView();
    await flushPromises();
    const store = useInteractiveStore();
    expect(byIm(wrapper, "virtual-intent").length).toBe(1);

    mocks.api.fetchIntentViews.mockRejectedValueOnce(new Error("连接被重置"));
    await store.loadIntentViews();
    await flushPromises();
    expect(store.intentViews.length, "读取失败不能清掉最后一次成功内容").toBe(1);
    expect(byIm(wrapper, "virtual-intent").length).toBe(1);
    // #07：任务入口带失败标记；消息区不再重复同一失败横幅。
    expect(byIm(wrapper, "tasks-entry-error").length).toBe(1);
    expect(byIm(wrapper, "intent-read-error").length).toBe(0);
    expect(byIm(wrapper, "intent-read-retry").length).toBe(0);

    mocks.api.fetchIntentViews.mockResolvedValue({ available: true, source: "service", intents: [view({ title: "新标题" })] });
    await store.loadIntentViews();
    await flushPromises();
    expect(byIm(wrapper, "tasks-entry-error").length).toBe(0);
    expect(byIm(wrapper, "virtual-intent")[0].text()).toContain("新标题");
    wrapper.unmount();
  });

  it("首次读取失败与「真的没有意图」必须区分开", async () => {
    mocks.api.fetchIntentViews.mockRejectedValueOnce(new Error("后端还没起来"));
    const wrapper = mountInteractiveView();
    await flushPromises();
    // #07：入口标记存在且只有一个；失败（还没成功读到过）不等于「没有意图」。
    expect(byIm(wrapper, "tasks-entry-error").length).toBe(1);
    expect(byIm(wrapper, "intent-read-error").length).toBe(0);

    mocks.api.fetchIntentViews.mockResolvedValue({ available: true, source: "service", intents: [] });
    await useInteractiveStore().loadIntentViews();
    await flushPromises();
    expect(byIm(wrapper, "intent-read-error").length).toBe(0);
    expect(byIm(wrapper, "tasks-entry-error").length).toBe(0);
    // 成功返回空集合才是真的空：任务面板据此显示空态（#18 文案「暂无任务」）
    await byIm(wrapper, "tasks-entry")[0].trigger("click");
    await flushPromises();
    // 空态措辞由 i-tasks 按 BRIEF #18 统一为「暂无任务」：精确锁死，避免回退成「没有」
    expect(textOf(wrapper, "task-empty")).toBe("暂无任务");
    wrapper.unmount();
  });

  it("旧失败响应不清掉后续成功读取的新内容（板面切换后的迟到失败）", async () => {
    const store = useInteractiveStore();
    let rejectOld: (reason: unknown) => void = () => undefined;
    mocks.api.fetchIntentViews.mockImplementationOnce(
      () => new Promise((_resolve, reject) => { rejectOld = reject as (reason: unknown) => void; }),
    );
    // 板面 A 的读取在飞
    const oldRead = store.loadIntentViews();
    await flushPromises();
    // 板面切到 B 并成功读到内容
    store.boardId = "board_other";
    mocks.api.fetchIntentViews.mockResolvedValue({
      available: true,
      source: "service",
      intents: [view({ title: "新内容" })],
    });
    await store.loadIntentViews();
    await flushPromises();
    // 板面 A 的旧请求这时才失败：不许把 B 的内容清掉，也不许把状态改成失败
    rejectOld(new Error("旧请求失败"));
    await oldRead;
    await flushPromises();
    expect(store.intentViews.map((item) => item.title)).toEqual(["新内容"]);
    expect(store.intentViewsLoadState).toBe("ok");
    expect(store.intentViewsLoadError).toBeNull();
  });

  it("板面读取失败后的「重试」同时恢复意图读取", async () => {
    mocks.api.fetchBoardState.mockRejectedValueOnce(new Error("板面读取失败"));
    const wrapper = mountInteractiveView();
    await flushPromises();
    /*
     * #20：基线 store 在 load 失败时会放一块空板面（没有 boardLoadedOnce 字段），
     * 所以这里等价于「有上次数据 → 只读展示 + 重试」这条路径；「首次失败 → 重试 / 返回对话」
     * 由 i-state 的 boardLoadedOnce=false 在合并后区分（页面壳已实现两个入口，见 pm032-layout-shell）。
     */
    expect(byIm(wrapper, "load-error").length).toBe(1);
    expect(byIm(wrapper, "board-retry").length).toBe(1);
    mocks.api.fetchIntentViews.mockClear();
    await byIm(wrapper, "board-retry")[0].trigger("click");
    await flushPromises();
    expect(mocks.api.fetchBoardState).toHaveBeenCalledTimes(2);
    expect(mocks.api.fetchIntentViews.mock.calls.length, "重试必须同时恢复意图读取").toBeGreaterThanOrEqual(1);
    wrapper.unmount();
  });
});

describe("F06 数据可疑时确认 / 恢复先核实", () => {
  it("意图清单读取失败时，确认与恢复暂停并说明原因；重试读取成功后恢复可用", async () => {
    mocks.api.fetchIntentViews.mockResolvedValueOnce({ available: true, source: "service", intents: [view()] });
    const wrapper = mountInteractiveView();
    await flushPromises();
    const store = useInteractiveStore();
    mocks.api.fetchIntentViews.mockRejectedValueOnce(new Error("读取失败"));
    await store.loadIntentViews();
    await flushPromises();

    const card = byIm(wrapper, "virtual-intent")[0];
    card.element.dispatchEvent(new MouseEvent("pointerdown", { clientX: 10, clientY: 10, bubbles: true }));
    window.dispatchEvent(new MouseEvent("pointerup", { clientX: 10, clientY: 10, bubbles: true }));
    await flushPromises();
    expect(byIm(wrapper, "intent-stale").length).toBe(1);
    const confirmButton = byIm(wrapper, "intent-confirm")[0];
    expect(confirmButton.attributes("disabled")).toBeDefined();

    mocks.api.fetchIntentViews.mockResolvedValue({ available: true, source: "service", intents: [view()] });
    await byIm(wrapper, "intent-stale-retry")[0].trigger("click");
    await flushPromises();
    expect(byIm(wrapper, "intent-stale").length).toBe(0);
    expect(byIm(wrapper, "intent-confirm")[0].attributes("disabled")).toBeUndefined();
    wrapper.unmount();
  });
});
