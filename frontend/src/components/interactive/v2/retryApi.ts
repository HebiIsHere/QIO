/**
 * 重试系列（Phase 3 契约 §6.3）的**前端兼容层**（Phase 3-C）。
 *
 * 后端 D 并行实现持久化 retry 系列（series_id / intent_id / authorization_version /
 * attempts / limit / status / last_error / next_action / updated_at）。本模块：
 * - 只做**真实 API 调用**与响应归一化，不猜、不伪造次数；
 * - 服务端还没有这些接口（404 / 405 / 501）时返回 available=false 与可读原因，
 *   界面据此如实显示「能力未上线」，而不是显示 0 次或假上限；
 * - 字段缺失时保留 null：界面只显示服务端真的给了的事实（次数、上限、下一次动作）。
 *
 * 端点命名与 D 的最终实现对齐点（Lead 集成时确认一次即可）：
 *   GET  /api/interactive/boards/{boardId}/retry-series
 *   GET  /api/interactive/intents/{intentId}/retry-series
 *   POST /api/interactive/intents/{intentId}/retry
 * 都经本文件收敛 —— 后端改名时只改这里。
 */
import { authHeaders, resolveBackend } from "../../../services/backend";

export interface RetrySeriesView {
  seriesId: string;
  intentId: string;
  /** 授权版本（服务端事实；缺失为 null，不猜） */
  authorizationVersion: string | null;
  /** 已经发生的自动重试次数（首次失败不算；缺失为 null） */
  attempts: number | null;
  /** 服务端给的上限（契约默认 3；缺失为 null，界面不编造） */
  limit: number | null;
  status: string;
  lastError: string | null;
  /** 服务端给出的下一步动作（人话原文；缺失为 null） */
  nextAction: string | null;
  updatedAt: string | null;
}

export interface RetrySeriesEnvelope {
  available: boolean;
  reason: string;
  series: RetrySeriesView[];
}

/** 组件测试缝：默认就是本文件的真实实现（生产不传）。 */
export interface RetryPanelApi {
  fetchSeries: typeof fetchRetrySeries;
  retry: typeof requestManualRetry;
}

export interface RetryTarget {
  boardId?: string | null;
  intentId?: string | null;
}

export interface ManualRetryResult {
  ok: boolean;
  reason: string;
  /** 服务端在回执里返回的最新系列（可为 null：以随后的读取为准） */
  series: RetrySeriesView | null;
}

export class RetryApiError extends Error {
  constructor(
    readonly status: number,
    readonly path: string,
    message: string,
  ) {
    super(message);
    this.name = "RetryApiError";
  }
}

const BASE = "/api/interactive";

