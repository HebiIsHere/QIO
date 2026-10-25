/**
 * 互动任务中心 / 批量待确认的**纯逻辑**（A 组 F03）。
 *
 * 边界：不读全局、不发请求、不碰 DOM。输入是 store 收到的真实服务端事实
 * （新链意图视图 + 旧链意图清单 + 执行记录某条意图的最新执行状态），
 * 输出是**同一份任务行**：界面显示、操作资格（暂停 / 继续 / 取消 / 批准 / 拒绝）
 * 与数量都从这份行数据来，避免「两份列表分别驱动界面和资格」。
 */
import {
  canRecover,
  isConfirmable,
  statusLabel,
  statusTone,
  type IntentView,
  type StatusTone,
} from "../../../interactive/intentWorkbench";
import type { ExecutionStatus } from "../../../interactive/types";

/** 执行控制动作：与 services/interactive 的 pauseIntent / resumeIntent / cancelIntent 一一对应。 */
export type TaskControlAction = "pause" | "resume" | "cancel";

/** 任务分组：执行中 / 已暂停 / 等待处理 / 已结束（同一行只属于一组）。 */
export type TaskBucket = "live" | "paused" | "waiting" | "finished";

export interface TaskRow {
  intentId: string;
  title: string;
  /** 意图状态（新链视图状态，或旧链状态原文） */
  status: string;
  /** 状态人话：优先真实执行记录，其次意图状态；未知状态原样显示 */
  statusText: string;
  tone: StatusTone;
  bucket: TaskBucket;
  version: number;
  /** 旧链存储里的原始状态（有它说明这个 id 属于旧链存储，批准 / 拒绝接口对它有效） */
  legacyStatus: string | null;
  /** 可以确认（新链：proposed / awaiting_confirmation，且不是 needs_update） */
  confirmable: boolean;
  /** 可以拒绝（旧链存储的待处理意图；能力保留，不在迁移中丢失） */
  rejectable: boolean;
  /** 可以进入恢复（paused / failed / partially_completed 且授权仍有效） */
  recoverable: boolean;
  actions: TaskControlAction[];
  executionStatus: ExecutionStatus | "none";
  hasExecutionRecord: boolean;
  /** 是否来自新链意图视图（false = 只有旧链清单里的一条） */
  fromView: boolean;
}

export interface TaskSourceView {
  intentId: string;
  title: string;
  status: string;
  version: number;
  legacyStatus: string | null;
  authorizationValid: boolean | null;
}

export interface TaskSourceLegacy {
  intentId: string;
  title: string;
  status: string;
  version: number;
}

/** 旧链意图状态的人话（只用于「只有旧链清单」的行，不冒充新链状态）。 */
export const LEGACY_TASK_STATUS_LABELS: Record<string, string> = {
  pending: "等待审批",
  needs_update: "需要更新",
  rejected: "已拒绝",
  waiting_dependency: "等待前项",
  waiting_confirm: "等待再次确认",
  running: "执行中",
  paused: "已暂停",
  done: "已完成",
  failed: "失败",
  cancelled: "已取消",
};

const LIVE_EXECUTION: readonly ExecutionStatus[] = ["queued", "running"];
const SETTLED_EXECUTION: readonly ExecutionStatus[] = ["done", "failed", "cancelled"];

function isNeedsUpdate(view: { status: string; legacyStatus: string | null }): boolean {
  return view.status === "needs_update" || view.legacyStatus === "needs_update";
}

/** 真实执行状态优先：界面必须说「执行中」而不是「已确认（等待执行）」。 */
export function taskStatusText(
  status: string,
  executionStatus: ExecutionStatus | "none",
  fromView = true,
): string {
  if (executionStatus === "queued" || executionStatus === "running") return "执行中";
  if (executionStatus === "paused") return "已暂停";
  if (executionStatus === "done") return "已完成";
  if (executionStatus === "failed") return "失败";
  if (executionStatus === "cancelled") return "已取消";
  if (fromView) return statusLabel(status);
  return LEGACY_TASK_STATUS_LABELS[status] ?? status;
}

