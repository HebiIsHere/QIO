import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

const view = readFileSync(resolve(process.cwd(), "src/views/InteractiveView.vue"), "utf8");

describe("互动页旧重叠入口", () => {
  it("不再挂载旧批量托盘、旧状态浮层、旧工作台和旧影响确认框", () => {
    for (const component of ["IntentBatchTray", "IntentStatusPopover", "IntentWorkbench", "ImpactConfirmDialog"]) {
      expect(view, component).not.toContain(component);
    }
    expect(view).not.toContain('data-im="workbench-entry"');
  });

  it("执行记录、统一审批和新的虚拟意图层仍在", () => {
    expect(view).toContain("ExecutionRecordPanel");
    expect(view).toContain("UnifiedApprovalLayer");
    expect(view).toContain("IntentLayer");
  });
});
