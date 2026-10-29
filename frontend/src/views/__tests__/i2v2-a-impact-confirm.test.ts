/**
 * F01 行为回归：保存前的影响确认必须是**页面上真实可见、可确认、可取消**的流程。
 *
 * 基线缺陷：InteractiveView.vue 只挂了统一审批层，store.saveNow 产生的 pendingImpact
 * 没有任何界面承接 —— 用户看到的是「有改动尚未保存」但永远没有确认入口（死等待），
 * 旧的 ImpactConfirmDialog 已实现但没有挂载。
 */
import { beforeEach, describe, expect, it, vi, afterEach } from "vitest";
import { flushPromises } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";

const mocks = vi.hoisted(() => {
  const state = {
    boardCalls: 0,
    saveBoardState: vi.fn(),
    checkMaterialImpact: vi.fn(),
    fetchBoardState: vi.fn(),
    fetchVisibleRange: vi.fn(),
    fetchIntents: vi.fn(),
    fetchIntentViews: vi.fn(),
    fetchUnderstanding: vi.fn(),
    fetchExecutions: vi.fn(),
    fetchDrafts: vi.fn(),
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
  };
  return { state, api: state };
});
vi.mock("../../services/interactive", () => mocks.api);

import { useInteractiveStore } from "../../stores/interactive";
import { mountInteractiveView, byIm, textOf } from "./i2v2-a-harness";

const STAMP = "2026-10-25T00:00:00.000Z";

function card(overrides: Record<string, unknown> = {}) {
  return {
    id: "c1",
    kind: "text",
    content: "原文",
    x: 0,
    y: 0,
    w: 200,
    h: 120,
    checked: true,
    hidden: false,
    deleted: false,
    bookmarked: false,
    participation: "included",
    version: 1,
    createdAt: STAMP,
    updatedAt: STAMP,
    ...overrides,
  };
}

function boardState(content = "原文") {
  return {
    boardId: "board_default",
    seq: 1,
    cards: [card({ content })],
    groups: [],
    links: [],
    updatedAt: STAMP,
  };
}

function boardPayload(content = "原文") {
  return {
    board: { id: "board_default", title: "互动板面", seq: 1 },
    seq: 1,
    state: boardState(content),
    submissions: [],
    drafts: { drafts: {}, updatedAt: null, rev: 1, keyVersions: {} },
  };
}

function runningIntent(id = "i-run", title = "整理材料") {
  return {
    id,
    boardId: "board_default",
    title,
    status: "running",
    version: 1,
    createdAt: STAMP,
    updatedAt: STAMP,
  };
}

const AFFECTED = {
  intentId: "i-run",
  title: "整理材料",
  materials: ["c1"],
  consequence: "这次保存会让该任务暂停并保留进度",
};

afterEach(async () => {
  // 测试隔离：先让上一个用例的异步更新（页面加载 / 保存链 / 定时器）落地，再清空挂载点；
  // 否则残留更新会进入下一个用例，造成与产品无关的顺序依赖。
  await flushPromises();
  document.body.innerHTML = "";
});

beforeEach(() => {
  // resetAllMocks：连 mockImplementationOnce 的排队一起清掉，避免上一个用例的
  // 未决 promise 被下一个用例的页面加载吃掉（跨用例串扰会让正确实现看起来像卡住）
  vi.resetAllMocks();
  setActivePinia(createPinia());
  document.body.innerHTML = "";
  Object.defineProperty(document, "visibilityState", { value: "visible", configurable: true });
  localStorage.clear();
  const api = mocks.api;
  api.fetchBoardState.mockResolvedValue(boardPayload());
  api.fetchVisibleRange.mockResolvedValue({ visibleRange: { cardIds: ["c1"], groupIds: [] } });
  api.fetchIntents.mockResolvedValue({ intents: [], conflicts: [], batchAvailable: false, recovery: { paused: [] } });
  api.fetchIntentViews.mockResolvedValue({ available: true, source: "service", intents: [] });
  api.fetchUnderstanding.mockResolvedValue({ understandings: [] });
  api.fetchExecutions.mockResolvedValue({ executions: [] });
  api.fetchDrafts.mockResolvedValue({ drafts: {}, updatedAt: null, rev: 1, keyVersions: {} });
  api.saveDrafts.mockResolvedValue({ drafts: {}, updatedAt: null, rev: 1, keyVersions: {} });
  api.createIntents.mockResolvedValue({ created: [], demo: false });
  api.checkMaterialImpact.mockResolvedValue({
    ok: true,
    checkId: "chk-1",
    stateVersion: 2,
    affected: [AFFECTED],
    impactConfirmationRequired: true,
  });
  api.saveBoardState.mockResolvedValue({
    ok: true,
    seq: 2,
    savedAt: STAMP,
    state: boardState("新内容"),
  });
});

