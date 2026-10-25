/**
 * 审批弹窗 v2（R9 契约 §4 / §5）—— 定向测试。
 *
 * 复用既有 ApprovalModal/ApprovalFacts（不另造弹窗），只验证：
 * 状态机展示、内部数据三次连续确认按钮与进度、执行结果历史入口、已批准执行中不再给决定按钮。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";

const mocks = vi.hoisted(() => ({
  decideApproval: vi.fn(),
  confirmApprovalStage: vi.fn(),
  getApprovalHistory: vi.fn(),
  getPendingApprovals: vi.fn(),
}));

vi.mock("../../services/approvalLifecycle", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../services/approvalLifecycle")>();
  return {
    ...actual,
    decideApproval: mocks.decideApproval,
    confirmApprovalStage: mocks.confirmApprovalStage,
    getApprovalHistory: mocks.getApprovalHistory,
    getPendingApprovals: mocks.getPendingApprovals,
  };
});
vi.mock("../../services/api", () => ({
  api: { respondApproval: vi.fn(async () => ({ ok: true })) },
}));

import ApprovalModal from "../ApprovalModal.vue";
import { useApprovalsStore } from "../../stores/approvals";
import { useApprovalResultsStore } from "../../stores/approvalResults";

function mountWithItem(meta: Record<string, unknown>) {
  const pinia = createPinia();
  setActivePinia(pinia);
  const approvals = useApprovalsStore();
  approvals.enqueue("ap_1", "computer", { description: "维护 QIO 内部运行数据" }, {
    autoOpen: true,
    meta,
  });
  const w = mount(ApprovalModal, { global: { plugins: [pinia] } });
  return { w, approvals };
}

beforeEach(() => {
  for (const fn of Object.values(mocks)) fn.mockReset();
  mocks.getApprovalHistory.mockResolvedValue([]);
});

describe("内部数据：状态与三次连续确认", () => {
  it("展示状态机、内部数据警告与确认进度；按钮是「第 1/3 次确认」而不是一次批准", async () => {
    const { w } = mountWithItem({
      state: "waiting",
      requiresConfirmations: 3,
      confirmedStage: 0,
      purpose: "整理内部数据库",
      source: "tool",
      operationSummary: "维护内部运行数据",
      expiresAt: "2026-10-22T10:10:00+08:00",
    });
    await flushPromises();
    const text = w.text();
    expect(text).toContain("等待确认");
    expect(text).toContain("内部运行数据");
    expect(text).toContain("连续 3 次");
    expect(text).toContain("已确认 0/3 次");
    expect(text).toContain("整理内部数据库");
    expect(text).toContain("维护内部运行数据");
    const confirm = w.get('[data-test="approval-confirm-next"]');
    expect(confirm.text()).toContain("继续确认（第 1/3 次）");
    w.unmount();
  });

  it("点击确认 → 调 confirm 接口（stage=1，后端 CAS 为准）", async () => {
    const { w, approvals } = mountWithItem({
      state: "waiting",
      requiresConfirmations: 3,
      confirmedStage: 0,
    });
    mocks.confirmApprovalStage.mockResolvedValue({
      approval_id: "ap_1",
      kind: "computer",
      label: "维护内部数据",
      purpose: "",
      instruction_id: null,
      instruction_text: null,
      created_at: "",
      expires_at: null,
      state: "waiting",
      confirmed_stage: 1,
      requires_confirmations: 3,
      source: "tool",
      mode_at_request: "restricted",
      payload: {},
    });
    await w.get('[data-test="approval-confirm-next"]').trigger("click");
    await flushPromises();
    expect(mocks.confirmApprovalStage).toHaveBeenCalledWith("ap_1", 1, {
      expectedRevision: undefined,
    });
    expect(approvals.current?.confirmedStage).toBe(1);
    w.unmount();
  });
});

describe("状态机：已批准/执行中不再给决定按钮", () => {
  it("executing：显示等待执行结果，「稍后处理」变「知道了」", async () => {
    const { w } = mountWithItem({ state: "executing", requiresConfirmations: 1 });
    await flushPromises();
    expect(w.get('[data-test="approval-awaiting-execution"]').text()).toContain("等待后端执行结果");
    expect(w.find('[data-test="approval-confirm-next"]').exists()).toBe(false);
    const buttons = w.findAll(".actions button");
    expect(buttons.some((b) => b.text() === "知道了")).toBe(true);
    expect(buttons.some((b) => b.text() === "允许此次操作")).toBe(false);
    w.unmount();
  });
});

describe("执行结果历史", () => {
  it("弹窗里有只读历史入口；最近结局与历史记录可读", async () => {
    const { w, approvals } = mountWithItem({ state: "waiting", requiresConfirmations: 1 });
    // 历史真源已迁到 stores/approvalResults.ts；旧弹窗通过 getter 读取同一份数据
    useApprovalResultsStore().mergeHistory([
      {
        approval_id: "ap_0",
        kind: "computer",
        summary: "清理临时文件",
        outcome: "completed",
        detail: {},
        at: "2026-10-21T09:00:00+08:00",
      },
    ]);
    approvals.lastResult = {
      approvalId: "ap_0",
      phase: "executed",
      state: "completed",
      summary: "清理临时文件",
      ok: true,
    };
    await flushPromises();
    expect(w.find('[data-test="approval-history"]').exists()).toBe(true);
    expect(w.get('[data-test="approval-last-result"]').text()).toContain("已完成");
    expect(w.get('[data-test="approval-last-result"]').text()).toContain("清理临时文件");
    w.unmount();
  });
});
