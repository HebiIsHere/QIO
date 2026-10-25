/**
 * Phase 3-C：重试面板 / 恢复面板 / 事项计数（契约 §3 / §6.3 / §6.4）。
 *
 * 钉住的行为：
 * - 重试：次数、上限、状态、last_error、next_action 全部来自服务端；手动重试一次点击
 *   只发一次 POST，随后**重新读取**服务端记录，本地绝不 ++ 次数、绝不重置自动次数；
 *   自动重试用尽只说明一次；服务端还没有接口时如实显示「能力未上线」；
 * - 恢复：可恢复意图只来自服务端视图（paused/failed/partially_completed 且授权有效）；
 *   恢复预览 / 恢复都调用真实服务并有真实回执文案；部分完成原样标注；
 * - 保存失败持续可见且可重试（调用既有 store 动作，不重复提交）。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount, type VueWrapper } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { nextTick } from "vue";
import RetryPanel from "../v2/RetryPanel.vue";
import RecoveryPanel from "../v2/RecoveryPanel.vue";
import {
  recoveryEntryVisible,
  recoveryItemCount,
  recoveryStoreCount,
} from "../v2/recoverySummary";
import { useInteractiveStore } from "../../../stores/interactive";
import { normalizeRetryEnvelope, type RetryPanelApi } from "../v2/retryApi";
import type { IntentView } from "../../../interactive/intentWorkbench";

let wrappers: VueWrapper[] = [];

function wrap<T extends VueWrapper>(w: T): T {
  wrappers.push(w);
  return w;
}

function retrySeries(overrides: Record<string, unknown> = {}) {
  return {
    series_id: "rs_1",
    intent_id: "intent_1",
    authorization_version: "auth_v2",
    attempts: 3,
    limit: 3,
    status: "exhausted",
    last_error: "工具调用超时",
    next_action: "等待你手动重试或结束这次任务",
    updated_at: "2026-10-24T03:00:00.000Z",
    ...overrides,
  };
}

function seriesEnvelope(series: Record<string, unknown>[]) {
  return normalizeRetryEnvelope({ available: true, series });
}

function fakeRetryApi(overrides: Partial<Record<keyof RetryPanelApi, unknown>> = {}): RetryPanelApi {
  return {
    fetchSeries: vi.fn(async () => seriesEnvelope([retrySeries()])),
    retry: vi.fn(async () => ({ ok: true, reason: "服务端已受理", series: null })),
    ...overrides,
  } as unknown as RetryPanelApi;
}

function view(overrides: Partial<IntentView> = {}): IntentView {
  return {
    intent_id: "intent_1",
    version: 3,
    status: "failed",
    title: "整理这次提交的材料",
    authorization_valid: true,
    operations_summary: { applied: 2, failed: 1 },
    summary: "失败前的改动已保留",
    ...overrides,
  };
}

beforeEach(() => {
  localStorage.clear();
  setActivePinia(createPinia());
});

afterEach(() => {
  for (const w of wrappers) {
    try { w.unmount(); } catch { /* 已卸载 */ }
  }
  wrappers = [];
  vi.restoreAllMocks();
});

