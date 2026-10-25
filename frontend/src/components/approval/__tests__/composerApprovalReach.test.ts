/**
 * 输入区 ⑤ 审批入口的可达性（A / F02）。
 *
 * 保留 B 的「没有事项不占位置」规则（完全空白不渲染），但「审批结果与历史」也是
 * 需要可达的事项：0 待办但有历史 / 恢复或历史读取失败时，⑤ 仍在。
 * 点击行为：有待办 → 打开队列；无待办 → 打开同一个弹窗的结果视图。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { nextTick } from "vue";

vi.mock("../../../services/api", () => ({
  api: {
    cancelTurn: vi.fn(async () => ({ ok: true })),
    cancelContinuation: vi.fn(async () => ({ ok: true })),
  },
}));

import ComposerEntries from "../../conversation/ComposerEntries.vue";
import { useApprovalsStore } from "../../../stores/approvals";
import { useApprovalResultsStore } from "../../../stores/approvalResults";
import { useUnifiedApprovalsStore } from "../../../stores/unifiedApprovals";

function mountEntries() {
  const pinia = createPinia();
  setActivePinia(pinia);
  return {
    pinia,
    w: mount(ComposerEntries, { global: { plugins: [pinia] } }),
    approvals: useApprovalsStore(),
    results: useApprovalResultsStore(),
    unified: useUnifiedApprovalsStore(),
  };
}

function historyEntry() {
  return {
    approvalId: "ap_done",
    kind: "tool_execution",
    label: "写入报告",
    phase: "executed",
    state: "completed",
    stateLabel: "已完成",
    summary: "已写入 1 个文件",
    ok: true,
    detail: {},
    items: [],
    batchSummary: "",
    source: "history" as const,
    at: new Date().toISOString(),
    updatedAt: Date.now(),
  };
}

beforeEach(() => {
  localStorage.clear();
});

describe("⑤ 审批入口（结果与历史可达）", () => {
  it("0 待办但有历史：入口可见、aria 含「审批结果与历史」，点击打开结果视图", async () => {
    const { w, results, unified } = mountEntries();
    expect(w.find('[data-entry="approvals"]').exists()).toBe(false);

    results.put(historyEntry());
    await nextTick();
    const entry = w.get('[data-entry="approvals"]');
    expect(String(entry.attributes("aria-label"))).toContain("审批结果与历史");
    expect(String(entry.attributes("title"))).toContain("审批结果与历史");
    // 0 待办不显示数量徽标
    expect(entry.find(".count").exists()).toBe(false);

    await entry.trigger("click");
    expect(unified.resultsVisible).toBe(true);
    expect(unified.visible).toBe(false);
    w.unmount();
  });

  it("有待办：显示真实数量，点击打开队列（不是结果视图）", async () => {
    const { w, approvals, unified } = mountEntries();
    approvals.enqueue("ap_1", "computer", { description: "写入文件" }, { autoOpen: false });
    approvals.enqueue("ap_2", "computer", { description: "执行命令" }, { autoOpen: false });
    await nextTick();
    const entry = w.get('[data-entry="approvals"]');
    expect(entry.find(".count").text()).toBe("2");
    expect(String(entry.attributes("aria-label"))).toContain("待审批申请（2 项）");
    await entry.trigger("click");
    expect(unified.visible).toBe(true);
    expect(unified.resultsVisible).toBe(false);
    w.unmount();
  });

  it("恢复失败：入口带警告点可见，文案说明可查看原因与重试", async () => {
    const { w, approvals } = mountEntries();
    approvals.recoveryError = "恢复待审批项失败：连接超时（已有待办保留；可以重试）";
    await nextTick();
    const entry = w.get('[data-entry="approvals"]');
    expect(entry.find(".dot").exists()).toBe(true);
    expect(String(entry.attributes("aria-label"))).toContain("失败");
    w.unmount();
  });

  it("完全空白（无待办、无历史、无错误）仍不渲染入口行（B 的规则不回归）", async () => {
    const { w } = mountEntries();
    await nextTick();
    expect(w.find('[data-entry="approvals"]').exists()).toBe(false);
    expect(w.find(".entry-row").exists()).toBe(false);
    w.unmount();
  });
});
