/**
 * A · 只提醒仍需用户处理的失败（F07）+ 弹窗期间分时失败与真实快照的集成（F05）。
 *
 * 断言在修复前失败的根因：失败判定只看了 status=failed / acknowledged，
 * 没有用权威 requiresAttention（isAttentionItem）；弹窗期间两条失败仍是两条通知。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { nextTick } from "vue";
import { createPinia, setActivePinia } from "pinia";
import { useTaskFailureNotices } from "../useTaskFailureNotices";
import { useConversationTasksStore } from "../../../stores/conversationTasks";
import {
  NOTICE_MERGE_WINDOW_MS,
  noticeDisplayText,
  noticeTaskCount,
  useNotificationsStore,
} from "../../../stores/notifications";
import type { TaskFeedItem } from "../../../conversation/contracts";

function task(over: Partial<TaskFeedItem> = {}): TaskFeedItem {
  return {
    id: "subagent:t1",
    source: "subagent",
    sourceId: "t1",
    title: "整理资料",
    status: "failed",
    rawStatus: "failed",
    origin: "unknown",
    topicId: null,
    topicTitle: null,
    executor: null,
    summary: "",
    queuedAt: null,
    startedAt: null,
    finishedAt: "2026-10-27T00:00:00+00:00",
    durationMs: null,
    requiresAttention: true,
    acknowledged: false,
    output: { truncated: false, preview: "", hint: "", readMore: false },
    actions: [],
    ...over,
  };
}

function setup() {
  setActivePinia(createPinia());
  return {
    tasks: useConversationTasksStore(),
    notifications: useNotificationsStore(),
  };
}

async function pushSnapshot(
  tasks: ReturnType<typeof useConversationTasksStore>,
  items: TaskFeedItem[],
  opts: { loadedAt: number; instanceId?: string | null },
) {
  tasks.current.loaded = true;
  tasks.current.items = items;
  tasks.current.instanceId = opts.instanceId ?? "inst-1";
  tasks.current.loadedAt = opts.loadedAt;
  await nextTick();
  await nextTick();
}

describe("F07 只提醒仍需用户处理的失败（requiresAttention）", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    document.body.innerHTML = "";
  });

  it("failed + requiresAttention=false：不弹通知", async () => {
    const { tasks, notifications } = setup();
    const handle = useTaskFailureNotices();
    await pushSnapshot(tasks, [], { loadedAt: 1 });
    await pushSnapshot(
      tasks,
      [task({ id: "subagent:no-action", title: "无需处理的历史失败", requiresAttention: false })],
      { loadedAt: 2 },
    );
    expect(notifications.notices).toHaveLength(0);
    vi.advanceTimersByTime(NOTICE_MERGE_WINDOW_MS + 10);
    expect(notifications.hasActive).toBe(false);
    handle.stop();
  });

  it("真实需要用户处理的失败（requiresAttention=true）正常提醒", async () => {
    const { tasks, notifications } = setup();
    const handle = useTaskFailureNotices();
    await pushSnapshot(tasks, [], { loadedAt: 1 });
    await pushSnapshot(
      tasks,
      [task({ id: "subagent:real", sourceId: "real", title: "编译报告", requiresAttention: true })],
      { loadedAt: 2 },
    );
    expect(notifications.notices).toHaveLength(1);
    vi.advanceTimersByTime(NOTICE_MERGE_WINDOW_MS + 10);
    const active = notifications.active!;
    expect(noticeDisplayText(active)).toBe("「编译报告」失败了");
    expect(active.members.map((member) => member.taskId)).toEqual(["subagent:real"]);
    handle.stop();
  });

  it("排队期间变为不需要处理（requiresAttention=false）：显示前移除，不弹过期提醒", async () => {
    const { tasks, notifications } = setup();
    const handle = useTaskFailureNotices();
    await pushSnapshot(tasks, [], { loadedAt: 1 });
    await pushSnapshot(tasks, [task({ id: "subagent:t2", sourceId: "t2" })], { loadedAt: 2 });
    expect(notifications.notices).toHaveLength(1);

    // 还没显示前，后端把它改成不再需要用户处理
    await pushSnapshot(
      tasks,
      [task({ id: "subagent:t2", sourceId: "t2", requiresAttention: false })],
      { loadedAt: 3 },
    );
    vi.advanceTimersByTime(NOTICE_MERGE_WINDOW_MS + 10);
    expect(notifications.hasActive).toBe(false);
    expect(notifications.notices).toHaveLength(0);
    handle.stop();
  });

  it("排队期间被「知道了」（acknowledged）：显示前移除，不再提醒", async () => {
    const { tasks, notifications } = setup();
    const handle = useTaskFailureNotices();
    await pushSnapshot(tasks, [], { loadedAt: 1 });
    await pushSnapshot(tasks, [task({ id: "subagent:t3", sourceId: "t3" })], { loadedAt: 2 });
    await pushSnapshot(
      tasks,
      [task({ id: "subagent:t3", sourceId: "t3", acknowledged: true })],
      { loadedAt: 3 },
    );
    vi.advanceTimersByTime(NOTICE_MERGE_WINDOW_MS + 10);
    expect(notifications.hasActive).toBe(false);
    expect(notifications.notices).toHaveLength(0);
    handle.stop();
  });
});

describe("F05 弹窗期间分时到达的失败（真实快照 + 通知调度集成）", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    document.body.innerHTML = "";
  });

  it("弹窗期间 A 失败，1.5s 后 B 失败：弹窗结束只提醒一条「2 项任务失败了」", async () => {
    const { tasks, notifications } = setup();
    const handle = useTaskFailureNotices();
    await pushSnapshot(tasks, [], { loadedAt: 1 });

    notifications.suspendForModal();
    await pushSnapshot(
      tasks,
      [task({ id: "subagent:a", sourceId: "a", title: "整理资料" })],
      { loadedAt: 2 },
    );
    expect(notifications.notices).toHaveLength(1);
    vi.advanceTimersByTime(1500);

    await pushSnapshot(
      tasks,
      [
        task({ id: "subagent:a", sourceId: "a", title: "整理资料" }),
        task({ id: "subagent:b", sourceId: "b", title: "编译报告", finishedAt: "2026-10-27T00:00:01+00:00" }),
      ],
      { loadedAt: 3 },
    );
    expect(notifications.notices).toHaveLength(2);

    notifications.resumeFromModal();
    vi.advanceTimersByTime(NOTICE_MERGE_WINDOW_MS + 10);

    expect(notifications.notices).toHaveLength(1);
    const active = notifications.active!;
    expect(noticeTaskCount(active)).toBe(2);
    expect(noticeDisplayText(active)).toBe("2 项任务失败了");
    expect(active.members.map((member) => member.taskId)).toEqual(["subagent:a", "subagent:b"]);
    handle.stop();
  });
});
