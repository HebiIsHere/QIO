/**
 * 对话页 v2 · 共享契约（唯一来源）
 *
 * 本文件由 Lead 维护。子分支只读取/使用，不要复制一份改：状态映射、身份规则、
 * 布局偏好形状一旦分叉，左右两栏与审批入口就会各说一套。
 */
import type { PreviewPosition, PreviewTabState, PreviewTarget } from "./previewTypes";

export type { PreviewPosition, PreviewTabState, PreviewTarget };

// ── 任务：来源 / 状态 / 展示模型 ────────────────────────────────────────────

/** 左侧任务栏的真实来源。每个来源都有自己的持久化/恢复方式，展示层不合并身份。 */
export type TaskSource =
  | "subagent" // 独立子智能体任务（TaskManager，可排队/运行/完成/失败）
  | "derived" // 派生任务（摘要 / 知识提炼 / 实体提炼，持久队列）
  | "dev_task" // 未完成的开发任务（可继续开发 / 撤销授权 / 放弃）
  | "maintenance" // 记忆整理与维护（手动或已触发的定时任务）
  | "tool" // 代码 / 测试 / 脚本 / 清理等真实工具执行
  | "background" // 其他真实登记的后台工作
  | "recovery"; // 需要人工处理的恢复事项

/**
 * 展示状态。**后端原状态保留在 rawStatus**，本枚举只做诚实映射：
 * - 不认识的原状态 → unknown（「状态无法确认」），绝不默认成 running；
 * - 部分完成只能是 partial，不能折叠成 completed；
 * - budget_exhausted / cancelled 各自独立。
 */
export type TaskStatus =
  | "queued"
  | "running"
  | "waiting_approval"
  | "waiting_user"
  | "completed"
  | "partial"
  | "failed"
  | "cancelled"
  | "budget_exhausted"
  | "unknown";

export type TaskOrigin = "topic" | "global" | "unknown";

export type TaskActionKind =
  | "read_more" // 按需读取完整输出（不拿预览冒充全文）
  | "open_preview" // 在右侧预览任务成果文件/代码
  | "open_approval" // 打开统一审批弹窗
  | "continue_dev" // 继续开发
  | "revoke_auth" // 撤销执行授权
  | "abandon_dev" // 放弃开发
  | "cancel" // 申请取消（≠ 底层已停止）
  | "ack" // 知道了（失败/完成项进入历史）
  | "open_recovery"; // 打开恢复事项

export interface TaskAction {
  kind: TaskActionKind;
  label: string;
  /** 动作目标（审批 id / 预览身份 / 恢复记录 id 等），由后端给，不在前端猜。 */
  target?: string;
  /** 后端是否真实支持这个动作；false 不渲染按钮。 */
  supported: boolean;
}

export interface TaskOutput {
  /** 是否被截断（true 时界面必须说「仅显示部分」，不能当全文）。 */
  truncated: boolean;
  /** 已保留的简短预览（正文可能已被保留策略清理，此时 hint 说明原因）。 */
  preview: string;
  /** 「全文已清理」「仅显示部分」等真实说明。 */
  hint: string;
  readMore: boolean;
}

export interface TaskFeedItem {
  /** 稳定身份：`${source}:${sourceId}`；同一 id 在增量事件与快照里必须指同一份工作。 */
  id: string;
  source: TaskSource;
  sourceId: string;
  title: string;
  status: TaskStatus;
  /** 后端原始状态/终态，原样保留。 */
  rawStatus: string;
  origin: TaskOrigin;
  topicId: string | null;
  topicTitle: string | null;
  /** 执行者（子智能体 / 工具展示名 / 维护程序）；未知时为 null，不编造。 */
  executor: string | null;
  summary: string;
  /** 排队任务的提交/入队时间；没有就是 null。 */
  queuedAt: string | null;
  /** 实际开始时间；排队中不得伪造。 */
  startedAt: string | null;
  finishedAt: string | null;
  durationMs: number | null;
  /** 是否需要用户处理（置顶 + 保留）。 */
  requiresAttention: boolean;
  /** 用户已「知道了」的失败/完成项：留在历史里，不再置顶。 */
  acknowledged: boolean;
  output: TaskOutput;
  actions: TaskAction[];
}

export interface TaskCounts {
  queued: number;
  running: number;
  waitingApproval: number;
  waitingUser: number;
  completed: number;
  partial: number;
  failed: number;
  cancelled: number;
  budgetExhausted: number;
  unknown: number;
}

export type TaskView = "current" | "history";

export interface TaskFeedPage {
  items: TaskFeedItem[];
  nextCursor: string | null;
  counts: TaskCounts;
  /** 服务端生成快照时间；前端用于丢弃乱序的旧快照。 */
  serverTime: string;
  /** 当前实例仍存活的后端实例 id；历史行不因重启变成 running。 */
  instanceId: string | null;
}

