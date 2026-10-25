/**
 * 互动模式的 API 客户端。
 *
 * 契约：docs/interactive-mode-contract.md §2。
 * 路径与 `agent/api/interactive_store.py` / `interactive_intents.py` 一一对应。
 */
import { authHeaders, resolveBackend } from "./backend";
import type {
  AcceptanceLookup,
  BoardState,
  BoardStateResponse,
  DecideResult,
  Execution,
  ExecutionControlResult,
  FrozenView,
  Intent,
  IntentListResponse,
  IntentPreview,
  SubmissionResult,
  UnderstandingResponse,
  VisibleRange,
} from "../interactive/types";
import type {
  ConfirmResponse,
  IntentResultsResponse,
  IntentViewListResponse,
  LedgerSummaryView,
  RecoveryPreviewResponse,
  RecoveryResultView,
} from "../interactive/intentWorkbench";

/** 服务端错误体（M4 定稿）：error/reason 是给用户看的真实原因，其余是可核对的结构。 */
export interface InteractiveErrorPayload {
  error?: string;
  reason?: string;
  detail?: string;
  affectedTasks?: { intentId: string; title: string; materials: string[]; consequence: string }[];
  /**
   * R6（后端 B 定稿）：409 stale_check 时额外说明「这次拒绝是因为真实受影响范围变了」。
   * 缺失/忽略都安全（前端仍会重新预判并重新展示范围）。
   */
  scopeChanged?: boolean;
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
      const body = (await resp.json()) as { detail?: unknown } & InteractiveErrorPayload;
      const rawDetail = body?.detail;
      if (rawDetail && typeof rawDetail === "object") {
        // FastAPI 的 HTTPException(detail=<dict>) 会长成 { detail: { error, reason, ... } }
        payload = rawDetail as InteractiveErrorPayload;
        detail = payload.detail ?? payload.reason ?? payload.error ?? "";
      } else {
        payload = body as InteractiveErrorPayload;
        const text = typeof rawDetail === "string" ? rawDetail : "";
        detail = text || payload.reason || payload.error || "";
      }
    } catch {
      detail = await resp.text().catch(() => "");
    }
    throw new InteractiveApiError(resp.status, path, String(detail).slice(0, 300), payload);
  }
  return (await resp.json()) as T;
}

const BASE = "/api/interactive";

export function fetchBoardState(boardId: string): Promise<BoardStateResponse> {
  return request(`${BASE}/boards/${encodeURIComponent(boardId)}/state`);
}

/**
 * 保存：每完成一次操作就写一次。**这个调用不经过 QIO**。
 *
 * `confirm`（M4）：影响确认句柄。带它表示用户已经为这次改动做过影响确认；
 * 服务端会校验 checkId 与当前已保存版本，过期就 409 stale_check（不落库）。
 */
/** 保存回执（与后端 D 的 PUT /state 响应同形）。 */
export interface SaveBoardStateResult {
  ok: boolean;
  seq: number;
  savedAt: string;
  /** 服务端归一化后的权威状态：带 participation / collapsed / meta.display 与权威 version */
  state: BoardState;
  /** 保存时真实暂停的任务（旧链，可选） */
  materialImpact?: { paused?: Intent[] };
}

export function saveBoardState(
  boardId: string,
  state: BoardState,
  reason = "op",
  confirm?: { checkId: string; stateVersion?: number },
): Promise<SaveBoardStateResult> {
  return request(`${BASE}/boards/${encodeURIComponent(boardId)}/state`, {
    method: "PUT",
    body: JSON.stringify({ state, reason, ...(confirm ? { confirm } : {}) }),
  });
}

export function fetchVisibleRange(boardId: string): Promise<{ visibleRange: VisibleRange }> {
  return request(`${BASE}/boards/${encodeURIComponent(boardId)}/visible-range`);
}

