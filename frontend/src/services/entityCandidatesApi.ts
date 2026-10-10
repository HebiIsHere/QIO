/**
 * A04 待处理候选 API（冻结契约 §3.3：`api/entity_pending_routes.py`）。
 *
 * 为什么单独一个模块而不是塞进 `services/api.ts`：
 * - 候选是一组独立语义（列出 / 采纳 / 丢弃 + 409 冲突），前端要能**只**针对它做
 *   受控替身测试（界面接线是这次改动的重点）；
 * - 409 必须能被上层识别：`adoptCandidate` / `dismissCandidate` 抛
 *   `EntityCandidateConflictError`（`status === 409`），界面据此就地提示
 *   「这条实体已被改动，请刷新后重试」，而不是把它当成一次普通失败吞掉。
 *
 * 写操作（adopt / dismiss）**永不自动重试**：超时只说明「没等到响应」，
 * 操作可能已经生效，自动重发会变成第二次决定。读操作（两个列表）可以有限重试一次。
 */
import { authHeaders, resetBackend, resolveBackend } from "./backend";
import type { EntityCard } from "./api";

/** 一条待处理候选（后端 `CandidateView.to_dict()`，契约 §3.2）。 */
export interface EntityCandidateView {
  candidate_id: string;
  entity_id: string;
  entity_name: string;
  /** active / revoked / …（归档卡片的候选仍然可见，但采纳是被拒绝的） */
  card_state: string;
  /** "summary" | "kind" | "aliases" | "attributes.<key>" */
  field: string;
  /** 中文短标签：界面直接显示，不自己翻译字段名 */
  field_label: string;
  /** "summary" | "kind" | "aliases" | "attribute" */
  kind: string;
  /** 当前值（可读形态：字符串 / 字符串数组 / 标量） */
  current_value: unknown;
  /** 候选值（可读形态） */
  candidate_value: unknown;
  /** 机器原因：stale_revision / user_value_conflict / user_deleted / … */
  reason: string;
  /** 一句话中文原因（后端已去掉 revision / 字段名等内部细节） */
  reason_label: string;
  created_at: string;
  /** 这条候选是针对卡的哪个 revision 提出的：采纳/丢弃要带回去做条件校验 */
  card_revision: number;
  adoptable: boolean;
  /** 例如 "card_archived"：不可采纳的具体原因 */
  blocked_reason: string;
}

/** 跨卡片的候选清单（`GET /api/entities/candidates`）。 */
export interface EntityCandidateListing {
  candidates: EntityCandidateView[];
  /** 匹配总数（不受 limit 影响） */
  total: number;
  shown: number;
  truncated: boolean;
}

/** 单张卡片的候选（`GET /api/entities/{entity_id}/candidates`）。 */
export interface EntityCandidatesForEntity {
  /** 这张卡当前的真实形状；采纳成功后就地用它刷新界面 */
  entity: EntityCard | null;
  candidates: EntityCandidateView[];
  /** 后端给了就用它，没给就是这次真的取回来的条数（不夸大） */
  total: number;
  truncated: boolean;
}

/** 采纳结果：`ok === true` 才算真的写进去了。 */
export interface AdoptCandidateResult {
  ok: boolean;
  entity: EntityCard | null;
  adopted: { field: string; value: unknown } | null;
  /** 候选已经被解决过（重复点击）：幂等成功，不是 500 */
  already_resolved?: boolean;
  /** 例如 "card_archived"：这次没有写任何值，界面必须如实说明 */
  blocked_reason?: string;
}

/** 丢弃结果：只解决这一个候选，不改实体值。 */
export interface DismissCandidateResult {
  ok: boolean;
  dismissed: boolean;
  /** 候选已经被解决过（重复点击）：幂等成功 */
  already_resolved?: boolean;
  blocked_reason?: string;
}

/** 读 / 写超时（毫秒）。与 `services/api.ts` 的取法一致，但本模块自带一份，
 *  这样界面替身测试不需要连带 mock 整个 api 模块。 */
