/**
 * 待审批恢复失败可见 + 可重试（A / F04）。
 *
 * 重点：失败不能冒充「没有待审批」，不能清空已有待办；重试只重读服务端快照
 * ——不重置期限、不重复创建、不推进确认阶段；与历史读取失败分开。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";

const mocks = vi.hoisted(() => ({
  getPendingApprovals: vi.fn(),
  getApprovalHistory: vi.fn(),
}));

vi.mock("../../services/approvalLifecycle", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../services/approvalLifecycle")>();
  return {
    ...actual,
    getPendingApprovals: mocks.getPendingApprovals,
    getApprovalHistory: mocks.getApprovalHistory,
  };
});

vi.mock("../../services/api", () => ({
  api: { respondApproval: vi.fn(async () => ({ ok: true })) },
}));

import { useApprovalsStore } from "../approvals";
import { useApprovalResultsStore } from "../approvalResults";
import type { PendingApproval } from "../../services/approvalLifecycle";

function pending(overrides: Partial<PendingApproval> = {}): PendingApproval {
  return {
    approval_id: "ap_1",
    kind: "computer",
    label: "维护内部数据",
    purpose: "整理数据库",
    instruction_id: null,
    instruction_text: null,
    created_at: "2026-10-22T10:00:00+08:00",
    expires_at: "2026-10-22T10:10:00+08:00",
    state: "waiting",
    confirmed_stage: 0,
    requires_confirmations: 1,
    source: "tool",
    mode_at_request: "restricted",
    payload: { description: "维护 QIO 内部数据" },
    operation: { kind: "internal.data_write", label: "维护内部数据", summary: "整理数据库" },
    revisions: 2,
    ...overrides,
  };
}

function setup() {
  setActivePinia(createPinia());
  return {
    approvals: useApprovalsStore(),
    results: useApprovalResultsStore(),
  };
}

beforeEach(() => {
  mocks.getPendingApprovals.mockReset();
  mocks.getApprovalHistory.mockReset();
  mocks.getApprovalHistory.mockResolvedValue([]);
});

describe("恢复失败：可见 + 不清空 + 可重试", () => {
  it("失败时保留已有待办，给出可重试原因（不当作空列表）", async () => {
    const { approvals } = setup();
    approvals.enqueue("ap_local", "computer", { description: "本地已有待办" }, { autoOpen: false });
    mocks.getPendingApprovals.mockRejectedValueOnce(new Error("连接超时"));
    await approvals.recoverPending();

    expect(approvals.queue.map((item) => item.approval_id)).toEqual(["ap_local"]);
    expect(approvals.recoveryError).toContain("恢复待审批项失败");
    expect(approvals.recoveryError).toContain("可以重试");
    expect(approvals.recoveryError).toContain("已有待办保留");
    expect(approvals.recovered).toBe(true);
    expect(approvals.recoveryBusy).toBe(false);
  });

  it("重试成功：错误清除，用服务端快照收敛，且不重复入队", async () => {
    const { approvals } = setup();
    approvals.enqueue("ap_1", "computer", {}, { autoOpen: false });
    mocks.getPendingApprovals.mockRejectedValueOnce(new Error("500"));
    await approvals.recoverPending();
    expect(approvals.recoveryError).toBeTruthy();

    mocks.getPendingApprovals.mockResolvedValueOnce([pending()]);
    const ok = await approvals.retryRecovery();
    expect(ok).toBe(true);
    expect(approvals.recoveryError).toBeNull();
    expect(approvals.queue).toHaveLength(1);
    expect(approvals.queue[0].approval_id).toBe("ap_1");
  });

  it("重试只重读快照：不重置期限、不推进确认阶段、不多建审批", async () => {
    const { approvals } = setup();
    approvals.enqueue("ap_1", "computer", {}, {
      autoOpen: false,
      meta: { state: "waiting", requiresConfirmations: 3, confirmedStage: 1, revision: 5, expiresAt: "2026-10-22T10:10:00+08:00" },
    });
    mocks.getPendingApprovals.mockResolvedValue([
      pending({
        requires_confirmations: 3,
        confirmed_stage: 1,
        revisions: 5,
        expires_at: "2026-10-22T10:10:00+08:00",
      }),
    ]);
    await approvals.retryRecovery();
    await approvals.retryRecovery();
    expect(approvals.queue).toHaveLength(1);
    // 只读快照：阶段/期限/修订号保持服务端原值（重试不推进、不续期）
    expect(approvals.current?.state).toBe("waiting");
    expect(approvals.current?.confirmedStage).toBe(1);
    expect(approvals.current?.expiresAt).toBe("2026-10-22T10:10:00+08:00");
    expect(approvals.current?.revision).toBe(5);
    expect(approvals.queue.filter((item) => item.approval_id === "ap_1")).toHaveLength(1);
  });

  it("老后端没有 pending 接口（404）：不显示失败，也不清空待办", async () => {
    const { approvals } = setup();
    approvals.enqueue("ap_local", "computer", {}, { autoOpen: false });
    mocks.getPendingApprovals.mockRejectedValueOnce({ status: 404, message: "not found" });
    await approvals.recoverPending();
    expect(approvals.recoveryError).toBeNull();
    expect(approvals.queue).toHaveLength(1);
  });

  it("提交中的审批不并发触发恢复（避免状态互相覆盖）", async () => {
    const { approvals } = setup();
    approvals.enqueue("ap_1", "computer", {}, { autoOpen: false });
    approvals.responding = "ap_1";
    await approvals.recoverPending();
    expect(mocks.getPendingApprovals).not.toHaveBeenCalled();
    expect(approvals.recoveryError).toBeNull();
  });

  it("恢复失败与历史读取失败是两件独立的事（各有各的错误与重试）", async () => {
    const { approvals, results } = setup();
    mocks.getPendingApprovals.mockRejectedValueOnce(new Error("pending 读失败"));
    mocks.getApprovalHistory.mockRejectedValueOnce(new Error("history 读失败"));
    await approvals.recoverPending();
    await results.loadHistory();
    expect(approvals.recoveryError).toContain("恢复待审批项失败");
    expect(results.historyError).toContain("读取审批历史失败");
    expect(approvals.recoveryError).not.toContain("历史");
    expect(results.historyError).not.toContain("恢复");
  });
});