/**
 * 提交：QIO 取得未提交表达的唯一入口。baseStateVersion = 本次候选所基于的服务器版本。
 *
 * 契约 §1.3：`clientRequestId` = 这次提交请求的**稳定身份**，由前端在发出请求之前生成
 * （见 `interactive/submission.ts`）。服务端按 (boardId, clientRequestId) 做幂等：
 * 同身份重试返回原事实，同身份但内容不同按冲突拒绝（409 request_identity_conflict）。
 * 传空 = 旧客户端行为（不参与幂等）。
 */
export function submitBoard(
  boardId: string,
  requestedVisible?: string[],
  note = "",
  baseStateVersion?: number,
  confirmedCheckId?: string,
  clientRequestId?: string,
  view?: FrozenView | null,
): Promise<SubmissionResult> {
  return request(`${BASE}/boards/${encodeURIComponent(boardId)}/submissions`, {
    method: "POST",
    body: JSON.stringify({
      requestedVisible,
      note,
      // F6：客户端冻结视图（板面坐标下可见区域 + scale）；量不到时显式传 null，
      // 服务端把它原样固化进本次快照的 geometry.view，绝不编造。
      view: view ?? null,
      ...(baseStateVersion !== undefined ? { baseStateVersion } : {}),
      ...(confirmedCheckId ? { confirmedCheckId } : {}),
      ...(clientRequestId ? { clientRequestId } : {}),
    }),
  });
}

/**
 * 契约 §1.3：按**精确请求身份**查证受理情况（受理情况不明时的唯一查证入口）。
 *
 * - 200 → `{ acceptance }`（事实字典，与 {@link AcceptanceLookup} 一一对应）；
 * - 404 → **不抛**，返回 `{ acceptance: null, reason }`：服务端没有这条身份的记录。
 *   调用方据此继续「结果未知」——不是错误推导的成功，也不是改判失败（§1.1.4 / §1.1.5）；
 * - 其它错误（断网 / 5xx）原样抛出：查询失败同样只说明「还没确认」，不能当成失败结论。
 */
export function fetchAcceptance(
  boardId: string,
  clientRequestId: string,
): Promise<{ acceptance: AcceptanceLookup | null; reason?: string }> {
  return request<{ acceptance: AcceptanceLookup | null; reason?: string }>(
    `${BASE}/boards/${encodeURIComponent(boardId)}/submissions/acceptance?clientRequestId=${encodeURIComponent(clientRequestId)}`,
  ).catch((err: unknown) => {
    if (err instanceof InteractiveApiError && err.status === 404) {
      return { acceptance: null, reason: err.payload?.reason ?? "服务端没有这个请求身份的记录" };
    }
    throw err;
  });
}

export interface DraftState {
  drafts: Record<string, string>;
  updatedAt: string | null;
  /** 服务端存簿版本（C1）：单调递增，用来证明「这次更新 / 删除基于哪一版」 */
  rev?: number;
  /** 逐键版本（C1）：与服务端一致的版本号才允许覆盖同一个键 */
  keyVersions?: Record<string, number>;
}

/** 逐键三态保存请求（契约 C1）：更新 / 明确删除 / 未修改。 */
export interface DraftSavePatch {
  /** 本次**确实上传**的逐键更新 */
  drafts: Record<string, string>;
  /** 本次**明确删除**的键（带删除身份） */
  removed?: string[];
  /** 客户端读到的服务端 rev：与服务端当前 rev 一致即「无并发改写」的安全基线 */
  baseRev?: number | null;
  /** 逐键版本：没有安全基线时，用它证明这次覆盖/删除基于当前那一版 */
  keyVersions?: Record<string, number>;
}