describe("RetryPanel：只呈现服务端事实，手动重试一次一次", () => {
  it("耗尽态：显示次数 / 上限 / 最近错误 / 下一步，并只说明一次「不会再自动尝试」", async () => {
    const api = fakeRetryApi();
    const w = wrap(mount(RetryPanel, { props: { intentId: "intent_1", boardId: "board_1", api } }));
    await flushPromises();

    expect(api.fetchSeries).toHaveBeenCalledWith(
      { boardId: "board_1", intentId: "intent_1" },
      expect.objectContaining({ signal: expect.anything() }),
    );
    const root = w.get('[data-im="p3c-retry-panel"]');
    expect(root.attributes("data-state")).toBe("ready");
    expect(w.get('[data-im="p3c-retry-attempts"]').text()).toContain("3 / 最多 3");
    expect(w.get('[data-im="p3c-retry-status"]').text()).toContain("自动重试已用尽");
    expect(w.get('[data-im="p3c-retry-last-error"]').text()).toContain("工具调用超时");
    expect(w.get('[data-im="p3c-retry-next-action"]').text()).toContain("等待你手动重试");
    expect(w.findAll('[data-im="p3c-retry-exhausted"]')).toHaveLength(1);
    expect(w.get('[data-im="p3c-retry-exhausted"]').text()).toContain("不会重新赠送自动重试次数");
  });

  it("手动重试：一次点击 = 一次提交；随后重新读取服务端记录，次数不被本地改写", async () => {
    const api = fakeRetryApi();
    const w = wrap(mount(RetryPanel, { props: { intentId: "intent_1", boardId: "board_1", api } }));
    await flushPromises();
    expect(api.fetchSeries).toHaveBeenCalledTimes(1);

    await w.get('[data-im="p3c-retry-manual"]').trigger("click");
    await flushPromises();

    expect(api.retry).toHaveBeenCalledTimes(1);
    expect(api.retry).toHaveBeenCalledWith("intent_1", {
      boardId: "board_1",
      seriesId: "rs_1",
      expectedAuthorizationVersion: "auth_v2",
    });
    // 提交后必须重新读服务端（次数不是本地算的）
    expect(api.fetchSeries).toHaveBeenCalledTimes(2);
    expect(w.get('[data-im="p3c-retry-notice"]').text()).toContain("不会重置自动重试次数");
    expect(w.get('[data-im="p3c-retry-attempts"]').text()).toContain("3 / 最多 3");
    const retriedEvents = w.emitted("retried") as unknown[][];
    expect(retriedEvents[retriedEvents.length - 1]).toEqual([
      { ok: true, intentId: "intent_1", reason: "服务端已受理" },
    ]);
  });

  it("服务端说正在重试时禁用按钮（同一系列不并行两次）", async () => {
    const api = fakeRetryApi({
      fetchSeries: vi.fn(async () => seriesEnvelope([retrySeries({ status: "retrying", attempts: 1 })])),
    });
    const w = wrap(mount(RetryPanel, { props: { intentId: "intent_1", api } }));
    await flushPromises();
    expect(w.get('[data-im="p3c-retry-manual"]').attributes("disabled")).toBeDefined();
    expect(w.get('[data-im="p3c-retry-attempts"]').text()).toContain("1 / 最多 3");
  });

  it("接口未上线（available=false）：如实说明，不给假按钮、不显示 0 次", async () => {
    const api = fakeRetryApi({
      fetchSeries: vi.fn(async () => ({
        available: false,
        reason: "服务端还没有提供重试记录（这个能力未上线，不是「没有重试」）",
        series: [],
      })),
    });
    const w = wrap(mount(RetryPanel, { props: { intentId: "intent_1", api } }));
    await flushPromises();
    expect(w.get('[data-im="p3c-retry-unavailable"]').text()).toContain("未上线");
    expect(w.find('[data-im="p3c-retry-manual"]').exists()).toBe(false);
    expect(w.text()).not.toContain("0 / 最多");
  });

  it("读取失败：给真实原因与重试读取，不假装没有记录", async () => {
    const fetchSeries = vi
      .fn()
      .mockRejectedValueOnce(new Error("连接被拒绝"))
      .mockResolvedValueOnce(seriesEnvelope([]));
    const api = { fetchSeries, retry: vi.fn() } as unknown as RetryPanelApi;
    const w = wrap(mount(RetryPanel, { props: { intentId: "intent_1", api } }));
    await flushPromises();
    expect(w.get('[data-im="p3c-retry-error"]').text()).toContain("连接被拒绝");
    await w.get('[data-im="p3c-retry-error"] button').trigger("click");
    await flushPromises();
    expect(w.get('[data-im="p3c-retry-empty"]').text()).toContain("没有重试系列");
  });
});

