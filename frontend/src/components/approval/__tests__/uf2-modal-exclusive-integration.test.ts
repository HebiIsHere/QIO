/**
 * uf2 · F01 集成：审批弹窗与影响确认层同时挂载时，同一时刻只有一个真正可见/可交互。
 *
 * 断言真实 DOM（display / 属性 / activeElement），覆盖用户验收的四条顺序：
 * - 先审批后影响确认：审批让位（隐藏），影响确认占位；
 * - 先影响确认后审批：审批不强行显示也不丢申请，影响确认结束后自动进入；
 * - 两层之间没有第二个 role/遮罩同时可交互；
 * - 让位与恢复都不丢当前申请与草稿。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount, type VueWrapper } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";

const mocks = vi.hoisted(() => ({ request: vi.fn(), respondApproval: vi.fn() }));
vi.mock("../../../services/api", () => ({
  request: mocks.request,
  api: { respondApproval: mocks.respondApproval },
}));

import UnifiedApprovalModal from "../UnifiedApprovalModal.vue";
import ImpactConfirmLayer from "../../interactive/v2/ImpactConfirmLayer.vue";
import { useApprovalsStore } from "../../../stores/approvals";
import { useUnifiedApprovalsStore } from "../../../stores/unifiedApprovals";
import { useModalArbiterStore } from "../../../stores/modalArbiter";
import { useInteractiveStore } from "../../../stores/interactive";

let wrappers: VueWrapper[] = [];

function setup() {
  setActivePinia(createPinia());
  return {
    unified: useUnifiedApprovalsStore(),
    approvals: useApprovalsStore(),
    arbiter: useModalArbiterStore(),
    interactive: useInteractiveStore(),
  };
}

function mountBoth() {
  wrappers = [
    mount(UnifiedApprovalModal, { attachTo: document.body }),
    mount(ImpactConfirmLayer, { attachTo: document.body }),
  ];
  return wrappers;
}

function pendingImpact() {
  return {
    affected: [{ intentId: "i1", title: "整理材料", materials: ["a.pdf"], consequence: "会暂停任务" }],
    previewRev: 1,
    stateVersion: 0,
    checkId: "chk",
  };
}

function approvalMaskDisplay(): string {
  const element = document.querySelector<HTMLElement>(".ua-mask");
  return element ? element.style.display : "missing";
}

function impactScrimVisible(): boolean {
  const scrim = document.querySelector<HTMLElement>('[data-im="impact-confirm-scrim"]');
  return Boolean(scrim && scrim.style.display !== "none");
}

beforeEach(() => {
  mocks.request.mockReset();
  mocks.respondApproval.mockReset();
  document.body.innerHTML = "";
  try {
    localStorage.clear();
  } catch {
    // 存储不可用时只影响跨用例的暂缓键
  }
});

afterEach(() => {
  for (const wrapper of wrappers) wrapper.unmount();
  wrappers = [];
  document.body.innerHTML = "";
});

describe("uf2-F01 集成：审批与影响确认互斥", () => {
  it("先审批后影响确认：审批让位；影响确认结束后审批恢复可见并保留申请", async () => {
    const { unified, approvals, interactive, arbiter } = setup();
    approvals.enqueue("appr_1", "tool_execution", { description: "执行命令" }, { autoOpen: true });
    unified.open("tool:appr_1");
    mountBoth();
    await flushPromises();

    expect(arbiter.activeId).toBe("unified-approval");
    expect(approvalMaskDisplay()).toBe("");
    expect(impactScrimVisible()).toBe(false);

    // 影响确认出现（更高优先级）：审批必须让位
    interactive.pendingImpact = pendingImpact() as never;
    await flushPromises();
    expect(arbiter.activeId).toBe("impact-confirm");
    expect(approvalMaskDisplay()).toBe("none");
    expect(impactScrimVisible()).toBe(true);

    // 同一时刻只有一个真正可交互的中央窗口：审批遮罩隐藏并 inert（不可点击/不可聚焦）
    const approvalMask = document.querySelector<HTMLElement>(".ua-mask");
    expect(approvalMask?.style.display).toBe("none");
    expect(approvalMask?.getAttribute("inert")).not.toBeNull();
    expect(approvalMask?.getAttribute("aria-hidden")).toBe("true");

    // 影响确认结束（取消/确认后清空）：审批恢复，当前申请仍是同一项
    interactive.pendingImpact = null;
    await flushPromises();
    expect(arbiter.activeId).toBe("unified-approval");
    expect(approvalMaskDisplay()).toBe("");
    expect(impactScrimVisible()).toBe(false);
    expect(unified.activeKey).toBe("tool:appr_1");
    expect(unified.visible).toBe(true);
  });

  it("先影响确认后审批：审批不强行显示、申请不丢；影响确认结束后自动进入", async () => {
    const { unified, approvals, interactive, arbiter } = setup();
    approvals.enqueue("appr_1", "tool_execution", { description: "执行命令" }, { autoOpen: true });
    interactive.pendingImpact = pendingImpact() as never;
    mountBoth();
    await flushPromises();
    expect(arbiter.activeId).toBe("impact-confirm");
    expect(impactScrimVisible()).toBe(true);

    // 用户此时表达了「要处理这项审批」：不得叠层，但申请与当前项不能丢
    unified.open("tool:appr_1");
    await flushPromises();
    expect(arbiter.activeId).toBe("impact-confirm");
    expect(document.querySelector(".ua-modal")).toBeNull();
    expect(unified.visible).toBe(true);
    expect(unified.activeKey).toBe("tool:appr_1");

    // 影响确认结束：审批自动成为活动层（不需要用户再点一次）
    interactive.pendingImpact = null;
    await flushPromises();
    expect(arbiter.activeId).toBe("unified-approval");
    expect(approvalMaskDisplay()).toBe("");
    expect(document.querySelector(".ua-modal")).not.toBeNull();
    expect(document.querySelector(".ua-modal")?.contains(document.activeElement)).toBe(true);
  });
});
