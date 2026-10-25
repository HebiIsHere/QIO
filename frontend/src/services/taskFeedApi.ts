/**
 * 任务栏取数封装（A 只调用这里，不要直接 fetch；契约 §5）。
 *
 * 权威快照 + 分页；增量事件由 A 接到 events store 后调用 reload，
 * 断线/重连/后端重启必须以本文件的快照为准。
 */
import { api } from "./api";
import type { TaskDetail, TaskFeedPage, TaskView } from "../conversation/contracts";

export async function fetchTaskFeed(opts: {
  view: TaskView;
  cursor?: string | null;
  limit?: number;
}): Promise<TaskFeedPage> {
  return api.getTaskFeed(opts);
}

export async function fetchTaskDetail(id: string): Promise<TaskDetail> {
  return api.getTaskDetail(id);
}

export async function acknowledgeTask(id: string): Promise<void> {
  await api.ackTask(id);
}

/** 只有后端真实支持时才应提供取消按钮；返回值只代表「请求已提交」。 */
export async function requestTaskCancel(id: string): Promise<{ accepted: boolean; detail: string }> {
  const res = await api.cancelTask(id);
  return {
    accepted: res.accepted === true,
    detail: res.detail ?? (res.accepted === true ? "已提交取消请求" : "后端没有接受这次取消"),
  };
}