describe("RecoveryPanel：保存失败持续可见 + 服务端恢复", () => {
  function mountPanel(deps: unknown, retryApi = fakeRetryApi()) {
    const store = useInteractiveStore();
    store.boardId = "board_1";
    const w = wrap(
      mount(RecoveryPanel, {
        props: { boardId: "board_1", deps: deps as never, retryApi },
      }),
    );
    return { w, store };
  }

  function baseDeps(overrides: Record<string, unknown> = {}) {
    return {
      fetchIntentViews: vi.fn(async () => ({
        available: true,
        intents: [view(), view({ intent_id: "intent_2", status: "partially_completed", title: "另一项" })],
      })),
      fetchRecoveryPreview: vi.fn(async () => ({
        available: true,
        preview: { intent_id: "intent_1", status: "failed", can_resume: true, reasons: [], unfinished_operations: ["op_1"] },
      })),
      recoverIntent: vi.fn(async () => ({ ok: true, status: "approved" })),
      ...overrides,
    };
  }

  it("保存失败：持续显示原因并可重试（调用既有 saveNow，不重复提交）", async () => {
    const deps = baseDeps();
    const { w, store } = mountPanel(deps);
    store.saveStatus = "error";
    store.saveError = "本机磁盘写满";
    const saveNow = vi.spyOn(store, "saveNow").mockImplementation(async () => {
      // 模拟服务端确认保存成功：状态由 store 真实字段表达
      store.saveStatus = "saved";
      store.saveError = null;
      store.dirty = false;
    });
    await flushPromises();
    await nextTick();

    expect(w.get('[data-im="p3c-recovery-save"]').text()).toContain("板面没有保存");
    expect(w.get('[data-im="p3c-recovery-save"]').text()).toContain("保存失败");
    await w.get('[data-im="p3c-recovery-save-retry"]').trigger("click");
    await flushPromises();
    expect(saveNow).toHaveBeenCalledTimes(1);
    expect(w.get('[data-im="p3c-recovery-store-notice"]').text()).toContain("已重新保存");
  });

  it("可恢复意图：只列服务端的可恢复项；部分完成原样标注；恢复预览与恢复都调真实服务", async () => {
    const deps = baseDeps();
    const { w } = mountPanel(deps);
    await flushPromises();

    const rows = w.findAll('[data-im="p3c-recovery-intent"]');
    expect(rows).toHaveLength(2);
    expect(rows[0].attributes("data-status")).toBe("failed");
    expect(rows[1].attributes("data-status")).toBe("partially_completed");
    expect(rows[1].text()).toContain("部分完成");
    expect(rows[0].text()).toContain("已完成操作 2");
    expect(rows[0].text()).toContain("失败 1");

    await rows[0].get('[data-im="p3c-recovery-preview-btn"]').trigger("click");
    await flushPromises();
    expect(deps.fetchRecoveryPreview).toHaveBeenCalledWith("intent_1");
    expect(rows[0].get('[data-im="p3c-recovery-preview"]').text()).toContain("可恢复：是");

    await rows[0].get('[data-im="p3c-recovery-recover"]').trigger("click");
    await flushPromises();
    expect(deps.recoverIntent).toHaveBeenCalledWith("intent_1", "board_1", 3);
    expect(w.get('[data-im="p3c-recovery-recover-notice"]').text()).toContain("恢复已受理");
    expect(deps.fetchIntentViews).toHaveBeenCalledTimes(2);
  });

  it("内容冲突 / 草稿冲突：列出两份内容，由用户明确选择（不静默覆盖）", async () => {
    const deps = baseDeps();
    const { w, store } = mountPanel(deps);
    store.board = {
      ...(store.board ?? { cards: [] }),
      cards: [{ id: "card_1" }, { id: "card_2" }],
    } as never;
    vi.spyOn(store, "boardContentConflictFor").mockImplementation((cardId: string) =>
      cardId === "card_1" ? { local: "本页写的", server: "服务器写的" } : null,
    );
    const resolveContent = vi.spyOn(store, "resolveBoardContentConflict").mockImplementation(() => undefined);
    store.draftConflicts = { "card_9:content": { local: "旧草稿", server: "新草稿" } };
    vi.spyOn(store, "draftConflictFor").mockReturnValue({ local: "旧草稿", server: "新草稿" });
    const resolveDraft = vi.spyOn(store, "resolveDraftConflict").mockImplementation(() => undefined);
    await flushPromises();
    await nextTick();

    const block = w.get('[data-im="p3c-recovery-content-conflict"]');
    expect(block.text()).toContain("本页写的");
    expect(block.text()).toContain("服务器写的");
    await block.get('[data-im="p3c-recovery-conflict-local"]').trigger("click");
    expect(resolveContent).toHaveBeenCalledWith("card_1", "local");

    const draftBlock = w.get('[data-im="p3c-recovery-draft-conflict"]');
    expect(draftBlock.text()).toContain("旧草稿");
    await draftBlock.get('[data-im="p3c-recovery-draft-conflict-server"]').trigger("click");
    expect(resolveDraft).toHaveBeenCalledWith("card_9:content", "server");
  });

  it("服务端恢复能力不可用：如实说明，不显示成「没有事项」", async () => {
    const deps = baseDeps({
      fetchIntentViews: vi.fn(async () => ({ available: false, intents: [] })),
    });
    const { w } = mountPanel(deps);
    await flushPromises();
    expect(w.get('[data-im="p3c-recovery-load-error"]').text()).toContain("不可用");
    expect(w.get('[data-im="p3c-recovery-load-error"]').text()).toContain("不代表没有需要恢复的事项");
  });

  it("草稿保存失败 / 受理不明：给出真实动作，重试只做对应那件事", async () => {
    const deps = baseDeps();
    const { w, store } = mountPanel(deps);
    // 草稿整体状态是 computed：用真实状态表制造一次失败（不是改只读展示值）
    store.draftStates = { "card_1:content": { status: "error", error: "本机存储不可用" } };
    store.submitResultUnknown = true;
    const retryDraft = vi.spyOn(store, "retryDraftSave").mockResolvedValue(undefined);
    const verify = vi
      .spyOn(store, "confirmAcceptanceAfterUnclear")
      .mockResolvedValue({ ok: true } as never);
    await flushPromises();
    await nextTick();

    expect(w.get('[data-im="p3c-recovery-draft-save"]').text()).toContain("本机存储不可用");
    await w.get('[data-im="p3c-recovery-draft-retry"]').trigger("click");
    await flushPromises();
    expect(retryDraft).toHaveBeenCalledTimes(1);

    expect(w.get('[data-im="p3c-recovery-submit-unknown"]').text()).toContain("结果还没有确认");
    await w.get('[data-im="p3c-recovery-submit-verify"]').trigger("click");
    await flushPromises();
    expect(verify).toHaveBeenCalledTimes(1);
  });
});