export const CANDIDATE_TIMEOUT_MS = {
  read: 15_000,
  write: 20_000,
} as const;

/** 候选接口的错误：**带 status**，上层才能区分 404 / 409 / 5xx。 */
export class EntityCandidatesApiError extends Error {
  constructor(
    readonly status: number,
    readonly path: string,
    readonly detail: string,
  ) {
    super(
      status === 401 || status === 403
        ? `${path} -> ${status}: 本机 API 拒绝了这次请求（会话令牌缺失或已失效）`
        : status === 0
          ? `${path} -> 没有响应：${detail}`
          : `${path} -> ${status}: ${detail}`,
    );
    this.name = "EntityCandidatesApiError";
  }
}

/**
 * 409：这条实体已经被改动过（或候选已经被处理过）。
 *
 * 界面拿到它必须**原地**说清楚并给「刷新」，不能把它显示成「采纳成功」。
 */
export class EntityCandidateConflictError extends EntityCandidatesApiError {
  constructor(
    path: string,
    detail: string,
    /** 后端返回的当前 revision；老响应可能没有 */
    readonly currentRevision: number | null = null,
    /** 后端给的机器原因 */
    readonly conflictReason: string = "",
  ) {
    super(409, path, detail);
    this.name = "EntityCandidateConflictError";
  }
}

/** 这次失败是不是「被改动过」的冲突（包括任何带 409 的错误）。 */
export function isCandidateConflict(err: unknown): boolean {
  if (err instanceof EntityCandidateConflictError) return true;
  if (err instanceof EntityCandidatesApiError) return err.status === 409;
  return (err as { status?: unknown } | null)?.status === 409;
}

interface RequestOptions {
  method?: "GET" | "POST";
  body?: unknown;
  /** 相对路径，例如 `/api/entities/candidates?include_archived=true` */
  timeoutMs?: number;
  /** 读类请求的有限重试次数；写操作固定 0 */
  retries?: number;
}

function safeJson(text: string): Record<string, unknown> | null {
  try {
    const parsed = JSON.parse(text) as unknown;
    return parsed && typeof parsed === "object" ? (parsed as Record<string, unknown>) : null;
  } catch {
    return null;
  }
}

function detailOf(text: string, parsed: Record<string, unknown> | null): string {
  const reason = parsed?.reason;
  if (typeof reason === "string" && reason.trim()) return reason.trim();
  return text.slice(0, 200);
}

async function requestOnce<T>(path: string, opts: RequestOptions, timeoutMs: number): Promise<T> {
  const { base, token } = await resolveBackend();
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  let resp: Response;
  try {
    resp = await fetch(`${base}${path}`, {
      method: opts.method ?? "GET",
      signal: controller.signal,
      headers: {
        "Content-Type": "application/json",
        ...authHeaders(token),
      },
      ...(opts.body === undefined ? {} : { body: JSON.stringify(opts.body) }),
    });
  } catch (err) {
    if (controller.signal.aborted) {
      // 如实说「没有响应」：写操作绝不自动重试，调用方自己决定是否再来一次
      throw new EntityCandidatesApiError(
        0,
        path,
        `在 ${Math.round(timeoutMs / 1000)} 秒内没有响应（这次请求已取消，可以重试）`,
      );
    }
    throw err;
  } finally {
    clearTimeout(timer);
  }

  const text = await resp.text().catch(() => "");
  const parsed = safeJson(text);
  if (!resp.ok) {
    // 认证明确失效：令牌可能已轮换 → 下一次请求重新解析地址与令牌
    if (resp.status === 401 || resp.status === 403) resetBackend();
    const detail = detailOf(text, parsed);
    if (resp.status === 409) {
      const rev = parsed?.current_revision;
      const reason = parsed?.reason;
      throw new EntityCandidateConflictError(
        path,
        detail,
        typeof rev === "number" ? rev : null,
        typeof reason === "string" ? reason : "",
      );
    }
    throw new EntityCandidatesApiError(resp.status, path, detail);
  }
  if (!parsed) {
    throw new EntityCandidatesApiError(resp.status, path, "响应不是合法 JSON（这次的结果不可用）");
  }
  return parsed as unknown as T;
}

