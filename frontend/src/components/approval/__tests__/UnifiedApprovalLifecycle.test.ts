/**
 * 统一审批层 × R9 生命周期（B 阶段二新增）。
 *
 * 钉住五条：
 * 1. v2 审批（带 state）只走 decide / confirm，绝不回落旧 /respond；
 * 2. 内部运行数据：按钮是「继续确认（第 N/M 次）」，阶段由后端 CAS 决定，前端不累计；
 * 3. v2 失败（409/404）保留待办 + 「未做出任何授权」，且不降级到 /respond；
 * 4. 已批准 / 执行中：不给决定按钮，只给「知道了」；
 * 5. 旧审批（无 state）行为不变。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";

const mocks = vi.hoisted(() => ({
  request: vi.fn(),
  respondApproval: vi.fn(),
  decideApproval: vi.fn(),
  confirmApprovalStage: vi.fn(),
  getPendingApprovals: vi.fn(),
  getApprovalHistory: vi.fn(),
}));

vi.mock("../../../services/api", () => ({
  request: mocks.request,
  api: { respondApproval: mocks.respondApproval },
}));

vi.mock("../../../services/approvalLifecycle", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../../services/approvalLifecycle")>();
  return {
    ...actual,
    getPendingApprovals: mocks.getPendingApprovals,
    decideApproval: mocks.decideApproval,
    confirmApprovalStage: mocks.confirmApprovalStage,
    getApprovalHistory: mocks.getApprovalHistory,
  };
});

import UnifiedApprovalModal from "../UnifiedApprovalModal.vue";
import { useApprovalsStore } from "../../../stores/approvals";
import { useUnifiedApprovalsStore } from "../../../stores/unifiedApprovals";
import type { PendingApproval } from "../../../services/approvalLifecycle";

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
  return { unified: useUnifiedApprovalsStore(), approvals: useApprovalsStore() };
}

beforeEach(() => {
  for (const fn of Object.values(mocks)) fn.mockReset();
  mocks.request.mockResolvedValue({ ok: true, decision: "approve" });
  mocks.respondApproval.mockResolvedValue({ ok: true });
  mocks.getApprovalHistory.mockResolvedValue([]);
});

describe("v2 审批：出口只走生命周期接口", () => {
  it("普通批准 → decide，不碰旧 /respond", async () => {
    const { unified, approvals } = setup();
    approvals.enqueue("ap_1", "computer", { description: "写文件" }, {
      autoOpen: true,
      meta: { state: "waiting", requiresConfirmations: 1, revision: 3 },
    });
    unified.open("tool:ap_1");
    mocks.decideApproval.mockResolvedValue(pending({ state: "approved" }));

    const ok = await unified.submit("approve");

    expect(ok).toBe(true);
    expect(mocks.decideApproval).toHaveBeenCalledWith("ap_1", "approve", { expectedRevision: 3 });
    expect(mocks.respondApproval).not.toHaveBeenCalled();
    expect(approvals.queue).toHaveLength(0);
  });

  it("内部数据：第一次「允许」是第 1 次确认，成功但仍在队列（进度前进≠失败）", async () => {
    const { unified, approvals } = setup();
    approvals.enqueue("ap_1", "computer", {}, {
      autoOpen: true,
      meta: { state: "waiting", requiresConfirmations: 3, confirmedStage: 0, revision: 1 },
    });
    unified.open("tool:ap_1");
    mocks.confirmApprovalStage.mockResolvedValue(
      pending({ state: "waiting", confirmed_stage: 1, requires_confirmations: 3, revisions: 2 }),
    );

    const ok = await unified.submit("approve");

    expect(ok).toBe(true);
    expect(mocks.confirmApprovalStage).toHaveBeenCalledWith("ap_1", 1, { expectedRevision: 1 });
    expect(mocks.decideApproval).not.toHaveBeenCalled();
    expect(mocks.respondApproval).not.toHaveBeenCalled();
    expect(approvals.queue).toHaveLength(1);
    expect(approvals.current?.confirmedStage).toBe(1);
    expect(unified.submitError).toBe("");
  });

  it("拒绝 → decide(reject)", async () => {
    const { unified, approvals } = setup();
    approvals.enqueue("ap_1", "computer", {}, { autoOpen: true, meta: { state: "waiting" } });
    unified.open("tool:ap_1");
    mocks.decideApproval.mockResolvedValue(pending({ state: "rejected" }));

    await unified.submit("reject");

    expect(mocks.decideApproval).toHaveBeenCalledWith("ap_1", "reject", {
      expectedRevision: undefined,
    });
    expect(mocks.respondApproval).not.toHaveBeenCalled();
  });

  it("409 → 保留待办、说明未授权，绝不降级到 /respond", async () => {
    const { unified, approvals } = setup();
    approvals.enqueue("ap_1", "computer", {}, { autoOpen: true, meta: { state: "waiting" } });
    unified.open("tool:ap_1");
    mocks.decideApproval.mockRejectedValue({ status: 409, message: "确认阶段不连续：应为第 1 次确认" });

    const ok = await unified.submit("approve");

    expect(ok).toBe(false);
    expect(mocks.respondApproval).not.toHaveBeenCalled();
    expect(approvals.queue).toHaveLength(1);
    expect(approvals.error).toContain("未做出任何授权");
    expect(unified.submitError).toContain("未做出任何授权");
  });

  it("404 → 标记失效，不重试旧接口", async () => {
    const { unified, approvals } = setup();
    approvals.enqueue("ap_1", "computer", {}, { autoOpen: true, meta: { state: "waiting" } });
    unified.open("tool:ap_1");
    mocks.decideApproval.mockRejectedValue({ status: 404, message: "没有找到这条审批" });

    await unified.submit("approve");

    expect(mocks.respondApproval).not.toHaveBeenCalled();
    expect(approvals.current?.stale).toBe(true);
  });
});

describe("旧审批（无 state）：/respond 兼容不丢", () => {
  it("照旧走 respondApproval，带 turn/session/digest 绑定", async () => {
    const { unified, approvals } = setup();
    approvals.enqueue("ap_old", "tool_execution", { description: "旧审批" }, {
      autoOpen: true,
      turnId: "turn_1",
      sessionId: "sess_1",
      requestDigest: "dig_1",
    });
    unified.open("tool:ap_old");

    const ok = await unified.submit("approve");

    expect(ok).toBe(true);
    expect(mocks.respondApproval).toHaveBeenCalledWith("ap_old", "approved", undefined, {
      turnId: "turn_1",
      sessionId: "sess_1",
      requestDigest: "dig_1",
    });
    expect(mocks.decideApproval).not.toHaveBeenCalled();
  });
});

describe("统一弹窗：R9 生命周期呈现", () => {
  it("内部数据：按钮是「继续确认（第 1/3 次）」，不出现「允许此次操作」", async () => {
    const { unified, approvals } = setup();
    approvals.enqueue("ap_1", "computer", { description: "维护 QIO 内部运行数据" }, {
      autoOpen: true,
      meta: { state: "waiting", requiresConfirmations: 3, confirmedStage: 0 },
    });
    unified.open("tool:ap_1");
    const w = mount(UnifiedApprovalModal);
    await flushPromises();

    const next = w.find('[data-test="unified-approval-confirm-next"]');
    expect(next.exists()).toBe(true);
    expect(next.text()).toContain("继续确认（第 1/3 次）");
    expect(w.find('[data-test="unified-approval-allow"]').exists()).toBe(false);
    const progress = w.find('[data-test="unified-approval-progress"]');
    expect(progress.exists()).toBe(true);
    expect(progress.text()).toContain("0/3");
    w.unmount();
  });

  it("已批准 / 执行中：不给决定按钮，只给「知道了」", async () => {
    const { unified, approvals } = setup();
    approvals.enqueue("ap_1", "computer", {}, {
      autoOpen: true,
      meta: { state: "executing", requiresConfirmations: 1 },
    });
    unified.open("tool:ap_1");
    const w = mount(UnifiedApprovalModal);
    await flushPromises();

    expect(w.find('[data-test="unified-approval-awaiting"]').exists()).toBe(true);
    expect(w.find('[data-test="unified-approval-allow"]').exists()).toBe(false);
    expect(w.find('[data-test="unified-approval-reject"]').exists()).toBe(false);
    await w.find('[data-test="unified-approval-awaiting-ack"]').trigger("click");
    expect(approvals.queue).toHaveLength(0);
    w.unmount();
  });

  it("状态标签与期限来自后端；旧审批不渲染这一行", async () => {
    const { unified, approvals } = setup();
    approvals.enqueue("ap_1", "computer", {}, {
      autoOpen: true,
      meta: {
        state: "waiting",
        requiresConfirmations: 1,
        expiresAt: "2026-10-22T10:10:00+08:00",
      },
    });
    unified.open("tool:ap_1");
    const w = mount(UnifiedApprovalModal);
    await flushPromises();
    const line = w.find('[data-test="unified-approval-lifecycle"]');
    expect(line.exists()).toBe(true);
    expect(line.text()).toContain("等待确认");
    expect(line.text()).toContain("2026-10-22T10:10:00+08:00");
    w.unmount();

    const second = setup();
    second.approvals.enqueue("ap_old", "tool_execution", {}, { autoOpen: true });
    second.unified.open("tool:ap_old");
    const w2 = mount(UnifiedApprovalModal);
    await flushPromises();
    expect(w2.find('[data-test="unified-approval-lifecycle"]').exists()).toBe(false);
    expect(w2.find('[data-test="unified-approval-allow"]').exists()).toBe(true);
    w2.unmount();
  });

  it("409 失败按「对象已变化」前缀如实显示后端原因", async () => {
    const { unified, approvals } = setup();
    approvals.enqueue("ap_1", "computer", {}, { autoOpen: true, meta: { state: "waiting" } });
    unified.open("tool:ap_1");
    mocks.decideApproval.mockRejectedValue({ status: 409, message: "这条审批已经被更新过（revision 不匹配），请刷新后再决定" });
    const w = mount(UnifiedApprovalModal);
    await flushPromises();

    await w.find('[data-test="unified-approval-allow"]').trigger("click");
    await flushPromises();
    const err = w.find(".ua-error");
    expect(err.exists()).toBe(true);
    expect(err.text()).toContain("对象已变化");
    expect(err.text()).toContain("revision 不匹配");
    w.unmount();
  });
});
