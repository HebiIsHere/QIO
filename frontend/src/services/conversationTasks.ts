/**
 * 左侧任务栏的展示纯逻辑与接线（A 维护；取数仍走 services/taskFeedApi.ts）。
 *
 * 为什么单独一份：状态映射、置顶排序、去重、时间/耗时、动作可用性这些规则
 * 会被列表、详情、收起入口三处共用。放进组件里就会出现「列表说运行中、详情说
 * 状态无法确认」这种自相矛盾。这里全部不读 DOM（预览/聚焦的事件桥接除外），可直接单测。
 *
 * 两条诚实底线（契约 §4.1）：
 * - 后端状态不认识就显示「状态无法确认」，绝不默认成运行中；
 * - partial 就是「部分完成」，不得折叠成已完成。
 */
import type {
  TaskAction,
  TaskActionKind,
  TaskCounts,
  TaskFeedItem,
  TaskStatus,
} from "../conversation/contracts";
import {
  TASK_SOURCE_LABEL,
  TASK_STATUS_LABEL,
  isAttentionStatus,
  normalizeTaskStatus,
} from "../conversation/contracts";
import type { RecoveryRecordView } from "./recoveryApi";

/** 每次向权威快照要多少条（分页仍由后端游标决定）。 */
export const TASK_FEED_PAGE_SIZE = 40;

/** 完成项在当前视图短暂停留的时间；用户展开阅读时不受此时限影响。 */
export const COMPLETION_GRACE_MS = 5000;

/** 增量事件触发刷新的防抖。 */
export const REFRESH_DEBOUNCE_MS = 450;

/** 有活动/需要处理的任务时的兜底轮询（事件丢失时仍能看到真实推进）。 */
export const ACTIVE_POLL_MS = 12000;

/**
 * 任务成果打开右侧预览的接线事件。
 *
 * C 的 stores/conversationPreview.ts 合并后由 Lead 统一改成直接调用 open(target)；
 * 在 C 尚未合并的基线上，本事件是两边约定的临时通道（契约 §4.3 的身份规则不变）。
 */
export const OPEN_PREVIEW_EVENT = "qio:open-preview";

/** 中央执行记录点击定位左侧任务的事件入口（B 也可以直接调用 store 的 focusTask）。 */
export const FOCUS_TASK_EVENT = "qio:focus-task";

const TASK_STATUSES: TaskStatus[] = [
  "queued",
  "running",
  "waiting_approval",
  "waiting_user",
  "completed",
  "partial",
  "failed",
  "cancelled",
  "budget_exhausted",
  "unknown",
];
const KNOWN_STATUS = new Set<string>(TASK_STATUSES);

/** 后端 status 优先；缺失/不认识时回落到 rawStatus 的已知映射（未知仍是 unknown）。 */
export function taskDisplayStatus(
  item: Pick<TaskFeedItem, "status" | "rawStatus"> | null | undefined,
): TaskStatus {
  const status = String(item?.status ?? "").trim();
  if (KNOWN_STATUS.has(status)) return status as TaskStatus;
  return normalizeTaskStatus(item?.rawStatus);
}

export function taskStatusLabel(
  item: Pick<TaskFeedItem, "status" | "rawStatus"> | null | undefined,
): string {
  return TASK_STATUS_LABEL[taskDisplayStatus(item)];
}

export function isActiveStatus(status: TaskStatus): boolean {
  return (
    status === "queued" ||
    status === "running" ||
    status === "waiting_approval" ||
    status === "waiting_user"
  );
}

/** 需要用户处理：后端标注 requiresAttention 且未「知道了」，并且状态确实是待处理类。 */
export function isAttentionItem(item: TaskFeedItem): boolean {
  if (!item || item.acknowledged) return false;
  if (!item.requiresAttention) return false;
  return isAttentionStatus(taskDisplayStatus(item));
}

