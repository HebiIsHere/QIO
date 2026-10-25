/**
 * 输入区上方五个图标入口的**纯逻辑**（B 负责）。
 *
 * 组件只负责渲染；「哪些入口该出现、徽标数字是多少、排队消息怎么合并」都在这里，
 * 这样状态映射与去重可以单独测试，不依赖挂载与样式。
 *
 * 三条事实边界：
 * 1. **有事项才显示**：数量来自真实 store（failedSends / pendingSends / turnQueue /
 *    审批队列 / 草稿与恢复状态），没有就返回不可见 —— 不显示常驻空入口。
 * 2. **按身份去重**：同一条失败原文、同一条未执行消息、同一份被让位草稿只计一次；
 *    徽标数字不是「DOM 行数」。
 * 3. **已受理 ≠ 投递不明 ≠ 失败**：排队行要区分「服务端已接受」与「本机已提交但
 *    后端还没确认」；受理未确认的发送不混进排队数量（它属于入口①）。
 */
import type {
  DisplacedChatDraft,
  FailedSend,
  PendingSendRecord,
  TurnQueueState,
} from "../../stores/session";

/* ------------------------------------------------------------------ */
/* 入口模型                                                            */
/* ------------------------------------------------------------------ */

export type ComposerEntryId =
  | "sendIssues"
  | "continuation"
  | "topicSwitch"
  | "queue"
  | "approvals";

/** 入口的状态色：warn 需要处理 / info 有进行中的事项 / plain 只说明。 */
export type ComposerEntryTone = "warn" | "info" | "plain";

export interface ComposerEntryBadge {
  id: ComposerEntryId;
  visible: boolean;
  /** 需要用户处理的事项数量；0 表示「有入口但没有计数」（此时用状态点表达）。 */
  count: number;
  tone: ComposerEntryTone;
}

export const COMPOSER_ENTRIES: readonly ComposerEntryId[] = [
  "sendIssues",
  "continuation",
  "topicSwitch",
  "queue",
  "approvals",
];

/** 入口的悬停/读屏说明：图标本身不能自证用途，必须有文字。 */
export const COMPOSER_ENTRY_TEXT: Record<
  ComposerEntryId,
  { label: string; title: string; hint: string }
> = {
  sendIssues: {
    label: "发送异常与草稿",
    title: "发送异常与草稿恢复",
    hint: "查看发送失败、受理未确认、上次没有执行的消息与被让位的草稿",
  },
  continuation: {
    label: "从历史继续",
    title: "从历史继续",
    hint: "说明下一条消息从哪段历史继续；收起不会取消已选位置",
  },
  topicSwitch: {
    label: "话题切换建议",
    title: "话题切换建议",
    hint: "这段内容看起来属于另一个话题；是否切换由你决定",
  },
  queue: {
    label: "排队消息",
    title: "已提交的排队消息",
    hint: "查看已经交给 QIO、还在等待执行的消息，可逐条取消",
  },
  approvals: {
    label: "待审批",
    title: "待审批申请",
    hint: "打开统一审批弹窗处理等待你授权的申请",
  },
};

/* ------------------------------------------------------------------ */
/* 入口①：发送异常与草稿恢复                                            */
/* ------------------------------------------------------------------ */

export interface SendIssueFacts {
  /** 当前话题下可恢复的失败原文（store.failedSendsForTopic） */
  failedSends: ReadonlyArray<Pick<FailedSend, "id" | "draftId" | "topicId">>;
  /** 当前话题下受理情况未知的发送（store.unresolvedSendsForTopic） */
  pendingSends: ReadonlyArray<Pick<PendingSendRecord, "id" | "topicId" | "state">>;
  /** 当前话题下被让位的旧草稿（store.displacedDraftsForTopic） */
  displacedDrafts: ReadonlyArray<Pick<DisplacedChatDraft, "id">>;
  /** 上次退出时没有执行的消息 id（session.interruptedTurns） */
  interruptedTurnIds: readonly string[];
  /** 最近一次恢复动作的结果（session.interruptedNotice）：处理完列表清空后仍要说清楚 */
  interruptedNotice?: string;
  /** 聊天草稿保存失败（session.draftSaveStatus === "error"） */
  draftSaveFailed: boolean;
  /** 失败原文**写入**本机失败（session.failedSendPersistError） */
  storageWriteError?: string | null;
  /** 失败原文**读取**失败（session.failedSendReadError） */
  storageReadError?: string | null;
}

/** 一条失败原文的稳定身份：优先记录 id，缺省退回发送身份，再退回文本。 */
export function failedSendKey(record: Pick<FailedSend, "id" | "draftId">, index = 0): string {
  return record.id ? `id:${record.id}` : record.draftId ? `draft:${record.draftId}` : `row:${index}`;
}

