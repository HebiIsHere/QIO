/**
 * FU2-C 红用例 4：预算一致性（可编辑 ⇔ 后端真实支持 overrides）。
 *
 * 先红后绿（基线 373fda40 上应失败）：
 * 1. v2 生命周期记录（带 state）即使有 subagent_budget 也只读，不显示可编辑控件；
 *    批准只走 /decide，不携带 overrides；
 * 2. 旧 tool_create 子智能体路径（overrides 端到端支持）仍可编辑；
 * 3. 后端明确 overrides_supported=false 的旧路径只读，并在弹窗里说明真实原因；
 * 4. 后端 400 overrides_unsupported → 显示真实原因、不表现为"已接受修改"。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";

const mocks = vi.hoisted(() => ({
  respondApproval: vi.fn(),
  request: vi.fn(),
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
    decideApproval: mocks.decideApproval,
    confirmApprovalStage: mocks.confirmApprovalStage,
    getPendingApprovals: mocks.getPendingApprovals,
    getApprovalHistory: mocks.getApprovalHistory,
  };
});

import UnifiedApprovalModal from "../UnifiedApprovalModal.vue";
import { useApprovalsStore } from "../../../stores/approvals";
import { useUnifiedApprovalsStore } from "../../../stores/unifiedApprovals";

const SUBAGENT_PAYLOAD: Record<string, unknown> = {
  name: "sub",
  tool_type: "subagent",
  description: "创建一个子 agent 型工具",
  subagent_budget: { max_iterations: 4, max_tokens: 8000, output_limit_chars: 700 },
};

function setup() {
  setActivePinia(createPinia());
  return { approvals: useApprovalsStore(), unified: useUnifiedApprovalsStore() };
}

beforeEach(() => {
  for (const fn of Object.values(mocks)) fn.mockReset();
  mocks.request.mockResolvedValue({ items: [], total: 0 });
  mocks.respondApproval.mockResolvedValue({ ok: true });
  mocks.getApprovalHistory.mockResolvedValue([]);
});

describe("v2 生命周期记录：预算只读、提交不带 overrides", () => {
  it("带 state 的子智能体审批只读；批准只调 /decide，不带 overrides", async () => {
    const { approvals, unified } = setup();
    approvals.enqueue("ap_v2", "tool_create", SUBAGENT_PAYLOAD, {
      autoOpen: true,
      meta: { state: "waiting", requiresConfirmations: 1, revision: 5 },
    });
    unified.open("tool:ap_v2");
    mocks.decideApproval.mockResolvedValue(
      (await import("../../../services/approvalLifecycle")).toPendingApproval({
        approval_id: "ap_v2",
        kind: "tool_create",
        state: "approved",
        payload: SUBAGENT_PAYLOAD,
      }),
    );
    const w = mount(UnifiedApprovalModal);
    await flushPromises();

    expect(w.find('[data-test="approval-budget-readonly"]').exists()).toBe(true);
    expect(w.find('[data-test="approval-budget-editable"]').exists()).toBe(false);

    await w.find('[data-test="unified-approval-allow"]').trigger("click");
    await flushPromises();

    expect(mocks.decideApproval).toHaveBeenCalledWith("ap_v2", "approve", { expectedRevision: 5 });
    expect(mocks.respondApproval).not.toHaveBeenCalled();
    // 提交参数里不得出现 overrides / subagent_budget
    for (const call of mocks.decideApproval.mock.calls) {
      expect(JSON.stringify(call)).not.toContain("subagent_budget");
      expect(JSON.stringify(call)).not.toContain("overrides");
    }
    w.unmount();
  });
});

describe("旧路径：只有后端真实支持 overrides 才能编辑", () => {
  it("旧 tool_create 子智能体（未给标志、历史行为支持）→ 可编辑", async () => {
    const { approvals, unified } = setup();
    approvals.enqueue("ap_old", "tool_create", SUBAGENT_PAYLOAD, {
      autoOpen: true,
      meta: { legacy: true },
    });
    unified.open("tool:ap_old");
    const w = mount(UnifiedApprovalModal);
    await flushPromises();
    expect(w.find('[data-test="approval-budget-editable"]').exists()).toBe(true);
    expect(w.find('[data-test="approval-budget-readonly"]').exists()).toBe(false);
    w.unmount();
  });

  it("overrides_supported=false → 只读，并说明后端不支持修改（不假装可改）", async () => {
    const { approvals, unified } = setup();
    approvals.enqueue("ap_legacy", "tool_create", SUBAGENT_PAYLOAD, {
      autoOpen: true,
      meta: { legacy: true, overridesSupported: false },
    });
    unified.open("tool:ap_legacy");
    const w = mount(UnifiedApprovalModal);
    await flushPromises();

    expect(w.find('[data-test="approval-budget-editable"]').exists()).toBe(false);
    expect(w.find('[data-test="approval-budget-readonly"]').exists()).toBe(true);
    expect(w.text()).toContain("不支持修改");
    expect(w.find('[data-test="unified-approval-budget-unsupported"]').exists()).toBe(true);
    w.unmount();
  });

  it("后端 400 overrides_unsupported → 显示真实原因，不表现为已接受修改，条目保留", async () => {
    const { approvals, unified } = setup();
    approvals.enqueue("ap_legacy", "tool_create", SUBAGENT_PAYLOAD, {
      autoOpen: true,
      meta: { legacy: true },
    });
    mocks.respondApproval.mockRejectedValueOnce(
      Object.assign(new Error("这条旧审批不支持修改执行预算（overrides_unsupported）"), {
        status: 400,
        body: { detail: { code: "overrides_unsupported", message: "这条旧审批不支持修改执行预算" } },
      }),
    );
    unified.open("tool:ap_legacy");
    const w = mount(UnifiedApprovalModal);
    await flushPromises();

    // 直接走 store 的真实提交路径（与弹窗按钮同一入口）
    await approvals.respondById("ap_legacy", "approved", { subagent_budget: { max_iterations: 9 } });
    await flushPromises();

    const text = w.text();
    expect(text).toContain("不支持修改执行预算");
    expect(text).not.toContain("已按你修改");
    expect(approvals.queue).toHaveLength(1);
    w.unmount();
  });
});