export function taskTone(
  status: string,
  executionStatus: ExecutionStatus | "none",
  fromView = true,
): StatusTone {
  if (executionStatus === "failed" || executionStatus === "cancelled") return "problem";
  if (executionStatus === "running" || executionStatus === "queued") return "active";
  if (executionStatus === "paused" || executionStatus === "done") return "waiting";
  return fromView ? statusTone(status) : statusTone(status === "done" ? "completed" : status);
}

/** 这次能对这条任务做哪些真实控制：只有运行中可暂停/取消，暂停可继续/取消。 */
export function taskControlActions(
  status: string,
  executionStatus: ExecutionStatus | "none",
): TaskControlAction[] {
  if (executionStatus === "queued" || executionStatus === "running") return ["pause", "cancel"];
  if (executionStatus === "paused") return ["resume", "cancel"];
  if (status === "running") return ["pause", "cancel"];
  if (status === "paused") return ["resume", "cancel"];
  if (status === "approved") return ["cancel"];
  return [];
}

export function taskBucket(status: string, executionStatus: ExecutionStatus | "none"): TaskBucket {
  if (executionStatus === "paused" || status === "paused") return "paused";
  if (
    (LIVE_EXECUTION as readonly string[]).includes(executionStatus) ||
    status === "running" ||
    status === "approved"
  ) {
    return "live";
  }
  if (
    (SETTLED_EXECUTION as readonly string[]).includes(executionStatus) ||
    ["completed", "partially_completed", "failed", "cancelled", "rejected", "done"].includes(status)
  ) {
    return "finished";
  }
  return "waiting";
}

function confirmableOf(status: string, legacyStatus: string | null, fromView: boolean): boolean {
  if (!fromView) {
    return ["pending", "needs_update", "waiting_confirm", "waiting_dependency"].includes(status);
  }
  const fake = { status, raw_legacy_status: legacyStatus ?? undefined } as unknown as IntentView;
  if (isNeedsUpdate({ status, legacyStatus })) return false;
  return isConfirmable(fake);
}

function recoverableOf(
  status: string,
  authorizationValid: boolean | null,
  fromView: boolean,
): boolean {
  if (!fromView) return ["paused", "failed"].includes(status);
  if (authorizationValid === false) return false;
  return canRecover({ status, authorization_valid: authorizationValid ?? undefined } as IntentView);
}

/**
 * 合并成**唯一一份任务行**：新链视图优先（更完整），旧链清单里没被覆盖的条目补在后面。
 * 同一 intent_id 只出现一次 —— 界面与操作资格不会出现「同一个对象两套状态」。
 */