/**
 * 「需要处理」数量：优先用服务端计数（当前视图是分页的，本地条数不是总数）；
 * 服务端没给计数时才按已加载条数兜底 —— 不把分页后的数量冒充总数。
 */
export function attentionCountOf(
  counts: TaskCounts | null | undefined,
  items: readonly TaskFeedItem[],
): number {
  if (counts && typeof counts === "object") {
    const waiting = numberOrZero(counts.waitingApproval) + numberOrZero(counts.waitingUser);
    const failed = numberOrZero(counts.failed);
    if (waiting + failed > 0) return waiting + failed;
    if (hasAnyCount(counts)) return 0;
  }
  return items.filter(isAttentionItem).length;
}

/** 运行/排队数量（同上：优先服务端计数）。 */
export function activeCountOf(
  counts: TaskCounts | null | undefined,
  items: readonly TaskFeedItem[],
): number {
  if (counts && typeof counts === "object" && hasAnyCount(counts)) {
    return numberOrZero(counts.queued) + numberOrZero(counts.running);
  }
  return items.filter((item) => {
    const status = taskDisplayStatus(item);
    return status === "queued" || status === "running";
  }).length;
}

function numberOrZero(value: unknown): number {
  const n = Number(value);
  return Number.isFinite(n) && n > 0 ? Math.floor(n) : 0;
}

function hasAnyCount(counts: TaskCounts): boolean {
  for (const value of Object.values(counts)) {
    if (Number(value) > 0) return true;
  }
  return false;
}

export function parseTaskTime(value: string | null | undefined): number | null {
  if (!value) return null;
  const parsed = Date.parse(value);
  return Number.isFinite(parsed) ? parsed : null;
}

/** 排序用的真实时间：实际开始时间 → 排队时间 → 结束时间（都不伪造）。 */
export function taskTimeValue(item: TaskFeedItem): number | null {
  for (const iso of [item.startedAt, item.queuedAt, item.finishedAt]) {
    const parsed = parseTaskTime(iso);
    if (parsed !== null) return parsed;
  }
  return null;
}

/**
 * 统一平铺排序：需要处理的任务置顶；其余按实际开始时间（排队项用入队时间）从新到旧。
 * 逐项都缺失时间时保持后端给的原始顺序（稳定排序）。
 */
export function sortTasks(
  items: readonly TaskFeedItem[],
  opts: { pinAttention: boolean },
): TaskFeedItem[] {
  const indexed = items.map((item, index) => ({
    item,
    index,
    time: taskTimeValue(item),
    attention: opts.pinAttention && isAttentionItem(item),
  }));
  indexed.sort((a, b) => {
    if (a.attention !== b.attention) return a.attention ? -1 : 1;
    const at = a.time === null ? Number.NEGATIVE_INFINITY : a.time;
    const bt = b.time === null ? Number.NEGATIVE_INFINITY : b.time;
    if (at !== bt) return bt - at;
    return a.index - b.index;
  });
  return indexed.map((entry) => entry.item);
}

/**
 * 去重：以后端 id 为唯一键，并用 source+sourceId 兜底。
 * 快照里若同一工作出现两行（后端 bug 或增量拼接），只保留第一行。
 */
export function dedupeTasks(items: readonly TaskFeedItem[]): TaskFeedItem[] {
  const seen = new Set<string>();
  const out: TaskFeedItem[] = [];
  for (const item of items) {
    if (!item) continue;
    const keys: string[] = [];
    const id = typeof item.id === "string" ? item.id.trim() : "";
    if (id) keys.push(id);
    if (item.source && item.sourceId) keys.push(item.source + ":" + item.sourceId);
    if (!keys.length) continue;
    if (keys.some((key) => seen.has(key))) continue;
    for (const key of keys) seen.add(key);
    out.push(item);
  }
  return out;
}

/** 展开集合的成员判断（集合以数组存内存，顺序无所谓）。 */
export function isExpandedFlag(list: readonly string[] | null | undefined, id: string): boolean {
  return Boolean(id) && Array.isArray(list) && list.indexOf(id) >= 0;
}

