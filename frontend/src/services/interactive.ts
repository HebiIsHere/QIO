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

export class InteractiveApiError extends Error {
  constructor(
    readonly status: number,
    readonly path: string,
    detail: string,
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
    let detail = "";
    try {
      const payload = (await resp.json()) as { detail?: string };
      detail = payload?.detail ?? "";
    } catch {
      detail = await resp.text().catch(() => "");
    }
    throw new InteractiveApiError(resp.status, path, detail.slice(0, 300));
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

/** 保存：每完成一次操作就写一次。**这个调用不经过 QIO**。 */
export function saveBoardState(
  boardId: string,
  state: BoardState,
  reason = "op",
): Promise<{ ok: boolean; seq: number; savedAt: string; state: BoardState }> {
  return request(`${BASE}/boards/${encodeURIComponent(boardId)}/state`, {
    method: "PUT",
    body: JSON.stringify({ state, reason }),
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

/** 提交：QIO 取得未提交表达的唯一入口。 */
export function submitBoard(
  boardId: string,
  requestedVisible?: string[],
  note = "",
): Promise<SubmissionResult> {
  return request(`${BASE}/boards/${encodeURIComponent(boardId)}/submissions`, {
    method: "POST",
    body: JSON.stringify({ requestedVisible, note }),
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

/** 演示执行推进（第一阶段没有真实执行；界面上明确标注为演示）。 */
export function advanceIntent(
  intentId: string,
  outcome: "done" | "failed" | "paused" | "cancelled",
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
