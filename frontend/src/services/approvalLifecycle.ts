/**
 * 审批生命周期 v2 客户端（R9 契约 §5；W1 HTTP 答复）。
 *
 * 只做三件事：
 * 1. 恢复：GET /api/approvals/pending（刷新/断线/重启后补回待审批项）；
 * 2. 决定：POST /api/approvals/{id}/decide（approve / reject / cancel）；
 * 3. 内部数据三次确认：POST /api/approvals/{id}/confirm（stage 必须 CAS 递增，后端为准）。
 *
 * 前端**只呈现**确认进度，不自己实现三次确认状态机：`confirmed_stage` 由服务端 CAS 校验，
 * 任何越级 / 重复确认由后端拒绝（409），这里把原因如实显示。
 */
import { authHeaders, resetBackend, resolveBackend } from "./backend";

export type ApprovalDecision = "approve" | "reject" | "cancel";
export type ApprovalLifecycleState =
  | "waiting"
  | "approved"
  | "executing"
  | "completed"
  | "failed"
  | "rejected"
  | "cancelled"
  | "expired"
  | "invalidated"
  | "interrupted_unverified";

export const APPROVAL_STATE_LABELS: Record<string, string> = {
  waiting: "等待确认",
  approved: "已批准（等待执行）",
  executing: "执行中",
  completed: "已完成",
  failed: "执行失败",
  rejected: "已拒绝",
  cancelled: "已取消",
  expired: "已过期",
  invalidated: "已失效（执行前核对不通过）",
  interrupted_unverified: "执行中断，待核对",
};

/** 「还要继续推进」的非终态：这些状态不能从待办里移除（仍需要用户动作或等待执行）。 */
export const NON_TERMINAL_APPROVAL_STATES = new Set(["waiting", "approved", "executing"]);

export interface ApprovalOperationSummary {
  kind: string;
  label: string;
  summary: string;
}

/** GET /api/approvals/pending 的一条（W1 冻结形状；未知字段原样保留在 payload）。 */
export interface PendingApproval {
  approval_id: string;
  kind: string;
  label: string;
  purpose: string;
  instruction_id: string | null;
  instruction_text: string | null;
  created_at: string;
  expires_at: string | null;
  state: string;
  confirmed_stage: number;
  requires_confirmations: number;
  source: string;
  mode_at_request: string | null;
  payload: Record<string, unknown>;
  operation?: ApprovalOperationSummary | null;
  /** 后端 CAS 修订号（decide/confirm 可带 expected_revision；缺失时不带） */
  revisions?: number;
  revision?: number;
}

export interface ApprovalHistoryEntry {
  approval_id: string;
  kind: string;
  summary: string;
  outcome: string;
  detail: Record<string, unknown>;
  at: string;
}

export class ApprovalRequestError extends Error {
  constructor(
    readonly status: number,
    readonly path: string,
    detail: string,
    readonly body?: unknown,
  ) {
    super(path + " -> " + status + "：" + detail);
    this.name = "ApprovalRequestError";
  }
}

async function request<T>(
  path: string,
  init: RequestInit & { timeoutMs?: number } = {},
): Promise<T> {
  const { base, token } = await resolveBackend();
  const { timeoutMs = 20_000, ...rest } = init;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const resp = await fetch(base + path, {
      ...rest,
      signal: controller.signal,
      headers: {
        ...authHeaders(token),
        ...((rest.headers as Record<string, string> | undefined) ?? {}),
      },
    });
    if (!resp.ok) {
      const text = await resp.text().catch(() => "");
      if (resp.status === 401 || resp.status === 403) resetBackend();
      let body: unknown = null;
      try {
        body = JSON.parse(text);
      } catch {
        /* 非 JSON：保留原文 */
      }
      // W1 的失败响应 detail 形如 {code, message(中文)}：优先把中文 message 给用户
      const detailMessage =
        body && typeof body === "object"
          ? (body as Record<string, unknown>).detail
          : null;
      const nested =
        detailMessage && typeof detailMessage === "object"
          ? (detailMessage as Record<string, unknown>).message
          : null;
      const shown =
        typeof nested === "string" && nested
          ? nested
          : typeof detailMessage === "string" && detailMessage
            ? detailMessage
            : text.slice(0, 300) || resp.statusText;
      throw new ApprovalRequestError(resp.status, path, shown, body);
    }
    if (resp.status === 204) return undefined as T;
    return (await resp.json()) as T;
  } finally {
    clearTimeout(timer);
  }
}

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

function text(value: unknown, fallback = ""): string {
  return typeof value === "string" ? value : typeof value === "number" ? String(value) : fallback;
}

function numberOr(value: unknown, fallback: number): number {
  const n = Number(value);
  return Number.isFinite(n) ? n : fallback;
}

