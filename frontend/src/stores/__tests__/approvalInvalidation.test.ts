/**
 * 放弃开发时被作废的确认，必须在**收到 `APPROVAL_RESULT` 的那一刻**从待审批界面消失：
 * 不刷新页面、不等任何超时、也不需要用户再点一次「知道了」。
 *
 * 迁移说明（A-CP1）：旧 ApprovalModal/ApprovalEntry 已让位给统一体系 ——
 * 本文件改为挂载统一弹窗 + ComposerEntries 的 ⑤ 入口断言同一件事；作废的结局
 * 仍按 F02 保留在「结果与历史」里（不是从界面彻底抹掉）。
 *
 * 1. 事件按 `approval_id` 收敛（`stores/events.ts` → `approvals.recordResult`）；
 * 2. 队列只剩一项时少一项而不是清空；队列空了 `deferred` 复位；
 * 3. 待审批卡片随之消失，且不依赖刷新；前端不替用户做任何决定。
 */
import { describe, expect, it, vi, beforeEach } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";
import { createPinia, setActivePinia, type Pinia } from "pinia";
import { nextTick } from "vue";
import ComposerEntries from "../../components/conversation/ComposerEntries.vue";
import UnifiedApprovalModal from "../../components/approval/UnifiedApprovalModal.vue";
import { useApprovalsStore } from "../approvals";
import { useApprovalResultsStore } from "../approvalResults";
import { useEventStore } from "../events";
import { useUnifiedApprovalsStore } from "../unifiedApprovals";
import type { AgentEvent } from "../../services/events";
import { useSessionStore } from "../session";
import { api } from "../../services/api";

vi.mock("../../services/api", () => ({
  api: { respondApproval: vi.fn(async () => ({ ok: true })) },
}));

const respond = api.respondApproval as unknown as ReturnType<typeof vi.fn>;

function setup(): {
  pinia: Pinia;
  approvals: ReturnType<typeof useApprovalsStore>;
  results: ReturnType<typeof useApprovalResultsStore>;
  unified: ReturnType<typeof useUnifiedApprovalsStore>;
  events: ReturnType<typeof useEventStore>;
} {
  const pinia = createPinia();
  setActivePinia(pinia);
  useSessionStore(); // 事件路由要用到会话（审批归属/继续条）
  return {
    pinia,
    approvals: useApprovalsStore(),
    results: useApprovalResultsStore(),
    unified: useUnifiedApprovalsStore(),
    events: useEventStore(),
  };
}

/** 后端放弃任务后发出的那一条事件（`approval.py::_publish_result` 的形状）。 */
function routeCancelled(
  events: ReturnType<typeof useEventStore>,
  approvalId: string,
  phase?: string,
) {
  events.route({
    type: "APPROVAL_RESULT",
    id: "ev_1",
    ts: "",
    data: {
      approval_id: approvalId,
      decision: "cancelled",
      ...(phase ? { phase } : {}),
      reason: "task_abandoned",
      turn_id: "turn_main",
    },
  } as unknown as AgentEvent);
}

beforeEach(() => {
  vi.clearAllMocks();
  respond.mockResolvedValue({ ok: true });
  document.body.innerHTML = "";
  localStorage.clear();
});

describe("作废事件（APPROVAL_RESULT{cancelled}）让待审批界面立即收敛", () => {
  it("只移除被作废的那一项：其余待办不受影响，队列空了 deferred 复位", () => {
    const { approvals, events } = setup();
    approvals.enqueue(
      "appr_cred",
      "credential_grant",
      { key_id: "weather-key", tool_name: "weather_fetch", workspace: "ws_1" },
      { autoOpen: false },
    );
    approvals.enqueue("appr_create", "tool_create", { name: "t" }, { autoOpen: false });
    expect(approvals.pendingCount).toBe(2);
    expect(approvals.visible).toBe(false); // 用户按过「稍后处理」

    routeCancelled(events, "appr_cred");

    // 同步收敛：事件处理完的这一拍就已经生效，中间没有任何 await
    expect(approvals.queue.map((a) => a.approval_id)).toEqual(["appr_create"]);
    expect(approvals.pendingCount).toBe(1);
    expect(approvals.deferred).toBe(true); // 还有别的待办，用户的选择保留

    routeCancelled(events, "appr_create");

    expect(approvals.queue).toEqual([]);
    expect(approvals.deferred).toBe(false); // 队列空了必须复位，否则下次审批会被静默藏起来
    expect(approvals.visible).toBe(false);
    // 前端没有替用户做任何决定：作废是后端的事实，不是「点了拒绝」
    expect(respond).not.toHaveBeenCalled();
  });

  it("作废别的审批不影响本项（按 id 收敛是幂等的）", () => {
    const { approvals, events } = setup();
    approvals.enqueue("appr_cred", "credential_grant", { workspace: "ws_1" });

    routeCancelled(events, "appr_not_mine");
    routeCancelled(events, "");

    expect(approvals.queue.map((a) => a.approval_id)).toEqual(["appr_cred"]);
    expect(approvals.current?.approval_id).toBe("appr_cred");
  });

  it("作废的结局进入结果模型（可回看），但待审批卡片立刻消失", async () => {
    const { pinia, approvals, unified, results, events } = setup();
    const entry = mount(ComposerEntries, { attachTo: document.body, global: { plugins: [pinia] } });
    const modal = mount(UnifiedApprovalModal, { attachTo: document.body, global: { plugins: [pinia] } });

    approvals.enqueue(
      "appr_cred",
      "credential_grant",
      { key_id: "weather-key", tool_name: "weather_fetch", workspace: "ws_1" },
      { autoOpen: false },
    );
    unified.open("tool:appr_cred");
    await flushPromises();
    expect(modal.find('[role="dialog"]').exists()).toBe(true);

    // v2 生命周期路径放弃开发会带 phase=cancelled；旧内存路径没有 phase（只收敛队列）
    routeCancelled(events, "appr_cred", "cancelled");
    await nextTick();
    await flushPromises();

    // 待审批卡片消失；结局保留在结果模型里（F02：可回看，不是静默抹掉）
    expect(approvals.pendingCount).toBe(0);
    expect(modal.find('[role="dialog"]').exists()).toBe(false);
    expect(results.byId["appr_cred"]?.state).toBe("cancelled");
    // ⑤ 入口仍然可达，但文案变成「结果与历史」而不是「有 N 项待审批」
    const approvalEntry = entry.get('[data-entry="approvals"]');
    expect(String(approvalEntry.attributes("aria-label"))).toContain("审批结果与历史");
    expect(String(approvalEntry.attributes("aria-label"))).not.toContain("待审批申请");
    expect(respond).not.toHaveBeenCalled();
  });

  it("「稍后处理」收起后的待办：作废后不再显示为待办（⑤ 不再报数量）", async () => {
    const { pinia, approvals, events } = setup();
    const entry = mount(ComposerEntries, { attachTo: document.body, global: { plugins: [pinia] } });
    approvals.enqueue("appr_cred", "credential_grant", { workspace: "ws_1" }, { autoOpen: false });
    await flushPromises();
    expect(entry.get('[data-entry="approvals"]').find(".count").text()).toBe("1");

    routeCancelled(events, "appr_cred", "cancelled");
    await nextTick();
    await flushPromises();

    expect(approvals.pendingCount).toBe(0);
    const approvalEntry = entry.get('[data-entry="approvals"]');
    expect(approvalEntry.find(".count").exists()).toBe(false);
    expect(String(approvalEntry.attributes("aria-label"))).not.toContain("等待确认");
  });
});
