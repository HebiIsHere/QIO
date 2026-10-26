/**
 * E 独立验收 · 只读生命周期记录与确认进度（阶段 1 基线复现 / 阶段 2 候选复验）
 *
 * COMMON §4.1 / F06：
 * - 已批准 / 执行中的生命周期记录是**只读**的：不再显示可编辑预算控件，
 *   也不能继续写「可修改后批准」——对用户来说那是一个改不了的假入口；
 * - 内部数据的三次确认进度只能来自后端事实（confirmed_stage / requires_confirmations），
 *   前端不得自行推进。
 *
 * 基线：ApprovalFacts 只要 payload 带 subagent_budget 就渲染预算表单，
 * 与审批状态无关；已批准的记录仍显示「子 agent 执行预算（可修改后批准）」。
 *
 * FU2-C 契约更新（COMMON §4.3 / B-CP1）：预算可编辑 ⇔ 后端端到端支持 overrides 的
 * **旧路径**（legacy，无 state）。v2 生命周期记录（有 state）即使仍是 waiting 也只读
 * —— /decide 不接受 overrides，显示可编辑按钮就是「改了也不生效」的假入口。
 * 因此「等待中仍可修改」一条改为钉在旧路径上，并新增 v2 waiting 只读的反向断言。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { mount, type VueWrapper } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { nextTick } from "vue";

const mocks = vi.hoisted(() => ({ request: vi.fn(), respondApproval: vi.fn() }));
vi.mock("../../services/api", () => ({
  request: mocks.request,
  api: { respondApproval: mocks.respondApproval, loadHistory: vi.fn(), getUISettings: vi.fn() },
}));

import UnifiedApprovalModal from "../../components/approval/UnifiedApprovalModal.vue";
import { useApprovalsStore, type ApprovalItem } from "../../stores/approvals";
import { useUnifiedApprovalsStore } from "../../stores/unifiedApprovals";

const mounted: VueWrapper[] = [];
function track<T extends VueWrapper>(wrapper: T): T {
  mounted.push(wrapper);
  return wrapper;
}

function subagentApproval(over: Partial<ApprovalItem> = {}): ApprovalItem {
  return {
    approval_id: "appr_budget",
    kind: "tool_create",
    payload: {
      name: "子智能体：整理发布材料",
      tool_type: "subagent",
      subagent_budget: { max_iterations: 7, max_tokens: 200000, output_limit_chars: 4000 },
      description: "创建一个子智能体来整理发布材料",
    },
    turnId: null,
    sessionId: null,
    requestDigest: null,
    state: "waiting",
    label: "创建子智能体",
    purpose: "整理发布材料",
    ...over,
  };
}

function openModal(item: ApprovalItem): VueWrapper {
  const approvals = useApprovalsStore();
  const unified = useUnifiedApprovalsStore();
  approvals.queue = [item];
  unified.noteQueueChanged();
  unified.open();
  const w = track(mount(UnifiedApprovalModal));
  return w;
}

beforeEach(() => {
  vi.clearAllMocks();
  setActivePinia(createPinia());
  document.body.innerHTML = "";
  mocks.request.mockResolvedValue({ items: [] });
  mocks.respondApproval.mockResolvedValue({ ok: true });
});

afterEach(() => {
  while (mounted.length) mounted.pop()?.unmount();
  document.body.innerHTML = "";
});

describe("只读生命周期记录不提供假的可编辑预算（F06）", () => {
  it("已批准 / 等待执行时：仍有「等待执行」说明，但没有预算编辑控件与「可修改后批准」文案", async () => {
    const w = openModal(subagentApproval({ state: "approved" }));
    await nextTick();

    expect(w.text()).toContain("已批准");
    expect(w.text(), "只读的已批准记录仍然写着「可修改后批准」，但界面上已没有可修改后批准的按钮").not.toContain("可修改后批准");
    // 只读展示允许（事实要照实说），但可编辑控件必须消失
    expect(w.find('[data-test="approval-budget-editable"]').exists(), "只读记录仍显示可编辑的预算控件").toBe(false);
    expect(w.find('[data-test="approval-budget-readonly"]').exists(), "只读记录没有把登记预算照实显示（信息丢失）").toBe(true);
    // 确认没有可编辑的数字输入（预算三件套）
    const numberInputs = w.findAll('input[type="number"]');
    expect(numberInputs.length, "只读记录仍有数字输入框").toBe(0);
    // 只读值必须真的显示出来（不是空壳）
    expect(w.text()).toContain("4000");
  });

  it("执行中同样只读：不出现预算编辑", async () => {
    const w = openModal(subagentApproval({ state: "executing" }));
    await nextTick();
    expect(w.find('[data-test="approval-budget-editable"]').exists()).toBe(false);
    expect(w.text()).not.toContain("可修改后批准");
  });

  it("仍在等待用户决定的**旧路径**审批：预算编辑与「可修改后批准」必须保留（真实 overrides 入口不能误删）", async () => {
    // legacy：没有 v2 state，后端端到端支持 overrides（B-CP1）
    const w = openModal(subagentApproval({ state: undefined, legacy: true }));
    await nextTick();
    expect(w.find('[data-test="approval-budget-editable"]').exists(), "可修改后批准的真实入口被误删").toBe(true);
    expect(w.text()).toContain("可修改后批准");
    expect(w.find('[data-test="unified-approval-allow"]').exists()).toBe(true);
  });

  it("v2 生命周期记录即使还在 waiting 也只读（/decide 不接受 overrides，不能给假入口）", async () => {
    const w = openModal(subagentApproval({ state: "waiting" }));
    await nextTick();
    expect(w.find('[data-test="approval-budget-editable"]').exists(), "v2 waiting 仍可编辑预算").toBe(false);
    expect(w.find('[data-test="approval-budget-readonly"]').exists()).toBe(true);
    expect(w.text()).not.toContain("可修改后批准");
  });
});

describe("内部数据三次确认：进度只显示后端事实（F06 / §4.1）", () => {
  it("confirmed_stage=1 / requires=3 时显示 1/3 与「第 2 次」，按钮不是一次批准", async () => {
    const item = subagentApproval({
      approval_id: "appr_internal",
      kind: "internal.data_write",
      payload: { description: "直接维护内部数据", name: "内部运行数据" },
      state: "waiting",
      requiresConfirmations: 3,
      confirmedStage: 1,
    });
    const w = openModal(item);
    await nextTick();

    expect(w.text()).toContain("1/3");
    expect(w.text()).toContain("第 2/3 次");
    // 三次确认期间不能出现「只点一次就批准」的主按钮文案
    const allow = w.find('[data-test="unified-approval-allow"]');
    if (allow.exists()) expect(allow.text()).toContain("继续确认");
  });
});