/** 权威快照与「短暂停留的完成项」合并（同一 id 以快照为准，不出现两行）。 */
export function mergeRetainedTasks(
  snapshot: readonly TaskFeedItem[],
  retained: readonly TaskFeedItem[],
): TaskFeedItem[] {
  const known = new Set(snapshot.map((item) => item.id));
  const extras = retained.filter((item) => !known.has(item.id));
  return dedupeTasks([...snapshot, ...extras]);
}

export interface RemovalResolution {
  /** retain = 留在当前视图（完成短暂停留或用户正在展开阅读）。 */
  action: "retain" | "drop";
  /** retain 时的展示数据（历史里的最终事实优先）。 */
  item: TaskFeedItem | null;
  /** true = 状态来自历史权威记录；false = 最终结果没拿到（界面需说明）。 */
  verified: boolean;
}

/**
 * 当前快照里消失的任务怎么处理。
 *
 * - 历史快照里找得到 → 用最终事实短暂停留（「完成」是真的完成）；
 * - 找不到但用户正展开阅读 → 保留最后一次后端状态并标注「最终结果未确认」，
 *   收起时移除（不突然消失，也不伪造结局）；
 * - 其余直接移除（后端权威快照说它已经不在当前任务里）。
 */
export function resolveRemovedTask(
  removed: TaskFeedItem,
  historyItems: readonly TaskFeedItem[] | null,
  expanded: boolean,
): RemovalResolution {
  const found = historyItems ? historyItems.find((item) => item.id === removed.id) : undefined;
  if (found) return { action: "retain", item: found, verified: true };
  if (expanded) return { action: "retain", item: removed, verified: false };
  return { action: "drop", item: null, verified: false };
}

function pad2(value: number): string {
  return value < 10 ? "0" + value : String(value);
}

/** 绝对时间：今天只显示时分；今年显示月-日；跨年显示完整日期。 */
export function formatTaskClock(value: string | null | undefined, now: Date = new Date()): string {
  const parsed = parseTaskTime(value);
  if (parsed === null) return "";
  const date = new Date(parsed);
  const clock = pad2(date.getHours()) + ":" + pad2(date.getMinutes());
  if (
    date.getFullYear() === now.getFullYear() &&
    date.getMonth() === now.getMonth() &&
    date.getDate() === now.getDate()
  ) {
    return clock;
  }
  const day = pad2(date.getMonth() + 1) + "-" + pad2(date.getDate()) + " " + clock;
  return date.getFullYear() === now.getFullYear()
    ? day
    : date.getFullYear() + "-" + pad2(date.getMonth() + 1) + "-" + pad2(date.getDate()) + " " + clock;
}

/** 耗时的人话；没有真实耗时就返回空串（不编造）。 */
export function formatTaskDuration(ms: number | null | undefined): string {
  if (ms === null || ms === undefined) return "";
  const value = Number(ms);
  if (!Number.isFinite(value) || value < 0) return "";
  if (value < 1000) return "不到 1 秒";
  const totalSeconds = Math.round(value / 1000);
  if (totalSeconds < 60) return totalSeconds + " 秒";
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  if (minutes < 60) return seconds ? minutes + " 分 " + seconds + " 秒" : minutes + " 分";
  const hours = Math.floor(minutes / 60);
  const restMinutes = minutes % 60;
  return restMinutes ? hours + " 小时 " + restMinutes + " 分" : hours + " 小时";
}

/**
 * 列表行的时间/耗时说明。排队项只说排队时间；排队中绝不伪造开始时间。
 */
