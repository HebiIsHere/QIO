/**
 * PM-QIO032 · i-tasks：审批历史（文案 8 / #56）与恢复列表（文案 11）。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount, type VueWrapper } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";

import ApprovalResultsPanel from "../../approval/ApprovalResultsPanel.vue";
import RecoveryPanel from "../v2/RecoveryPanel.vue";
import { useApprovalResultsStore } from "../../../stores/approvalResults";
import { recoveryEmptyText } from "../v2/recoverySummary";

const mounted: VueWrapper[] = [];

function track<T extends VueWrapper>(wrapper: T): T {
  mounted.push(wrapper);
  return wrapper;
}

beforeEach(() => {
  while (mounted.length) mounted.pop()?.unmount();
  document.body.innerHTML = "";
  localStorage.clear();
  setActivePinia(createPinia());
});

function mountApproval() {
  const results = useApprovalResultsStore();
  const ensureFresh = vi.spyOn(results, "ensureFresh").mockImplementation(() => {});
  const wrapper = track(mount(ApprovalResultsPanel, { attachTo: document.body }));
  return { wrapper, results, ensureFresh };
}

describe("文案 8 审批历史空态与失败重试", () => {
  it("空态只写「暂无审批结果」", async () => {
    const { wrapper, results } = mountApproval();
    results.historyLoadedAt = Date.now();
    results.historyError = null;
    await flushPromises();
    expect(wrapper.get('[data-test="approval-results-empty"]').text()).toBe("暂无审批结果");
    expect(wrapper.find('[data-test="approval-history-error"]').exists()).toBe(false);
  });

  it("读取失败：保留重试入口，不显示空态", async () => {
    const { wrapper, results } = mountApproval();
    results.historyError = "读取审批历史失败：网络错误（可以重试）";
    await flushPromises();
    expect(wrapper.find('[data-test="approval-results-empty"]').exists()).toBe(false);
    expect(wrapper.get('[data-test="approval-history-error"]').text()).toContain("网络错误");
    const loadHistory = vi.spyOn(results, "loadHistory").mockResolvedValue(true);
    await wrapper.get('[data-test="approval-history-retry"]').trigger("click");
    await flushPromises();
    expect(loadHistory).toHaveBeenCalledTimes(1);
  });
});

describe("#56 审批历史计数来自历史查询", () => {
  it("计数只用历史查询行数，不把实时事件条数算进来", async () => {
    const { wrapper, results } = mountApproval();
    results.historyLoadedAt = Date.now();
    results.history = [
      { approval_id: "a1", outcome: "executed", at: "2026-10-25T00:00:00.000Z" },
    ] as never;
    results.mergeHistory(results.history);
    // 一条实时事件：不是历史查询的事实，不进入历史计数
    results.recordLocal({ approvalId: "live-1", phase: "executed", state: "completed", summary: "实时结果" });
    await flushPromises();
    expect(wrapper.get('[data-test="approval-history-count"]').text()).toBe("审批历史 1 条");
  });
});

describe("文案 11 恢复列表空态", () => {
  it("纯逻辑空态文案是「暂无可恢复的任务」", () => {
    expect(recoveryEmptyText()).toBe("暂无可恢复的任务");
  });

  it("服务端没有可恢复意图时，列表空态只写「暂无可恢复的任务」", async () => {
    const wrapper = track(
      mount(RecoveryPanel, {
        props: {
          boardId: "board_1",
          deps: {
            fetchIntentViews: vi.fn(async () => ({ available: true, intents: [] })),
            fetchRecoveryPreview: vi.fn(),
            recoverIntent: vi.fn(),
          },
          retryApi: {
            fetchSeries: vi.fn(async () => ({ available: false, reason: "能力未上线", series: [] })),
            retry: vi.fn(),
          },
        },
        attachTo: document.body,
      }),
    );
    await flushPromises();
    expect(wrapper.get('[data-im="p3c-recovery-intents-empty"]').text()).toBe("暂无可恢复的任务");
  });
});
