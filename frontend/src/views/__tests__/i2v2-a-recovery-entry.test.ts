/**
 * F05 行为回归：恢复事项入口不依赖面板挂载。
 *
 * 基线缺陷：InteractiveView 的恢复入口数量来自 RecoveryPanel 的 @updated 回传，并且
 * count=Math.max(fallback, recoveryPanelCount) 只增不减；RecoveryPanel 只在对话展开时
 * 以 active=true 挂载（页面传 :active="store.chatOpen"）—— 对话收起时入口拿不到服务端
 * 事项，点开面板也不会加载（读取依赖消息区已展开）。
 */
import { beforeEach, describe, expect, it, vi, afterEach } from "vitest";
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
    fetchRecoveryPreview: vi.fn(),
    recoverIntent: vi.fn(),
    fetchRetrySeries: vi.fn(),
    requestManualRetry: vi.fn(),
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

function recoverableView() {
  return {
    intent_id: "i-paused",
    board_id: "board_default",
    version: 2,
    status: "paused",
    title: "暂停的任务",
    authorization_valid: true,
    created_at: STAMP,
    updated_at: STAMP,
  };
}

function recoveryEntry(wrapper: ReturnType<typeof mountInteractiveView>) {
  return wrapper.findAll('[data-extra="1"][data-entry="recovery"]');
}

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
  api.fetchBoardState.mockResolvedValue(emptyPayload());
  api.fetchVisibleRange.mockResolvedValue({ visibleRange: { cardIds: [], groupIds: [] } });
  api.fetchIntents.mockResolvedValue({ intents: [], conflicts: [], batchAvailable: false, recovery: { paused: [] } });
  api.fetchIntentViews.mockResolvedValue({ available: true, source: "service", intents: [] });
  api.fetchUnderstanding.mockResolvedValue({ understandings: [] });
  api.fetchExecutions.mockResolvedValue({ executions: [] });
  api.fetchDrafts.mockResolvedValue({ drafts: {}, updatedAt: null, rev: 1, keyVersions: {} });
  api.saveDrafts.mockResolvedValue({ drafts: {}, updatedAt: null, rev: 1, keyVersions: {} });
  api.fetchRecoveryPreview.mockResolvedValue({ available: true, preview: { intent_id: "i-paused", can_resume: true } });
});

describe("F05 恢复入口：不依赖面板挂载", () => {
  it("对话收起时入口仍然显示；点开后真的加载服务端恢复事项", async () => {
    mocks.api.fetchIntentViews.mockResolvedValue({ available: true, source: "service", intents: [recoverableView()] });
    const wrapper = mountInteractiveView();
    await flushPromises();
    const store = useInteractiveStore();
    expect(store.chatOpen).toBe(false);
    expect(recoveryEntry(wrapper).length, "有真实事项时入口必须出现在输入区事项行").toBe(1);
    expect(recoveryEntry(wrapper)[0].find(".count").text()).toBe("1");

    await recoveryEntry(wrapper)[0].trigger("click");
    await flushPromises();
    expect(byIm(wrapper, "p3c-recovery-panel").length).toBe(1);
    const intentRows = wrapper.findAll('[data-im="p3c-recovery-intent"]');
    expect(intentRows.length, "面板内容不能依赖消息区已展开").toBe(1);
    expect(intentRows[0].text()).toContain("暂停的任务");
    wrapper.unmount();
  });

  it("读取失败不伪装成「没有事项」：入口给出读取失败状态并可重试", async () => {
    mocks.api.fetchIntentViews.mockRejectedValue(new Error("板面服务暂时不可用"));
    const wrapper = mountInteractiveView();
    await flushPromises();
    const notice = byIm(wrapper, "recovery-read-error");
    expect(notice.length).toBe(1);
    expect(notice[0].text()).toContain("不代表没有事项");
    expect(byIm(wrapper, "recovery-retry").length).toBe(1);
    wrapper.unmount();
  });

  it("相同事项按稳定身份去重，处理后计数立刻减少（不保留历史最大数量）", async () => {
    const wrapper = mountInteractiveView();
    await flushPromises();
    const store = useInteractiveStore();
    store.draftConflicts = {
      "card:c1": { local: "本页", server: "服务器" },
      "card:c2": { local: "本页", server: "服务器" },
    } as never;
    await flushPromises();
    expect(recoveryEntry(wrapper)[0].find(".count").text()).toBe("2");
    const ids = store.recoveryItems.map((item) => item.id);
    expect(new Set(ids).size).toBe(ids.length);

    store.draftConflicts = { "card:c1": { local: "本页", server: "服务器" } } as never;
    await flushPromises();
    expect(recoveryEntry(wrapper)[0].find(".count").text()).toBe("1");
    wrapper.unmount();
  });

  it("重新进入页面仍能发现未处理的服务端恢复事项", async () => {
    mocks.api.fetchIntentViews.mockResolvedValue({ available: true, source: "service", intents: [recoverableView()] });
    const first = mountInteractiveView();
    await flushPromises();
    expect(recoveryEntry(first).length).toBe(1);
    first.unmount();
    document.body.innerHTML = "";

    const second = mountInteractiveView();
    await flushPromises();
    expect(recoveryEntry(second).length).toBe(1);
    expect(recoveryEntry(second)[0].find(".count").text()).toBe("1");
    second.unmount();
  });
});