async function openImpactConfirm() {
  const wrapper = mountInteractiveView();
  await flushPromises();
  const store = useInteractiveStore();
  store.intents = [runningIntent()] as never;
  // 一次真实的板面改动：正文被编辑 → 保存前预判会命中受影响任务
  store.commit(boardState("新内容") as never, "编辑正文");
  await store.saveNow();
  await flushPromises();
  return { wrapper, store };
}

describe("F01 影响确认：真实展示 → 确认 → 以回执为准", () => {
  it("改动影响执行中任务时，页面展示真实受影响任务、材料与后果，并明确「等待确认，尚未保存」", async () => {
    const { wrapper, store } = await openImpactConfirm();
    const dialog = byIm(wrapper, "impact-confirm");
    expect(dialog.length, "影响确认必须渲染在页面上（基线没有任何承接界面）").toBe(1);
    const text = dialog[0].text();
    expect(text).toContain("等待确认，尚未保存");
    expect(text).toContain("整理材料");
    expect(text).toContain("c1");
    expect(text).toContain("暂停并保留进度");
    // 等待确认时不许说已保存，也没有发出写入
    expect(text).not.toContain("已保存");
    expect(mocks.api.saveBoardState).not.toHaveBeenCalled();
    // 两个决定入口都在
    expect(byIm(wrapper, "impact-confirm-submit").length).toBe(1);
    expect(byIm(wrapper, "impact-confirm-cancel").length).toBe(1);
    expect(store.saveStatus).toBe("idle");
    wrapper.unmount();
  });

  it("确认后以真实保存回执为准：带 checkId 保存、回执成功才说已保存", async () => {
    const { wrapper, store } = await openImpactConfirm();
    await byIm(wrapper, "impact-confirm-submit")[0].trigger("click");
    await flushPromises();
    expect(mocks.api.saveBoardState).toHaveBeenCalledTimes(1);
    const [, , , confirm] = mocks.api.saveBoardState.mock.calls[0];
    expect(confirm).toEqual({ checkId: "chk-1", stateVersion: 2 });
    expect(store.pendingImpact).toBeNull();
    expect(store.saveStatus).toBe("saved");
    // v-show：窗口 DOM 保留（便于恢复状态），确认后不再显示
    expect(byIm(wrapper, "impact-confirm-scrim")[0].isVisible()).toBe(false);
    const outcome = textOf(wrapper, "impact-confirm-outcome");
    expect(outcome).toContain("改动已保存生效");
    wrapper.unmount();
  });

  it("保存失败时保留用户输入、如实说明失败，并给重试入口（不显示保存成功）", async () => {
    const { wrapper, store } = await openImpactConfirm();
    mocks.api.saveBoardState.mockRejectedValueOnce(Object.assign(new Error("写入失败：磁盘只读"), { status: 500 }));
    await byIm(wrapper, "impact-confirm-submit")[0].trigger("click");
    await flushPromises();
    expect(store.saveStatus).toBe("error");
    // 用户这次改动仍在候选里（没有静默丢弃）
    expect(store.board?.cards[0]?.content).toBe("新内容");
    expect(store.dirty).toBe(true);
    const outcome = textOf(wrapper, "impact-confirm-outcome");
    expect(outcome).toContain("没有保存");
    expect(outcome).not.toContain("已保存生效");
    expect(byIm(wrapper, "impact-confirm-retry").length).toBe(1);
    wrapper.unmount();
  });

  it("确认过期 / 影响范围变化：重新核实后重新展示范围，不自动批准、不自动重发", async () => {
    const { wrapper, store } = await openImpactConfirm();
    mocks.api.saveBoardState.mockRejectedValueOnce(
      Object.assign(new Error("check expired"), {
        status: 409,
        payload: {
          error: "stale_check",
          reason: "板面版本已变化",
          scopeChanged: true,
          affectedTasks: [
            AFFECTED,
            { intentId: "i-new", title: "新加入的任务", materials: ["c2"], consequence: "也会被暂停" },
          ],
        },
      }),
    );
    mocks.api.checkMaterialImpact.mockResolvedValueOnce({
      ok: true,
      checkId: "chk-2",
      stateVersion: 2,
      affected: [AFFECTED],
      impactConfirmationRequired: true,
    });
    await byIm(wrapper, "impact-confirm-submit")[0].trigger("click");
    await flushPromises();
    // 没有自动重发写入：只发生了那一次被拒绝的 PUT
    expect(mocks.api.saveBoardState).toHaveBeenCalledTimes(1);
    expect(store.pendingImpact?.checkId).toBe("chk-2");
    // 服务端列出的完整范围 + 重新预判的范围取并集，用户按新范围重新确认
    const ids = (store.pendingImpact?.affected ?? []).map((item) => item.intentId).sort();
    expect(ids).toEqual(["i-new", "i-run"]);
    expect(textOf(wrapper, "impact-confirm-note").length).toBeGreaterThan(0);
    expect(byIm(wrapper, "impact-confirm").length).toBe(1);
    expect(store.saveStatus).not.toBe("saved");
    wrapper.unmount();
  });

  it("取消本次改动：不保存、任务继续；撤回未被服务器确认时如实说明并可重新核对", async () => {
    const { wrapper, store } = await openImpactConfirm();
    await byIm(wrapper, "impact-confirm-cancel")[0].trigger("click");
    await flushPromises();
    expect(mocks.api.saveBoardState).not.toHaveBeenCalled();
    expect(store.pendingImpact).toBeNull();
    expect(store.board?.cards[0]?.content).toBe("原文");
    const outcome = textOf(wrapper, "impact-confirm-outcome");
    expect(outcome).toContain("取消");
    wrapper.unmount();
  });
});

