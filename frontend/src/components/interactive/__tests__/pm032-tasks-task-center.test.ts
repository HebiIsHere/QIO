/**
 * PM-QIO032 · i-tasks：任务面板（#18 / #07 / #56 / #74 / #84）。
 *
 * 这些用例只读真实 store 事实（intentViews / intents / executionError / 读取状态），
 * 不 mock 组件内部逻辑：面板必须自己从 store 的状态得出空态、分组计数、读取失败形态。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount, type VueWrapper } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";

vi.mock("../../../services/interactive", () => ({
  fetchBoardState: vi.fn(async () => ({ board: { id: "board_default", title: "板", seq: 1 }, seq: 1, state: { boardId: "board_default", seq: 1, cards: [], groups: [], links: [], updatedAt: "t" }, submissions: [], drafts: { drafts: {}, updatedAt: null, rev: 1, keyVersions: {} } })),
  fetchVisibleRange: vi.fn(async () => ({ visibleRange: { cardIds: [], groupIds: [] } })),
  fetchIntents: vi.fn(async () => ({ intents: [], conflicts: [], batchAvailable: false, recovery: { paused: [] } })),
  fetchIntentViews: vi.fn(async () => ({ available: true, source: "service", intents: [] })),
  fetchUnderstanding: vi.fn(async () => ({ understandings: [] })),
  fetchExecutions: vi.fn(async () => ({ executions: [] })),
  fetchDrafts: vi.fn(async () => ({ drafts: {}, updatedAt: null, rev: 1, keyVersions: {} })),
  saveDrafts: vi.fn(async () => ({ drafts: {}, updatedAt: null, rev: 1, keyVersions: {} })),
  saveBoardState: vi.fn(async () => ({ ok: true, seq: 2, savedAt: "t" })),
  checkMaterialImpact: vi.fn(async () => ({ ok: true, affected: [], impactConfirmationRequired: false })),
  submitBoard: vi.fn(),
  fetchAcceptance: vi.fn(),
  createIntents: vi.fn(),
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
  fetchRecoveryPreview: vi.fn(),
  recoverIntent: vi.fn(),
  fetchRetrySeries: vi.fn(),
  requestManualRetry: vi.fn(),
}));

import TaskCenterPanel from "../v2/TaskCenterPanel.vue";
import { useInteractiveStore } from "../../../stores/interactive";
import { useApprovalResultsStore } from "../../../stores/approvalResults";
import {
  batchAreaVisible,
  taskCountsSummary,
  taskEmptyText,
  taskReadFailures,
} from "../v2/taskCenter";

const STAMP = "2026-10-25T00:00:00.000Z";

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

function legacy(overrides: Record<string, unknown> = {}) {
  return { id: "l1", boardId: "board_default", title: "旧任务", status: "pending", version: 1, createdAt: STAMP, updatedAt: STAMP, ...overrides };
}

const mounted: VueWrapper[] = [];

function setup(storePatch: (store: ReturnType<typeof useInteractiveStore>) => void = () => {}) {
  setActivePinia(createPinia());
  document.body.innerHTML = "";
  const store = useInteractiveStore();
  store.intentViews = [] as never;
  store.intentViewsLoadState = "ok";
  store.intentViewsLoadedOnce = true;
  store.intentViewsLoadError = null;
  store.intents = [] as never;
  store.intentsLoadState = "ok";
  store.intentsLoadedOnce = true;
  store.intentsLoadError = null;
  store.executionError = null;
  store.executions = [] as never;
  storePatch(store);
  const refresh = vi.spyOn(store, "refreshAfterIntentAction").mockResolvedValue();
  const loadViews = vi.spyOn(store, "loadIntentViews").mockResolvedValue(true);
  const loadLegacy = vi.spyOn(store, "loadIntents").mockResolvedValue(true);
  const loadExec = vi.spyOn(store, "loadExecutions").mockResolvedValue();
  return { store, refresh, loadViews, loadLegacy, loadExec };
}

function mountPanel(props: Record<string, unknown> = {}) {
  const wrapper = mount(TaskCenterPanel, {
    props,
    attachTo: document.body,
    global: { plugins: [createPinia()] },
  });
  mounted.push(wrapper);
  return wrapper;
}

/** 组件里 useInteractiveStore() 取的是 active pinia；mount 前先激活同一 pinia。 */
function mountWithActiveStore(props: Record<string, unknown> = {}) {
  const wrapper = mount(TaskCenterPanel, { props, attachTo: document.body });
  mounted.push(wrapper);
  return wrapper;
}

beforeEach(() => {
  while (mounted.length) mounted.pop()?.unmount();
  document.body.innerHTML = "";
  localStorage.clear();
});

