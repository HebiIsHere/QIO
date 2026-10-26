/**
 * F 独立验收（基线 373fda40）· 反例 4/5：断线重连后审批结果必须闭合。
 *
 * 契约（COMMON §4.3）：
 * - SSE onopen（断线重连）/ RESYNC / 窗口回前台三类时机主动补读 pending + history，
 *   按 approval_id 合并；终态 history/事件可以收口「执行中/已批准」；不串项；
 *   读取失败可见且可重试，不伪装空集合；
 * - 已批准（等待执行）与执行成功/失败严格区分。
 *
 * 基线预期（红）：restoreRuntimeState（唯一恢复入口）只拉运行时快照，
 * 不补读审批历史 / 待审批；断线期间已完成的审批会一直停在「执行中」。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";

const mocks = vi.hoisted(() => ({
  getApprovalHistory: vi.fn(),
  getPendingApprovals: vi.fn(),
}));

vi.mock("../../services/api", () => ({
  api: {
    getRuntimeState: vi.fn(),
    getDevTasks: vi.fn(async () => ({ tasks: [] })),
    listDevAuthorizations: vi.fn(async () => ({ authorizations: [] })),
    dismissInterruptedTurn: vi.fn(async () => ({ ok: true })),
    resendInterruptedTurn: vi.fn(async () => ({ ok: true })),
  },
}));

vi.mock("../../services/recoveryApi", () => ({
  fetchRecoveryRecords: vi.fn(async () => ({ records: [], total: 0 })),
  confirmStopped: vi.fn(async () => ({ ok: true })),
  continueRecovery: vi.fn(async () => ({ ok: true })),
  ignoreRecovery: vi.fn(async () => ({ ok: true })),
  repairOrphan: vi.fn(async () => ({ ok: true })),
  requeueDerived: vi.fn(async () => ({ ok: true })),
  isRecoveryConflict: () => false,
}));

vi.mock("../../services/approvalLifecycle", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../services/approvalLifecycle")>();
  return {
    ...actual,
    getApprovalHistory: mocks.getApprovalHistory,
    getPendingApprovals: mocks.getPendingApprovals,
  };
});

import { api } from "../../services/api";
import { restoreRuntimeState } from "../../stores/restore";
import { useApprovalResultsStore } from "../../stores/approvalResults";

const STAMP = "2026-10-26T00:00:00.000Z";

function runtimeState(overrides: Record<string, unknown> = {}) {
  return {
    instance_id: "inst_fu2",
    revision: 7,
    turn_queue: {
      instance_id: "inst_fu2",
      revision: 7,
      running: null,
      queued: [],
      cancelled: [],
    },
    approvals: [],
    tasks: [],
    tools: [],
    narratives: [],
    interrupted_approvals: [],
    interrupted_turns: [],
    ...overrides,
  };
}

function historyRow(approvalId: string, outcome: string, summary: string) {
  return { approval_id: approvalId, outcome, summary, at: STAMP };
}

function setup() {
  const pinia = createPinia();
  setActivePinia(pinia);
  return { results: useApprovalResultsStore() };
}

beforeEach(() => {
  vi.clearAllMocks();
  mocks.getApprovalHistory.mockResolvedValue([]);
  mocks.getPendingApprovals.mockResolvedValue([]);
  vi.mocked(api.getRuntimeState).mockResolvedValue(runtimeState() as never);
});

describe("断线重连：审批结果闭环", () => {
  it("断线期间审批完成：重连后按 approval_id 收口「执行中」→ 终态（不再停执行中）", async () => {
    const { results } = setup();
    results.recordEvent({
      approval_id: "appr_reconnect_done",
      phase: "executing",
      summary: "正在执行",
    });
    expect(results.byId["appr_reconnect_done"]?.state).toBe("executing");

    mocks.getApprovalHistory.mockResolvedValue([
      historyRow("appr_reconnect_done", "executed", "执行完成：写入 2 个文件"),
    ]);

    await restoreRuntimeState("reconnect");

    expect(
      mocks.getApprovalHistory,
      "重连恢复没有补读审批历史：断线期间完成的审批会一直停在「执行中」",
    ).toHaveBeenCalled();
    const entry = results.byId["appr_reconnect_done"];
    expect(entry?.state, "重连后仍停在非终态（应为 completed/executed）").toBe("completed");
    expect(entry?.summary).toContain("执行完成");
    expect(results.inFlight.map((item) => item.approvalId)).not.toContain("appr_reconnect_done");
  });

  it("重连同时补读待审批（pending）；读取结果按 approval_id 合并，不串项", async () => {
    const { results } = setup();
    results.recordEvent({ approval_id: "appr_stuck", phase: "approved", summary: "已批准，等待执行" });
    results.recordEvent({ approval_id: "appr_other", phase: "executing", summary: "另一条正在执行" });

    mocks.getApprovalHistory.mockResolvedValue([
      historyRow("appr_stuck", "executed", "执行完成：只更新这一条"),
    ]);
    mocks.getPendingApprovals.mockResolvedValue([]);

    await restoreRuntimeState("resync");

    expect(mocks.getPendingApprovals, "重连恢复没有补读待审批").toHaveBeenCalled();
    expect(results.byId["appr_stuck"]?.state).toBe("completed");
    // 串项反例：另一条审批不得被这条历史改写
    expect(results.byId["appr_other"]?.state).toBe("executing");
    expect(results.byId["appr_other"]?.summary).toContain("另一条");
  });

  it("历史读取失败：保留已有内容 + 可见错误（不能伪装成没有历史 / 静默停在进行中）", async () => {
    const { results } = setup();
    results.recordEvent({
      approval_id: "appr_failread",
      phase: "executing",
      summary: "正在执行（断线前最后状态）",
    });
    mocks.getApprovalHistory.mockRejectedValue(new Error("HTTP 500：服务端内部错误"));

    await restoreRuntimeState("reconnect");

    expect(
      mocks.getApprovalHistory,
      "重连恢复没有补读审批历史（读失败也不可能被看见）",
    ).toHaveBeenCalled();
    expect(results.historyError, "历史读取失败没有可见原因").toBeTruthy();
    expect(results.historyError).toContain("读取审批历史失败");
    expect(results.byId["appr_failread"]?.state, "失败时不应丢掉已有内容").toBe("executing");
    // 可重试：同一入口再读一次（成功）后错误清除、终态落地
    mocks.getApprovalHistory.mockResolvedValue([
      historyRow("appr_failread", "failed", "执行失败：目标文件被占用"),
    ]);
    await results.loadHistory();
    expect(results.historyError).toBeNull();
    expect(results.byId["appr_failread"]?.state).toBe("failed");
  });

  it("已批准 ≠ 已执行：历史只有 approved 时不得显示成完成", async () => {
    const { results } = setup();
    results.recordEvent({ approval_id: "appr_approved_only", phase: "approved", summary: "已批准，等待执行" });
    mocks.getApprovalHistory.mockResolvedValue([
      historyRow("appr_approved_only", "approved", "已批准，等待后端执行"),
    ]);

    await restoreRuntimeState("reconnect");

    const entry = results.byId["appr_approved_only"];
    expect(entry?.state).toBe("approved");
    expect(entry?.ok).toBeNull();
    expect(results.inFlight.map((item) => item.approvalId)).toContain("appr_approved_only");
  });
});
