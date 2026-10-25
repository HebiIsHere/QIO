/**
 * 审批结果与历史（工作流 A / F02、F03）。
 *
 * 职责：把「一条审批最后发生了什么」做成**按 approval_id 建键**的只读模型，
 * 数据只有两个真实来源，绝不互相冒充：
 *
 * * SSE `APPROVAL_RESULT`（实时结局：approved / executing / executed / failed /
 *   invalidated / interrupted_unverified / rejected / cancelled / expired）；
 * * `GET /api/approvals/history`（后端持久化的终态；刷新 / 换页 / 重启后从这里重建）。
 *
 * 三条硬约束（COMMON §4.1）：
 * 1. 并发两条审批的结果分别按 id 存放，不互相覆盖，也不退化成「最近一次成功」；
 * 2. 已批准 / 执行中 ≠ 执行成功 —— 非终态只如实显示状态，不写成完成；
 * 3. 逐项结果只用后端给的 item_id / 对象字段（detail.results / detail.items），
 *    不按文件名或自然语言摘要猜；历史读取失败保留已有内容并给出可重试错误，
 *    绝不当作「没有历史」。
 */
import { defineStore } from "pinia";
import {
  approvalStateFromPhase,
  approvalStateLabel,
  getApprovalHistory,
  NON_TERMINAL_APPROVAL_STATES,
  type ApprovalHistoryEntry,
} from "../services/approvalLifecycle";

/** 结果来源：实时事件 / 后端持久化历史（界面据此区分「刚发生」与「刷新后重建」）。 */
export type ApprovalResultSource = "event" | "history";

/** 结果里的一个批量对象（真实 item_id + 后端状态；缺失就留空，不猜）。 */
export interface BatchItemResult {
  /** 后端执行结果里的 item_id（没有就空串 —— 不拿文件名/序号冒充） */
  itemId: string;
  /** 可以给用户看的对象信息：后端给的 path / attachment_id / name 原样 */
  objectText: string;
  /** 后端 status 原样（completed / failed / invalidated / skipped / …） */
  status: string;
  /** 中文状态（未知状态如实显示原值） */
  statusText: string;
  /** 该对象是否真的成功：只有 ok===true 或 status=completed/success 才算 */
  ok: boolean;
  /** 失败 / 未执行原因（后端原因原文） */
  reason: string;
  raw: Record<string, unknown>;
}

export interface ApprovalResultEntry {
  approvalId: string;
  kind: string;
  /** 人类可读的操作名（后端 label / operation.summary / history detail.label） */
  label: string;
  /** 原始事件 phase 或历史 outcome（保留原值，便于核对） */
  phase: string;
  /** 生命周期状态（completed / failed / rejected / …） */
  state: string;
  stateLabel: string;
  summary: string;
  /** 执行是否成功；非终态（已批准 / 执行中）为 null —— 不能把「没失败」当成功 */
  ok: boolean | null;
  detail: Record<string, unknown>;
  items: BatchItemResult[];
  /** 整批汇总（没有逐项结果时为空） */
  batchSummary: string;
  source: ApprovalResultSource;
  /** 这条结果的时间（事件取本机时间，历史取后端 at） */
  at: string;
  /** 排序用的毫秒时间戳（越大越新） */
  updatedAt: number;
}

const TERMINAL_STATES = new Set([
  "completed",
  "failed",
  "rejected",
  "cancelled",
  "expired",
  "invalidated",
  "interrupted_unverified",
]);

/** 状态推进等级：用来防止乱序事件把终态「降级」回中间态。 */
const STATE_RANK: Record<string, number> = {
  waiting: 0,
  approved: 1,
  executing: 2,
};

export function isTerminalApprovalState(state: string): boolean {
  return TERMINAL_STATES.has(String(state ?? "").trim());
}

export function isNonTerminalApprovalState(state: string): boolean {
  return NON_TERMINAL_APPROVAL_STATES.has(String(state ?? "").trim());
}

function stateRank(state: string): number {
  if (isTerminalApprovalState(state)) return 3;
  return STATE_RANK[state] ?? 2;
}

/** 历史 outcome → 生命周期状态（未知 outcome 原样保留，不假装认识）。 */
export function resultStateFromOutcome(outcome: string): string {
  switch (String(outcome ?? "").trim()) {
    case "executed":
    case "completed":
      return "completed";
    case "failed":
      return "failed";
    case "invalidated":
      return "invalidated";
    case "interrupted_unverified":
      return "interrupted_unverified";
    case "rejected":
      return "rejected";
    case "cancelled":
    case "canceled":
      return "cancelled";
    case "expired":
    case "timeout":
      return "expired";
    case "approved":
      return "approved";
    case "executing":
      return "executing";
    default:
      return String(outcome ?? "").trim();
  }
}

