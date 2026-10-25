/**
 * F06 守卫：v2 生命周期路径不接受预算 overrides（绝不能「看起来改了、实际没改」）。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";

const mocks = vi.hoisted(() => ({
  decideApproval: vi.fn(),
  confirmApprovalStage: vi.fn(),
  getPendingApprovals: vi.fn(),
  getApprovalHistory: vi.fn(),
  respondApproval: vi.fn(),
}));

vi.mock("../../services/approvalLifecycle", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../services/approvalLifecycle")>();
  return {
    ...actual,
    decideApproval: mocks.decideApproval,
    confirmApprovalStage: mocks.confirmApprovalStage,
    getPendingApprovals: mocks.getPendingApprovals,
    getApprovalHistory: mocks.getApprovalHistory,
  };
});
vi.mock("../../services/api", () => ({ api: { respondApproval: mocks.respondApproval } }));

import { useApprovalsStore } from "../approvals";

function store() {
  setActivePinia(createPinia());
  return useApprovalsStore();
}

beforeEach(() => {
  for (const fn of Object.values(mocks)) fn.mockReset();
  mocks.getApprovalHistory.mockResolvedValue([]);
  mocks.respondApproval.mockResolvedValue({ ok: true });
});

describe("F06 预算修改守卫", () => {
  it("v2 生命周期记录带 overrides：拒绝提交并说明原因（不静默丢掉修改）", async () => {
    const s = store();
    s.enqueue("ap_life", "tool_execution", { description: "执行命令" }, {
      autoOpen: false,
      meta: { state: "waiting", requiresConfirmations: 1 },
    });
    await s.respondById("ap_life", "approved", {
      subagent_budget: { max_iterations: 9 },
    });
    expect(mocks.decideApproval).not.toHaveBeenCalled();
    expect(mocks.respondApproval).not.toHaveBeenCalled();
    expect(s.error).toContain("不支持修改执行预算");
    expect(s.error).toContain("没有提交任何决定");
    expect(s.queue).toHaveLength(1);
  });

  it("旧 /respond 路径带 overrides：照常提交（执行器会用用户批准值）", async () => {
    const s = store();
    s.enqueue("ap_old", "tool_create", { description: "创建子智能体工具" }, { autoOpen: false });
    await s.respondById("ap_old", "approved", {
      subagent_budget: { max_iterations: 9 },
    });
    expect(mocks.respondApproval).toHaveBeenCalledWith(
      "ap_old",
      "approved",
      { subagent_budget: { max_iterations: 9 } },
      expect.anything(),
    );
    expect(s.queue).toHaveLength(0);
  });
});
