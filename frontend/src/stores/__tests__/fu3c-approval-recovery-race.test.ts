/**
 * FU3-C 红用例：补读快照竞态保护（新审批不被较旧的空快照删掉）。
 *
 * 复现（Lead 的前端模型探针）：
 * 1. 开始补读 pending（请求在飞）；
 * 2. 期间收到新的审批事件 → 新审批入队；
 * 3. 较旧的空快照返回 → 旧实现无条件 reconcile(snapshotIds)，把请求期间新入队的审批删掉。
 *
 * 期望：
 * - 只移除「请求开始前已存在、且快照里没有」的项（旧已解决项必须被移除）；
 * - 请求期间新到达 / 更新的项保留；
 * - 读取失败保留全部已有待办（不伪装成空集合）；
 * - refreshApprovalsAndHistory 复用同一保护，语义不变。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";

const mocks = vi.hoisted(() => ({
  getPendingApprovals: vi.fn(),
  getApprovalHistory: vi.fn(),
  decideApproval: vi.fn(),
  confirmApprovalStage: vi.fn(),
  respondApproval: vi.fn(),
}));

vi.mock("../../services/approvalLifecycle", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../services/approvalLifecycle")>();
  return {
    ...actual,
    getPendingApprovals: mocks.getPendingApprovals,
    getApprovalHistory: mocks.getApprovalHistory,
    decideApproval: mocks.decideApproval,
    confirmApprovalStage: mocks.confirmApprovalStage,
  };
});

vi.mock("../../services/api", () => ({
  api: { respondApproval: mocks.respondApproval },
}));

import { useApprovalsStore } from "../approvals";
import { useApprovalResultsStore } from "../approvalResults";
import { toPendingApproval } from "../../services/approvalLifecycle";

function setup() {
  setActivePinia(createPinia());
  return { approvals: useApprovalsStore(), results: useApprovalResultsStore() };
}

/** 受控的 pending 请求：测试手动放行/拒绝（模拟「请求在飞」）。 */
function deferredPending() {
  let resolve!: (value: unknown) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise((res, rej) => {
    resolve = res;
    reject = rej;
  });
  mocks.getPendingApprovals.mockImplementationOnce(() => promise as never);
  return { resolve, reject };
}

function waitingRow(approvalId: string) {
  return toPendingApproval({
    approval_id: approvalId,
    kind: "computer",
    state: "waiting",
    created_at: "2026-10-26T12:00:00+08:00",
    payload: { description: "维护内部数据" },
  });
}

beforeEach(() => {
  for (const fn of Object.values(mocks)) fn.mockReset();
  mocks.getApprovalHistory.mockResolvedValue([]);
  mocks.respondApproval.mockResolvedValue({ ok: true });
});

describe("补读竞态：新审批不被旧快照删除", () => {
  it("请求期间新入队的审批保留，请求开始前已存在且快照没有的旧项移除", async () => {
    const { approvals } = setup();
    approvals.enqueue("ap_old", "computer", { description: "旧的待办" }, { autoOpen: false });
    expect(approvals.queue.map((a) => a.approval_id)).toEqual(["ap_old"]);

    const gate = deferredPending();
    const recovering = approvals.recoverPending();

    // 请求在飞：新的审批事件到达（真实入口：events.handleApprovalRequired → enqueue）
    approvals.enqueue("ap_new", "tool_execution", { description: "刚到达的审批" }, {
      autoOpen: true,
    });
    expect(approvals.queue.map((a) => a.approval_id)).toEqual(["ap_old", "ap_new"]);

    // 较旧的空快照返回（它只反映请求开始那一刻服务端的状态）
    gate.resolve([] as never);
    await recovering;

    // 旧项已解决 → 移除；请求期间新到达的 → 保留
    expect(approvals.queue.map((a) => a.approval_id)).toEqual(["ap_new"]);
  });

  it("请求期间被更新的旧项也保留（更新的到达序号晚于请求开始）", async () => {
    const { approvals } = setup();
    approvals.enqueue("ap_old", "computer", { description: "旧审批" }, { autoOpen: false });

    const gate = deferredPending();
    const recovering = approvals.recoverPending();

    // 请求在飞：这条旧审批收到新的 pending 事实（状态/确认进度推进）
    approvals.applyPending(
      toPendingApproval({
        approval_id: "ap_old",
        kind: "computer",
        state: "waiting",
        confirmed_stage: 1,
        requires_confirmations: 3,
        revisions: 9,
        created_at: "2026-10-26T12:00:00+08:00",
        payload: { description: "旧审批" },
      }),
      { autoOpen: false },
    );

    gate.resolve([] as never);
    await recovering;

    expect(approvals.queue.map((a) => a.approval_id)).toEqual(["ap_old"]);
    expect(approvals.queue[0]?.confirmedStage).toBe(1);
  });
});

describe("补读竞态：正常/失败路径语义不变", () => {
  it("快照里仍在等待的项保留（原有语义）", async () => {
    const { approvals } = setup();
    approvals.enqueue("ap_keep", "computer", { description: "仍在等待" }, { autoOpen: false });

    const gate = deferredPending();
    const recovering = approvals.recoverPending();
    gate.resolve([waitingRow("ap_keep")] as never);
    await recovering;

    expect(approvals.queue.map((a) => a.approval_id)).toEqual(["ap_keep"]);
  });

  it("读取失败：保留全部已有待办并给出可重试原因（不伪装成空集合）", async () => {
    const { approvals } = setup();
    approvals.enqueue("ap_old", "computer", {}, { autoOpen: false });
    const gate = deferredPending();
    const recovering = approvals.recoverPending();
    approvals.enqueue("ap_new", "tool_execution", {}, { autoOpen: false });

    gate.reject(new Error("连接超时"));
    await recovering;

    expect(approvals.queue.map((a) => a.approval_id)).toEqual(["ap_old", "ap_new"]);
    expect(approvals.recoveryError).toContain("恢复待审批项失败");
    expect(approvals.recoveryError).toContain("可以重试");
  });

  it("refreshApprovalsAndHistory 复用同一竞态保护（补读期间的新审批保留）", async () => {
    const { approvals } = setup();
    approvals.enqueue("ap_old", "computer", {}, { autoOpen: false });

    const gate = deferredPending();
    const refreshing = approvals.refreshApprovalsAndHistory("reconnect");
    approvals.enqueue("ap_new", "tool_execution", {}, { autoOpen: true });
    gate.resolve([] as never);
    await refreshing;

    expect(approvals.queue.map((a) => a.approval_id)).toEqual(["ap_new"]);
  });
});
