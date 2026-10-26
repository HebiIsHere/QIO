/**
 * FU2-C 结果面板可见断言：旧审批路径的「已批准」与「执行成功」严格分开。
 *
 * 先红后绿（基线 373fda40 上应失败）：
 * 1. legacy 用户批准只显示「已批准（等待执行）」，不得显示成完成；
 * 2. 真实 executed 事件/history 才收口为「已完成」（ok=true）；
 * 3. legacy 中断只读显示「未执行 / 中断待核对」，不标成功；
 * 4. history 的中间态 outcome=approved（B-CP1：无 ok 字段）不得判成功。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";

const mocks = vi.hoisted(() => ({ getApprovalHistory: vi.fn(), getPendingApprovals: vi.fn() }));

vi.mock("../../../services/approvalLifecycle", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../../services/approvalLifecycle")>();
  return {
    ...actual,
    getApprovalHistory: mocks.getApprovalHistory,
    getPendingApprovals: mocks.getPendingApprovals,
  };
});

import ApprovalResultsPanel from "../ApprovalResultsPanel.vue";
import { useApprovalsStore } from "../../../stores/approvals";
import { useApprovalResultsStore } from "../../../stores/approvalResults";

function setup() {
  const pinia = createPinia();
  setActivePinia(pinia);
  return { pinia, results: useApprovalResultsStore() };
}

beforeEach(() => {
  mocks.getApprovalHistory.mockReset();
  mocks.getPendingApprovals.mockReset();
  mocks.getApprovalHistory.mockResolvedValue([]);
  localStorage.clear();
});

describe("legacy 已批准 ≠ 执行成功（结果面板）", () => {
  it("legacy 批准后显示「已批准（等待执行）」+ 旧审批路径标记，不显示完成", async () => {
    const { pinia, results } = setup();
    results.recordLocal({
      approvalId: "ap_legacy",
      phase: "approved",
      state: "approved",
      summary: "已批准（旧审批路径），等待后端执行",
      ok: null,
      kind: "tool_create",
      label: "创建子 agent",
      legacy: true,
    });
    const w = mount(ApprovalResultsPanel, { global: { plugins: [pinia] } });
    await flushPromises();

    const entry = w.get('[data-approval-id="ap_legacy"]');
    expect(entry.attributes("data-state")).toBe("approved");
    expect(entry.text()).toContain("已批准");
    // 状态徽标不得是「已完成」（正文里「不等于已完成」的提醒不算）
    expect(entry.get('[data-test="approval-result-state"]').text()).not.toContain("已完成");
    expect(entry.find('[data-test="approval-result-legacy"]').exists()).toBe(true);
    expect(entry.find('[data-test="approval-result-legacy-note"]').text()).toContain("不是执行成功");
    w.unmount();
  });

  it("真实 executed 事件到达后才显示完成（ok=true）", async () => {
    const { pinia, results } = setup();
    results.recordLocal({
      approvalId: "ap_legacy",
      phase: "approved",
      state: "approved",
      ok: null,
      legacy: true,
    });
    results.recordEvent({
      approval_id: "ap_legacy",
      phase: "executed",
      legacy: true,
      ok: true,
      summary: "已执行：创建子 agent",
      detail: { ok: true, kind: "tool_create", label: "创建子 agent", error: null },
    });
    const w = mount(ApprovalResultsPanel, { global: { plugins: [pinia] } });
    await flushPromises();

    const entry = w.get('[data-approval-id="ap_legacy"]');
    expect(entry.attributes("data-state")).toBe("completed");
    expect(entry.text()).toContain("已完成");
    expect(results.byId["ap_legacy"].ok).toBe(true);
    expect(w.findAll('[data-approval-id="ap_legacy"]')).toHaveLength(1);
    w.unmount();
  });

  it("legacy 中断只读显示「未执行 / 中断待核对」，不标成功", async () => {
    const { pinia, results } = setup();
    results.recordEvent({
      approval_id: "ap_int",
      phase: "interrupted_legacy",
      legacy: true,
      ok: false,
      summary: "上次操作没有执行，结果待核对",
    });
    const w = mount(ApprovalResultsPanel, { global: { plugins: [pinia] } });
    await flushPromises();

    const entry = w.get('[data-approval-id="ap_int"]');
    expect(entry.text()).toContain("中断");
    expect(entry.text()).toContain("待核对");
    expect(entry.find('[data-test="approval-result-legacy-note"]').text()).toContain("没有执行");
    expect(results.byId["ap_int"].ok).toBe(false);
    w.unmount();
  });
});

describe("快照里的旧审批中断：只读展示，没有可点击的允许/拒绝", () => {
  it("显示「未执行 · 中断待核对」，且不计入待审批数量", async () => {
    const { pinia } = setup();
    const approvals = useApprovalsStore();
    approvals.legacyInterrupted = [
      {
        approvalId: "ia_1",
        kind: "computer",
        what: "运行一段命令",
        createdAt: "2026-10-26T09:00:00+08:00",
        status: "interrupted_legacy",
      },
    ];
    const w = mount(ApprovalResultsPanel, { global: { plugins: [pinia] } });
    await flushPromises();

    const section = w.get('[data-test="approval-legacy-interrupted"]');
    const text = section.text();
    expect(text).toContain("未执行");
    expect(text).toContain("中断待核对");
    expect(text).toContain("运行一段命令");
    // 只读事实：没有批准/拒绝/重试执行按钮
    expect(section.findAll("button")).toHaveLength(0);
    expect(approvals.queue).toHaveLength(0);
    expect(approvals.pendingCount).toBe(0);
    w.unmount();
  });

  it("旧后端没有中断字段（undefined）不会把已有清单擦空", async () => {
    const { results } = setup();
    const approvals = useApprovalsStore();
    approvals.legacyInterrupted = [
      { approvalId: "ia_1", kind: "computer", what: "运行一段命令", createdAt: "", status: "interrupted_legacy" },
    ];
    expect(results).toBeTruthy();
    approvals.ingestInterruptedLegacy(undefined);
    expect(approvals.legacyInterrupted).toHaveLength(1);
  });
});

describe("history 中间态 approved 不得判成功（B-CP1 ④）", () => {
  it("outcome=approved / detail 无 ok → ok=null，标签是等待执行", async () => {
    const { results } = setup();
    results.mergeHistory([
      {
        approval_id: "ap_mid",
        kind: "tool_create",
        summary: "已批准：创建子 agent（等待执行）",
        outcome: "approved",
        detail: { decision: "approved", kind: "tool_create", label: "创建子 agent" },
        at: "2026-10-26T10:00:00+08:00",
      },
    ]);
    const entry = results.byId["ap_mid"];
    expect(entry.state).toBe("approved");
    expect(entry.ok).toBeNull();
    expect(entry.stateLabel).toContain("已批准");
    expect(entry.summary).toContain("等待执行");
  });

  it("outcome=executed 且 detail.ok=true → ok=true（真实执行结果）", async () => {
    const { results } = setup();
    results.mergeHistory([
      {
        approval_id: "ap_done",
        kind: "tool_create",
        summary: "已执行：创建子 agent",
        outcome: "executed",
        detail: { ok: true, kind: "tool_create", label: "创建子 agent", error: null },
        at: "2026-10-26T10:01:00+08:00",
      },
    ]);
    const entry = results.byId["ap_done"];
    expect(entry.state).toBe("completed");
    expect(entry.ok).toBe(true);
  });
});
