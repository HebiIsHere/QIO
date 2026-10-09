/**
 * 互动模式的 API 客户端。
 *
 * 契约：docs/interactive-mode-contract.md §2。
 * 路径与 `agent/api/interactive_store.py` / `interactive_intents.py` 一一对应。
 */
import { authHeaders, resolveBackend } from "./backend";
import type {
  BoardState,
  BoardStateResponse,
  DecideResult,
  Intent,
  IntentListResponse,
  IntentPreview,
  SubmissionRecord,
  SubmissionResult,
  VisibleRange,
} from "../interactive/types";

/** 服务端错误体（M4 定稿）：error/reason 是给用户看的真实原因，其余是可核对的结构。 */
export interface InteractiveErrorPayload {
  error?: string;
  reason?: string;
  detail?: string;
  affectedTasks?: { intentId: string; title: string; materials: string[]; consequence: string }[];
  currentSeq?: number;
  limit?: number;
  keys?: string[];
}

export class InteractiveApiError extends Error {
  constructor(
    readonly status: number,
    readonly path: string,
    detail: string,
    readonly payload?: InteractiveErrorPayload,
  ) {
    super(
      status === 401 || status === 403
        ? `${path} -> ${status}: 本机 API 拒绝了这次请求（会话令牌缺失或已失效）`
        : `${path} -> ${status}: ${detail}`,
    );
    this.name = "InteractiveApiError";
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const { base, token } = await resolveBackend();
  const resp = await fetch(`${base}${path}`, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...authHeaders(token),
      ...((init?.headers as Record<string, string> | undefined) ?? {}),
    },
  });
  if (!resp.ok) {
    // 服务端会给出「为什么这次没成功」的真实原因（stale_state / stale_check /
    // impact_confirmation_required / draft_too_long …）：它必须原样到得了界面，
    // 不能被压成一句笼统的失败。payload 保留结构（例如 affectedTasks / limit）。
    let detail = "";
    let payload: InteractiveErrorPayload | undefined;
    try {
      payload = (await resp.json()) as InteractiveErrorPayload;
      detail = payload?.detail ?? payload?.reason ?? payload?.error ?? "";
    } catch {
      detail = await resp.text().catch(() => "");
    }
    throw new InteractiveApiError(resp.status, path, detail.slice(0, 300), payload);
  }
  return (await resp.json()) as T;
}

const BASE = "/api/interactive";

export function listBoards(): Promise<{ boards: { id: string; title: string }[] }> {
  return request(`${BASE}/boards`);
}

export function createBoard(title?: string): Promise<{ board: { id: string; title: string } }> {
  return request(`${BASE}/boards`, { method: "POST", body: JSON.stringify({ title }) });
}

export function fetchBoardState(boardId: string): Promise<BoardStateResponse> {
  return request(`${BASE}/boards/${encodeURIComponent(boardId)}/state`);
}

/**
 * 保存：每完成一次操作就写一次。**这个调用不经过 QIO**。
 *
 * `confirm`（M4）：影响确认句柄。带它表示用户已经为这次改动做过影响确认；
 * 服务端会校验 checkId 与当前已保存版本，过期就 409 stale_check（不落库）。
 */
export function saveBoardState(
  boardId: string,
  state: BoardState,
  reason = "op",
  confirm?: { checkId: string; stateVersion?: number },
): Promise<{ ok: boolean; seq: number; savedAt: string; state: BoardState }> {
  return request(`${BASE}/boards/${encodeURIComponent(boardId)}/state`, {
    method: "PUT",
    body: JSON.stringify({ state, reason, ...(confirm ? { confirm } : {}) }),
  });
}

export function fetchHistory(boardId: string): Promise<{ snapshots: unknown[] }> {
  return request(`${BASE}/boards/${encodeURIComponent(boardId)}/history`);
}

export function fetchSubmissions(boardId: string): Promise<{ submissions: SubmissionRecord[] }> {
  return request(`${BASE}/boards/${encodeURIComponent(boardId)}/submissions`);
}

export function fetchVisibleRange(boardId: string): Promise<{ visibleRange: VisibleRange }> {
  return request(`${BASE}/boards/${encodeURIComponent(boardId)}/visible-range`);
}

/** 提交：QIO 取得未提交表达的唯一入口。baseStateVersion = 本次候选所基于的服务器版本。 */
export function submitBoard(
  boardId: string,
  requestedVisible?: string[],
  note = "",
  baseStateVersion?: number,
  confirmedCheckId?: string,
): Promise<SubmissionResult> {
  return request(`${BASE}/boards/${encodeURIComponent(boardId)}/submissions`, {
    method: "POST",
    body: JSON.stringify({
      requestedVisible,
      note,
      ...(baseStateVersion !== undefined ? { baseStateVersion } : {}),
      ...(confirmedCheckId ? { confirmedCheckId } : {}),
    }),
  });
}

