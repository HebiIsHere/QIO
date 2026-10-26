/**
 * 审批门控与弹窗仲裁（B）：收起不连弹、处理完回列表、到期禁批、仲裁只一层、通知排队。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";

const mocks = vi.hoisted(() => ({ request: vi.fn(), respondApproval: vi.fn() }));
vi.mock("../../../services/api", () => ({
  request: mocks.request,
  api: { respondApproval: mocks.respondApproval },
}));

import UnifiedApprovalModal from "../UnifiedApprovalModal.vue";
import { useApprovalsStore } from "../../../stores/approvals";
import { useUnifiedApprovalsStore } from "../../../stores/unifiedApprovals";
import { useModalArbiterStore } from "../../../stores/modalArbiter";
import { useNotificationsStore } from "../../../stores/notifications";
import { feedbackGate } from "../../../stores/feedbackGate";

function setup() {
  setActivePinia(createPinia());
  return {
    unified: useUnifiedApprovalsStore(),
    approvals: useApprovalsStore(),
    arbiter: useModalArbiterStore(),
    notifications: useNotificationsStore(),
  };
}

beforeEach(() => {
  mocks.request.mockReset();
  mocks.respondApproval.mockReset();
  mocks.request.mockResolvedValue({ ok: true, decision: "approve" });
  mocks.respondApproval.mockResolvedValue({ ok: true });
  document.body.innerHTML = "";
  try {
    localStorage.clear();
  } catch {
    // 存储不可用时只影响跨用例的暂缓键
  }
  feedbackGate.editing = false;
  feedbackGate.composing = false;
  feedbackGate.lastInputLeftAt = 0;
});

describe("收起不连弹：已存在项不再自动弹，真正新到达仍按规则", () => {
  it("收起弹窗后，队列里原本就有的申请不自动弹；新到达的申请可以弹", () => {
    const { unified, approvals } = setup();
    approvals.enqueue("appr_1", "tool_execution", { description: "执行命令" }, { autoOpen: true });
    approvals.enqueue("appr_2", "tool_execution", { description: "另一个命令" }, { autoOpen: true });
    expect(unified.maybeAutoOpen()).toBe(true);
    expect(unified.activeKey).toBe("tool:appr_1");

    unified.close();
    expect(unified.visible).toBe(false);
    // 收起之后：已存在的 appr_2 不自动弹（不得连弹下一条）
    expect(unified.maybeAutoOpen()).toBe(false);
    expect(unified.visible).toBe(false);

    // 真正新到达的申请：仍按规则处理
    approvals.enqueue("appr_3", "tool_execution", { description: "新到达" }, { autoOpen: true });
    expect(unified.maybeAutoOpen()).toBe(true);
    expect(unified.activeKey).toBe("tool:appr_3");
  });

  it("输入法组合输入期间不自动弹；离开输入框并过延迟后自动弹", () => {
    const { unified, approvals } = setup();
    approvals.enqueue("appr_1", "tool_execution", {}, { autoOpen: true });
    feedbackGate.editing = true;
    feedbackGate.composing = true;
    expect(unified.maybeAutoOpen()).toBe(false);
    expect(unified.autoPending).toBe(true);

    // 组合结束、离开输入框：仍有「刚离开」延迟
    feedbackGate.composing = false;
    feedbackGate.editing = false;
    feedbackGate.lastInputLeftAt = Date.now();
    expect(unified.maybeAutoOpen()).toBe(false);

    feedbackGate.lastInputLeftAt = Date.now() - 500;
    expect(unified.maybeAutoOpen()).toBe(true);
  });
});

describe("处理完一项：回到列表、保留位置，不自动进入下一条详情", () => {
  it("提交成功后回到队列列表；用户点哪条才进入哪条详情", async () => {
    const { unified, approvals } = setup();
    approvals.enqueue("appr_1", "tool_execution", { description: "执行命令" }, { autoOpen: true });
    approvals.enqueue("appr_2", "tool_execution", { description: "另一个命令" }, { autoOpen: true });
    unified.open("tool:appr_1");

    // 模拟 appr_1 已成功应答（服务端权威队列里只剩 appr_2）
    approvals.resolve("appr_1");
    unified.finishHandled("tool:appr_1");

    expect(unified.visible).toBe(true);
    expect(unified.queueListOnly).toBe(true);
    expect(unified.activeKey).toBeNull();
    expect(unified.activeItem).toBeNull();

    const w = mount(UnifiedApprovalModal);
    const rows = w.findAll('[data-test="unified-approval-list-item"]');
    expect(rows).toHaveLength(1);
    // 列表态不显示批准/拒绝按钮（还没选中任何一条）
    expect(w.find('[data-test="unified-approval-allow"]').exists()).toBe(false);

    await rows[0].trigger("click");
    expect(unified.activeKey).toBe("tool:appr_2");
    expect(w.find('[data-test="unified-approval-allow"]').exists()).toBe(true);
    w.unmount();
  });
});

describe("到期禁止批准（期限不放宽）", () => {
  it("已到期的申请：不显示批准按钮，提交也不发请求", async () => {
    const { unified, approvals } = setup();
    approvals.enqueue("appr_expired", "tool_execution", { description: "执行命令" }, {
      autoOpen: true,
      meta: { expiresAt: new Date(Date.now() - 60_000).toISOString() },
    });
    unified.open("tool:appr_expired");
    const w = mount(UnifiedApprovalModal);

    expect(w.find('[data-test="unified-approval-expired"]').exists()).toBe(true);
    expect(w.find('[data-test="unified-approval-allow"]').exists()).toBe(false);
    expect(w.find('[data-test="unified-approval-reject"]').exists()).toBe(true);

    const ok = await unified.submit("approve");
    expect(ok).toBe(false);
    expect(mocks.respondApproval).not.toHaveBeenCalled();
    expect(unified.submitError).toContain("到期");
    w.unmount();
  });
});

describe("弹窗仲裁：同时只有一个中央弹窗，通知排队", () => {
  it("审批弹窗注册仲裁；被更高优先级弹窗挡住时不强行显示", async () => {
    const { unified, approvals, arbiter } = setup();
    approvals.enqueue("appr_1", "tool_execution", { description: "执行命令" }, { autoOpen: true });
    unified.open("tool:appr_1");
    const w = mount(UnifiedApprovalModal);
    await flushPromises();
    expect(arbiter.isOpen("unified-approval")).toBe(true);
    expect(arbiter.active?.kind).toBe("approval");

    // 高优先级弹窗（影响确认）出现：审批弹窗被收起，绝不叠着显示
    unified.closePanel();
    await flushPromises();
    expect(arbiter.isOpen("unified-approval")).toBe(false);

    arbiter.open({ id: "impact-1", kind: "impact" });
    unified.open("tool:appr_1");
    await flushPromises();
    expect(arbiter.isOpen("unified-approval")).toBe(false);
    expect(arbiter.active?.id).toBe("impact-1");
    expect(w.find(".ua-modal").exists()).toBe(false);

    // 高优先级弹窗关闭后，用户再打开审批可以正常显示
    arbiter.close("impact-1");
    unified.open("tool:appr_1");
    await flushPromises();
    expect(arbiter.isOpen("unified-approval")).toBe(true);
    expect(w.find(".ua-modal").exists()).toBe(true);
    w.unmount();
  });

  it("审批弹窗打开期间通知排队；弹窗关闭后显示", async () => {
    const { unified, approvals, arbiter, notifications } = setup();
    approvals.enqueue("appr_1", "tool_execution", { description: "执行命令" }, { autoOpen: true });
    unified.open("tool:appr_1");
    const w = mount(UnifiedApprovalModal);
    await flushPromises();

    notifications.enqueue({ key: "setup:1", kind: "setup_incomplete", text: "初始设置尚未完成" });
    expect(notifications.hasActive).toBe(false);
    expect(notifications.flushQueue()).toBe(false);

    unified.closePanel();
    await flushPromises();
    expect(arbiter.isOpen("unified-approval")).toBe(false);
    expect(notifications.flushQueue()).toBe(true);
    expect(notifications.hasActive).toBe(true);
    w.unmount();
  });
});
describe("真实缺陷回归：输入中到达 + 失焦后自动弹（不再被 approvals.deferred 早退）", () => {
  it("输入中到达（不打扰）→ 失焦延迟后自动弹出；恢复项不自动弹", () => {
    const { unified, approvals } = setup();
    // 用户正在输入：事件层把申请放进队列（autoOpen 交给统一门控判断）
    feedbackGate.editing = true;
    approvals.enqueue("appr_live", "tool_execution", { description: "执行命令" }, { autoOpen: true });
    expect(approvals.deferred).toBe(false);
    expect(unified.maybeAutoOpen()).toBe(false);
    expect(unified.autoPending).toBe(true);

    // 失焦瞬间仍受「刚离开输入框」延迟保护
    feedbackGate.editing = false;
    feedbackGate.lastInputLeftAt = Date.now();
    expect(unified.maybeAutoOpen()).toBe(false);
    expect(unified.autoPending).toBe(true);

    // 过了集中常量延迟：自动弹出（真实故障就是卡在这一步）
    feedbackGate.lastInputLeftAt = Date.now() - 500;
    expect(unified.maybeAutoOpen()).toBe(true);
    expect(unified.visible).toBe(true);
    expect(unified.activeKey).toBe("tool:appr_live");
  });

  it("恢复路径补读出来的旧审批不自动弹（亮入口、等用户主动打开）；新到达的仍可弹", () => {
    const { unified, approvals } = setup();
    approvals.enqueue("appr_restored", "tool_execution", { description: "旧审批" }, { autoOpen: false });
    expect(approvals.queue[0].restored).toBe(true);
    expect(unified.maybeAutoOpen()).toBe(false);
    expect(unified.visible).toBe(false);
    expect(unified.count).toBe(1);

    // 真正新到达的申请仍按规则自动弹
    approvals.enqueue("appr_live", "tool_execution", { description: "新申请" }, { autoOpen: true });
    expect(unified.maybeAutoOpen()).toBe(true);
    expect(unified.activeKey).toBe("tool:appr_live");
  });
});

describe("关闭询问等最高层触发器：业务弹窗让位但不丢状态", () => {
  it("suspendAll 隐藏审批弹窗（DOM 保留）；resumeAll 恢复同一上下文", async () => {
    const { unified, approvals, arbiter } = setup();
    approvals.enqueue("appr_1", "tool_execution", { description: "执行命令" }, { autoOpen: true });
    unified.open("tool:appr_1");
    const w = mount(UnifiedApprovalModal);
    await flushPromises();
    expect(w.find(".ua-modal").exists()).toBe(true);

    arbiter.suspendAll("close-ask");
    await flushPromises();
    // DOM 保留、仅隐藏：草稿/队列位置/当前项都不丢
    const root = w.find("[data-unified-approval-modal]");
    expect(root.exists()).toBe(true);
    expect((root.element as HTMLElement).style.display).toBe("none");
    expect(unified.activeKey).toBe("tool:appr_1");

    arbiter.resumeAll();
    await flushPromises();
    expect((w.find("[data-unified-approval-modal]").element as HTMLElement).style.display).toBe("");
    expect(w.find(".ua-modal").exists()).toBe(true);
    w.unmount();
  });
});
