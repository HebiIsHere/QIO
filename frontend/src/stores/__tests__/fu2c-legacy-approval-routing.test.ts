/**
 * FU2-C 红用例 1：旧审批（legacy）的路由与"不伪造状态"。
 *
 * 先红后绿（基线 373fda40 上应失败）：
 * 1. /api/approvals/pending 里的旧审批行（state 为空、legacy=true）**不得**被前端补成
 *    v2 waiting —— 补了 state 就会被判为生命周期记录、走 /decide，而后端对 legacy
 *    的 /decide 是明确拒绝的；
 * 2. state=interrupted_legacy 的恢复项只读（未执行/中断待核对），不提供可提交的决定；
 * 3. 旧 /respond 批准后的"已批准（等待执行）"与真实执行结果（executed）分开记录；
 * 4. overrides_supported=false 的旧路径不得发送预算修改，并给出真实原因；
 * 5. v2 记录仍然只走 /decide（防回归）。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";

const mocks = vi.hoisted(() => ({
  getPendingApprovals: vi.fn(),
  decideApproval: vi.fn(),
  confirmApprovalStage: vi.fn(),
  getApprovalHistory: vi.fn(),
  respondApproval: vi.fn(),
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

vi.mock("../../services/api", () => ({ api: { respondApproval: mocks.respondApproval } }));

import {
  approvalFacts,
  isInterruptedLegacyApproval,
  isLegacyApproval,
  isLifecycleApproval,
  useApprovalsStore,
} from "../approvals";
import { useApprovalResultsStore } from "../approvalResults";
import { toPendingApproval } from "../../services/approvalLifecycle";

/** 后端旧 request() 落库行的真实形状：没有 state / operation 为空，legacy=true。 */
const LEGACY_RAW: Record<string, unknown> = {
  approval_id: "ap_legacy",
  kind: "tool_create",
  legacy: true,
  overrides_supported: false,
  // B-CP1：旧审批 state 恒为 null，存活时 status="pending"
  status: "pending",
  created_at: "2026-10-20T09:00:00+08:00",
  payload: {
    name: "sub",
    tool_type: "subagent",
    description: "创建一个子 agent 型工具",
    subagent_budget: { max_iterations: 4, max_tokens: 8000, output_limit_chars: 700 },
  },
  operation: { kind: "tool_create", label: "创建子 agent", summary: "创建子 agent 型工具" },
};

function legacyPending() {
  // 走真实的归一化函数：这里要钉住的正是"归一化不得补 state"
  return toPendingApproval(LEGACY_RAW);
}

function setup() {
  setActivePinia(createPinia());
  return { approvals: useApprovalsStore(), results: useApprovalResultsStore() };
}

beforeEach(() => {
  for (const fn of Object.values(mocks)) fn.mockReset();
  mocks.getApprovalHistory.mockResolvedValue([]);
  mocks.respondApproval.mockResolvedValue({ ok: true });
});

describe("旧审批归一化：不补 state、保留 legacy 事实", () => {
  it("pending 行没有 state 时归一化为空串（绝不伪造 waiting）", () => {
    const p = toPendingApproval(LEGACY_RAW);
    expect(p.state).toBe("");
    expect(p.status).toBe("pending");
    expect(p.legacy).toBe(true);
    expect(p.overrides_supported).toBe(false);
  });

  it("status=interrupted_legacy 识别为旧审批中断事实（state 仍为空）", () => {
    const p = toPendingApproval({ ...LEGACY_RAW, status: "interrupted_legacy" });
    expect(p.state).toBe("");
    expect(p.legacy).toBe(true);
    expect(p.interrupted_legacy).toBe(true);
  });

  it("恢复出来的旧审批不判为 v2 生命周期记录", async () => {
    const { approvals } = setup();
    expect(isLegacyApproval(legacyPending() as never)).toBe(true);
    mocks.getPendingApprovals.mockResolvedValue([legacyPending()]);
    await approvals.recoverPending();
    const item = approvals.current!;
    expect(item.state ?? "").toBe("");
    expect(isLifecycleApproval(item)).toBe(false);
    expect(isLegacyApproval(item)).toBe(true);
    expect(item.legacy).toBe(true);
    expect(item.overridesSupported).toBe(false);
  });
});