export function taskTimingText(item: TaskFeedItem, now: Date = new Date()): string {
  const status = taskDisplayStatus(item);
  if (status === "queued") {
    const queued = formatTaskClock(item.queuedAt, now);
    return queued ? "排队于 " + queued : "";
  }
  const finishedAt = formatTaskClock(item.finishedAt, now);
  if (finishedAt) {
    const verb = status === "completed" || status === "partial" ? "完成于 " : "结束于 ";
    const duration = formatTaskDuration(item.durationMs);
    return verb + finishedAt + (duration ? " · 耗时 " + duration : "");
  }
  const startedAt = formatTaskClock(item.startedAt, now);
  if (startedAt) return "开始于 " + startedAt;
  // 恢复事项等只有登记时间、没有开始时间：如实说「记录于」，不冒充开始。
  const recordedAt = formatTaskClock(item.queuedAt, now);
  return recordedAt ? "记录于 " + recordedAt : "";
}

/** 来源：话题名 / 公共维护 / 来源未知（三类都不能互相冒充）。 */
export function taskOriginText(item: TaskFeedItem): string {
  if (item.origin === "global") return "公共维护";
  if (item.origin === "topic" && item.topicTitle) return item.topicTitle;
  return "来源未知";
}

export function taskSourceText(item: TaskFeedItem): string {
  return TASK_SOURCE_LABEL[item.source] ?? "来源未知";
}

/** 动作的展示文案（后端 label 优先；空 label 用契约里的中文说法）。 */
export const TASK_ACTION_LABELS: Record<TaskActionKind, string> = {
  read_more: "查看更多",
  open_preview: "在右侧查看",
  open_approval: "查看审批",
  continue_dev: "继续开发",
  revoke_auth: "撤销执行授权",
  abandon_dev: "放弃开发",
  cancel: "申请取消",
  ack: "知道了",
  open_recovery: "查看恢复事项",
};

/** 当前实现真实支持的动作（未知/未接线的一律不渲染，避免假按钮）。 */
const HANDLED_ACTIONS = new Set<TaskActionKind>([
  "read_more",
  "open_preview",
  "open_approval",
  "continue_dev",
  "revoke_auth",
  "abandon_dev",
  "cancel",
  "ack",
  "open_recovery",
]);

/** 只有 supported === true 且本端已接线的动作才进入界面；同 kind 只留一条。 */
export function visibleTaskActions(
  actions: readonly TaskAction[] | null | undefined,
): TaskAction[] {
  const out: TaskAction[] = [];
  const seen = new Set<string>();
  for (const action of actions ?? []) {
    if (!action || action.supported !== true) continue;
    if (!HANDLED_ACTIONS.has(action.kind)) continue;
    if (seen.has(action.kind)) continue;
    seen.add(action.kind);
    out.push(action);
  }
  return out;
}

export function taskActionLabel(action: TaskAction): string {
  const label = typeof action.label === "string" ? action.label.trim() : "";
  if (label) return label;
  return TASK_ACTION_LABELS[action.kind] ?? "操作";
}

/** 破坏性动作（需要就地确认一次） */
export function isDestructiveTaskAction(kind: TaskActionKind): boolean {
  return kind === "abandon_dev";
}

export interface TaskPreviewIntent {
  taskId: string;
  taskSourceId: string;
  title: string;
  /** 后端动作给的目标（附件/文件标识或路径）；不猜类型，交给预览侧按身份解析。 */
  target: string;
}

/** 请求在右侧预览任务成果（C 未合并前的约定通道）。 */
export function requestTaskPreview(item: TaskFeedItem, target?: string): TaskPreviewIntent {
  const intent: TaskPreviewIntent = {
    taskId: item.id,
    taskSourceId: item.sourceId,
    title: item.title,
    target: target ?? "",
  };
  if (typeof window !== "undefined") {
    window.dispatchEvent(new CustomEvent(OPEN_PREVIEW_EVENT, { detail: intent }));
  }
  return intent;
}

