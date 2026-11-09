/**
 * A 组回归：任务栏「需要处理」计数与历史行的诚实展示（问题 4 / 5）。
 *
 * 守的几条：
 * - 「需要处理」计数与后端当前视图同口径：已知晓的失败进历史后不再计数；
 *   counts 是权威的当前视图总数，列表分页不全，不能用本地条数覆盖它；
 * - 终态行没有结束时间时如实说明（不伪造「结束于」、也不当成仍在运行）；
 * - 自动重试中的后台整理任务仍是排队状态，不会被当成最终失败。
 */
import { describe, expect, it } from "vitest";
import type { TaskCounts, TaskFeedItem } from "../../conversation/contracts";
import {
  attentionCountOf,
  isAttentionItem,
  taskDisplayStatus,
  taskTimingText,
} from "../conversationTasks";

const ZERO_COUNTS: TaskCounts = {
  queued: 0,
  running: 0,
  waitingApproval: 0,
  waitingUser: 0,
  completed: 0,
  partial: 0,
  failed: 0,
  cancelled: 0,
  budgetExhausted: 0,
  unknown: 0,
};

function counts(overrides: Partial<TaskCounts> = {}): TaskCounts {
  return { ...ZERO_COUNTS, ...overrides };
}

function item(overrides: Partial<TaskFeedItem> = {}): TaskFeedItem {
  return {
    id: "derived:task_1",
    source: "derived",
    sourceId: "task_1",
    title: "记忆摘要",
    status: "failed",
    rawStatus: "failed",
    origin: "topic",
    topicId: "t1",
    topicTitle: "布局讨论",
    executor: null,
    summary: "模型调用失败",
    queuedAt: "2026-10-22T08:00:00.000Z",
    startedAt: null,
    finishedAt: null,
    durationMs: null,
    requiresAttention: true,
    acknowledged: false,
    output: { truncated: false, preview: "", hint: "", readMore: false },
    actions: [],
    ...overrides,
  };
}

describe("需要处理数量：与后端当前视图口径一致", () => {
  it("服务端 counts 只含当前仍需处理的事项；已知晓失败在历史里不再计数", () => {
    // 单项确认后：后端 current 视图里没有失败项，counts 全 0（历史里仍有这条记录）。
    expect(attentionCountOf(counts(), [])).toBe(0);
    // 多项失败里确认掉一项：计数只减掉已确认的那一项。
    expect(attentionCountOf(counts({ failed: 1, waitingUser: 1 }), [])).toBe(2);
    expect(attentionCountOf(counts({ failed: 2 }), [])).toBe(2);
  });

  it("有服务端计数时以服务端为准，不拿本地已加载条数覆盖它", () => {
    // 列表是分页的：本地只剩一条也不代表总数；而 counts 已是当前视图口径。
    const acknowledged = item({
      id: "derived:acked",
      acknowledged: true,
      finishedAt: "2026-10-22T09:00:00.000Z",
    });
    expect(attentionCountOf(counts(), [acknowledged])).toBe(0);
    expect(isAttentionItem(acknowledged)).toBe(false);
  });

  it("只有服务端没给 counts 时才按已加载条目兜底", () => {
    const failed = item({ id: "derived:f", requiresAttention: true });
    const acknowledged = item({ id: "derived:ack", requiresAttention: true, acknowledged: true });
    const running = item({ id: "derived:r", status: "running", requiresAttention: false });
    expect(attentionCountOf(null, [failed, acknowledged, running])).toBe(1);
    expect(attentionCountOf(undefined, [failed, acknowledged])).toBe(1);
  });

  it("等待处理 / 等待审批同样按真实状态计入", () => {
    const waiting = item({ id: "dev_task:dev_1", status: "waiting_user", source: "dev_task" });
    const approval = item({ id: "tool:approval", status: "waiting_approval", source: "tool" });
    expect(isAttentionItem(waiting)).toBe(true);
    expect(isAttentionItem(approval)).toBe(true);
  });
});

describe("终态但没有结束时间：如实说明，不伪造", () => {
  const now = new Date(2026, 9, 22, 15, 0, 0);

  it("最终失败的后台整理任务没有结束时间：历史行说明「结束时间未记录」", () => {
    const failed = item({ queuedAt: null, finishedAt: null, status: "failed" });
    expect(taskDisplayStatus(failed)).toBe("failed");
    expect(taskTimingText(failed, now)).toBe("结束时间未记录");
    expect(taskTimingText(failed, now)).not.toContain("完成于");
    expect(taskTimingText(failed, now)).not.toContain("结束于");
  });

  it("有登记时间就如实说「记录于」；没有结束时间不能变成完成", () => {
    const failed = item({
      status: "failed",
      queuedAt: new Date(2026, 9, 22, 14, 0).toISOString(),
      finishedAt: null,
    });
    expect(taskTimingText(failed, now)).toBe("记录于 14:00");
  });

  it("有真实结束时间才显示结束时间；已完成项照常显示耗时", () => {
    const failed = item({ status: "failed", finishedAt: new Date(2026, 9, 22, 13, 5).toISOString() });
    expect(taskTimingText(failed, now)).toBe("结束于 13:05");

    const completed = item({
      status: "completed",
      startedAt: new Date(2026, 9, 22, 13, 0).toISOString(),
      finishedAt: new Date(2026, 9, 22, 13, 2).toISOString(),
      durationMs: 120_000,
    });
    expect(taskTimingText(completed, now)).toBe("完成于 13:02 · 耗时 2 分");
  });

  it("排队中的后台整理（自动重试）只说排队时间，不能显示成失败/结束", () => {
    const retrying = item({
      status: "queued",
      rawStatus: "failed（自动重试第 2 次）",
      requiresAttention: false,
      queuedAt: new Date(2026, 9, 22, 14, 30).toISOString(),
      finishedAt: null,
    });
    expect(taskDisplayStatus(retrying)).toBe("queued");
    expect(isAttentionItem(retrying)).toBe(false);
    expect(taskTimingText(retrying, now)).toBe("排队于 14:30");
  });
});
