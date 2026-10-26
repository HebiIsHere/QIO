/**
 * 任务失败 → 顶部短通知（B）。
 *
 * 权威来源只有一处：`stores/conversationTasks` 的真实快照（GET /api/tasks）。
 * 这里**不复制**任务状态、不猜失败、也不看历史回放：
 * - 首份快照完成前：只建立基线，不提醒（避免一打开应用就把历史失败全弹一遍）；
 * - 断线重连 / 后端换实例：快照可能带回一批旧失败 —— 重新建立基线，不提醒；
 * - 基线之后**真正新增**的失败身份（任务身份 + 失败事件身份，重试后的新失败算新身份）才提醒；
 * - 失败合并 500ms 由 notifications store 负责（同一时间只显示一条）；
 * - 显示前最后核对：任务此刻仍是失败才显示（已「知道了」/已恢复的不再提醒）。
 *
 * 单条回复未完成 / 用户主动停止 / 发送结果未知都不属于任务失败，不进这里。
 */
import { watch, type WatchStopHandle } from "vue";
import { useConversationTasksStore } from "../../stores/conversationTasks";
import { useNotificationsStore } from "../../stores/notifications";
import { taskDisplayStatus } from "../../services/conversationTasks";
import { TASK_SOURCE_LABEL } from "../../conversation/contracts";
import type { TaskFeedItem } from "../../conversation/contracts";

/** 已见失败身份的上限（有界，避免长会话无限增长）。 */
export const TASK_FAILURE_SEEN_LIMIT = 500;

/**
 * 失败事件身份。
 *
 * 任务身份 + 结束时间（缺失时退到开始时间 / 原始状态）：同一条任务重试后再次失败，
 * 结束时间会变 → 新身份 → 可以再次提醒；同一份快照重复读到时身份不变 → 不重复提醒。
 */
export function taskFailureIdentity(item: TaskFeedItem): string {
  const at = String(item.finishedAt ?? item.startedAt ?? item.rawStatus ?? "");
  return item.id + "@" + at;
}

/**
 * 通知可见文案：**任务名为主**（单项「〈任务名〉失败了」；多项由通知调度合成
 * 「N 项任务失败了」）。来源不作为可见前缀；长任务名交给组件省略、完整读屏文案保留。
 */
export function taskFailureNoticeText(item: TaskFeedItem): string {
  const title = String(item.title || item.summary || "").trim();
  if (title) return "「" + title + "」失败了";
  const label = TASK_SOURCE_LABEL[item.source] ?? "任务";
  return label + "失败了";
}

export interface TaskFailureNoticesHandle {
  /** 卸载时必须调用：停止监听（不动任务状态本身）。 */
  stop: () => void;
}

/**
 * 挂上任务失败监听（由 GlobalFeedbackHost 挂一次；卸载时 stop）。
 */
export function useTaskFailureNotices(): TaskFailureNoticesHandle {
  const tasks = useConversationTasksStore();
  const notifications = useNotificationsStore();

  /** 已见过的失败身份（基线 + 后续新增）。 */
  let seen = new Set<string>();
  let baselineReady = false;
  let lastInstance: string | null = null;

  /**
   * 当前快照里**仍需要提醒**的失败项。
   *
   * 排除已「知道了」的项：acknowledged=true 表示后端已经把它移入历史（快照里可能
   * 还带旧行），它不再是一个待处理失败，也不应为了它弹顶部通知。
   */
  function failedItems(): TaskFeedItem[] {
    const items = Array.isArray(tasks.current.items) ? tasks.current.items : [];
    return items.filter((item) => !item.acknowledged && taskDisplayStatus(item) === "failed");
  }

  function captureBaseline(): void {
    seen = new Set(failedItems().map(taskFailureIdentity));
    // 有界：只保留最近的若干身份（Set 有序，保留尾部）
    if (seen.size > TASK_FAILURE_SEEN_LIMIT) {
      const keep = [...seen].slice(-TASK_FAILURE_SEEN_LIMIT);
      seen = new Set(keep);
    }
    baselineReady = true;
    lastInstance = tasks.current.instanceId ?? null;
  }

  function resetBaseline(): void {
    baselineReady = false;
    seen = new Set();
  }

  /** 显示前最后核对：这条任务此刻仍然是失败、且没有被「知道了」处理掉。 */
  function stillFailed(taskId: string): boolean {
    try {
      const rows = tasks.displayItems("current");
      const row = rows.find((item) => item.id === taskId);
      if (!row || row.acknowledged) return false;
      return taskDisplayStatus(row) === "failed";
    } catch {
      return false;
    }
  }

  function enqueueFailure(item: TaskFeedItem): void {
    const identity = taskFailureIdentity(item);
    notifications.enqueue({
      key: "task-failed:" + identity,
      kind: "task_failed",
      text: taskFailureNoticeText(item),
      taskIds: [item.id],
      mergeable: true,
      focus: true,
      stillRelevant: () => stillFailed(item.id),
    });
  }

  function sync(): void {
    if (!tasks.current.loaded) return;
    const instance = tasks.current.instanceId ?? null;
    // 后端换实例：旧实例的失败不再可信 → 静默重建基线
    if (baselineReady && lastInstance !== null && instance !== null && instance !== lastInstance) {
      resetBaseline();
    }
    if (!baselineReady) {
      captureBaseline();
      return;
    }
    const fresh: TaskFeedItem[] = [];
    for (const item of failedItems()) {
      const identity = taskFailureIdentity(item);
      if (seen.has(identity)) continue;
      seen.add(identity);
      fresh.push(item);
    }
    if (seen.size > TASK_FAILURE_SEEN_LIMIT) {
      seen = new Set([...seen].slice(-TASK_FAILURE_SEEN_LIMIT));
    }
    for (const item of fresh) enqueueFailure(item);
  }

  const stops: WatchStopHandle[] = [];
  // 快照签名：加载完成 / 实例 / 刷新时刻 / 条数变化都代表「有新快照到达」
  stops.push(
    watch(
      () =>
        [
          tasks.current.loaded ? "1" : "0",
          tasks.current.instanceId ?? "",
          String(tasks.current.loadedAt),
          String(tasks.current.items.length),
        ].join("|"),
      () => sync(),
      { immediate: true },
    ),
  );
  // 断线：下一次快照是重新对齐，不是「新失败」→ 静默重建基线
  stops.push(
    watch(
      () => tasks.connection,
      (connection, previous) => {
        if (connection === "offline") resetBaseline();
        // 逻辑：offline 时立刻作废基线；重连后的首份快照由 sync() 重新建立
        void previous;
      },
    ),
  );

  return {
    stop() {
      for (const stop of stops) stop();
      stops.length = 0;
    },
  };
}