/**
 * 入口①的计数（按身份去重后的「需要处理的事项」数量）。
 *
 * 存储读写失败不是「额外一条事项」，而是失败原文本身的状态 —— 只在计数为 0 时
 * 单独算一条（否则入口会因为同一批原文被重复计数）。
 */
export function sendIssueCount(facts: SendIssueFacts): number {
  const seen = new Set<string>();
  let count = 0;
  facts.pendingSends.forEach((record) => {
    const key = `pending:${record.id}`;
    if (seen.has(key)) return;
    seen.add(key);
    count += 1;
  });
  facts.failedSends.forEach((record, index) => {
    const key = `failed:${failedSendKey(record, index)}`;
    if (seen.has(key)) return;
    seen.add(key);
    count += 1;
  });
  facts.interruptedTurnIds.forEach((id) => {
    const key = `interrupted:${id}`;
    if (!id || seen.has(key)) return;
    seen.add(key);
    count += 1;
  });
  facts.displacedDrafts.forEach((record) => {
    const key = `draft:${record.id}`;
    if (seen.has(key)) return;
    seen.add(key);
    count += 1;
  });
  const storageIssue = Boolean(facts.storageWriteError || facts.storageReadError);
  if (facts.draftSaveFailed) count += 1;
  if (storageIssue && count === 0) count += 1;
  return count;
}

/** 入口①是否该出现：任何一类真实事项都算。 */
export function hasSendIssues(facts: SendIssueFacts): boolean {
  return sendIssueCount(facts) > 0 || Boolean(facts.interruptedNotice);
}

/* ------------------------------------------------------------------ */
/* 入口②③：从历史继续 / 话题切换建议                                    */
/* ------------------------------------------------------------------ */

export interface ContinuationFacts {
  /** 已登记、尚未落实的接续选择（session.pendingContinuation） */
  hasPendingIntent: boolean;
  /** 当前位置停在历史片段上（session.anchorHistoric） */
  anchorHistoric: boolean;
}

/** 入口②是否该出现：已登记接续，或当前位置本身就在历史上。 */
export function hasContinuation(facts: ContinuationFacts): boolean {
  return facts.hasPendingIntent || facts.anchorHistoric;
}

/** 接续说明：说清「哪一步生效」，不暴露 intent_id 这类内部术语。 */
export function continuationText(pending: { sourceTitle: string | null } | null): string {
  if (!pending) return "下一条消息将从所选历史继续";
  return pending.sourceTitle
    ? `下一条消息将从「${pending.sourceTitle}」继续`
    : "下一条消息将从所选历史继续";
}

/* ------------------------------------------------------------------ */
/* 入口④：已提交的排队消息                                              */
/* ------------------------------------------------------------------ */

export type QueueRowState = "running" | "queued" | "cancelled" | "local";

export interface QueueRow {
  /** 稳定渲染键：服务端 turn_id，或本地消息 id */
  key: string;
  turnId: string | null;
  message: string;
  state: QueueRowState;
  /** 排队位次（1 起）；运行中 / 已取消 / 仅本机为 null。 */
  position: number | null;
}

export interface LocalQueuedMessage {
  id: string;
  content: string;
}

export function normalizeQueuedText(text: string): string {
  return String(text ?? "").replace(/\s+/g, " ").trim();
}

/**
 * 排队行（服务端权威 + 本机已提交但后端尚未确认的乐观消息）。
 *
 * 合并规则：本机乐观消息与服务端队列里的某一条**文本相同**时只保留服务端那一条
 * （服务端有 turn_id，能精确取消）；没有服务端对应的本机消息单独成行，状态是
 * 「仅本机已提交」，界面必须说它还没被后端确认，而不是当成已经在排队。
 */
export function queueRows(
  queue: Pick<TurnQueueState, "running" | "queued" | "cancelled">,
  localQueued: readonly LocalQueuedMessage[] = [],
): QueueRow[] {
  const rows: QueueRow[] = [];
  if (queue.running) {
    rows.push({
      key: `run:${queue.running.turn_id}`,
      turnId: queue.running.turn_id,
      message: queue.running.message,
      state: "running",
      position: null,
    });
  }
  const queued = Array.isArray(queue.queued) ? queue.queued : [];
  queued.forEach((item, index) => {
    rows.push({
      key: `queued:${item.turn_id}`,
      turnId: item.turn_id,
      message: item.message,
      state: "queued",
      position: index + 1,
    });
  });
  const cancelled = Array.isArray(queue.cancelled) ? queue.cancelled : [];
  cancelled.forEach((item) => {
    rows.push({
      key: `cancelled:${item.turn_id}`,
      turnId: item.turn_id,
      message: item.message,
      state: "cancelled",
      position: null,
    });
  });
  // 本机乐观排队：与服务端已有的同文本行一一抵消（多重集匹配，避免同文两条被吞掉一条）
  const available = new Map<string, number>();
  for (const row of rows) {
    const key = normalizeQueuedText(row.message);
    available.set(key, (available.get(key) ?? 0) + 1);
  }
  for (const message of localQueued) {
    const text = normalizeQueuedText(message.content);
    const left = available.get(text) ?? 0;
    if (left > 0) {
      available.set(text, left - 1);
      continue;
    }
    rows.push({
      key: `local:${message.id}`,
      turnId: null,
      message: message.content,
      state: "local",
      position: null,
    });
  }
  return rows;
}

