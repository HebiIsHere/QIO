import { beforeEach, describe, expect, it } from "vitest";
import { mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";

import NonChatApprovalEntry from "../NonChatApprovalEntry.vue";
import { useApprovalsStore } from "../../../stores/approvals";
import { useApprovalResultsStore } from "../../../stores/approvalResults";
import { useSessionStore } from "../../../stores/session";
import { useUnifiedApprovalsStore } from "../../../stores/unifiedApprovals";

function setup() {
  const pinia = createPinia();
  setActivePinia(pinia);
  return {
    pinia,
    approvals: useApprovalsStore(),
    results: useApprovalResultsStore(),
    unified: useUnifiedApprovalsStore(),
    session: useSessionStore(),
  };
}

beforeEach(() => {
  localStorage.clear();
});

describe("非聊天路由的统一审批入口（迁移旧 ApprovalEntry 能力）", () => {
  it("有待办：亮出「有 N 项操作等待确认」，点击打开统一队列", async () => {
    const { pinia, approvals } = setup();
    approvals.enqueue("ap_1", "computer", { description: "写入文件" }, { autoOpen: false });
    const w = mount(NonChatApprovalEntry, { global: { plugins: [pinia] } });
    const entry = w.get('[data-test="nonchat-approval-entry"]');
    expect(entry.text()).toContain("有 1 项操作等待确认");
    await entry.trigger("click");
    expect(useUnifiedApprovalsStore().visible).toBe(true);
    w.unmount();
  });

  it("0 待办但有历史结果：入口仍可达并打开「结果与历史」", async () => {
    const { pinia, results } = setup();
    results.put({
      approvalId: "ap_done",
      kind: "tool_execution",
      label: "写入报告",
      phase: "executed",
      state: "completed",
      stateLabel: "已完成",
      summary: "已写入",
      ok: true,
      detail: {},
      items: [],
      batchSummary: "",
      source: "history",
      at: new Date().toISOString(),
      updatedAt: Date.now(),
    });
    const w = mount(NonChatApprovalEntry, { global: { plugins: [pinia] } });
    const entry = w.get('[data-test="nonchat-approval-entry"]');
    expect(entry.text()).toContain("审批结果与历史");
    expect(String(entry.attributes("aria-label"))).toContain("审批结果与历史");
    await entry.trigger("click");
    expect(useUnifiedApprovalsStore().resultsVisible).toBe(true);
    w.unmount();
  });

  it("恢复失败：入口出现并说明读取失败（可点开看原因与重试）", async () => {
    const { pinia, approvals } = setup();
    approvals.recoveryError = "恢复待审批项失败：连接超时（已有待办保留；可以重试）";
    const w = mount(NonChatApprovalEntry, { global: { plugins: [pinia] } });
    const entry = w.get('[data-test="nonchat-approval-entry"]');
    expect(entry.text()).toContain("失败");
    expect(entry.text()).toContain("重试");
    w.unmount();
  });

  it("完全空白（无待办、无历史、无错误）：不显示空入口", () => {
    const { pinia } = setup();
    const w = mount(NonChatApprovalEntry, { global: { plugins: [pinia] } });
    expect(w.find('[data-test="nonchat-approval-entry"]').exists()).toBe(false);
    w.unmount();
  });

  it("迁入旧 ApprovalEntry 的「上次有一项操作没有执行」提示（只读事实，不是待办）", () => {
    const { pinia, session } = setup();
    session.interruptedOperations = [
      {
        approval_id: "ia_1",
        kind: "tool_execution",
        what: "运行一段命令",
        createdAt: "2026-10-25T10:00:00+08:00",
      },
    ];
    const w = mount(NonChatApprovalEntry, { global: { plugins: [pinia] } });
    const note = w.get('[data-test="interrupted-operations-note"]');
    expect(note.text()).toBe("上次有一项操作没有执行：运行一段命令");
    expect(note.element.tagName).toBe("P");
    w.unmount();
  });
});
