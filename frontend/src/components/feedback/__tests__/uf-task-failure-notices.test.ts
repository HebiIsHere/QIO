/**
 * 任务失败 → 顶部通知：基线、重放、去重、已处理过滤、重试新失败。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { nextTick } from "vue";
import { createPinia, setActivePinia } from "pinia";
import { useTaskFailureNotices, taskFailureNoticeText } from "../useTaskFailureNotices";
import { useConversationTasksStore } from "../../../stores/conversationTasks";
import { NOTICE_MERGE_WINDOW_MS, useNotificationsStore } from "../../../stores/notifications";
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

/** 模拟一份新的权威快照（loadedAt 变化 = 有新快照到达）。 */
async function pushSnapshot(
  tasks: ReturnType<typeof useConversationTasksStore>,
  items: TaskFeedItem[],
  opts: { loadedAt: number; instanceId?: string | null } ,
) {
  tasks.current.loaded = true;
  tasks.current.items = items;
  tasks.current.instanceId = opts.instanceId ?? "inst-1";
  tasks.current.loadedAt = opts.loadedAt;
  await nextTick();
  await nextTick();
}

describe("任务失败顶部通知：真实快照基线", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    document.body.innerHTML = "";
  });

  it("首份快照里已有的历史失败不提醒；之后真正新增的失败才提醒", async () => {
    const { tasks, notifications } = setup();
    const handle = useTaskFailureNotices();
    await pushSnapshot(tasks, [task({ id: "subagent:old", sourceId: "old", finishedAt: "2026-10-26T00:00:00+00:00" })], { loadedAt: 1 });
    expect(notifications.notices).toHaveLength(0);

    await pushSnapshot(
      tasks,
      [
        task({ id: "subagent:old", sourceId: "old", finishedAt: "2026-10-26T00:00:00+00:00" }),
        task({ id: "subagent:new", sourceId: "new", title: "编译报告" }),
      ],
      { loadedAt: 2 },
    );
    expect(notifications.notices).toHaveLength(1);
    expect(notifications.notices[0].text).toContain("编译报告");
    expect(notifications.notices[0].members.map((member) => member.taskId)).toEqual(["subagent:new"]);
    handle.stop();
  });

  it("断线重连后的快照不重放历史失败", async () => {
    const { tasks, notifications } = setup();
    const handle = useTaskFailureNotices();
    await pushSnapshot(tasks, [task({ id: "subagent:old", sourceId: "old" })], { loadedAt: 1 });
    tasks.connection = "offline";
    await nextTick();
    tasks.connection = "online";
    await pushSnapshot(
      tasks,
      [
        task({ id: "subagent:old", sourceId: "old" }),
        task({ id: "subagent:older", sourceId: "older", title: "断线期间的旧失败" }),
      ],
      { loadedAt: 2 },
    );
    expect(notifications.notices).toHaveLength(0);
    handle.stop();
  });

  it("已「知道了」的失败不再提醒（显示前过滤已处理）", async () => {
    const { tasks, notifications } = setup();
    const handle = useTaskFailureNotices();
    await pushSnapshot(tasks, [], { loadedAt: 1 });
    // 失败先进入队列：建立一条待显示通知
    await pushSnapshot(tasks, [task({ id: "subagent:t2", sourceId: "t2", title: "上传附件" })], { loadedAt: 2 });
    expect(notifications.notices).toHaveLength(1);
    // 用户点了「知道了」：后端把这条移入历史（同一条快照行标 acknowledged=true）
    await pushSnapshot(
      tasks,
      [task({ id: "subagent:t2", sourceId: "t2", title: "上传附件", acknowledged: true })],
      { loadedAt: 3 },
    );
    // 合并窗口结束后尝试显示：显示前核对发现已处理 → 丢弃，不弹过期事实
    vi.advanceTimersByTime(NOTICE_MERGE_WINDOW_MS + 10);
    expect(notifications.hasActive).toBe(false);
    expect(notifications.notices).toHaveLength(0);
    handle.stop();
  });

  it("任务重试后再次失败：新失败身份可再次提醒", async () => {
    const { tasks, notifications } = setup();
    const handle = useTaskFailureNotices();
    await pushSnapshot(tasks, [task({ id: "subagent:retry", sourceId: "retry", finishedAt: "2026-10-26T00:00:00+00:00" })], { loadedAt: 1 });
    expect(notifications.notices).toHaveLength(0);
    // 第二次运行仍然失败：结束时间变了 = 新失败事件身份
    await pushSnapshot(tasks, [task({ id: "subagent:retry", sourceId: "retry", finishedAt: "2026-10-27T00:00:00+00:00" })], { loadedAt: 2 });
    expect(notifications.notices).toHaveLength(1);
    handle.stop();
  });

  it("可见文案以任务名为主，不带来源前缀", () => {
    expect(taskFailureNoticeText(task({ title: "编译报告" }))).toBe("「编译报告」失败了");
    expect(taskFailureNoticeText(task({ title: "", source: "dev_task" }))).toBe("开发任务失败了");
  });
});