function text(value: unknown): string {
  return typeof value === "string" ? value.trim() : "";
}

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

/** 终态/中间态的默认说明（后端没给 summary 时也不留空；不把中间态说成完成）。 */
const PHASE_SUMMARY: Record<string, string> = {
  approved: "已批准，等待后端执行",
  executing: "正在执行，结果还没回来",
  executed: "执行完成",
  completed: "执行完成",
  failed: "执行失败",
  invalidated: "执行前核对未通过，本次没有执行",
  interrupted_unverified: "上次进程在执行中退出，结果待核对；没有自动重放",
  rejected: "已拒绝，不会执行",
  cancelled: "已取消，不会执行",
  expired: "已过期，本次没有执行",
};

function summaryFor(state: string, phase: string, raw: unknown): string {
  const given = text(raw);
  if (given) return given;
  return PHASE_SUMMARY[state] || PHASE_SUMMARY[phase] || "";
}

/** 逐项状态的展示文案：未知状态如实显示原值，未执行不写成成功。 */
export function batchStatusText(status: string): string {
  switch (String(status ?? "").trim().toLowerCase()) {
    case "completed":
    case "success":
    case "succeeded":
    case "done":
    case "executed":
    case "ok":
      return "成功";
    case "failed":
    case "error":
      return "失败";
    case "invalidated":
      return "失效（未执行）";
    case "skipped":
      return "跳过（未执行）";
    case "blocked":
    case "not_executed":
    case "pending":
    case "queued":
      return "未执行";
    case "cancelled":
    case "canceled":
      return "已取消";
    default:
      return status ? "状态：" + status : "状态未知";
  }
}

function itemOk(status: string, okRaw: unknown): boolean {
  if (typeof okRaw === "boolean") return okRaw;
  const normalized = String(status ?? "").trim().toLowerCase();
  return normalized === "completed" || normalized === "success" || normalized === "succeeded" || normalized === "done";
}

function objectTextOf(raw: Record<string, unknown>): string {
  return (
    text(raw.path) ||
    text(raw.attachment_id) ||
    text(raw.name) ||
    text(raw.title) ||
    text(raw.object) ||
    ""
  );
}

/** 从后端 detail 里取逐项结果：detail.results 优先，其次 detail.items。 */
export function batchItemsFromDetail(detail: unknown): BatchItemResult[] {
  const data = asRecord(detail);
  const candidates: unknown[] = Array.isArray(data.results)
    ? data.results
    : Array.isArray(data.items)
      ? data.items
      : [];
  const out: BatchItemResult[] = [];
  for (const candidate of candidates) {
    const raw = asRecord(candidate);
    if (!Object.keys(raw).length) continue;
    const status = text(raw.status) || (raw.ok === true ? "completed" : raw.ok === false ? "failed" : "");
    out.push({
      // 只认后端身份字段；缺失就空着（界面显示「未提供对象身份」而不是猜文件名）
      itemId: text(raw.item_id) || text(raw.id),
      objectText: objectTextOf(raw),
      status,
      statusText: batchStatusText(status),
      ok: itemOk(status, raw.ok),
      reason: text(raw.reason) || text(raw.error) || text(raw.message),
      raw,
    });
  }
  return out;
}

/** 整批汇总：真实计数来自逐项结果（没有逐项结果就返回空串，不编造）。 */
export function batchSummaryText(items: readonly BatchItemResult[]): string {
  if (!items.length) return "";
  const total = items.length;
  const success = items.filter((item) => item.ok).length;
  const failed = items.filter((item) => !item.ok && /失败|错误/.test(item.statusText)).length;
  const skipped = total - success - failed;
  if (success === total) return `整批成功：${total} 项都已完成`;
  if (success === 0) return `整批未完成：${total} 项里没有一项成功（失败 ${failed}，未执行/其他 ${skipped}）`;
  return `部分成功：${total} 项中成功 ${success}，失败 ${failed}，未执行/其他 ${skipped}`;
}

function uniqueItems(items: BatchItemResult[]): BatchItemResult[] {
  const seen = new Set<string>();
  const out: BatchItemResult[] = [];
  for (const item of items) {
    const key = item.itemId || item.objectText || JSON.stringify(item.raw);
    if (seen.has(key)) continue;
    seen.add(key);
    out.push(item);
  }
  return out;
}