/** 监听「定位左侧任务」（B 的事件通道；也接受字符串或 { taskId } 两种 detail）。 */
export function listenFocusTask(handler: (taskId: string) => void): () => void {
  if (typeof window === "undefined") return () => undefined;
  const listener = (event: Event): void => {
    const detail = (event as CustomEvent).detail as unknown;
    let taskId = "";
    if (typeof detail === "string") taskId = detail;
    else if (detail && typeof detail === "object") {
      taskId = String((detail as Record<string, unknown>).taskId ?? "");
    }
    if (taskId) handler(taskId);
  };
  window.addEventListener(FOCUS_TASK_EVENT, listener);
  return () => window.removeEventListener(FOCUS_TASK_EVENT, listener);
}

/** 交给模型「继续开发」的指令（带任务身份与真实状态，模型才能接着做而不是从头来）。 */
export function buildContinueDevInstruction(item: TaskFeedItem): string {
  const requirement = (item.title || item.summary || "").trim() || "（需求原文没有随任务记录提供）";
  const raw = (item.rawStatus || "").trim() || "未知";
  return (
    "继续开发工具任务 " +
    item.sourceId +
    "（展示状态：" +
    taskStatusLabel(item) +
    "；后端状态：" +
    raw +
    "）。需求是：" +
    requirement +
    "\n先看看这个工作区里已经写了什么、上次卡在哪一步，接着把没做完的部分做完，不要从头重写。"
  );
}

// ── 恢复收件箱 → 任务行 ──────────────────────────────────────────────────

/**
 * 服务端恢复记录的状态映射。
 *
 * 这些记录是「等待用户处理」的收件箱条目：原状态原文保留在 rawStatus，
 * 展示状态只做最小、保守的映射 —— 明确在跑的说运行中，明确在排队的说排队，
 * 其余都说「等待处理」（它们确实在等用户决定）；绝不说成运行中。
 */
export function recoveryDisplayStatus(record: RecoveryRecordView): TaskStatus {
  const raw = String(record?.status ?? "").trim().toLowerCase();
  if (raw === "running" || raw === "in_progress") return "running";
  if (raw === "queued" || raw === "pending" || raw === "scheduled") return "queued";
  return "waiting_user";
}

/** 一条恢复记录 → TaskFeedItem 形状（只用于左侧呈现，不改现有恢复入口的语义）。 */
export function recoveryRecordToTaskItem(record: RecoveryRecordView): TaskFeedItem {
  const recordId = String(record?.record_id ?? "").trim();
  const message = String(record?.message ?? "").trim();
  const status = recoveryDisplayStatus(record);
  const title =
    message ||
    (record?.kind === "derived_task" ? "一个没有跑完的后台任务" : "一条需要处理的消息");
  const summary = String(record?.reason ?? "").trim() || String(record?.owner_note ?? "").trim();
  const updated = record?.updated_at || record?.created_at || null;
  return {
    id: "recovery:" + recordId,
    source: "recovery",
    sourceId: recordId,
    title,
    status,
    rawStatus: String(record?.status ?? "").trim() || "状态未提供",
    origin: record?.topic_id ? "topic" : "unknown",
    topicId: record?.topic_id ?? null,
    topicTitle: null,
    executor: null,
    summary,
    // 只有「登记时间」这一条真实时间：不伪造开始时间。
    queuedAt: record?.created_at ?? null,
    startedAt: null,
    finishedAt: null,
    durationMs: null,
    requiresAttention: true,
    acknowledged: false,
    output: {
      truncated: false,
      preview: "",
      hint: "",
      readMore: false,
    },
    // 动作沿用现有恢复入口：左栏把这条定位到中央恢复区去处理，不复制一套批准/继续逻辑。
    actions: Array.isArray(record?.actions) && record.actions.length
      ? [{ kind: "open_recovery", label: "去处理", target: recordId, supported: true }]
      : [],
  };
}

/** 恢复记录数量（真实待处理数量，没有就 0）。 */
export function recoveryCountOf(records: readonly RecoveryRecordView[] | null | undefined): number {
  if (!Array.isArray(records)) return 0;
  return records.filter((record) => String(record?.record_id ?? "").trim()).length;
}