/** 把后端 pending 条目收敛成稳定形状（未知状态不丢：原样保留，界面按 waiting 展示）。 */
export function toPendingApproval(raw: Record<string, unknown>): PendingApproval {
  const operation = raw.operation;
  return {
    approval_id: text(raw.approval_id ?? raw.id),
    kind: text(raw.kind, "unknown"),
    label: text(raw.label),
    purpose: text(raw.purpose),
    instruction_id: text(raw.instruction_id) || null,
    instruction_text: text(raw.instruction_text) || null,
    created_at: text(raw.created_at),
    expires_at: text(raw.expires_at) || null,
    state: text(raw.state, "waiting"),
    confirmed_stage: numberOr(raw.confirmed_stage, 0),
    requires_confirmations: Math.max(1, numberOr(raw.requires_confirmations, 1)),
    source: text(raw.source, "tool"),
    mode_at_request: text(raw.mode_at_request) || null,
    payload: asRecord(raw.payload),
    operation: operation && typeof operation === "object"
      ? {
          kind: text((operation as Record<string, unknown>).kind),
          label: text((operation as Record<string, unknown>).label),
          // W1 形状是 operation{kind,label,purpose}；也兼容 summary 字段
          summary:
            text((operation as Record<string, unknown>).summary) ||
            text((operation as Record<string, unknown>).purpose),
        }
      : null,
    revisions: numberOr(raw.revisions ?? raw.revision, 0) || undefined,
    revision: numberOr(raw.revision, 0) || undefined,
  };
}

/** GET /api/approvals/pending：断线/刷新/重启后恢复待审批项（唯一恢复入口）。 */
export async function getPendingApprovals(): Promise<PendingApproval[]> {
  const body = await request<{ approvals?: unknown[] }>("/api/approvals/pending", {
    timeoutMs: 15_000,
  });
  const list = Array.isArray(body?.approvals) ? body.approvals : [];
  return list.map((item) => toPendingApproval(asRecord(item))).filter((item) => item.approval_id);
}

/** POST /api/approvals/{id}/decide：批准 / 拒绝 / 取消（旧 /respond 不再由新前端使用）。 */
export async function decideApproval(
  approvalId: string,
  decision: ApprovalDecision,
  options: { expectedRevision?: number } = {},
): Promise<PendingApproval | null> {
  const path = "/api/approvals/" + encodeURIComponent(approvalId) + "/decide";
  const body: Record<string, unknown> = { decision };
  if (typeof options.expectedRevision === "number") body.expected_revision = options.expectedRevision;
  const res = await request<{ ok?: boolean; approval?: Record<string, unknown> }>(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return res?.approval ? toPendingApproval(res.approval) : null;
}

/** POST /api/approvals/{id}/confirm：下一次内部数据确认（stage=1|2|3，后端 CAS）。 */
export async function confirmApprovalStage(
  approvalId: string,
  stage: number,
  options: { expectedRevision?: number } = {},
): Promise<PendingApproval | null> {
  const path = "/api/approvals/" + encodeURIComponent(approvalId) + "/confirm";
  const body: Record<string, unknown> = { stage };
  if (typeof options.expectedRevision === "number") body.expected_revision = options.expectedRevision;
  const res = await request<{ ok?: boolean; approval?: Record<string, unknown> }>(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return res?.approval ? toPendingApproval(res.approval) : null;
}

/** GET /api/approvals/history：执行结果历史（读历史，不参与待办状态机）。 */
export async function getApprovalHistory(limit = 20): Promise<ApprovalHistoryEntry[]> {
  const body = await request<{ history?: unknown[]; approvals?: unknown[]; ok?: boolean }>(
    "/api/approvals/history?limit=" + encodeURIComponent(String(limit)),
    { timeoutMs: 15_000 },
  );
  const raw = Array.isArray(body?.history)
    ? body.history
    : Array.isArray(body?.approvals)
      ? body.approvals
      : [];
  return raw.map((item) => {
    const row = asRecord(item);
    return {
      approval_id: text(row.approval_id ?? row.id),
      kind: text(row.kind),
      summary: text(row.summary),
      outcome: text(row.outcome ?? row.state ?? row.status),
      detail: asRecord(row.detail),
      at: text(row.at ?? row.created_at),
    };
  });
}

/** 事件 phase → 待办状态（APPROVAL_RESULT 用；前端只呈现，不自行推进状态机）。 */
export function approvalStateFromPhase(phase: string): ApprovalLifecycleState | null {
  switch (phase) {
    case "waiting":
      return "waiting";
    case "approved":
      return "approved";
    case "executing":
      return "executing";
    case "executed":
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
      return "cancelled";
    case "expired":
      return "expired";
    default:
      return null;
  }
}

/** 状态的中文标签（未知状态如实显示原值，不假装认识）。 */
export function approvalStateLabel(state: string): string {
  const known = APPROVAL_STATE_LABELS[state];
  if (known) return known;
  const raw = String(state ?? "").trim();
  return raw ? "未知状态（" + raw + "）" : "等待确认";
}