export interface TaskDetailPart {
  kind: "text" | "output" | "error" | "note";
  label: string;
  text: string;
  truncated: boolean;
}

export interface TaskDetail {
  id: string;
  item: TaskFeedItem;
  parts: TaskDetailPart[];
  /** 输出是否已被保留策略清理；清理时说明原因，不伪造结果。 */
  outputMissing: boolean;
  missingReason: string;
}

export const TASK_STATUS_LABEL: Record<TaskStatus, string> = {
  queued: "排队中",
  running: "运行中",
  waiting_approval: "等待审批",
  waiting_user: "等待处理",
  completed: "已完成",
  partial: "部分完成",
  failed: "失败",
  cancelled: "已取消",
  budget_exhausted: "预算耗尽",
  unknown: "状态无法确认",
};

/** 需要用户处理、必须置顶并保留的状态。 */
export function isAttentionStatus(status: TaskStatus): boolean {
  return status === "waiting_approval" || status === "waiting_user" || status === "failed";
}

/** 任务来源的人话（不把内部枚举直接给用户看）。 */
export const TASK_SOURCE_LABEL: Record<TaskSource, string> = {
  subagent: "独立任务",
  derived: "后台整理",
  dev_task: "开发任务",
  maintenance: "记忆维护",
  tool: "工具执行",
  background: "后台工作",
  recovery: "待恢复",
};

export function taskIdentity(source: TaskSource, sourceId: string): string {
  return `${source}:${sourceId}`;
}

/** 后端状态 → 展示状态：只做已知映射，未知一律 unknown（不猜成 running）。 */
export function normalizeTaskStatus(raw: unknown): TaskStatus {
  const value = String(raw ?? "").trim().toLowerCase();
  const map: Record<string, TaskStatus> = {
    queued: "queued",
    pending: "queued",
    scheduled: "queued",
    waiting: "waiting_user",
    running: "running",
    in_progress: "running",
    waiting_approval: "waiting_approval",
    awaiting_approval: "waiting_approval",
    waiting_user: "waiting_user",
    needs_attention: "waiting_user",
    completed: "completed",
    complete: "completed",
    done: "completed",
    succeeded: "completed",
    success: "completed",
    partial: "partial",
    partially_completed: "partial",
    incomplete: "partial",
    failed: "failed",
    failure: "failed",
    error: "failed",
    cancelled: "cancelled",
    canceled: "cancelled",
    aborted: "cancelled",
    budget_exhausted: "budget_exhausted",
    out_of_budget: "budget_exhausted",
    unknown: "unknown",
  };
  return map[value] ?? "unknown";
}

// ── 布局偏好 ───────────────────────────────────────────────────────────────

export interface ConversationLayoutPrefs {
  version: 1;
  /** 左栏像素宽（用户手动保存值）。 */
  leftWidth: number;
  /** 用户手动收起左栏（与窗口变窄的临时收起是两件事）。 */
  leftCollapsed: boolean;
  /** 右栏像素宽（用户手动保存值）。 */
  rightWidth: number;
  /** 用户是否打开右侧预览栏。 */
  rightOpen: boolean;
  taskTab: TaskView;
  /** 用户主动要求的全窗口预览；临时全窗口由窄窗口规则推导，不写回这里。 */
  previewFull: boolean;
  /** 预览标签（只含身份与轻量阅读位置，不含正文）。 */
  previewTabs: PreviewTabState[];
  activePreviewTabId: string | null;
  /** 服务端写入时间（只读，用于跨端合并时判断新旧）。 */
  updatedAt?: string;
}

/** 通知/状态条的真实全局异常类型（放在话题栏下方，不做消息流内全局横幅）。 */
export type ConversationGlobalIssue =
  | "history_load_failed"
  | "no_credentials"
  | "connection_unavailable"
  | "storage_failure"
  | "migration_failure"
  | "capability_fallback";

export interface ConversationGlobalIssueState {
  kind: ConversationGlobalIssue;
  text: string;
  actionLabel: string;
  actionTo: string;
}

// ── 五入口 ─────────────────────────────────────────────────────────────────

export type ComposerEntryKind =
  | "send_recovery" // 发送异常与草稿恢复
  | "history_continue" // 从历史继续
  | "topic_switch" // 话题切换建议
  | "queue" // 已提交的排队消息
  | "approval"; // 待审批申请

export interface ComposerEntryBadge {
  kind: ComposerEntryKind;
  count: number;
  /** 状态点：有失败/需要注意时用 warn/error，不只靠颜色时同时有文字。 */
  tone: "normal" | "warn" | "error";
}