export function fetchDrafts(
  boardId: string,
): Promise<{ drafts: Record<string, string>; updatedAt: string | null }> {
  return request(`${BASE}/drafts/${encodeURIComponent(boardId)}`);
}

export function saveDrafts(
  boardId: string,
  drafts: Record<string, string>,
): Promise<{ drafts: Record<string, string>; updatedAt: string }> {
  return request(`${BASE}/drafts/${encodeURIComponent(boardId)}`, {
    method: "PUT",
    body: JSON.stringify({ drafts }),
  });
}

export function fetchIntents(boardId: string): Promise<IntentListResponse> {
  return request(`${BASE}/boards/${encodeURIComponent(boardId)}/intents`);
}

export function listIntentBoard(boardId: string): Promise<IntentListResponse> {
  return fetchIntents(boardId);
}

/**
 * 保存前的只读预判：这次改动会不会影响正在执行的任务（不改任何状态、不落库）。
 * 用户在保存前据此看到影响说明；取消就不保存，任务继续。
 */
export function previewMaterialImpact(
  boardId: string,
  state: BoardState,
): Promise<{ affected: { intentId: string; title: string; materials: string[]; consequence: string }[] }> {
  return request(`${BASE}/boards/${encodeURIComponent(boardId)}/material-impact`, {
    method: "POST",
    body: JSON.stringify({ state }),
  });
}

/**
 * 影响检查（M4，C 定稿）：保存前问服务端「这次候选会不会影响正在执行的任务」。
 *
 * 与旧的 material-impact 只读预判不同：命中影响时返回 `checkId`，
 * 用户确认后必须带着它保存，服务端据此拒绝过期确认。
 */
export interface ImpactCheckResult {
  ok: boolean;
  checkId?: string;
  stateVersion?: number;
  affected?: { intentId: string; title: string; status?: string; materials: string[]; consequence: string }[];
  affectedTasks?: { intentId: string; title: string; status?: string; materials: string[]; consequence: string }[];
  summary?: string;
  impactConfirmationRequired?: boolean;
  reason?: string;
  currentSeq?: number;
}

export function checkMaterialImpact(
  boardId: string,
  stateVersion: number,
  state: BoardState,
): Promise<ImpactCheckResult> {
  return request(`${BASE}/boards/${encodeURIComponent(boardId)}/impact-check`, {
    method: "POST",
    body: JSON.stringify({ stateVersion, changeSet: { state } }),
  });
}

export function createDemoIntents(boardId: string): Promise<{ created: Intent[]; demo: boolean }> {
  return request(`${BASE}/boards/${encodeURIComponent(boardId)}/intents`, {
    method: "POST",
    body: JSON.stringify({ demo: true }),
  });
}

export function approveIntent(intentId: string, confirmDependency = false): Promise<DecideResult> {
  return request(`${BASE}/intents/${encodeURIComponent(intentId)}/approve`, {
    method: "POST",
    body: JSON.stringify({ confirmDependency }),
  });
}

export function rejectIntent(intentId: string): Promise<DecideResult> {
  return request(`${BASE}/intents/${encodeURIComponent(intentId)}/reject`, { method: "POST" });
}

export function updateIntentPreview(
  intentId: string,
  preview: IntentPreview,
): Promise<{
  ok: boolean;
  intent?: Intent;
  requiresUpdate?: boolean;
  reason?: string;
  detail?: string;
}> {
  return request(`${BASE}/intents/${encodeURIComponent(intentId)}/preview`, {
    method: "POST",
    body: JSON.stringify({ preview }),
  });
}

/**
 * 演示执行推进（第一阶段没有真实执行；界面上明确标注为演示）。
 *
 * `revert_rest` 不是执行结果，而是「撤回会影响其他工作的那部分」的用户决定：
 * 只有在部分撤回留下 pendingDecision 时才会用到。
 */
export function advanceIntent(
  intentId: string,
  outcome: "done" | "failed" | "paused" | "cancelled" | "revert_rest",
): Promise<{ ok: boolean; intent?: Intent; revert?: unknown; detail?: string }> {
  return request(`${BASE}/intents/${encodeURIComponent(intentId)}/demo/advance`, {
    method: "POST",
    body: JSON.stringify({ outcome }),
  });
}

export function batchDecide(
  approve: string[],
  reject: string[],
): Promise<{ results: DecideResult[]; conflicts: string[][]; approved: string[]; rejected: string[] }> {
  return request(`${BASE}/intents/batch`, {
    method: "POST",
    body: JSON.stringify({ approve, reject }),
  });
}
