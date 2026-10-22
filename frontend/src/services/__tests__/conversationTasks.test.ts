/**
 * 左侧任务栏展示规则（A 负责的纯逻辑）。
 *
 * 这里守的是契约里最容易「看起来没问题、其实是假话」的几条：
 * 未知状态不能变运行中、部分完成不能变已完成、排队不能伪造开始时间、
 * 置顶只给真的需要处理的、去重按稳定身份、只有后端支持的动作才出现。
 */
import { describe, expect, it, vi } from "vitest";
import type { TaskAction, TaskFeedItem } from "../../conversation/contracts";
import type { RecoveryRecordView } from "../recoveryApi";
import {
  activeCountOf,
  attentionCountOf,
  buildContinueDevInstruction,
  dedupeTasks,
  formatTaskClock,
  formatTaskDuration,
  isAttentionItem,
  mergeRetainedTasks,
  recoveryCountOf,
  recoveryDisplayStatus,
  recoveryRecordToTaskItem,
  requestTaskPreview,
  resolveRemovedTask,
  sortTasks,
  taskDisplayStatus,
  taskOriginText,
  taskStatusLabel,
  taskTimingText,
  visibleTaskActions,
} from "../conversationTasks";

function item(overrides: Partial<TaskFeedItem> = {}): TaskFeedItem {
  return {
    id: "tool:tr_1",
    source: "tool",
    sourceId: "tr_1",
    title: "一次工具执行",
    status: "running",
    rawStatus: "running",
    origin: "topic",
    topicId: "t1",
    topicTitle: "布局讨论",
    executor: null,
    summary: "",
    queuedAt: null,
    startedAt: null,
    finishedAt: null,
    durationMs: null,
    requiresAttention: false,
    acknowledged: false,
    output: { truncated: false, preview: "", hint: "", readMore: false },
    actions: [],
    ...overrides,
  };
}

function action(overrides: Partial<TaskAction> = {}): TaskAction {
  return { kind: "open_preview", label: "", supported: true, ...overrides };
}

function recovery(overrides: Partial<RecoveryRecordView> = {}): RecoveryRecordView {
  return {
    record_id: "rec-1",
    kind: "user_turn",
    state_class: "ready",
    status: "interrupted",
    message: "把这条消息重新发出去",
    topic_id: "t1",
    reason: "上一次没有执行完",
    created_at: "2026-10-22T08:00:00Z",
    updated_at: null,
    owner_instance_id: null,
    owner_state: "dead",
    owner_note: "",
    claim_generation: null,
    attempts: 1,
    last_error: null,
    actions: [{ id: "continue", label: "继续", enabled: true, reason: "" }],
    ...overrides,
  };
}

describe("状态映射：不把未知说成运行中、不把部分完成说成完成", () => {
  it("unknown / 不认识的状态保持「状态无法确认」", () => {
    expect(taskDisplayStatus(item({ status: "unknown" }))).toBe("unknown");
    expect(taskStatusLabel(item({ status: "unknown" }))).toBe("状态无法确认");
    // 展示字段不认识时按后端原始状态映射（running 是真的 running）；原始状态也不认识才是 unknown。
    expect(taskDisplayStatus(item({ status: "mystery" as never }))).toBe("running");
    expect(taskDisplayStatus(item({ status: "mystery" as never, rawStatus: "also_mystery" }))).toBe(
      "unknown",
    );
  });

  it("partial 就是部分完成，不会变成已完成", () => {
    expect(taskDisplayStatus(item({ status: "partial" }))).toBe("partial");
    expect(taskStatusLabel(item({ status: "partial" }))).toBe("部分完成");
    expect(taskStatusLabel(item({ status: "buffer" as never, rawStatus: "partially_completed" }))).toBe(
      "部分完成",
    );
  });

  it("status 缺失时按 rawStatus 映射；仍不认识就是 unknown", () => {
    expect(taskDisplayStatus(item({ status: "" as never, rawStatus: "cancelled" }))).toBe("cancelled");
    expect(taskDisplayStatus(item({ status: "" as never, rawStatus: "???" }))).toBe("unknown");
  });
});