describe("SaveFailureBanner：保存失败持续可见 + 可重试", () => {
  it("失败时出现、没有关闭按钮（不解决就不消失），重试调用 saveNow", async () => {
    setActivePinia(createPinia());
    const store = useInteractiveStore();
    store.saveStatus = "error";
    store.saveError = "本机磁盘写满";
    const saveNow = vi.spyOn(store, "saveNow").mockResolvedValue(undefined);
    const SaveFailureBanner = (await import("../v2/SaveFailureBanner.vue")).default;
    const w = wrap(mount(SaveFailureBanner));
    await nextTick();
    const banner = w.get('[data-im="p3c-save-failure"]');
    expect(banner.text()).toContain("保存失败");
    // 没有关闭/忽略入口：失败没解决就不能从界面上消失
    expect(banner.findAll("button")).toHaveLength(1);
    await banner.get('[data-im="p3c-save-failure-retry"]').trigger("click");
    await flushPromises();
    expect(saveNow).toHaveBeenCalledTimes(1);
  });
});

describe("recoverySummary：只数真实需要处理的事项", () => {
  it("store 一侧逐类相加并按对象去重；服务端未读到时不多算", () => {
    const facts = {
      saveFailed: true,
      draftSaveFailed: true,
      submitRefreshFailed: false,
      submitUnknown: true,
      contentConflictCardIds: ["c1", "c1", "c2"],
      draftConflictKeys: ["k1"],
    };
    expect(recoveryStoreCount(facts)).toBe(6);
    expect(recoveryItemCount(facts, { recoverableIntents: 2, exhaustedRetries: 1 })).toBe(9);
    expect(recoveryItemCount(facts, null)).toBe(6);
    expect(recoveryEntryVisible(0)).toBe(false);
    expect(recoveryEntryVisible(1)).toBe(true);
  });
});