describe("legacy 只走 /respond", () => {
  it("恢复出的旧审批批准 → 只调 /respond，绝不 /decide 或 /confirm", async () => {
    const { approvals } = setup();
    mocks.getPendingApprovals.mockResolvedValue([legacyPending()]);
    await approvals.recoverPending();

    await approvals.respondById("ap_legacy", "approved");

    expect(mocks.respondApproval).toHaveBeenCalledTimes(1);
    expect(mocks.decideApproval).not.toHaveBeenCalled();
    expect(mocks.confirmApprovalStage).not.toHaveBeenCalled();
    expect(approvals.queue).toHaveLength(0);
  });

  it("status=interrupted_legacy 的恢复项只读：拒绝在这里提交决定，且不补 state", async () => {
    const { approvals } = setup();
    mocks.getPendingApprovals.mockResolvedValue([
      toPendingApproval({ ...LEGACY_RAW, status: "interrupted_legacy" }),
    ]);
    await approvals.recoverPending();
    const item = approvals.current!;
    expect(isInterruptedLegacyApproval(item)).toBe(true);
    expect(isLifecycleApproval(item)).toBe(false);

    await approvals.respondById("ap_legacy", "approved");
    expect(mocks.respondApproval).not.toHaveBeenCalled();
    expect(mocks.decideApproval).not.toHaveBeenCalled();
    expect(approvals.error).toMatch(/中断|未执行/);
    expect(approvals.queue).toHaveLength(1);
  });
});

describe("B-CP1 409 错误码：后端真相纠偏", () => {
  it("/respond 回 409 legacy_approval_not_executable → 转成只读中断事实，不再留可点按钮", async () => {
    const { approvals } = setup();
    mocks.getPendingApprovals.mockResolvedValue([legacyPending()]);
    await approvals.recoverPending();
    mocks.respondApproval.mockRejectedValueOnce(
      Object.assign(new Error("那次操作没有执行，也不会再执行"), {
        status: 409,
        body: {
          detail: {
            code: "legacy_approval_not_executable",
            message: "那次操作没有执行，也不会再执行",
          },
        },
      }),
    );
    await approvals.respondById("ap_legacy", "approved");
    expect(approvals.error).toContain("没有执行");
    expect(approvals.current?.interruptedLegacy).toBe(true);
    expect(isInterruptedLegacyApproval(approvals.current)).toBe(true);
    expect(approvals.queue).toHaveLength(1);
  });

  it("/decide 回 409 legacy_approval_use_respond → 以后端为准改走 /respond（只提交一次）", async () => {
    const { approvals } = setup();
    // 假设分类漏判：本地以为是 v2（带 state），后端说它其实是 legacy。
    approvals.enqueue("ap_miss", "computer", { description: "旧操作" }, {
      autoOpen: false,
      meta: { state: "waiting", requiresConfirmations: 1 },
    });
    mocks.decideApproval.mockRejectedValueOnce(
      Object.assign(new Error("这条属于旧审批，请走 /respond"), {
        status: 409,
        body: {
          detail: { code: "legacy_approval_use_respond", message: "这条属于旧审批，请走 /respond" },
        },
      }),
    );
    await approvals.respondById("ap_miss", "approved");
    expect(mocks.decideApproval).toHaveBeenCalledTimes(1);
    expect(mocks.respondApproval).toHaveBeenCalledTimes(1);
    expect(approvals.queue).toHaveLength(0);
    const entry = useApprovalResultsStore().byId["ap_miss"];
    expect(entry?.legacy).toBe(true);
    expect(entry?.state).toBe("approved");
  });
});

describe("旧审批的已批准 ≠ 已执行", () => {
  it("批准后只记「已批准（等待执行）」，executed 事件才收口为完成", async () => {
    const { approvals, results } = setup();
    mocks.getPendingApprovals.mockResolvedValue([legacyPending()]);
    await approvals.recoverPending();

    await approvals.respondById("ap_legacy", "approved");
    const approved = results.byId["ap_legacy"];
    expect(approved).toBeTruthy();
    expect(approved.state).toBe("approved");
    expect(approved.ok).toBeNull();
    expect(approved.summary).toContain("等待");
    expect(approved.stateLabel).not.toContain("已完成");

    approvals.recordResult({ approval_id: "ap_legacy", phase: "executed", ok: true, summary: "已创建" });
    const done = results.byId["ap_legacy"];
    expect(done.state).toBe("completed");
    expect(done.ok).toBe(true);
  });
});