describe("#18 空任务 / 分组计数", () => {
  it("空任务只显示「暂无任务」，审批历史入口仍在", async () => {
    const { store } = setup();
    store.intentViewsLoadState = "ok";
    const wrapper = mountWithActiveStore();
    await flushPromises();
    expect(wrapper.get('[data-im="task-empty"]').text()).toBe("暂无任务");
    expect(wrapper.find('[data-im="task-paused"]').exists()).toBe(false);
    expect(wrapper.find('[data-im="task-pending"]').exists()).toBe(false);
    expect(wrapper.find('[data-im="task-tab-approval-results"]').exists(), "空任务也要保留审批历史分区").toBe(true);
    expect(wrapper.find('[data-im="task-counts"]').exists(), "全零不显示计数行").toBe(false);
  });

  it("有任务时只显示有内容的组及计数", async () => {
    const { store } = setup();
    store.intentViews = [
      view({ intent_id: "i-run", status: "running", title: "写结论" }),
      view({ intent_id: "i-done", status: "completed", title: "已收尾" }),
    ] as never;
    const wrapper = mountWithActiveStore();
    await flushPromises();
    const counts = wrapper.get('[data-im="task-counts"]').text();
    expect(counts).toContain("执行中 1");
    expect(counts).toContain("已结束 1");
    expect(counts).not.toContain("已暂停 0");
    expect(counts).not.toContain("待处理 0");
    expect(wrapper.find('[data-im="task-live"]').exists()).toBe(true);
    expect(wrapper.find('[data-im="task-finished"]').exists()).toBe(true);
    expect(wrapper.find('[data-im="task-paused"]').exists()).toBe(false);
  });

  it("纯逻辑：只拼非零分组；空态文案是「暂无任务」", () => {
    expect(taskEmptyText()).toBe("暂无任务");
    expect(taskCountsSummary({ total: 3, live: 1, paused: 0, waiting: 2, finished: 0, needsUser: 3 })).toBe("执行中 1 · 待处理 2");
    expect(taskCountsSummary({ total: 0, live: 0, paused: 0, waiting: 0, finished: 0, needsUser: 0 })).toBe("");
  });
});

describe("#07 任务读取失败：一处可重试 + 上次结果/读取失败", () => {
  it("首次失败显示「读取失败」，只给一处重试，且不谎称「暂无任务」", async () => {
    const { store, loadViews, refresh } = setup();
    store.intentViewsLoadState = "error";
    store.intentViewsLoadedOnce = false;
    store.intentViewsLoadError = "没有连上本机后端";
    const wrapper = mountWithActiveStore();
    await flushPromises();
    refresh.mockClear();
    const errors = wrapper.findAll('[data-im="task-read-error"]');
    expect(errors.length, "同一次读取失败只出现一处错误").toBe(1);
    expect(wrapper.get('[data-im="task-read-marker"]').text()).toBe("读取失败");
    expect(errors[0].text()).toContain("没有连上本机后端");
    expect(wrapper.find('[data-im="task-empty"]').exists(), "读不到不等于没有任务").toBe(false);
    await wrapper.get('[data-im="task-read-retry"]').trigger("click");
    await flushPromises();
    expect(loadViews).toHaveBeenCalledTimes(1);
    expect(refresh, "单点重试只重试这一条读取").not.toHaveBeenCalled();
  });

  it("已有数据标「上次结果」，任务行仍在（只读展示上次事实）", async () => {
    const { store } = setup();
    store.intentViews = [view({ intent_id: "i-run", status: "running", title: "写结论" })] as never;
    store.intentViewsLoadState = "error";
    store.intentViewsLoadedOnce = true;
    store.intentViewsLoadError = "刷新超时";
    const wrapper = mountWithActiveStore();
    await flushPromises();
    expect(wrapper.get('[data-im="task-read-marker"]').text()).toBe("上次结果");
    expect(wrapper.findAll('[data-im="task-row"]').length).toBe(1);
  });

  it("多个真正独立的失败分别保留（任务记录 / 执行状态各自一条错误与重试）", async () => {
    const { store, loadExec, loadLegacy } = setup();
    store.intentsLoadState = "error";
    store.intentsLoadedOnce = false;
    store.intentsLoadError = "旧记录失败";
    store.executionError = "执行状态失败";
    store.executions = [] as never;
    const wrapper = mountWithActiveStore();
    await flushPromises();
    const failures = wrapper.findAll('[data-im="task-read-error"]').map((node) => node.attributes("data-failure"));
    expect(failures).toEqual(["legacy", "executions"]);
    await wrapper.get('[data-im="task-read-retry"][data-failure="executions"]').trigger("click");
    await flushPromises();
    expect(loadExec).toHaveBeenCalledTimes(1);
    await wrapper.get('[data-im="task-read-retry"][data-failure="legacy"]').trigger("click");
    await flushPromises();
    expect(loadLegacy).toHaveBeenCalledTimes(1);
  });

  it("纯逻辑：失败形态只由真实读取状态决定", () => {
    const first = taskReadFailures({ views: { state: "error", loadedOnce: false, error: "x" } });
    expect(first).toHaveLength(1);
    expect(first[0].marker).toBe("读取失败");
    expect(first[0].firstFailure).toBe(true);
    const stale = taskReadFailures({ views: { state: "error", loadedOnce: true, error: "x" } });
    expect(stale[0].marker).toBe("上次结果");
    expect(stale[0].firstFailure).toBe(false);
    expect(taskReadFailures({ views: { state: "ok", loadedOnce: true, error: null } })).toEqual([]);
  });
});

