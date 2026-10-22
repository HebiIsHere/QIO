/**
 * 审批生命周期 v2 store（R9 契约 §5 / W1 HTTP 形状）—— 定向测试。
 *
 * 覆盖：GET /api/approvals/pending 恢复、decide 单次决定、内部数据三次连续确认
 * （stage 由后端 CAS 返回，前端只呈现）、APPROVAL_RESULT phase 终态/中间态、执行历史。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";

const mocks = vi.hoisted(() => ({
  getPendingApprovals: vi.fn(),
  decideApproval: vi.fn(),
  confirmApprovalStage: vi.fn(),
  getApprovalHistory: vi.fn(),
}));

vi.mock("../../services/approvalLifecycle", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../services/approvalLifecycle")>();
  return {
    ...actual,
    getPendingApprovals: mocks.getPendingApprovals,
    decideApproval: mocks.decideApproval,
    confirmApprovalStage: mocks.confirmApprovalStage,
    getApprovalHistory: mocks.getApprovalHistory,
  };
});

vi.mock("../../services/api", () => ({
  api: { respondApproval: vi.fn(async () => ({ ok: true })) },
}));

import { useApprovalsStore } from "../approvals";
import type { PendingApproval } from "../../services/approvalLifecycle";

function pending(overrides: Partial<PendingApproval> = {}): PendingApproval {
  return {
    approval_id: "ap_1",
    kind: "computer",
    label: "维护内部数据",
    purpose: "整理数据库（测试）",
    instruction_id: "instr_1",
    instruction_text: "整理一下",
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

function store() {
  setActivePinia(createPinia());
  return useApprovalsStore();
}

beforeEach(() => {
  for (const fn of Object.values(mocks)) fn.mockReset();
  mocks.getApprovalHistory.mockResolvedValue([]);
});

describe("pending 恢复", () => {
  it("GET /api/approvals/pending → 入队并带上 v2 字段（不抢焦点）", async () => {
    const s = store();
    mocks.getPendingApprovals.mockResolvedValue([pending()]);
    await s.recoverPending();
    expect(s.queue).toHaveLength(1);
    const item = s.current!;
    expect(item.state).toBe("waiting");
    expect(item.requiresConfirmations).toBe(1);
    expect(item.purpose).toBe("整理数据库（测试）");
    expect(item.operationSummary).toContain("整理数据库");
    expect(item.revision).toBe(2);
    expect(s.recovered).toBe(true);
    expect(s.deferred).toBe(true); // 恢复不抢焦点：亮出入口由用户打开
  });

  it("kind=continue 的「继续/停止」不进审批队列", async () => {
    const s = store();
    mocks.getPendingApprovals.mockResolvedValue([pending({ kind: "continue" })]);
    await s.recoverPending();
    expect(s.queue).toHaveLength(0);
  });

  it("老后端没有接口（404）→ 静默跳过，不清空已有待办", async () => {
    const s = store();
    s.enqueue("ap_old", "computer", {}, { autoOpen: false });
    mocks.getPendingApprovals.mockRejectedValue({ status: 404, message: "not found" });
    await s.recoverPending();
    expect(s.queue.map((a) => a.approval_id)).toEqual(["ap_old"]);
    expect(s.recoveryError).toBeNull();
    expect(s.recovered).toBe(true);
  });
});

describe("decide：普通审批（一次决定）", () => {
  it("approve → decide 接口 + 出队 + 最近结局", async () => {
    const s = store();
    s.enqueue("ap_1", "computer", { description: "写文件" }, {
      autoOpen: false,
      meta: { state: "waiting", requiresConfirmations: 1, revision: 3 },
    });
    mocks.decideApproval.mockResolvedValue(pending({ approval_id: "ap_1", state: "approved" }));
    await s.decide("approve");
    expect(mocks.decideApproval).toHaveBeenCalledWith("ap_1", "approve", { expectedRevision: 3 });
    expect(s.queue).toHaveLength(0);
    expect(s.lastResult?.phase).toBe("approved");
  });

  it("失败（409）保留待办并给出原因：未做出任何授权", async () => {
    const s = store();
    s.enqueue("ap_1", "computer", {}, {
      autoOpen: false,
      meta: { state: "waiting", requiresConfirmations: 1 },
    });
    mocks.decideApproval.mockRejectedValue({
      status: 409,
      message: "审批已被其他地方处理",
    });
    await s.decide("approve");
    expect(s.queue).toHaveLength(1);
    expect(s.error).toContain("未做出任何授权");
    expect(s.current?.lifecycleError).toContain("审批已被其他地方处理");
  });
});

describe("内部数据：连续三次确认（后端 CAS，前端只呈现）", () => {
  it("三次 confirm 依次发 stage 1/2/3；第三次后出队并记录「3/3 次确认」", async () => {
    const s = store();
    s.enqueue("ap_1", "computer", {}, {
      autoOpen: false,
      meta: { state: "waiting", requiresConfirmations: 3, confirmedStage: 0, revision: 7 },
    });
    let stage = 0;
    mocks.confirmApprovalStage.mockImplementation(async (_id: string, next: number) => {
      stage = next;
      // revision 由后端 CAS 推进：每次确认后返回新 revision，前端按返回值带下一次
      return pending({
        approval_id: "ap_1",
        state: next >= 3 ? "executing" : "waiting",
        confirmed_stage: next,
        requires_confirmations: 3,
        revisions: 7 + next,
      });
    });

    await s.confirmNext();
    expect(mocks.confirmApprovalStage).toHaveBeenLastCalledWith("ap_1", 1, { expectedRevision: 7 });
    expect(s.current?.confirmedStage).toBe(1);

    await s.confirmNext();
    expect(mocks.confirmApprovalStage).toHaveBeenLastCalledWith("ap_1", 2, { expectedRevision: 8 });
    expect(s.current?.confirmedStage).toBe(2);

    await s.confirmNext();
    expect(mocks.confirmApprovalStage).toHaveBeenLastCalledWith("ap_1", 3, { expectedRevision: 9 });
    expect(stage).toBe(3);
    expect(s.queue).toHaveLength(0);
    expect(s.lastResult?.summary).toContain("3/3");
  });

  it("点「批准」在多次确认审批上是第 1 次确认（不当作一次批准）", async () => {
    const s = store();
    s.enqueue("ap_1", "computer", {}, {
      autoOpen: false,
      meta: { state: "waiting", requiresConfirmations: 3, confirmedStage: 0 },
    });
    mocks.confirmApprovalStage.mockResolvedValue(
      pending({ approval_id: "ap_1", state: "waiting", confirmed_stage: 1, requires_confirmations: 3 }),
    );
    await s.respond("approved");
    expect(mocks.confirmApprovalStage).toHaveBeenCalledTimes(1);
    expect(mocks.decideApproval).not.toHaveBeenCalled();
  });
});

describe("APPROVAL_RESULT phase：中间态保留、终态收口", () => {
  it("confirm_stage 只更新进度；executed 才出队并记结果", async () => {
    const s = store();
    s.enqueue("ap_1", "computer", {}, {
      autoOpen: false,
      meta: { state: "waiting", requiresConfirmations: 3, confirmedStage: 0 },
    });
    s.recordResult({
      approval_id: "ap_1",
      phase: "confirm_stage",
      confirmed_stage: 2,
      requires_confirmations: 3,
    });
    expect(s.queue).toHaveLength(1);
    expect(s.current?.confirmedStage).toBe(2);
    expect(s.lastResult?.phase).toBe("confirm_stage");

    s.recordResult({ approval_id: "ap_1", phase: "executed", ok: true, summary: "已写入 3 个文件" });
    expect(s.queue).toHaveLength(0);
    expect(s.lastResult).toMatchObject({ phase: "executed", ok: true, summary: "已写入 3 个文件" });
  });

  it("failed / interrupted_unverified 也如实收口（不静默）", () => {
    const s = store();
    s.enqueue("ap_1", "computer", {}, { autoOpen: false, meta: { state: "executing" } });
    s.recordResult({ approval_id: "ap_1", phase: "failed", ok: false, summary: "磁盘写满" });
    expect(s.queue).toHaveLength(0);
    expect(s.lastResult?.ok).toBe(false);
    expect(s.lastResult?.summary).toBe("磁盘写满");
  });

  it("旧后端没有 phase 的结果事件：保持旧语义（有结局即收敛）", () => {
    const s = store();
    s.enqueue("ap_1", "computer", {}, { autoOpen: false });
    s.recordResult({ approval_id: "ap_1", decision: "approved" });
    expect(s.queue).toHaveLength(0);
  });
});

describe("执行结果历史", () => {
  it("loadHistory 读取后端历史（只读）", async () => {
    const s = store();
    mocks.getApprovalHistory.mockResolvedValue([
      { approval_id: "ap_0", kind: "computer", summary: "清理临时文件", outcome: "completed", detail: {}, at: "2026-10-21T09:00:00+08:00" },
    ]);
    await s.loadHistory();
    expect(s.history).toHaveLength(1);
    expect(s.history[0].summary).toBe("清理临时文件");
    expect(s.historyError).toBeNull();
  });
});