describe("置顶与排序", () => {
  it("需要处理的任务置顶；其余按实际开始时间从新到旧", () => {
    const failed = item({
      id: "tool:failed",
      status: "failed",
      requiresAttention: true,
      startedAt: "2026-10-22T08:00:00Z",
    });
    const newer = item({ id: "tool:new", startedAt: "2026-10-22T10:00:00Z" });
    const older = item({ id: "tool:old", startedAt: "2026-10-22T09:00:00Z" });
    expect(sortTasks([newer, older, failed], { pinAttention: true }).map((row) => row.id)).toEqual([
      "tool:failed",
      "tool:new",
      "tool:old",
    ]);
    // 历史视图不置顶：纯粹按时间
    expect(sortTasks([newer, older, failed], { pinAttention: false }).map((row) => row.id)).toEqual([
      "tool:new",
      "tool:old",
      "tool:failed",
    ]);
  });

  it("排队项用入队时间排序，没有开始时间也照样参与排序", () => {
    const queued = item({
      id: "subagent:q",
      source: "subagent",
      sourceId: "q",
      status: "queued",
      queuedAt: "2026-10-22T11:00:00Z",
    });
    const running = item({ id: "tool:r", startedAt: "2026-10-22T10:00:00Z" });
    expect(sortTasks([running, queued], { pinAttention: true }).map((row) => row.id)).toEqual([
      "subagent:q",
      "tool:r",
    ]);
  });

  it("已「知道了」的旧失败不再置顶", () => {
    const acked = item({
      id: "tool:acked",
      status: "failed",
      requiresAttention: true,
      acknowledged: true,
      startedAt: "2026-10-22T12:00:00Z",
    });
    const active = item({ id: "tool:active", startedAt: "2026-10-22T09:00:00Z" });
    expect(isAttentionItem(acked)).toBe(false);
    expect(sortTasks([active, acked], { pinAttention: true }).map((row) => row.id)).toEqual([
      "tool:acked",
      "tool:active",
    ]);
  });
});

describe("去重与完成项短暂停留", () => {
  it("同一个 id 只出现一行；source+sourceId 相同也兜底去重", () => {
    const a = item({ id: "tool:1", sourceId: "1" });
    const b = item({ id: "tool:1", sourceId: "1", title: "重复行" });
    const c = item({ id: "tool:alias", source: "tool", sourceId: "1" });
    expect(dedupeTasks([a, b, c])).toHaveLength(1);
  });

  it("历史里找得到结局：用最终事实短暂停留（verified）", () => {
    const removed = item({ id: "tool:1", status: "running" });
    const final = item({ id: "tool:1", status: "completed", finishedAt: "2026-10-22T10:01:00Z" });
    const resolution = resolveRemovedTask(removed, [final], false);
    expect(resolution.action).toBe("retain");
    expect(resolution.verified).toBe(true);
    expect(resolution.item?.status).toBe("completed");
  });

  it("历史里找不到但用户正展开：保留并标注未确认；没展开就移除", () => {
    const removed = item({ id: "tool:2", status: "running" });
    const expanded = resolveRemovedTask(removed, [], true);
    expect(expanded.action).toBe("retain");
    expect(expanded.verified).toBe(false);
    expect(resolveRemovedTask(removed, null, false).action).toBe("drop");
  });

  it("快照与停留项同 id 时以快照为准，不出现两行", () => {
    const snapshot = item({ id: "tool:1", title: "快照里的最新值" });
    const retained = item({ id: "tool:1", title: "停留的旧值" });
    const merged = mergeRetainedTasks([snapshot], [retained]);
    expect(merged).toHaveLength(1);
    expect(merged[0].title).toBe("快照里的最新值");
  });
});

describe("数量：服务端计数优先，分页条数不冒充总数", () => {
  const counts = {
    queued: 2,
    running: 3,
    waitingApproval: 1,
    waitingUser: 2,
    completed: 9,
    partial: 1,
    failed: 4,
    cancelled: 0,
    budgetExhausted: 0,
    unknown: 0,
  };

  it("需要处理 = 等待审批 + 等待处理 + 失败（服务端总数）", () => {
    expect(attentionCountOf(counts, [])).toBe(7);
  });

  it("运行/排队数量用服务端计数", () => {
    expect(activeCountOf(counts, [])).toBe(5);
  });

  it("没有服务端计数时才按已加载条数兜底", () => {
    const failed = item({ id: "tool:f", status: "failed", requiresAttention: true });
    const running = item({ id: "tool:r" });
    expect(attentionCountOf(null, [failed])).toBe(1);
    expect(activeCountOf(undefined, [failed, running])).toBe(1);
  });
});

describe("时间与耗时", () => {
  const now = new Date(2026, 9, 22, 15, 0, 0);

  it("今天只显示时分；今年其他日期带月日；跨年带完整日期", () => {
    expect(formatTaskClock(new Date(2026, 9, 22, 9, 30).toISOString(), now)).toBe("09:30");
    expect(formatTaskClock(new Date(2026, 9, 1, 8, 5).toISOString(), now)).toBe("10-01 08:05");
    expect(formatTaskClock(new Date(2025, 11, 31, 23, 59).toISOString(), now)).toBe(
      "2025-12-31 23:59",
    );
    expect(formatTaskClock("not-a-date", now)).toBe("");
  });

  it("耗时用真实毫秒，不编造", () => {
    expect(formatTaskDuration(null)).toBe("");
    expect(formatTaskDuration(999)).toBe("不到 1 秒");
    expect(formatTaskDuration(5000)).toBe("5 秒");
    expect(formatTaskDuration(65_000)).toBe("1 分 5 秒");
    expect(formatTaskDuration(3_600_000)).toBe("1 小时");
  });

  it("排队只说排队时间，绝不伪造开始时间", () => {
    const queued = item({
      status: "queued",
      queuedAt: new Date(2026, 9, 22, 14, 0).toISOString(),
    });
    const text = taskTimingText(queued, now);
    expect(text).toBe("排队于 14:00");
    expect(text).not.toContain("开始");
  });

  it("运行中显示开始时间；完成显示完成时间与耗时", () => {
    const running = item({ startedAt: new Date(2026, 9, 22, 14, 10).toISOString() });
    expect(taskTimingText(running, now)).toBe("开始于 14:10");

    const completed = item({
      status: "completed",
      startedAt: new Date(2026, 9, 22, 13, 0).toISOString(),
      finishedAt: new Date(2026, 9, 22, 13, 2).toISOString(),
      durationMs: 120_000,
    });
    expect(taskTimingText(completed, now)).toBe("完成于 13:02 · 耗时 2 分");
  });

  it("没有时间事实就不显示时间", () => {
    expect(taskTimingText(item({ status: "waiting_user" }), now)).toBe("");
  });
});