/** SSE APPROVAL_RESULT → 结果条目（没有 phase 的旧事件不伪造结果）。 */
export function resultFromEvent(data: Record<string, unknown>): ApprovalResultEntry | null {
  const approvalId = text(data.approval_id);
  const phase = text(data.phase);
  if (!approvalId || !phase) return null;
  if (phase === "confirm_stage") return null;
  const approval = asRecord(data.approval);
  const state = approvalStateFromPhase(phase) ?? resultStateFromOutcome(phase);
  const detail = asRecord(data.detail ?? approval.result);
  const items = uniqueItems(batchItemsFromDetail(detail));
  const okRaw = typeof data.ok === "boolean" ? data.ok : null;
  const now = nextStamp();
  return {
    approvalId,
    kind: text(approval.kind) || text(data.kind),
    label: text(approval.label) || text(data.label) || text(approval.purpose),
    phase,
    state,
    stateLabel: approvalStateLabel(state),
    summary: summaryFor(state, phase, data.summary),
    ok: okRaw ?? (state === "completed" ? true : isTerminalApprovalState(state) ? false : null),
    detail,
    items,
    batchSummary: batchSummaryText(items),
    source: "event",
    at: new Date(now).toISOString(),
    updatedAt: now,
  };
}

/** 后端 history 行 → 结果条目（刷新 / 换页后的持久化终态）。 */
export function resultFromHistory(row: ApprovalHistoryEntry, index = 0): ApprovalResultEntry | null {
  if (!row || !row.approval_id) return null;
  const outcome = text(row.outcome);
  const state = resultStateFromOutcome(outcome);
  const detail = asRecord(row.detail);
  const items = uniqueItems(batchItemsFromDetail(detail));
  const parsed = Date.parse(text(row.at));
  const updatedAt = Number.isFinite(parsed) ? parsed : Date.now() - index;
  return {
    approvalId: row.approval_id,
    kind: text(row.kind) || text(detail.kind),
    label: text(detail.label) || text(detail.operation) || "",
    phase: outcome,
    state,
    stateLabel: approvalStateLabel(state),
    summary: text(row.summary) || PHASE_SUMMARY[state] || "",
    ok: state === "completed" ? true : isTerminalApprovalState(state) ? false : null,
    detail,
    items,
    batchSummary: batchSummaryText(items),
    source: "history",
    at: text(row.at) || new Date(updatedAt).toISOString(),
    updatedAt,
  };
}

/**
 * 同一条审批的两次结果合并（按 approval_id 是同一键）。
 *
 * 规则：
 * * 历史只补缺口 / 补终态：实时事件已经给出的终态不被历史覆盖（历史没有更细的事实）；
 * * 状态只能前进不能后退：迟到的 approved 不能把 executed 覆盖回「等待执行」；
 * * 逐项结果优先保留非空的那一份。
 */
export function mergeResultEntry(
  existing: ApprovalResultEntry | null | undefined,
  incoming: ApprovalResultEntry,
): ApprovalResultEntry {
  if (!existing) return incoming;
  if (existing.approvalId !== incoming.approvalId) return incoming;
  const existingRank = stateRank(existing.state);
  const incomingRank = stateRank(incoming.state);
  if (incomingRank < existingRank) {
    return existing;
  }
  if (incoming.source === "history" && existing.source === "event") {
    // 实时事件已经是终态：历史只补缺失的逐项结果 / 标签，不改写事实
    if (existingRank >= 3) {
      return {
        ...existing,
        items: existing.items.length ? existing.items : incoming.items,
        batchSummary: existing.batchSummary || incoming.batchSummary,
        kind: existing.kind || incoming.kind,
        label: existing.label || incoming.label,
        detail: Object.keys(existing.detail).length ? existing.detail : incoming.detail,
      };
    }
  }
  const merged: ApprovalResultEntry = { ...incoming };
  if (!merged.items.length && existing.items.length) {
    merged.items = existing.items;
    merged.batchSummary = existing.batchSummary;
  }
  if (!merged.label) merged.label = existing.label;
  if (!merged.kind) merged.kind = existing.kind;
  if (!merged.summary) merged.summary = existing.summary;
  return merged;
}

/** 历史读取的并发闸门（与 unifiedApprovals 的 knowledgeInflight 同一模式）。 */
let historyInflight: Promise<boolean> | null = null;

/**
 * 单调递增的时间戳：同一毫秒内到达的多条事件也要有稳定顺序
 * （否则「最近一条在前」会退化成按 id 字母序，看起来像随机排序）。
 */
let lastStamp = 0;
function nextStamp(): number {
  const now = Date.now();
  lastStamp = now > lastStamp ? now : lastStamp + 1;
  return lastStamp;
}