/** 入口④的计数：只数「还在排队」的（运行中不算排队；已取消不算待办）。 */
export function queueEntryCount(rows: readonly QueueRow[]): number {
  return rows.filter((row) => row.state === "queued" || row.state === "local").length;
}

/* ------------------------------------------------------------------ */
/* 汇总                                                                 */
/* ------------------------------------------------------------------ */

export interface ComposerEntryInput {
  sendIssues: SendIssueFacts;
  continuation: ContinuationFacts;
  /** 有待确认的话题切换建议（session.pendingSwitch） */
  topicSwitch: boolean;
  queue: Pick<TurnQueueState, "running" | "queued" | "cancelled">;
  localQueued?: readonly LocalQueuedMessage[];
  /** 去重后的待审批数量（approvals.pendingCount） */
  approvals: number;
}

/**
 * 五个入口的完整状态。
 *
 * 顺序固定（发送异常 → 历史继续 → 话题建议 → 排队 → 审批），组件按顺序渲染；
 * 不可见的入口不渲染 —— 有事项才显示，没有就不占位置。
 */
export function deriveComposerEntries(input: ComposerEntryInput): ComposerEntryBadge[] {
  const rows = queueRows(input.queue, input.localQueued ?? []);
  const queueCount = queueEntryCount(rows);
  const sendCount = sendIssueCount(input.sendIssues);
  const queueRunning = rows.some((row) => row.state === "running");
  const queueCancelled = rows.some((row) => row.state === "cancelled");
  const badges: Record<ComposerEntryId, ComposerEntryBadge> = {
    sendIssues: {
      id: "sendIssues",
      visible: hasSendIssues(input.sendIssues),
      count: sendCount,
      tone: "warn",
    },
    continuation: {
      id: "continuation",
      visible: hasContinuation(input.continuation),
      count: 0,
      tone: input.continuation.hasPendingIntent ? "info" : "plain",
    },
    topicSwitch: {
      id: "topicSwitch",
      visible: input.topicSwitch,
      count: 0,
      tone: "info",
    },
    queue: {
      id: "queue",
      visible: queueCount > 0 || queueRunning || queueCancelled,
      count: queueCount,
      tone: queueCount > 0 ? "info" : "plain",
    },
    approvals: {
      id: "approvals",
      visible: input.approvals > 0,
      count: Math.max(0, input.approvals),
      tone: "warn",
    },
  };
  return COMPOSER_ENTRIES.map((id) => badges[id]);
}

/* ------------------------------------------------------------------ */
/* 文案小工具                                                          */
/* ------------------------------------------------------------------ */

/** 单行摘要：换行压成空格，长文本截断并保留省略号（只用于列表预览）。 */
export function snippetOf(text: string, limit = 42): string {
  const oneLine = String(text ?? "").replace(/\s+/g, " ").trim();
  if (!oneLine) return "（空内容）";
  return oneLine.length > limit ? `${oneLine.slice(0, limit)}…` : oneLine;
}

/**
 * 「这件事是什么时候的」：台账时间都是 UTC ISO，用户的判断依据是本地时间。
 * 同一天只说时分；跨天补日期；解析不了就说「时间未知」，不猜。
 */
export function whenText(iso: string | null | undefined, now: Date = new Date()): string {
  if (!iso) return "时间未知";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "时间未知";
  const p = (n: number) => String(n).padStart(2, "0");
  const clock = `${p(d.getHours())}:${p(d.getMinutes())}`;
  const sameDay =
    d.getFullYear() === now.getFullYear() &&
    d.getMonth() === now.getMonth() &&
    d.getDate() === now.getDate();
  return sameDay ? `今天 ${clock}` : `${d.getMonth() + 1} 月 ${d.getDate()} 日 ${clock}`;
}

/** 受理未确认条目的状态说明（入口①）：不把「未知」说成「失败/未发送」。 */
export function pendingSendStateText(state: PendingSendRecord["state"]): string {
  return state === "needs-confirm"
    ? "发送未确认：后端还没有该请求的记录"
    : "已发出，正在等后端回执";
}
