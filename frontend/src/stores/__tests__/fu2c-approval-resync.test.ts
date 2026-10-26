/**
 * FU2-C 红用例 2：断线重连 / RESYNC / 回到前台时的审批补读与合并。
 *
 * 先红后绿（基线 373fda40 上应失败）：
 * 1. SSE onopen（含断线重连）除了权威运行状态，还必须主动补读 pending + history；
 * 2. RESYNC 完成后同样补读（不是只拉 runtime state）；
 * 3. 按 approval_id 合并：pending 里仍在等待的项保留；history/事件的终态收口
 *    本地一直停在「执行中/已批准」的记录；不重复增行、不串项、不触发任何提交；
 * 4. pending 与 history 读取失败分别可见、分别可重试，不伪装为空。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";

const captured = vi.hoisted(() => ({
  onEvent: null as ((event: unknown) => void) | null,
  handle: null as { onopen: (() => void) | null; onerror: ((e?: unknown) => void) | null; close: () => void } | null,
}));

const mocks = vi.hoisted(() => ({
  getPendingApprovals: vi.fn(),
  getApprovalHistory: vi.fn(),
  decideApproval: vi.fn(),
  confirmApprovalStage: vi.fn(),
  respondApproval: vi.fn(),
  getRuntimeState: vi.fn(),
}));

vi.mock("../../services/events", () => ({
  connectEvents: (onEvent: (event: unknown) => void) => {
    captured.onEvent = onEvent;
    captured.handle = { onopen: null, onerror: null, close: () => {} };
    return captured.handle;
  },
  publishTestEvent: vi.fn(async () => undefined),
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
  api: {
    respondApproval: mocks.respondApproval,
    getRuntimeState: mocks.getRuntimeState,
    getDevTasks: vi.fn(async () => ({ tasks: [] })),
    listDevAuthorizations: vi.fn(async () => ({ authorizations: [] })),
    getRecoveryRecords: vi.fn(async () => ({ records: [], shown: 0, total: 0 })),
  },
}));

import { useApprovalsStore } from "../approvals";
import { useApprovalResultsStore } from "../approvalResults";
import { useEventStore } from "../events";
import { toPendingApproval } from "../../services/approvalLifecycle";

function runtimeState(overrides: Record<string, unknown> = {}) {
  return {
    instance_id: "inst_1",
    revision: 1,
    turn_queue: { instance_id: "inst_1", revision: 1, running: null, queued: [], cancelled: [] },
    approvals: [],
    tasks: [],
    tools: [],
    narratives: [],
    interrupted_approvals: [],
    interrupted_turns: [],
    ...overrides,
  };
}

function setup() {
  setActivePinia(createPinia());
  return {
    approvals: useApprovalsStore(),
    results: useApprovalResultsStore(),
    events: useEventStore(),
  };
}

beforeEach(() => {
  captured.onEvent = null;
  captured.handle = null;
  for (const fn of Object.values(mocks)) fn.mockReset();
  mocks.getRuntimeState.mockResolvedValue(runtimeState() as never);
  mocks.getPendingApprovals.mockResolvedValue([]);
  mocks.getApprovalHistory.mockResolvedValue([]);
  mocks.respondApproval.mockResolvedValue({ ok: true });
});

describe("补读时机：onopen / RESYNC", () => {
  it("SSE 连接建立（含重连）→ 主动补读 pending + history", async () => {
    const { events } = setup();
    events.connect();
    await flushPromises();

    captured.handle?.onopen?.();
    await flushPromises();

    expect(mocks.getRuntimeState).toHaveBeenCalledTimes(1);
    expect(mocks.getPendingApprovals).toHaveBeenCalledTimes(1);
    expect(mocks.getApprovalHistory).toHaveBeenCalled();
  });

  it("RESYNC 完成 → 除了权威运行状态，还要补读 pending + history", async () => {
    const { events } = setup();
    events.connect();
    await flushPromises();
    expect(mocks.getPendingApprovals).not.toHaveBeenCalled();

    captured.onEvent?.({ id: "e1", ts: "", type: "RESYNC", data: {} });
    await flushPromises();

    expect(mocks.getRuntimeState).toHaveBeenCalledTimes(1);
    expect(mocks.getPendingApprovals).toHaveBeenCalledTimes(1);
    expect(mocks.getApprovalHistory).toHaveBeenCalled();
  });
});

describe("runtime 快照的旧审批中断事实（只读，不是待办）", () => {
  it("收进 approvals.legacyInterrupted；不进待办队列；旧后端缺字段时不擦成空", async () => {
    const { events, approvals } = setup();
    mocks.getRuntimeState.mockResolvedValue(
      runtimeState({
        interrupted_approvals: [
          {
            approval_id: "ia_legacy",
            kind: "computer",
            what: "运行一段命令",
            legacy: true,
            state: null,
            status: "interrupted_legacy",
            outcome: "not_executed",
            created_at: "2026-10-26T09:00:00+08:00",
          },
        ],
      }) as never,
    );

    events.connect();
    captured.handle?.onopen?.();
    await flushPromises();

    expect(approvals.legacyInterrupted.map((row) => row.approvalId)).toEqual(["ia_legacy"]);
    expect(approvals.legacyInterrupted[0]?.what).toBe("运行一段命令");
    // 只读事实不是待办：不能进审批队列、不能算作等待确认
    expect(approvals.queue.some((item) => item.approval_id === "ia_legacy")).toBe(false);

    // 旧后端快照没有 interrupted_approvals 字段：保持现状（不伪装成「没有中断」）
    events.applyRuntimeState({ approvals: [], tasks: [] } as never);
    expect(approvals.legacyInterrupted).toHaveLength(1);
  });
});

describe("按 approval_id 合并：终态历史收口，不重复、不串项、不提交", () => {
  it("本机停在 executing，history 说 executed → 结果收口为完成，队列不再等待", async () => {
    const { approvals, results } = setup();
    approvals.enqueue("ap_run", "tool_execution", { description: "写入文件" }, {
      autoOpen: false,
      meta: { state: "executing" },
    });
    results.recordEvent({ approval_id: "ap_run", phase: "executing" });
    expect(results.byId["ap_run"].state).toBe("executing");

    mocks.getPendingApprovals.mockResolvedValue([
      toPendingApproval({
        approval_id: "ap_run",
        kind: "tool_execution",
        state: "approved",
        created_at: "2026-10-26T09:00:00+08:00",
        payload: { description: "写入文件" },
      }),
    ]);
    mocks.getApprovalHistory.mockResolvedValue([
      {
        approval_id: "ap_run",
        kind: "tool_execution",
        summary: "已写入 2 个文件",
        outcome: "executed",
        detail: { ok: true, label: "写入文件" },
        at: "2026-10-26T09:01:00+08:00",
      },
    ]);

    await approvals.refreshApprovalsAndHistory("reconnect");
    await flushPromises();

    const entry = results.byId["ap_run"];
    expect(entry.state).toBe("completed");
    expect(entry.ok).toBe(true);
    // 终态收口：这条不再停在待审批队列里等用户
    expect(approvals.queue.some((item) => item.approval_id === "ap_run")).toBe(false);
    // 不新增重复行
    expect(results.entries.filter((item) => item.approvalId === "ap_run")).toHaveLength(1);
    // 补读是只读：不重复提交 / 执行
    expect(mocks.respondApproval).not.toHaveBeenCalled();
    expect(mocks.decideApproval).not.toHaveBeenCalled();
    expect(mocks.confirmApprovalStage).not.toHaveBeenCalled();
  });

  it("pending 里仍在等待的项保留；重复补读不产生第二行", async () => {
    const { approvals, results } = setup();
    const waiting = toPendingApproval({
      approval_id: "ap_wait",
      kind: "computer",
      state: "waiting",
      created_at: "2026-10-26T09:00:00+08:00",
      payload: { description: "维护内部数据" },
    });
    mocks.getPendingApprovals.mockResolvedValue([waiting]);

    await approvals.refreshApprovalsAndHistory("reconnect");
    await approvals.refreshApprovalsAndHistory("reconnect");

    expect(approvals.queue.filter((item) => item.approval_id === "ap_wait")).toHaveLength(1);
    expect(approvals.current?.state).toBe("waiting");
    expect(results.entries.filter((item) => item.approvalId === "ap_wait")).toHaveLength(0);
  });
});

describe("读取失败：分别可见、分别可重试、不伪装为空", () => {
  it("pending 与 history 同时失败 → 两个独立错误；重试后各自恢复", async () => {
    const { approvals, results } = setup();
    mocks.getPendingApprovals.mockRejectedValueOnce(new Error("pending 读失败"));
    mocks.getApprovalHistory.mockRejectedValueOnce(new Error("history 读失败"));

    await approvals.refreshApprovalsAndHistory("reconnect");

    expect(approvals.recoveryError).toContain("恢复待审批项失败");
    expect(results.historyError).toContain("读取审批历史失败");
    // 不伪装为空集合
    expect(approvals.recovered).toBe(true);

    mocks.getPendingApprovals.mockResolvedValueOnce([]);
    mocks.getApprovalHistory.mockResolvedValueOnce([]);
    await approvals.retryRecovery();
    await results.loadHistory();
    expect(approvals.recoveryError).toBeNull();
    expect(results.historyError).toBeNull();
  });
});