export const useApprovalResultsStore = defineStore("approvalResults", {
  state: () => ({
    /** approval_id → 结果；并发两条审批各占一个键，互不覆盖 */
    byId: {} as Record<string, ApprovalResultEntry>,
    /** 展示顺序：最近的在前 */
    order: [] as string[],
    /** 后端 history 原始行（只读；界面可核对来源） */
    history: [] as ApprovalHistoryEntry[],
    historyLoading: false,
    historyError: null as string | null,
    /** 最近一次成功读到历史的时间（毫秒）；null = 还没成功读过 */
    historyLoadedAt: null as number | null,
  }),
  getters: {
    entries(state): ApprovalResultEntry[] {
      return state.order
        .map((id) => state.byId[id])
        .filter((entry): entry is ApprovalResultEntry => Boolean(entry));
    },
    latest(state): ApprovalResultEntry | null {
      return this.entries?.[0] ?? null;
    },
    /** 有没有任何可展示的结果 / 历史（入口据此决定是否可见） */
    hasAny(state): boolean {
      return Object.keys(state.byId).length > 0 || state.history.length > 0;
    },
    /** 还在进行中的（已批准 / 执行中）—— 不能显示成完成。 */
    inFlight(state): ApprovalResultEntry[] {
      return this.entries.filter((entry) => !isTerminalApprovalState(entry.state));
    },
    byApprovalId(state) {
      return (approvalId: string): ApprovalResultEntry | null =>
        state.byId[String(approvalId ?? "")] ?? null;
    },
  },
  actions: {
    /** 重排展示顺序（最近的在前；同一毫秒按 approval_id 稳定排序）。 */
    _reorder() {
      this.order = Object.keys(this.byId).sort((a, b) => {
        const left = this.byId[a]?.updatedAt ?? 0;
        const right = this.byId[b]?.updatedAt ?? 0;
        if (right !== left) return right - left;
        return a < b ? -1 : a > b ? 1 : 0;
      });
    },

    /** 写入一条结果（同一 approval_id 合并，不覆盖别的审批）。 */
    put(entry: ApprovalResultEntry | null): void {
      if (!entry) return;
      const existing = this.byId[entry.approvalId] ?? null;
      this.byId = {
        ...this.byId,
        [entry.approvalId]: mergeResultEntry(existing, entry),
      };
      this._reorder();
    },

    /** 实时事件入口（approvals.recordResult 调用）。 */
    recordEvent(data: Record<string, unknown>): ApprovalResultEntry | null {
      const entry = resultFromEvent(data);
      if (!entry) return null;
      this.put(entry);
      return this.byId[entry.approvalId] ?? entry;
    },

    /**
     * 本机用户动作的即时结果（批准 / 拒绝 / 取消）。
     *
     * 只记录**真的发生过**的用户决定；执行结果仍以后端事件/历史为准。
     */
    recordLocal(input: {
      approvalId: string;
      phase: string;
      state?: string;
      summary?: string;
      ok?: boolean | null;
      kind?: string;
      label?: string;
    }): void {
      const approvalId = text(input.approvalId);
      if (!approvalId) return;
      const state = text(input.state) || resultStateFromOutcome(input.phase) || input.phase;
      const now = nextStamp();
      this.put({
        approvalId,
        kind: text(input.kind),
        label: text(input.label),
        phase: text(input.phase),
        state,
        stateLabel: approvalStateLabel(state),
        summary: text(input.summary) || PHASE_SUMMARY[state] || "",
        ok:
          typeof input.ok === "boolean"
            ? input.ok
            : isTerminalApprovalState(state)
              ? false
              : null,
        detail: {},
        items: [],
        batchSummary: "",
        source: "event",
        at: new Date(now).toISOString(),
        updatedAt: now,
      });
    },

    /** 用后端历史行重建结果（刷新后从持久化终态恢复；实时终态优先，不被降级）。 */
    mergeHistory(rows: ApprovalHistoryEntry[]): void {
      const list = Array.isArray(rows) ? rows : [];
      this.history = list;
      const next = { ...this.byId };
      list.forEach((row, index) => {
        const entry = resultFromHistory(row, index);
        if (!entry) return;
        const existing = next[entry.approvalId] ?? null;
        next[entry.approvalId] = mergeResultEntry(existing, entry);
      });
      this.byId = next;
      this._reorder();
    },

    /** 读后端历史；失败保留已有内容并给出可重试原因（绝不当作空历史）。 */
    async loadHistory(limit = 50): Promise<boolean> {
      if (historyInflight) return historyInflight;
      this.historyLoading = true;
      historyInflight = (async () => {
        try {
          const rows = await getApprovalHistory(limit);
          this.mergeHistory(rows);
          this.historyError = null;
          this.historyLoadedAt = Date.now();
          return true;
        } catch (error) {
          this.historyError =
            "读取审批历史失败：" + ((error as Error)?.message || "未知原因") + "（可以重试）";
          return false;
        } finally {
          this.historyLoading = false;
          historyInflight = null;
        }
      })();
      return historyInflight;
    },

    /** 首次进入聊天页时补一次历史（已读过就不重复请求）。 */
    ensureLoaded(): void {
      if (this.historyLoadedAt !== null || this.historyLoading) return;
      void this.loadHistory();
    },

    reset(): void {
      this.byId = {};
      this.order = [];
      this.history = [];
      this.historyLoading = false;
      this.historyError = null;
      this.historyLoadedAt = null;
    },
  },
});