describe("预算：只有后端真实支持 overrides 的旧路径可改", () => {
  it("overrides_supported=false 的旧审批带预算修改：不发送、说明真实原因", async () => {
    const { approvals } = setup();
    approvals.enqueue("ap_legacy", "tool_create", LEGACY_RAW.payload as Record<string, unknown>, {
      autoOpen: false,
      meta: { legacy: true, overridesSupported: false },
    });
    await approvals.respondById("ap_legacy", "approved", {
      subagent_budget: { max_iterations: 9 },
    });
    expect(mocks.respondApproval).not.toHaveBeenCalled();
    expect(approvals.error).toMatch(/不支持修改执行预算/);
    expect(approvals.error).toMatch(/没有提交/);
    expect(approvals.queue).toHaveLength(1);
  });

  it("未知 overrides_supported 的旧 tool_create 子智能体路径保持可编辑、照常提交", async () => {
    const { approvals } = setup();
    approvals.enqueue("ap_old", "tool_create", LEGACY_RAW.payload as Record<string, unknown>, {
      autoOpen: false,
      meta: { legacy: true },
    });
    await approvals.respondById("ap_old", "approved", {
      subagent_budget: { max_iterations: 9 },
    });
    expect(mocks.respondApproval).toHaveBeenCalledTimes(1);
    expect(approvals.queue).toHaveLength(0);
  });

  it("approvalFacts：overrides_supported=false 时只读展示预算，不再给可编辑控件", () => {
    const item = {
      approval_id: "ap_legacy",
      kind: "tool_create",
      payload: LEGACY_RAW.payload as Record<string, unknown>,
      legacy: true,
      overridesSupported: false,
    };
    const facts = approvalFacts(item as never);
    expect(facts?.budget).toBeNull();
    expect(facts?.budgetReadOnly).not.toBeNull();
    expect(facts?.isSubagentCreate).toBe(true);
  });

  it("后端 400 overrides_unsupported：显示真实原因，并把这条旧审批改为只读", async () => {
    const { approvals } = setup();
    approvals.enqueue("ap_legacy", "tool_create", LEGACY_RAW.payload as Record<string, unknown>, {
      autoOpen: false,
      meta: { legacy: true },
    });
    mocks.respondApproval.mockRejectedValueOnce(
      Object.assign(new Error("这条旧审批不支持修改执行预算（overrides_unsupported）"), {
        status: 400,
        body: { detail: { code: "overrides_unsupported", message: "这条旧审批不支持修改执行预算" } },
      }),
    );
    await approvals.respondById("ap_legacy", "approved", {
      subagent_budget: { max_iterations: 9 },
    });
    expect(approvals.error).toContain("不支持修改执行预算");
    expect(approvals.queue).toHaveLength(1);
    expect(approvals.current?.overridesSupported).toBe(false);
    expect(approvalFacts(approvals.current)?.budget).toBeNull();
  });
});

describe("防回归：v2 记录仍只走 /decide", () => {
  it("带 state 的审批批准 → decideApproval，不碰 /respond", async () => {
    const { approvals } = setup();
    approvals.enqueue("ap_v2", "computer", { description: "写文件" }, {
      autoOpen: false,
      meta: { state: "waiting", requiresConfirmations: 1, revision: 3 },
    });
    mocks.decideApproval.mockResolvedValue(
      toPendingApproval({ ...LEGACY_RAW, approval_id: "ap_v2", state: "approved", legacy: false }),
    );
    await approvals.respondById("ap_v2", "approved");
    expect(mocks.decideApproval).toHaveBeenCalledWith("ap_v2", "approve", { expectedRevision: 3 });
    expect(mocks.respondApproval).not.toHaveBeenCalled();
  });
});