describe("来源与动作可用性", () => {
  it("公共维护 / 话题 / 来源未知三者不互相冒充", () => {
    expect(taskOriginText(item({ origin: "global", topicId: null, topicTitle: null }))).toBe(
      "公共维护",
    );
    expect(taskOriginText(item({ origin: "topic", topicTitle: "布局讨论" }))).toBe("布局讨论");
    expect(taskOriginText(item({ origin: "topic", topicTitle: null }))).toBe("来源未知");
    expect(taskOriginText(item({ origin: "unknown" }))).toBe("来源未知");
  });

  it("只有 supported 且本端已接线的动作才进入界面；同 kind 只留一条", () => {
    const actions = [
      action({ kind: "open_preview", supported: true }),
      action({ kind: "cancel", supported: false }),
      action({ kind: "made_up" as never, supported: true }),
      action({ kind: "open_preview", supported: true }),
    ];
    const visible = visibleTaskActions(actions);
    expect(visible.map((entry) => entry.kind)).toEqual(["open_preview"]);
  });

  it("后端没给 label 时用契约里的中文说法", () => {
    const visible = visibleTaskActions([action({ kind: "ack", label: "" })]);
    expect(visible[0].label).toBe("");
  });
});

describe("恢复事项 → 任务行（来自现有收件箱，不重复拉后端任务接口）", () => {
  it("稳定身份 recovery:<record_id>，原始状态保留", () => {
    const record = recovery();
    const row = recoveryRecordToTaskItem(record);
    expect(row.id).toBe("recovery:rec-1");
    expect(row.source).toBe("recovery");
    expect(row.rawStatus).toBe("interrupted");
    expect(row.requiresAttention).toBe(true);
    expect(row.actions.map((entry) => entry.kind)).toEqual(["open_recovery"]);
    expect(recoveryCountOf([record])).toBe(1);
    expect(recoveryCountOf(undefined)).toBe(0);
  });

  it("明确在跑的恢复记录才显示运行中；其余是等待处理（原状态仍在 rawStatus）", () => {
    expect(recoveryDisplayStatus(recovery({ status: "running" }))).toBe("running");
    expect(recoveryDisplayStatus(recovery({ status: "queued" }))).toBe("queued");
    expect(recoveryDisplayStatus(recovery({ status: "interrupted" }))).toBe("waiting_user");
    expect(recoveryDisplayStatus(recovery({ status: "???", kind: "derived_task" }))).toBe(
      "waiting_user",
    );
  });

  it("派生任务没有用户原文时给不编造的标题", () => {
    const row = recoveryRecordToTaskItem(
      recovery({ kind: "derived_task", message: null, actions: [] }),
    );
    expect(row.title).toBe("一个没有跑完的后台任务");
    expect(row.actions).toHaveLength(0);
  });
});

describe("接线：预览事件与继续开发指令", () => {
  it("请求预览会发出带任务身份的事件（C 未合并前的约定通道）", () => {
    const handler = vi.fn();
    window.addEventListener("qio:open-preview", handler);
    const intent = requestTaskPreview(item({ id: "tool:9" }), "attachment:a1");
    expect(intent.taskId).toBe("tool:9");
    expect(handler).toHaveBeenCalledTimes(1);
    const event = handler.mock.calls[0][0] as CustomEvent;
    expect(event.detail.target).toBe("attachment:a1");
    window.removeEventListener("qio:open-preview", handler);
  });

  it("继续开发的指令带任务身份、真实状态与需求原文", () => {
    const text = buildContinueDevInstruction(
      item({
        id: "dev_task:dev_1",
        source: "dev_task",
        sourceId: "dev_1",
        title: "做一个批量重命名工具",
        status: "waiting_user",
        rawStatus: "testing_failed",
      }),
    );
    expect(text).toContain("dev_1");
    expect(text).toContain("testing_failed");
    expect(text).toContain("做一个批量重命名工具");
    expect(text).toContain("不要从头重写");
  });
});