export interface DraftSaveResult extends DraftState {
  /** 这次保存**真的**清掉的键（服务端确认） */
  cleared?: string[];
  /** 同键冲突：服务端保留自己那份文字，同时把双方文字交回客户端 */
  conflicts?: { key: string; local: string; server: string }[];
  /** 无法安全删除的键：服务端保留原文字，等客户端处理 */
  blockedRemovals?: string[];
  /** true = 服务端按旧「整集合替换」语义处理了这次请求（旧客户端） */
  legacyReplace?: boolean;
  mode?: "patch" | "replace";
}

/**
 * 保存草稿（契约 C1）。
 *
 * 默认就是**逐键三态**：`drafts` 只放本次确实上传的键，`removed` 放明确删除的键，
 * 其余键是「未修改」，服务端绝不因此删除它们（F01 的根治点）。
 * 老调用方只传两个参数时同样走这条路径（removed 为空 = 纯更新）。
 */
export function saveDrafts(
  boardId: string,
  drafts: Record<string, string>,
  patch: Omit<DraftSavePatch, "drafts"> = {},
): Promise<DraftSaveResult> {
  return request(`${BASE}/drafts/${encodeURIComponent(boardId)}`, {
    method: "PUT",
    body: JSON.stringify({
      mode: "patch",
      drafts,
      ...(patch.removed ? { removed: patch.removed } : {}),
      ...(patch.baseRev !== undefined && patch.baseRev !== null ? { baseRev: patch.baseRev } : {}),
      ...(patch.keyVersions ? { keyVersions: patch.keyVersions } : {}),
    }),
  });
}