describe("F01 顶栏保存状态：等待确认时不得显示「已保存」", () => {
  it("pendingImpact 时保存状态是「等待确认，尚未保存」，失败/已保存各自如实", async () => {
    const { saveStatusView } = await import("../../components/interactive/v2/saveStatus");
    expect(
      saveStatusView({ status: "saved", dirty: true, error: null, pendingImpact: true }).text,
    ).toBe("等待确认，尚未保存");
    // 没有影响确认时行为不变
    expect(saveStatusView({ status: "saved", dirty: false, error: null }).text).toBe("已保存");
    expect(saveStatusView({ status: "saving", dirty: true, error: null }).text).toBe("正在保存…");
  });
});

describe("F01 影响确认与统一审批：不叠两个阻塞面、互不替代", () => {
  it("关闭询问挂起时让位（隐藏但不卸载），恢复后影响确认回来", async () => {
    const { wrapper } = await openImpactConfirm();
    const { useModalArbiterStore } = await import("../../stores/modalArbiter");
    const arbiter = useModalArbiterStore();
    arbiter.suspendAll("close-ask");
    await flushPromises();
    expect(byIm(wrapper, "impact-confirm-scrim")[0].isVisible()).toBe(false);
    expect(byIm(wrapper, "impact-confirm-deferred").length).toBe(1);
    arbiter.resumeAll();
    await flushPromises();
    expect(byIm(wrapper, "impact-confirm-scrim")[0].isVisible()).toBe(true);
    wrapper.unmount();
  });

  it("影响确认等待期间不再自动弹出工具/权限审批（同一时刻只有一个阻塞确认）", async () => {
    const { wrapper, store } = await openImpactConfirm();
    expect(store.pendingImpact).not.toBeNull();
    const { useUnifiedApprovalsStore } = await import("../../stores/unifiedApprovals");
    const unified = useUnifiedApprovalsStore();
    const spy = vi.spyOn(unified, "maybeAutoOpen");
    const { mount } = await import("@vue/test-utils");
    const UnifiedApprovalLayer = (await import("../../components/conversation/UnifiedApprovalLayer.vue")).default;
    const layer = mount(UnifiedApprovalLayer, { attachTo: document.body });
    (layer.vm as unknown as { retryAutoOpen: () => void }).retryAutoOpen();
    expect(spy).not.toHaveBeenCalled();
    layer.unmount();
    wrapper.unmount();
  });
});