export function buildTaskRows(input: {
  views: readonly TaskSourceView[];
  legacy: readonly TaskSourceLegacy[];
  executionStatus: Record<string, ExecutionStatus>;
}): TaskRow[] {
  const rows: TaskRow[] = [];
  const seen = new Set<string>();
  const execOf = (intentId: string): ExecutionStatus | "none" =>
    input.executionStatus?.[intentId] ?? "none";

  for (const view of input.views ?? []) {
    const intentId = String(view?.intentId ?? "");
    if (!intentId || seen.has(intentId)) continue;
    seen.add(intentId);
    const status = String(view.status ?? "");
    const legacyStatus = view.legacyStatus ?? null;
    const exec = execOf(intentId);
    rows.push({
      intentId,
      title: String(view.title ?? "") || intentId,
      status,
      statusText: taskStatusText(status, exec, true),
      tone: taskTone(status, exec, true),
      bucket: taskBucket(status, exec),
      version: Number(view.version ?? 0),
      legacyStatus,
      confirmable: confirmableOf(status, legacyStatus, true),
      rejectable: legacyStatus !== null && confirmableOf(status, legacyStatus, true),
      recoverable: recoverableOf(status, view.authorizationValid ?? null, true),
      actions: taskControlActions(status, exec),
      executionStatus: exec,
      hasExecutionRecord: exec !== "none",
      fromView: true,
    });
  }

  for (const legacy of input.legacy ?? []) {
    const intentId = String(legacy?.intentId ?? "");
    if (!intentId || seen.has(intentId)) continue;
    seen.add(intentId);
    const status = String(legacy.status ?? "");
    const exec = execOf(intentId);
    rows.push({
      intentId,
      title: String(legacy.title ?? "") || intentId,
      status,
      statusText: taskStatusText(status, exec, false),
      tone: taskTone(status, exec, false),
      bucket: taskBucket(status, exec),
      version: Number(legacy.version ?? 0),
      legacyStatus: status,
      confirmable: confirmableOf(status, status, false),
      rejectable: confirmableOf(status, status, false),
      recoverable: recoverableOf(status, null, false),
      actions: taskControlActions(status, exec),
      executionStatus: exec,
      hasExecutionRecord: exec !== "none",
      fromView: false,
    });
  }

  // 执行中 / 已暂停在前，其次待处理，最后已结束：入口点开先看到需要动手的
  const order: Record<TaskBucket, number> = { live: 0, paused: 1, waiting: 2, finished: 3 };
  rows.sort((a, b) => order[a.bucket] - order[b.bucket]);
  return rows;
}

export interface TaskCounts {
  total: number;
  live: number;
  paused: number;
  waiting: number;
  finished: number;
  /** 需要用户动手的数量（执行中 / 已暂停 / 待确认） */
  needsUser: number;
}

export function taskCounts(rows: readonly TaskRow[] | null | undefined): TaskCounts {
  const list = rows ?? [];
  const counts: TaskCounts = { total: list.length, live: 0, paused: 0, waiting: 0, finished: 0, needsUser: 0 };
  for (const row of list) counts[row.bucket] += 1;
  counts.needsUser = counts.live + counts.paused + list.filter((row) => row.confirmable).length;
  return counts;
}

/** 空态：准确说明「哪一类现在没有」，不笼统留白。 */
export function taskEmptyText(section?: TaskBucket): string {
  switch (section) {
    case "live":
      return "现在没有执行中的互动任务。";
    case "paused":
      return "现在没有已暂停的互动任务。";
    case "waiting":
      return "现在没有等待确认或排队的互动任务。";
    case "finished":
      return "还没有已结束的互动任务。";
    default:
      return "现在没有互动任务（确认意图之后才会开始执行）。";
  }
}

/** 控制按钮的进行中 / 默认文案（按钮上必须看得出正在处理）。 */
export function taskControlLabel(action: TaskControlAction, busy: boolean): string {
  if (action === "pause") return busy ? "暂停中…" : "暂停任务";
  if (action === "resume") return busy ? "继续中…" : "继续任务";
  return busy ? "取消中…" : "取消任务";
}

/** 这一行的动作是不是「改任务状态」而不是「聊天停止」：批量入口与文案分开的判据。 */
export function taskControlHint(action: TaskControlAction): string {
  if (action === "pause") return "暂停这条互动任务（不影响聊天）";
  if (action === "resume") return "继续这条互动任务（按当前材料，不自动重试）";
  return "取消这条互动任务（与停止当前聊天不是同一件事）";
}

/** 批量操作是否可用：只对显式选中的、还能处理的旧链意图。 */
export function batchDecideIds(rows: readonly TaskRow[], selected: readonly string[]): string[] {
  const eligible = new Set(
    (rows ?? []).filter((row) => row.fromView === false && row.rejectable).map((row) => row.intentId),
  );
  const out: string[] = [];
  for (const id of selected ?? []) {
    if (eligible.has(id) && !out.includes(id)) out.push(id);
  }
  return out;
}