export function fetchIntents(boardId: string): Promise<IntentListResponse> {
  return request(`${BASE}/boards/${encodeURIComponent(boardId)}/intents`);
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
  /**
   * N3/N4（契约 §2）：只处理这次**明确展示给用户**的待决定项。
   * 不传 = 保持既有语义（由服务端决定），但服务端同样要按当前内容重核（N4）。
   */
  decisionIds?: string[],
): Promise<{ ok: boolean; intent?: Intent; revert?: unknown; detail?: string; reason?: string }> {
  return request(`${BASE}/intents/${encodeURIComponent(intentId)}/demo/advance`, {
    method: "POST",
    body: JSON.stringify({ outcome, ...(decisionIds ? { decisionIds } : {}) }),
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


// --- 真实模型理解与真实执行（第 9 轮，2026-10-11）-------------------------
// 契约：docs/interactive-mode-contract.md §14

/**
 * 创建意图（真实入口）：服务端只接受「已可靠受理的理解请求」产出的意图。
 *
 * 旧的前端「演示意图生成」入口（createDemoIntents）已随合并后清理删除；正式路径不显示演示内容。
 */
export function createIntents(
  boardId: string,
  submissionId?: string,
): Promise<{ created: Intent[]; demo: boolean; reason?: string; detail?: string }> {
  return request(`${BASE}/boards/${encodeURIComponent(boardId)}/intents`, {
    method: "POST",
    body: JSON.stringify(submissionId ? { submissionId } : {}),
  });
}

/** 读取本板面的理解过程（受理 / 理解中 / 已完成 / 需要补充 / 无需行动 / 失败）。 */
export function fetchUnderstanding(
  boardId: string,
  submissionId?: string,
): Promise<UnderstandingResponse> {
  const query = submissionId ? `?submissionId=${encodeURIComponent(submissionId)}` : "";
  return request(`${BASE}/boards/${encodeURIComponent(boardId)}/understanding${query}`);
}

/**
 * 重试失败的理解：服务端从**已保存的提交快照**恢复，不重读当前板面、
 * 不重新创建同一批任务。受理情况不明时先 fetchUnderstanding 确认，不盲目重发。
 */
export function retryUnderstanding(
  understandingId: string,
): Promise<{ ok: boolean; understanding?: unknown; reason?: string; detail?: string }> {
  return request(`${BASE}/understanding/${encodeURIComponent(understandingId)}/retry`, {
    method: "POST",
  });
}

export function fetchExecutions(boardId: string): Promise<{ executions: Execution[] }> {
  return request(`${BASE}/boards/${encodeURIComponent(boardId)}/executions`);
}

/** 暂停正在执行的任务：作用于真实执行记录；迟到结果不再写回。 */
export function pauseIntent(intentId: string): Promise<ExecutionControlResult> {
  return request(`${BASE}/intents/${encodeURIComponent(intentId)}/pause`, { method: "POST" });
}

/** 继续：由用户决定，不自动重试；继续时校验材料与已完成步骤。 */
export function resumeIntent(intentId: string): Promise<ExecutionControlResult> {
  return request(`${BASE}/intents/${encodeURIComponent(intentId)}/resume`, { method: "POST" });
}

export function cancelIntent(intentId: string): Promise<ExecutionControlResult> {
  return request(`${BASE}/intents/${encodeURIComponent(intentId)}/cancel`, { method: "POST" });
}

// --- 第三阶段 C：意图视图 / 批量确认 / 等待 / 账本 / 结果 / 恢复（契约 §13.5）--

/** 意图视图列表（目标 / 预期 / 依据 / 范围 / 依赖 / 冲突 / 版本 / 授权 / 真实状态）。 */
export function fetchIntentViews(boardId: string): Promise<IntentViewListResponse> {
  return request(`${BASE}/boards/${encodeURIComponent(boardId)}/intent-views`);
}

/** 单条确认：门槛由后端/前端强制（单条不走批量）。 */
export function confirmSingleIntent(intentId: string, boardId: string): Promise<ConfirmResponse> {
  return request(`${BASE}/intents/${encodeURIComponent(intentId)}/confirm`, {
    method: "POST",
    body: JSON.stringify({ boardId }),
  });
}

/**
 * 同批批量确认：至少两条（BATCH_CONFIRM_MIN，与后端同口径）。
 * 不足两条时服务端返回 400 batch_confirm_min；调用方必须先禁用按钮，不靠错误兜底。
 */
export function batchConfirmIntents(
  boardId: string,
  intentIds: string[],
): Promise<ConfirmResponse> {
  return request(`${BASE}/intents/batch-confirm`, {
    method: "POST",
    body: JSON.stringify({ boardId, intentIds }),
  });
}

/** 权威操作账本摘要（只有 applied 算完成）。 */
export function fetchLedger(boardId: string): Promise<LedgerSummaryView> {
  return request(`${BASE}/boards/${encodeURIComponent(boardId)}/ledger`);
}

/** 结果引用 + 完整正文（结果卡按文件 / Markdown 交付，不在这里截断）。 */
export function fetchIntentResults(boardId: string): Promise<IntentResultsResponse> {
  return request(`${BASE}/boards/${encodeURIComponent(boardId)}/results`);
}

/** 恢复预览（只读）：能续作什么、为什么不能、还有哪些未完成操作 / 依赖。 */
export function fetchRecoveryPreview(intentId: string): Promise<RecoveryPreviewResponse> {
  return request(`${BASE}/intents/${encodeURIComponent(intentId)}/recovery-preview`);
}

/** 恢复（按版本 CAS）：放回可继续状态，并启动真实续作 Turn；失败不在响应里伪装成功。 */
export function recoverIntent(
  intentId: string,
  boardId: string,
  expectedVersion: number,
): Promise<RecoveryResultView> {
  return request(`${BASE}/intents/${encodeURIComponent(intentId)}/recover`, {
    method: "POST",
    body: JSON.stringify({ boardId, expectedVersion }),
  });
}

/** 保存虚拟意图显示位置：只写 display，不推进语义版本或授权。 */
export function saveIntentDisplay(
  intentId: string,
  boardId: string,
  x: number,
  y: number,
): Promise<{ ok: boolean; display: { x: number; y: number } }> {
  return request(`${BASE}/intents/${encodeURIComponent(intentId)}/display`, {
    method: "POST",
    body: JSON.stringify({ boardId, x, y }),
  });
}

