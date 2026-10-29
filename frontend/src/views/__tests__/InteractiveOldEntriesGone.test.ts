/**
 * 互动页旧重叠入口的回归守卫。
 *
 * 2026-10-26（合并后清理 · C2）：旧批量托盘 / 旧状态浮层 / 旧工作台 / 旧影响确认框
 * 与旧对话窗都已删除（能力由 v2 的 TaskCenterPanel / IntentLayer / ImpactConfirmLayer /
 * InteractiveConversation 承接）。这里改为断言**当前页面的真实组成**：
 * 执行记录、统一审批、虚拟意图层仍在，且页面不再引用任何已删除的旧组件。
 */
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

const view = readFileSync(resolve(process.cwd(), "src/views/InteractiveView.vue"), "utf8");

describe("互动页入口组成", () => {
  it("执行记录、统一审批、虚拟意图层与会话仍由真实组件承担", () => {
    expect(view).toContain("ExecutionRecordPanel");
    expect(view).toContain("UnifiedApprovalLayer");
    expect(view).toContain("IntentLayer");
    expect(view).toContain("InteractiveConversation");
    expect(view).toContain("TaskCenterPanel");
  });

  it("不再引用已删除的旧组件（旧托盘 / 旧状态浮层 / 旧工作台 / 旧影响确认框）", () => {
    for (const component of [
      "IntentBatchTray",
      "IntentStatusPopover",
      "IntentWorkbench",
      "ImpactConfirmDialog",
      "ChatDock",
      "BoardSearchPanel",
    ]) {
      expect(view, component).not.toContain(component);
    }
    expect(view).not.toContain('data-im="workbench-entry"');
  });
});
