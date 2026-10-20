import { describe, expect, it } from "vitest";
import {
  BATCH_CONFIRM_MIN,
  batchConfirmEnabled,
  batchConfirmHint,
  canRecover,
  confirmEligibleIds,
  confirmOutcomeText,
  dependencyText,
  hasResultContent,
  isConfirmable,
  isLongResultContent,
  operationsSummaryText,
  scopeSummary,
  statusLabel,
  statusTone,
  type IntentView,
} from "../intentWorkbench";

function view(overrides: Partial<IntentView> = {}): IntentView {
  return {
    intent_id: "in_1",
    version: 1,
    status: "proposed",
    title: "整理采访记录",
    goal: "把采访记录整理成清单",
    expected: "得到可读的清单",
    scope: {
      read_card_ids: ["c1", "c2"],
      write_card_ids: ["c3"],
      allowed_actions: ["update_card"],
      max_new_cards: 2,
    },
    depends_on: ["in_0"],
    conflicts_with: ["in_2"],
    authorization_valid: true,
    operations_summary: { applied: 2, running: 1, pending: 3 },
    ...overrides,
  };
}

describe("intentWorkbench 纯逻辑", () => {
  it("批量确认门槛与后端同口径：两条才可批量确认", () => {
    expect(BATCH_CONFIRM_MIN).toBe(2);
    expect(batchConfirmEnabled(0)).toBe(false);
    expect(batchConfirmEnabled(1)).toBe(false);
    expect(batchConfirmEnabled(2)).toBe(true);
    expect(batchConfirmHint(1)).toContain("至少 2 条");
    expect(batchConfirmHint(2)).toContain("可同批确认");
  });

  it("只有未受理的意图可确认；已确认 / 执行中 / 已结束都不可再确认", () => {
    expect(isConfirmable(view({ status: "proposed" }))).toBe(true);
    expect(isConfirmable(view({ status: "awaiting_confirmation" }))).toBe(true);
    for (const status of ["approved", "running", "completed", "failed", "cancelled"]) {
      expect(isConfirmable(view({ status }))).toBe(false);
    }
    expect(
      confirmEligibleIds([
        view({ intent_id: "a", status: "proposed" }),
        view({ intent_id: "b", status: "running" }),
        view({ intent_id: "c", status: "awaiting_confirmation" }),
      ]),
    ).toEqual(["a", "c"]);
  });

  it("状态文案只映射真实状态，未知状态原样显示（绝不猜成完成）", () => {
    expect(statusLabel("completed")).toBe("已完成");
    expect(statusLabel("partially_completed")).toBe("部分完成");
    expect(statusLabel("weird_status")).toBe("weird_status");
    expect(statusLabel(undefined)).toBe("状态未知");
    expect(statusTone("completed")).toBe("settled");
    expect(statusTone("failed")).toBe("problem");
    expect(statusTone("running")).toBe("active");
  });

  it("恢复只对失败 / 部分完成 / 暂停且授权仍有效的意图开放", () => {
    expect(canRecover(view({ status: "paused" }))).toBe(true);
    expect(canRecover(view({ status: "failed" }))).toBe(true);
    expect(canRecover(view({ status: "partially_completed" }))).toBe(true);
    expect(canRecover(view({ status: "completed" }))).toBe(false);
    expect(canRecover(view({ status: "failed", authorization_valid: false }))).toBe(false);
  });

  it("影响范围 / 依赖冲突 / 账本摘要给人话，不隐藏重要字段", () => {
    expect(scopeSummary(view().scope)).toContain("读取 2 张卡");
    expect(scopeSummary(view().scope)).toContain("修改 1 张卡");
    expect(dependencyText(view())).toContain("依赖 1 项");
    expect(dependencyText(view())).toContain("冲突");
    expect(operationsSummaryText(view())).toContain("已完成操作 2");
    expect(operationsSummaryText(view())).toContain("执行中 1");
  });

  it("确认结果只说「已确认」，明确不等于已完成", () => {
    const text = confirmOutcomeText({
      batch: true,
      results: [
        { intent_id: "in_1", ok: true, status: "approved" },
        { intent_id: "in_2", ok: true, status: "approved" },
      ],
    });
    expect(text).toContain("同批确认结果");
    expect(text).toContain("已确认（等待执行）");
    // 逐项结果只到「已确认」：不出现「已完成 / 任务完成」这类完成结论
    expect(text).not.toContain("任务已完成");
    expect(text).toContain("已确认不等于已完成");
  });

  it("长正文判定：短正文默认不折叠，长正文折叠并可展开", () => {
    expect(isLongResultContent("短正文")).toBe(false);
    expect(isLongResultContent("x".repeat(1201))).toBe(true);
    expect(isLongResultContent("line\n".repeat(20))).toBe(true);
    expect(hasResultContent({ result_id: "r1", content: "   " })).toBe(false);
    expect(hasResultContent({ result_id: "r2", content: "有正文" })).toBe(true);
  });
});