/** 「能力未上线」的三类响应：不是失败，也不是零次。 */
function isUnavailableStatus(status: number): boolean {
  return status === 404 || status === 405 || status === 501;
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

function textOf(...values: unknown[]): string | null {
  for (const value of values) {
    if (typeof value === "string" && value.trim()) return value.trim();
  }
  return null;
}

function numberOrNull(value: unknown): number | null {
  const n = typeof value === "number" ? value : Number(value);
  if (!Number.isFinite(n) || n < 0) return null;
  return Math.floor(n);
}

/** 单条重试系列归一化：字段缺失就是 null（绝不补默认次数/上限）。 */
export function normalizeRetrySeries(raw: unknown): RetrySeriesView | null {
  const record = asRecord(raw);
  if (!record) return null;
  const seriesId = textOf(record.series_id, record.seriesId, record.id);
  const intentId = textOf(record.intent_id, record.intentId);
  if (!seriesId || !intentId) return null;
  const rawLimit = numberOrNull(record.limit ?? record.max_attempts ?? record.maxAttempts);
  return {
    seriesId,
    intentId,
    authorizationVersion: textOf(
      record.authorization_version,
      record.authorizationVersion,
      record.auth_version,
    ),
    attempts: numberOrNull(record.attempts ?? record.attempt ?? record.retry_count),
    limit: rawLimit && rawLimit > 0 ? rawLimit : null,
    status: textOf(record.status, record.state) ?? "unknown",
    lastError: textOf(record.last_error, record.lastError, record.error),
    nextAction: textOf(record.next_action, record.nextAction),
    updatedAt: textOf(record.updated_at, record.updatedAt),
  };
}

/** 列表响应归一化：容忍 series / retries / items 三种数组键与直接数组。 */
export function normalizeRetryEnvelope(raw: unknown): RetrySeriesEnvelope {
  const record = asRecord(raw);
  if (Array.isArray(raw)) {
    return {
      available: true,
      reason: "",
      series: raw.map(normalizeRetrySeries).filter((item): item is RetrySeriesView => item !== null),
    };
  }
  if (!record) return { available: false, reason: "服务端没有返回可读的重试记录", series: [] };
  const available = record.available !== false;
  const reason = textOf(record.reason, record.error, record.detail) ?? "";
  if (!available) return { available: false, reason: reason || "服务端暂不提供重试记录", series: [] };
  const list = Array.isArray(record.series)
    ? record.series
    : Array.isArray(record.retries)
      ? record.retries
      : Array.isArray(record.items)
        ? record.items
        : [];
  return {
    available: true,
    reason,
    series: list.map(normalizeRetrySeries).filter((item): item is RetrySeriesView => item !== null),
  };
}

/** 手动重试回执归一化：ok 只认服务端明确成功；series 可为空。 */
export function normalizeManualRetryResult(raw: unknown): ManualRetryResult {
  const record = asRecord(raw);
  if (!record) return { ok: false, reason: "服务端没有返回重试回执", series: null };
  const seriesRaw = record.series ?? record.retry ?? record.retry_series;
  return {
    ok: record.ok === true,
    reason: textOf(record.reason, record.error, record.detail) ?? "",
    series: normalizeRetrySeries(seriesRaw),
  };
}

async function requestRaw(
  path: string,
  init: { method?: string; body?: unknown; signal?: AbortSignal } = {},
): Promise<unknown> {
  const { base, token } = await resolveBackend();
  let resp: Response;
  try {
    resp = await fetch(base + path, {
      method: init.method ?? "GET",
      signal: init.signal,
      headers: {
        "Content-Type": "application/json",
        ...authHeaders(token),
      },
      ...(init.body === undefined ? {} : { body: JSON.stringify(init.body) }),
    });
  } catch (err) {
    throw new RetryApiError(
      0,
      path,
      "没有连上本机后端：" + (err instanceof Error ? err.message : String(err)),
    );
  }
  if (!resp.ok) {
    let detail = "";
    try {
      const body = (await resp.json()) as Record<string, unknown>;
      detail = textOf(body.reason, body.error, body.detail) ?? "";
    } catch {
      detail = await resp.text().catch(() => "");
    }
    throw new RetryApiError(resp.status, path, String(detail).slice(0, 240));
  }
  try {
    return (await resp.json()) as unknown;
  } catch {
    return null;
  }
}

function targetQuery(target: RetryTarget): string {
  const params: string[] = [];
  if (target.boardId) params.push("board_id=" + encodeURIComponent(target.boardId));
  if (target.intentId) params.push("intent_id=" + encodeURIComponent(target.intentId));
  return params.length ? "?" + params.join("&") : "";
}

/**
 * 读取重试系列：给 intentId 时读这一条；只给 boardId 时读整块板面的记录。
 * 服务端接口尚未提供 → available=false（不是空列表）。
 */
export async function fetchRetrySeries(
  target: RetryTarget,
  options: { signal?: AbortSignal } = {},
): Promise<RetrySeriesEnvelope> {
  const intentId = target.intentId ? String(target.intentId) : "";
  const boardId = target.boardId ? String(target.boardId) : "";
  const path = intentId
    ? `${BASE}/intents/${encodeURIComponent(intentId)}/retry-series`
    : `${BASE}/boards/${encodeURIComponent(boardId)}/retry-series`;
  try {
    const raw = await requestRaw(path, { signal: options.signal });
    const normalized = normalizeRetryEnvelope(raw);
    // 空响应也当作「服务端没给记录」，由界面如实显示，不编造 0 次
    return normalized;
  } catch (err) {
    if (err instanceof RetryApiError && isUnavailableStatus(err.status)) {
      return {
        available: false,
        reason: "服务端还没有提供重试记录（这个能力未上线，不是「没有重试」）",
        series: [],
      };
    }
    throw err;
  }
}

/**
 * 手动重试：**一次点击一次尝试**。
 * 只把服务端回执原样带回来；本地绝不 ++attempts、绝不重置自动次数。
 */
export async function requestManualRetry(
  intentId: string,
  options: {
    boardId?: string | null;
    seriesId?: string | null;
    expectedAuthorizationVersion?: string | number | null;
    signal?: AbortSignal;
  } = {},
): Promise<ManualRetryResult> {
  const path = `${BASE}/intents/${encodeURIComponent(String(intentId))}/retry`;
  const raw = await requestRaw(path, {
    method: "POST",
    signal: options.signal,
    body: {
      ...(options.boardId ? { boardId: options.boardId } : {}),
      ...(options.seriesId ? { seriesId: options.seriesId } : {}),
      ...(options.expectedAuthorizationVersion !== undefined &&
      options.expectedAuthorizationVersion !== null &&
      options.expectedAuthorizationVersion !== ""
        ? { expectedAuthorizationVersion: options.expectedAuthorizationVersion }
        : {}),
    },
  });
  return normalizeManualRetryResult(raw);
}

/** 状态 → 人话（未知状态原样显示，不猜成成功/失败）。 */
export function retryStatusLabel(status: string | null | undefined): string {
  const value = String(status ?? "").trim();
  const labels: Record<string, string> = {
    idle: "等待首次尝试",
    pending: "等待自动重试",
    stopped: "已停止自动重试",
    completed: "已完成",
    scheduled: "已安排自动重试",
    auto_retry_scheduled: "已安排自动重试",
    retrying: "正在重试",
    running: "正在重试",
    exhausted: "自动重试已用尽",
    succeeded: "重试后已成功",
    recovered: "重试后已恢复",
    failed: "重试没有成功",
    cancelled: "重试已取消",
    manual_pending: "已提交手动重试",
    manual: "手动重试",
    unknown: "状态未记录",
  };
  if (!value) return "状态未记录";
  return labels[value] ?? value + "（未识别的状态）";
}

/** 服务端正在重试时，手动按钮要禁用（避免同一系列并行两次）。 */
export function retryInFlight(status: string | null | undefined): boolean {
  const value = String(status ?? "").trim();
  return [
    "pending",
    "scheduled",
    "auto_retry_scheduled",
    "retrying",
    "running",
    "manual_pending",
  ].includes(value);
}

/** 次数事实：只显示服务端给的；上限缺失时明确说「上限以服务端为准」。 */
export function retryAttemptsText(series: Pick<RetrySeriesView, "attempts" | "limit">): string {
  const attempts = series.attempts;
  if (attempts === null || attempts === undefined) return "自动重试次数未记录（以服务端为准）";
  if (series.limit === null || series.limit === undefined) {
    return "已自动重试 " + attempts + " 次（上限以服务端为准）";
  }
  return "已自动重试 " + attempts + " / 最多 " + series.limit + " 次";
}