async function request<T>(path: string, opts: RequestOptions = {}): Promise<T> {
  const method = opts.method ?? "GET";
  const isRead = method === "GET";
  const timeoutMs =
    opts.timeoutMs ?? (isRead ? CANDIDATE_TIMEOUT_MS.read : CANDIDATE_TIMEOUT_MS.write);
  const retries = isRead ? Math.max(0, opts.retries ?? 1) : 0;
  let attempt = 0;
  for (;;) {
    try {
      return await requestOnce<T>(path, opts, timeoutMs);
    } catch (err) {
      // 409 是「结论」，不是「网络抖动」：读也不重试
      const retryable =
        isRead &&
        !isCandidateConflict(err) &&
        (err instanceof EntityCandidatesApiError
          ? err.status === 401 || err.status === 403 || err.status === 0
          : err instanceof TypeError);
      if (attempt >= retries || !retryable) throw err;
      attempt += 1;
    }
  }
}

function asNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function candidateArray(raw: unknown): EntityCandidateView[] {
  return Array.isArray(raw) ? (raw as EntityCandidateView[]) : [];
}

/** 跨卡片的待处理候选（归档卡片的候选默认也带回来：用户要看得见才能处理）。 */
export function listAllCandidates(
  includeArchived = true,
  limit = 200,
): Promise<EntityCandidateListing> {
  const query = new URLSearchParams({
    include_archived: includeArchived ? "true" : "false",
    limit: String(limit),
  });
  return request<EntityCandidateListing>(`/api/entities/candidates?${query.toString()}`).then(
    (raw) => {
      const candidates = candidateArray(raw?.candidates);
      return {
        candidates,
        total: asNumber(raw?.total) ?? candidates.length,
        shown: asNumber(raw?.shown) ?? candidates.length,
        truncated: Boolean(raw?.truncated),
      };
    },
  );
}

/** 一张卡片自己的候选（`entity` 是权威现状，采纳成功后就地刷新界面）。 */
export function listEntityCandidates(entityId: string): Promise<EntityCandidatesForEntity> {
  return request<{ entity?: EntityCard | null; candidates?: unknown; total?: unknown; truncated?: unknown }>(
    `/api/entities/${encodeURIComponent(entityId)}/candidates`,
  ).then((raw) => {
    const candidates = candidateArray(raw?.candidates);
    return {
      entity: raw?.entity ?? null,
      candidates,
      total: asNumber(raw?.total) ?? candidates.length,
      truncated: Boolean(raw?.truncated),
    };
  });
}

/**
 * 采纳一条候选。
 *
 * `expectedRevision` 是这条候选所属卡的 revision：后端做条件校验，不符就是 409
 * （一个字段都不改）。界面必须把 409 显示成「已被改动，请刷新后重试」。
 */
export function adoptCandidate(
  entityId: string,
  candidateId: string,
  expectedRevision: number,
): Promise<AdoptCandidateResult> {
  return request<AdoptCandidateResult>(
    `/api/entities/${encodeURIComponent(entityId)}/candidates/${encodeURIComponent(candidateId)}/adopt`,
    { method: "POST", body: { expected_revision: expectedRevision } },
  );
}

/** 丢弃一条候选：只解决这一个，不改实体当前值。 */
export function dismissCandidate(
  entityId: string,
  candidateId: string,
  expectedRevision: number,
): Promise<DismissCandidateResult> {
  return request<DismissCandidateResult>(
    `/api/entities/${encodeURIComponent(entityId)}/candidates/${encodeURIComponent(candidateId)}/dismiss`,
    { method: "POST", body: { expected_revision: expectedRevision } },
  );
}
