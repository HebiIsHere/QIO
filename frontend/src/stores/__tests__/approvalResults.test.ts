/**
 * 审批结果模型（A / F02、F03）定向测试。
 *
 * 覆盖：按 approval_id 建键（并发不串）、approved/executing 不是完成、终态推进
 * 不降级、批量逐项结果用真实 item_id 与状态、历史失败不清空可重试、历史重建。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";

const mocks = vi.hoisted(() => ({ getApprovalHistory: vi.fn() }));

vi.mock("../../services/approvalLifecycle", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../services/approvalLifecycle")>();
  return { ...actual, getApprovalHistory: mocks.getApprovalHistory };
});

import {
  batchItemsFromDetail,
  batchSummaryText,
  mergeResultEntry,
  resultFromEvent,
  resultFromHistory,
  useApprovalResultsStore,
} from "../approvalResults";

function store() {
  setActivePinia(createPinia());
  return useApprovalResultsStore();
}

beforeEach(() => {
  mocks.getApprovalHistory.mockReset();
  mocks.getApprovalHistory.mockResolvedValue([]);
});

describe("按 approval_id 建键：并发两条不互相覆盖", () => {
  it("A/B 的结局分别记录，最近更新在前", () => {
    const s = store();
    s.recordEvent({ approval_id: "ap_a", phase: "approved", summary: "已批准，等待执行" });
    s.recordEvent({ approval_id: "ap_b", phase: "approved" });
    s.recordEvent({ approval_id: "ap_a", phase: "executed", ok: true, summary: "A 写完了" });
    s.recordEvent({ approval_id: "ap_b", phase: "failed", ok: false, summary: "B 没写成功" });

    expect(s.entries.map((entry) => entry.approvalId)).toEqual(["ap_b", "ap_a"]);
    expect(s.byId["ap_a"].state).toBe("completed");
    expect(s.byId["ap_a"].ok).toBe(true);
    expect(s.byId["ap_b"].state).toBe("failed");
    expect(s.byId["ap_b"].ok).toBe(false);
  });

  it("迟到的非终态事件不能把终态降级（状态只能前进）", () => {
    const s = store();
    s.recordEvent({ approval_id: "ap_1", phase: "executed", ok: true, summary: "执行完成" });
    s.recordEvent({ approval_id: "ap_1", phase: "approved" });
    expect(s.byId["ap_1"].state).toBe("completed");
    expect(s.byId["ap_1"].summary).toBe("执行完成");
  });
});

describe("阶段语义：已批准 / 执行中 ≠ 已完成", () => {
  it("approved / executing 的 ok 为 null，状态文案不是完成", () => {
    const s = store();
    s.recordEvent({ approval_id: "ap_1", phase: "approved" });
    expect(s.byId["ap_1"].state).toBe("approved");
    expect(s.byId["ap_1"].ok).toBeNull();
    expect(s.byId["ap_1"].stateLabel).toContain("已批准");
    expect(s.byId["ap_1"].summary).toContain("等待后端执行");

    s.recordEvent({ approval_id: "ap_1", phase: "executing" });
    expect(s.byId["ap_1"].state).toBe("executing");
    expect(s.byId["ap_1"].stateLabel).not.toContain("完成");
    expect(s.byId["ap_1"].ok).toBeNull();
    expect(s.inFlight.map((entry) => entry.approvalId)).toEqual(["ap_1"]);
  });

  it("终态的中文文案覆盖执行失败/失效/过期/拒绝/取消/中断待核对", () => {
    const s = store();
    const cases: Array<[string, string, boolean | null]> = [
      ["failed", "执行失败", false],
      ["invalidated", "已失效（执行前核对不通过）", false],
      ["interrupted_unverified", "执行中断，待核对", false],
      ["expired", "已过期", false],
      ["rejected", "已拒绝", false],
      ["cancelled", "已取消", false],
    ];
    for (const [phase, label, ok] of cases) {
      s.recordEvent({ approval_id: "ap_" + phase, phase, ok });
      const entry = s.byId["ap_" + phase];
      expect(entry.stateLabel).toContain(label);
      expect(entry.ok).toBe(ok);
      expect(entry.summary.length).toBeGreaterThan(0);
    }
  });

  it("没有 phase 的旧事件、confirm_stage 进度都不伪造结果", () => {
    const s = store();
    s.recordEvent({ approval_id: "ap_old", decision: "approved" });
    s.recordEvent({ approval_id: "ap_1", phase: "confirm_stage", confirmed_stage: 2 });
    expect(s.entries).toHaveLength(0);
    expect(resultFromEvent({ approval_id: "ap_1" })).toBeNull();
  });
});

describe("批量逐项结果（F03）：真实 item_id + 状态，不按文件名猜", () => {
  const detail = {
    batch: true,
    items: [
      { item_id: "item_1", path: "C:\\work\\a.txt", ok: true, status: "completed", reason: "" },
      { item_id: "item_2", path: "C:\\work\\b.txt", ok: false, status: "failed", reason: "磁盘写满" },
      { item_id: "item_3", path: "C:\\work\\c.txt", ok: false, status: "invalidated", reason: "源文件内容在审批期间发生了变化" },
    ],
    results: [
      { item_id: "item_1", path: "C:\\work\\a.txt", ok: true, status: "completed", reason: "" },
      { item_id: "item_2", path: "C:\\work\\b.txt", ok: false, status: "failed", reason: "磁盘写满" },
      { item_id: "item_3", path: "C:\\work\\c.txt", ok: false, status: "invalidated", reason: "源文件内容在审批期间发生了变化" },
    ],
    partial: true,
    completed_count: 1,
    failed_count: 2,
  };

  it("事件 detail 里的逐项结果原样进结果模型（含 item_id / 原因）", () => {
    const s = store();
    s.recordEvent({
      approval_id: "ap_batch",
      phase: "failed",
      ok: false,
      summary: "批量写回部分完成：1/3 项已写回，其余项已保留文件",
      detail,
      approval: { kind: "tool_execution", label: "批量写回附件源文件" },
    });
    const entry = s.byId["ap_batch"];
    expect(entry.items.map((item) => item.itemId)).toEqual(["item_1", "item_2", "item_3"]);
    expect(entry.items[0].statusText).toBe("成功");
    expect(entry.items[1].statusText).toBe("失败");
    expect(entry.items[1].reason).toBe("磁盘写满");
    expect(entry.items[2].statusText).toContain("失效");
    expect(entry.items[2].ok).toBe(false);
    expect(entry.kind).toBe("tool_execution");
    expect(entry.label).toBe("批量写回附件源文件");
    expect(entry.batchSummary).toContain("部分成功");
    expect(entry.batchSummary).toContain("成功 1");
  });

  it("全部成功 / 全部失败 / 未执行都不得混成「成功」", () => {
    const allOk = batchItemsFromDetail({
      results: [
        { item_id: "i1", status: "completed", ok: true },
        { item_id: "i2", status: "completed", ok: true },
      ],
    });
    expect(batchSummaryText(allOk)).toContain("整批成功");

    const allFailed = batchItemsFromDetail({
      results: [
        { item_id: "i1", status: "failed", ok: false, reason: "x" },
        { item_id: "i2", status: "invalidated", ok: false, reason: "y" },
      ],
    });
    expect(batchSummaryText(allFailed)).toContain("整批未完成");

    const skipped = batchItemsFromDetail({ items: [{ item_id: "i9", status: "skipped" }] });
    expect(skipped[0].ok).toBe(false);
    expect(skipped[0].statusText).toContain("未执行");
  });

  it("未知 item 状态如实显示原值，不假装成功", () => {
    const items = batchItemsFromDetail({ results: [{ item_id: "i1", status: "weird_state" }] });
    expect(items[0].ok).toBe(false);
    expect(items[0].statusText).toBe("状态：weird_state");
  });
});

describe("历史：失败不清空可重试，成功则重建持久化终态", () => {
  it("历史读取失败保留已有结果并给出可重试错误（不当作空历史）", async () => {
    const s = store();
    s.recordEvent({ approval_id: "ap_1", phase: "executed", ok: true, summary: "本地已执行" });
    mocks.getApprovalHistory.mockRejectedValueOnce(new Error("network down"));
    const ok = await s.loadHistory();
    expect(ok).toBe(false);
    expect(s.historyError).toContain("读取审批历史失败");
    expect(s.historyError).toContain("重试");
    expect(s.entries.map((entry) => entry.approvalId)).toEqual(["ap_1"]);
    expect(s.historyLoadedAt).toBeNull();
  });

  it("重试成功后清除错误并从 history 重建（含逐项 detail）", async () => {
    const s = store();
    mocks.getApprovalHistory.mockRejectedValueOnce(new Error("boom"));
    await s.loadHistory();
    expect(s.historyError).toBeTruthy();

    mocks.getApprovalHistory.mockResolvedValueOnce([
      {
        approval_id: "ap_h1",
        kind: "tool_execution",
        summary: "批量写回部分完成",
        outcome: "executed",
        detail: {
          kind: "tool_execution",
          label: "批量写回附件源文件",
          results: [
            { item_id: "i1", status: "completed", ok: true },
            { item_id: "i2", status: "failed", ok: false, reason: "磁盘写满" },
          ],
        },
        at: "2026-10-25T10:00:00+08:00",
      },
      {
        approval_id: "ap_h2",
        kind: "tool_create",
        summary: "你没有同意创建这个工具",
        outcome: "rejected",
        detail: { kind: "tool_create", label: "创建工具 x" },
        at: "2026-10-25T09:00:00+08:00",
      },
    ]);
    const ok = await s.loadHistory();
    expect(ok).toBe(true);
    expect(s.historyError).toBeNull();
    expect(s.byId["ap_h1"].state).toBe("completed");
    expect(s.byId["ap_h1"].label).toBe("批量写回附件源文件");
    expect(s.byId["ap_h1"].items.map((item) => item.itemId)).toEqual(["i1", "i2"]);
    expect(s.byId["ap_h2"].state).toBe("rejected");
    expect(s.byId["ap_h2"].ok).toBe(false);
    expect(s.byId["ap_h2"].label).toBe("创建工具 x");
  });

  it("实时终态优先于同一 id 的历史行，不被历史降级/改写", () => {
    const live = resultFromEvent({
      approval_id: "ap_1",
      phase: "failed",
      ok: false,
      summary: "执行失败：磁盘写满",
      detail: { results: [{ item_id: "i1", status: "failed", ok: false }] },
    })!;
    const history = resultFromHistory({
      approval_id: "ap_1",
      kind: "tool_execution",
      summary: "后端历史（较旧）",
      outcome: "failed",
      detail: { results: [{ item_id: "i1", status: "failed" }] },
      at: "2026-10-25T10:00:00+08:00",
    })!;
    expect(live).not.toBeNull();
    expect(history).not.toBeNull();

    const s = store();
    s.put(live);
    const merged = mergeResultEntry(s.byId["ap_1"], history);
    // 实时终态不会被历史行改写（历史只补缺口）
    expect(merged.state).toBe("failed");
    expect(merged.summary).toContain("磁盘写满");
    expect(merged.items).toHaveLength(1);
    // 非终态历史也不能把终态降级
    const lower = resultFromHistory({
      approval_id: "ap_1",
      kind: "tool_execution",
      summary: "旧的非终态",
      outcome: "approved",
      detail: {},
      at: "2026-10-25T10:00:00+08:00",
    })!;
    expect(mergeResultEntry(s.byId["ap_1"], lower).state).toBe("failed");
  });
});