describe("#56 分区刷新：任务页与审批历史各刷自己的分区", () => {
  it("任务分区刷新任务事实；审批历史分区刷新历史查询", async () => {
    const { store, refresh } = setup();
    const wrapper = mountWithActiveStore();
    await flushPromises();
    refresh.mockClear();
    await wrapper.get('[data-im="task-refresh"]').trigger("click");
    await flushPromises();
    expect(refresh).toHaveBeenCalledTimes(1);

    const results = useApprovalResultsStore();
    vi.spyOn(results, "ensureFresh").mockImplementation(() => {});
    const loadHistory = vi.spyOn(results, "loadHistory").mockResolvedValue(true);
    await wrapper.get('[data-im="task-tab-approval-results"]').trigger("click");
    await flushPromises();
    await wrapper.get('[data-im="task-refresh"]').trigger("click");
    await flushPromises();
    expect(loadHistory).toHaveBeenCalledTimes(1);
    expect(refresh, "审批历史分区的刷新不碰任务读取").toHaveBeenCalledTimes(1);
  });
});

describe("#74 批量区门禁：只在批量选择且存在可处理待确认项时出现", () => {
  it("普通任务入口不显示批量区（也不显示勾选框）", async () => {
    const { store } = setup();
    store.intents = [legacy({ id: "l1" }), legacy({ id: "l2" })] as never;
    const wrapper = mountWithActiveStore();
    await flushPromises();
    expect(wrapper.find('[data-im="task-pending"]').exists()).toBe(true);
    expect(wrapper.find('[data-im="task-batch"]').exists()).toBe(false);
    expect(wrapper.find('[data-im="task-select"]').exists()).toBe(false);
  });

  it("批量入口 + 有可处理项：显示批量区与勾选框", async () => {
    const { store } = setup();
    store.intents = [legacy({ id: "l1" }), legacy({ id: "l2" })] as never;
    const wrapper = mountWithActiveStore({ focus: "pending" });
    await flushPromises();
    expect(wrapper.find('[data-im="task-batch"]').exists()).toBe(true);
    expect(wrapper.findAll('[data-im="task-select"]').length).toBe(2);
    expect(wrapper.find('[data-im="task-batch-approve"]').exists()).toBe(true);
  });

  it("批量入口但零个可处理项：批量区收起", async () => {
    const { store } = setup();
    store.intentViews = [view({ intent_id: "i1", status: "proposed", title: "待确认意图" })] as never;
    const wrapper = mountWithActiveStore({ focus: "pending" });
    await flushPromises();
    expect(wrapper.find('[data-im="task-pending"]').exists()).toBe(true);
    expect(wrapper.find('[data-im="task-batch"]').exists(), "零项时批量区必须收起").toBe(false);
  });

  it("纯逻辑：非批量选择 / 零项都不可见", () => {
    expect(batchAreaVisible("tasks", 5)).toBe(false);
    expect(batchAreaVisible("pending", 0)).toBe(false);
    expect(batchAreaVisible("pending", 2)).toBe(true);
  });
});

describe("#84 任务面板视觉层级：共享图标与次级按钮", () => {
  it("状态带共享图标，刷新/关闭复用共享图标按钮，任务名/状态/操作分层", async () => {
    const { store } = setup();
    store.intentViews = [view({ intent_id: "i-run", status: "running", title: "写结论" })] as never;
    const wrapper = mountWithActiveStore();
    await flushPromises();
    expect(wrapper.find('[data-im="task-status"] .qio-icon').exists(), "状态复用共享图标").toBe(true);
    expect(wrapper.get('[data-im="task-refresh"]').classes()).toContain("qio-icon-btn");
    expect(wrapper.get('[data-im="task-close"]').classes()).toContain("qio-icon-btn");
    expect(wrapper.find('[data-im="task-row"] .tc-name').exists()).toBe(true);
    expect(wrapper.find('[data-im="task-actions"]').exists()).toBe(true);
    expect(wrapper.get('[data-im="task-pause"]').classes()).toContain("tc-btn");
  });
});
