/** 后端 API 客户端（本机 HTTP + 会话令牌）。 */
import { authHeaders, resetBackend, resolveBackend } from "./backend";
import { takeNextSendRequestId } from "./sendIdentity";

/** API 错误：带上状态码，调用方才能区分「没权限」和「真的坏了」。 */
export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly path: string,
    detail: string,
    /**
     * 响应体解析出来的结构（能解析成 JSON 才有）。
     * 结构化失败（例如附件没附上）靠它拿到逐条原因 —— 光有一句话的 detail
     * 让调用方只能把 JSON 当字符串显示。
     */
    readonly body?: unknown,
  ) {
    super(
      status === 401 || status === 403
        ? `${path} -> ${status}: 本机 API 拒绝了这次请求（会话令牌缺失或已失效）`
        : `${path} -> ${status}: ${detail}`,
    );
    this.name = "ApiError";
  }
}

/** 请求超时（已取消）：如实说「没有响应」，调用方据此决定是否重试。 */
export class ApiTimeoutError extends Error {
  constructor(
    readonly path: string,
    readonly timeoutMs: number,
  ) {
    super(`${path} 在 ${Math.round(timeoutMs / 1000)} 秒内没有响应（这次请求已取消，可以重试）`);
    this.name = "ApiTimeoutError";
  }
}

/**
 * 用途匹配的超时（毫秒）。
 *
 * 不套同一个短超时：会调外部模型/凭据服务的长操作几十秒是正常的，
 * 而历史页、工具全文这类大对象也比普通读慢。写操作给的等待比读长一点，
 * 但**超时后绝不自动重试**（见 `request`）。
 */
export const API_TIMEOUT_MS = {
  /** 普通读：设置、列表、健康类 */
  read: 15_000,
  /** 大对象 / 权威状态 / 历史页：后端忙的时候也要给足时间 */
  bulk: 45_000,
  /** 写：可能已经生效，只如实报错，不自动重复提交 */
  write: 20_000,
  /** 会调外部模型或凭据服务的长操作 */
  long: 90_000,
} as const;

export interface RequestOptions extends RequestInit {
  /** 这次请求允许等多久（毫秒）。默认按方法取：GET = read，其余 = write。 */
  timeoutMs?: number;
  /** 读类请求的有限重试次数；写操作固定 0。 */
  retries?: number;
}

/** 读类请求可以有限重试的失败：超时、网络层失败、（重新解析后的）认证失效。 */
function isRetryableFailure(err: unknown): boolean {
  if (err instanceof ApiTimeoutError) return true;
  if (err instanceof ApiError) return err.status === 401 || err.status === 403;
  return err instanceof TypeError; // fetch 的网络层失败
}

async function requestOnce<T>(
  path: string,
  init: RequestOptions,
  timeoutMs: number,
): Promise<T> {
  const { base, token } = await resolveBackend();
  const controller = new AbortController();
  const external = init.signal ?? null;
  const onExternalAbort = () => controller.abort();
  if (external) {
    if (external.aborted) controller.abort();
    else external.addEventListener("abort", onExternalAbort, { once: true });
  }
  let timedOut = false;
  const timer = setTimeout(() => {
    timedOut = true;
    controller.abort();
  }, timeoutMs);
  try {
    const resp = await fetch(`${base}${path}`, {
      ...init,
      signal: controller.signal,
      headers: {
        "Content-Type": "application/json",
        ...authHeaders(token),
        ...((init.headers as Record<string, string> | undefined) ?? {}),
      },
    });
    if (!resp.ok) {
      const text = await resp.text();
      // 认证明确失效：令牌可能已经轮换、或后端换了实例 → 下一次请求重新解析地址与令牌
      if (resp.status === 401 || resp.status === 403) resetBackend();
      let body: unknown;
      try {
        body = JSON.parse(text) as unknown;
      } catch {
        body = undefined; // 不是 JSON（代理页 / 纯文本错误）：保持 undefined，不伪造结构
      }
      throw new ApiError(resp.status, path, text.slice(0, 200), body);
    }
    return (await resp.json()) as T;
  } catch (err) {
    if (timedOut) throw new ApiTimeoutError(path, timeoutMs);
    throw err;
  } finally {
    clearTimeout(timer);
    external?.removeEventListener("abort", onExternalAbort);
  }
}

/**
 * 一次请求。
 *
 * 读类（GET）失败可以有限重试（默认 1 次）：超时、网络层失败、认证失效后重新解析。
 * **写操作（发送 / 审批 / 工具执行 / 设置）永远不自动重试** —— 超时只说明「没等到响应」，
 * 操作可能已经生效，自动重发会变成重复提交；调用方要么查询原操作状态，要么用既有的
 * 幂等标识（例如 turn 的一次性 claim）。
 */
export async function request<T>(path: string, init: RequestOptions = {}): Promise<T> {
  const method = (init.method ?? "GET").toUpperCase();
  const isRead = method === "GET";
  const timeoutMs = init.timeoutMs ?? (isRead ? API_TIMEOUT_MS.read : API_TIMEOUT_MS.write);
  const retries = isRead ? Math.max(0, init.retries ?? 1) : 0;
  let attempt = 0;
  for (;;) {
    try {
      return await requestOnce<T>(path, init, timeoutMs);
    } catch (err) {
      const canRetry =
        attempt < retries && !init.signal?.aborted && isRetryableFailure(err);
      if (!canRetry) throw err;
      attempt += 1;
    }
  }
}

export interface CredentialMeta {
  key_id: string;
  version: number;
  tags: string[];
  endpoint: string | null;
  default_model: string | null;
  budget: number | null;
  budget_used: number;
  /**
   * 真实调用累计的「进 / 出」token（分开记，便于说明钱花在哪一头）。
   * `budget_used` 仍是两者合计，上限比较只认它。老数据 / 老响应可能没有这两个字段。
   */
  usage_input?: number;
  usage_output?: number;
  status: string;
  enabled: boolean;
  note: string | null;
  /** 连接协议：openai（兼容接口）| anthropic；老数据可能为空，按地址判断 */
  kind?: string | null;
  /** unverified 尚未验证 | verified 已验证可用 | failed 上次没通过 | legacy 老数据 */
  verify_state?: string;
  verified_at?: string | null;
  verify_error?: string | null;
  /** 用户显式设定的默认主对话凭据（同一时刻只有一条） */
  is_default?: boolean;
  provider_id?: string | null;
  provider_name?: string | null;
}

/** 厂商预设：名称 / 协议 / 地址 / 建议模型的唯一来源（后端下发）。 */
export interface ProviderPreset {
  id: string;
  name: string;
  kind: string;
  base_url: string;
  suggested_model: string;
  /** official 官方服务 | aggregator 第三方转发 | custom 自定义 */
  category: string;
  category_label: string;
  note: string;
}

/** 一次可用性验证的结果。「保存成功」与「验证通过」是两件事。 */
export interface VerifyReport {
  ok: boolean;
  state: string;
  reason_code: string | null;
  message: string;
  detail: string;
  mode: string | null;
  /** true = 只是临时故障，这条凭据原来的状态保持不变 */
  state_kept?: boolean;
}

export interface CredentialCreateResult {
  ok: boolean;
  saved: boolean;
  idempotent?: boolean;
  key_id: string;
  version: number;
  credential: CredentialMeta;
  verify: VerifyReport;
}

export interface SearchSettings {
  searxng_url: string;
  bocha_has_key: boolean;
  /** 免密钥通道（Exa / Parallel 免费 MCP + DuckDuckGo HTML），默认开启 */
  keyless_fallback?: boolean;
  top_k_default: number;
  max_fetch_chars: number;
}

export interface ComputerSettings {
  root_dir: string;
  permission_mode: string;
}

/**
 * 工具调用历史（历史接口随消息带回来的预览，不含输出全文）。
 * 全文走 `getToolRecord(id)`，只在用户展开某张卡片时才取。
 */
export interface ToolRecordPreview {
  id: string;
  turn_id: string;
  call_id: string;
  seq: number;
  tool_name: string;
  /** 中文展示名（后端按工具名现算） */
  title: string;
  status: string;
  error: string;
  duration_ms: number | null;
  truncated: boolean;
  output_missing: boolean;
  /** '' | 'setting'（关闭了保存全文） | 'retention'（按保留期清掉） */
  missing_reason: string;
  /** 库里实际存下的字符数 */
  output_chars: number;
  preview: string;
  created_at: string;
}

export interface ToolRecordFull extends ToolRecordPreview {
  arguments: unknown;
  output: string;
}

export interface ToolHistorySettings {
  record_outputs: boolean;
  output_retention_days: number;
  /** 保存设置时顺带清掉的输出条数（只有 PUT 会带） */
  purged?: number;
}

export interface LoopSettings {
  max_iterations: number;
  output_token_budget: number;
}

export interface TraceSummary {
  turn_id: string;
  status: string;
  started_at: string;
  ended_at: string | null;
  duration_ms: number | null;
  initial_topic: string | null;
  final_topic: string | null;
  error: string | null;
}

export interface TraceDetail extends TraceSummary {
  topic: Record<string, unknown> | null;
  injection: Record<string, unknown> | null;
  model_calls: Record<string, unknown>[] | null;
  tool_runs: Record<string, unknown>[] | null;
  writes: Record<string, unknown> | null;
  warnings: Record<string, unknown>[] | null;
  final_preview: string;
}

export interface KnowledgeItem {
  id: string;
  category: string;
  state: string;
  content: string;
  confidence: number | null;
  topic_id: string | null;
  topic_name: string | null;
  /** 从哪来（引导 / 对话 / 你的修正 / 后台整理 / 未记录） */
  source?: string;
  /** 管多大范围（全局（你） / 话题：X / 实体：Y / 未指定） */
  scope?: string;
  /** 是否已结束：不再是当前状态，但相关内容仍能被参考到 */
  ended?: boolean;
  ended_at?: string | null;
  created_at: string;
  updated_at: string;
}

export interface EntityAttribute {
  key: string;
  value: string;
  confidence: number;
}

export interface EntityRelation {
  type: string;
  target: string;
}

export interface EntityCard {
  id: string;
  node_id: string | null;
  name: string;
  aliases: string[];
  kind: string | null;
  summary: string;
  attributes: EntityAttribute[];
  relations: EntityRelation[];
  state: string;
  created_at: string;
  updated_at: string;
}

/**
 * 一轮的**终态词**（契约 §七 C2）。
 *
 * incomplete 只用于「不完整 EOF」：流在结束标记（OpenAI 的 finish_reason /
 * Anthropic 的 message_stop）之前就断了 —— 已确认正文保留，但**不是完成**。
 * 厂商合法的 length_limit / content_filter 仍是 completed，只用 reason_code 区分。
 */
export type TurnEndStatus =
  | "completed"
  | "failed"
  | "cancelled"
  | "stopped"
  | "unavailable"
  | "incomplete";

/**
 * 一轮的结束事实（turn_facts，历史分页 / RESYNC 快照里的同一份形状）。
 *
 * 字段与后端台账一一对应，前端**只消费、不编造**：台账里没有事实的轮次不会出现在
 * 这个数组里，界面因此不会显示一个假原因。incomplete 也在这里如实带回 ——
 * 刷新 / 换设备之后仍然是「未完成 + 原因 + retry」。
 */
export interface TurnFactsRow {
  turn_id: string;
  /**
   * 终态词：已知取值见 TurnEndStatus（含 incomplete）；未知词按原样收下，
   * 由 store 归一化 —— 界面不认识的词绝不会被当成「已完成」。
   */
  status?: TurnEndStatus | (string & {}) | null;
  duration_ms?: number | null;
  queue_ms?: number | null;
  started_at?: string | null;
  ended_at?: string | null;
  reason?: string | null;
  reason_code?: string | null;
  stopped_by?: string | null;
  actions?: string[] | null;
  error?: string | null;
  message?: string | null;
}

/**
 * 一条「已经被后端接受、但没有执行完」的用户消息（后端 `turn_journal` 台账）。
 *
 * 语义（见 storage/turn_journal.py）：进程退出时还在 `queued` / `running` 的行
 * 会被标成 `interrupted`，**不会自动重放**；消息原文留在台账里，由用户明确决定
 * 「继续发送」或「忽略」。`completed` / `cancelled` 等终态永远不会出现在这里。
 */
export interface InterruptedTurn {
  turn_id: string;
  /** 用户当时发的原文。界面必须如实显示（截断也要看得见） */
  message: string;
  topic_id?: string | null;
  status: string;
  /** 机器可读原因：queued_at_restart | running_at_restart | shutdown */
  reason?: string | null;
  /** 后端给的人话说明（reason 的中文）；老数据可能为空 */
  reason_text?: string;
  created_at?: string | null;
  started_at?: string | null;
  ended_at?: string | null;
  updated_at?: string | null;
}

export const api = {
  listCredentials: () =>
    request<{ credentials: CredentialMeta[]; default_key_id: string | null }>(
      "/api/credentials",
    ),
  listProviders: () =>
    request<{ providers: ProviderPreset[]; model_note: string }>(
      "/api/credentials/providers",
    ),
  createCredential: (payload: Record<string, unknown>, signal?: AbortSignal) =>
    request<CredentialCreateResult>("/api/credentials", {
      method: "POST",
      body: JSON.stringify(payload),
      signal,
      // 保存前会真的连一次服务商：属于长操作，不能套普通读的超时
      timeoutMs: API_TIMEOUT_MS.long,
    }),
  /** 重试验证：作用在同一条记录上，不会重复创建凭据 */
  verifyCredential: (keyId: string, signal?: AbortSignal) =>
    request<{ ok: boolean; key_id: string; verify: VerifyReport }>(
      `/api/credentials/${encodeURIComponent(keyId)}/verify`,
      { method: "POST", signal, timeoutMs: API_TIMEOUT_MS.long },
    ),
  /** 保存前验证一份草稿（换钥用）；只请求给定的服务地址，不落库 */
  verifyCredentialDraft: (payload: {
    secret: string;
    endpoint: string;
    default_model: string;
    kind?: string;
  }, signal?: AbortSignal) =>
    request<{ ok: boolean; verify: VerifyReport }>("/api/credentials/verify-draft", {
      method: "POST",
      body: JSON.stringify(payload),
      signal,
      timeoutMs: API_TIMEOUT_MS.long,
    }),
  listCredentialModels: (params: { endpoint: string; kind?: string; keyId?: string; secret?: string }, signal?: AbortSignal) => {
    const query = new URLSearchParams({ endpoint: params.endpoint });
    if (params.kind) query.set("kind", params.kind);
    if (params.keyId) query.set("key_id", params.keyId);
    if (params.secret) query.set("secret", params.secret);
    return request<{ models: string[]; note?: string }>(
      `/api/credentials/models?${query.toString()}`,
      { signal, timeoutMs: API_TIMEOUT_MS.long },
    );
  },
  setCredentialDefault: (keyId: string) =>
    request<{ ok: boolean; credential: CredentialMeta }>(
      `/api/credentials/${encodeURIComponent(keyId)}/default`,
      { method: "POST" },
    ),
  revokeCredential: (keyId: string) =>
    request<{ ok: boolean }>(`/api/credentials/${encodeURIComponent(keyId)}/revoke`, {
      method: "POST",
    }),
  deleteCredential: (keyId: string) =>
    request<{ ok: boolean; key_id: string }>(
      `/api/credentials/${encodeURIComponent(keyId)}`,
      { method: "DELETE" },
    ),
  updateCredentialMeta: (keyId: string, payload: Record<string, unknown>, signal?: AbortSignal) =>
    request<{ ok: boolean; credential: CredentialMeta; verify: VerifyReport | null }>(
      `/api/credentials/${encodeURIComponent(keyId)}`,
      { method: "PATCH", body: JSON.stringify(payload), signal },
    ),
  setCredentialEnabled: (keyId: string, enabled: boolean) =>
    request<{ ok: boolean; credential: CredentialMeta }>(
      `/api/credentials/${encodeURIComponent(keyId)}/${enabled ? "enable" : "disable"}`,
      { method: "POST" },
    ),
  getCredentialAudit: (keyId: string) =>
    request<{ ok: boolean; audit: { id: string; action: string; from_version: number | null; to_version: number | null; triggered_by: string | null; created_at: string }[] }>(
      `/api/credentials/${encodeURIComponent(keyId)}/audit`,
    ),
  testCredential: (keyId: string) =>
    request<{ key_id: string; verify: VerifyReport; probe: { mode: string; detail: string } }>(
      `/api/credentials/${encodeURIComponent(keyId)}/test`,
      { method: "POST", timeoutMs: API_TIMEOUT_MS.long },
    ),
  /**
   * 取消一个**准备中**的轮次（幂等；契约 §1.1）。
   *
   * 返回的是**服务端事实**，前端据此如实显示：
   * * cancelled=true → 这一轮已被放弃（不入队、不调用模型）；
   * * already_started=true → 已经放行/开始，必须走既有停止流程（不得假装没发送）；
   * * unknown=true → 未知/已过期标识（不报错）。
   */
  cancelPreparing: (prepareId: string) =>
    request<{ ok: boolean; cancelled?: boolean; already_started?: boolean; unknown?: boolean; turn_id?: string }>(
      `/api/turns/prepare/${encodeURIComponent(prepareId)}/cancel`,
      { method: "POST" },
    ),

  /**
   * 提交一轮。
   *
   * `retryOfTurnId` 只在「重试/重发某一轮」时给：后端据此允许把**原来绑在那一轮**的
   * 附件克隆到新一轮（新 id + 复用已保存副本）。不给的话，原轮的附件已经属于别的一轮，
   * 后端只能拒绝 —— 旧实现正是这里漏了参数，导致「界面有附件、模型实际没有」。
   */
  sendTurn: (
    message: string,
    topicId?: string | null,
    attachmentIds?: string[],
    retryOfTurnId?: string | null,
    /**
     * 准备期间请求是**挂起**的：调用方（输入区）用它在「正在准备附件…」时中止这次请求。
     * 真 abort 才有用 —— 后端据此判定客户端断开并 abandon 预留（契约 §1.1）。
     */
    signal?: AbortSignal,
    /**
     * 准备标识（契约 §1.1）：用户点「中止」时前端用它调取消端点，**以后端确认为准**。
     * abort 只是客户端行为，不能当后端证据 —— 所以准备标识必须随请求发给后端。
     */
    prepareId?: string,
  ) => {
      // 请求身份（幂等键）：发送方在调用前用 setNextSendRequestId 挂上，这里取走。
      // 同一次发送动作（含重试）永远复用同一个 id —— 后端据此保证幂等命中也回 200、
      // 绝不产生第二次执行；不挂 id 的调用保持旧行为（body 里没有这个字段）。
      const requestId = takeNextSendRequestId();
      return request<{
      ok: boolean;
      accepted: boolean;
      /** 服务端确认「这一轮没有被受理执行」（用户中止 / 断连）—— 界面不得显示成已发送 */
      cancelled?: boolean;
      prepare_id?: string | null;
      /** 受理时就有：乐观消息关联与「停止」都直接用它，不必等 TURN_START */
      turn_id: string;
      status: string;
      topic_id: string | null;
      /** true = 这个 client_request_id 之前已受理过：这是同一次发送的回执，不是新一轮 */
      deduplicated?: boolean;
      /** 本轮真实绑定到的附件（受理回执；前端以它为准） */
      bound_attachment_ids?: string[];
      /** 旧形状的回执：真实绑定到的附件 payload（id 就是事实） */
      attachments?: { id?: string }[];
      /** 没绑上的附件与原因（严格语义下非空即整轮被拒） */
      rejected?: { id: string; reason: string }[];
    }>("/api/turns", {
      method: "POST",
      signal,
      headers: prepareId ? { "X-QIO-Prepare-Id": prepareId } : undefined,
      body: JSON.stringify({
        message,
        topic_id: topicId ?? null,
        ...(requestId ? { client_request_id: requestId } : {}),
        // 重试/重发：告诉后端这些附件原来属于哪一轮（克隆复用的唯一凭据）
        ...(retryOfTurnId ? { retry_of_turn_id: retryOfTurnId } : {}),
        // 附件随这一轮绑定（契约 §1.4）：attachment_ids 的**存在性**即语义 ——
        // 只要调用方给了这个参数就一律带上，**包括空数组**（= 这一轮没有附件）。
        // 以前写成 attachmentIds?.length ? {...} : {}：空数组被省略成「缺字段」，
        // 后端于是走旧客户端兜底，把话题下的遗留附件绑到这条纯文字消息上（审计问题 3）。
        // 只有完全没传这个参数（undefined）才省略字段：那是真正的旧客户端路径。
        ...(attachmentIds === undefined ? {} : { attachment_ids: attachmentIds }),
      }),
    });
  },
  /**
   * 查证一次「发送动作」在后端有没有记录 —— 发送回执丢失（超时 / 断网）后的
   * 唯一恢复通道，请求身份就是发送时的 client_request_id。
   *
   * 200 { turn_id, status } = 已受理（幂等命中也算）；
   * 404 { ok:false, unknown:true } = 本进程没有这次请求的记录。
   * 进程重启 = 记录丢失 = 404：它只代表「未确认」，**不等于**「未发送」。
   */
  lookupTurnByRequest: (clientRequestId: string) =>
    request<
      { turn_id: string; status: string; deduplicated?: boolean } | { ok: false; unknown: true }
    >(`/api/turns/by-request/${encodeURIComponent(clientRequestId)}`, {
      timeoutMs: API_TIMEOUT_MS.read,
    }),
  getInstance: () =>
    request<{ instance_id: string; pid: number; auth_required: boolean; version: string }>(
      "/api/instance",
    ),
  /**
   * 应答一次审批。
   *
   * `binding` 带上这次审批原本的身份（turn / session / request digest）：
   * 后端据此确认「这次批准就是为这次具体请求发的」。正常用户点击允许完全不受影响，
   * 只有审批与当前请求已经不匹配时才会被拒绝。
   */
  respondApproval: (
    approvalId: string,
    decision: string,
    overrides?: Record<string, unknown>,
    binding?: { turnId?: string | null; sessionId?: string | null; requestDigest?: string | null },
  ) =>
    request<{ ok: boolean }>(`/api/approvals/${encodeURIComponent(approvalId)}/respond`, {
      method: "POST",
      body: JSON.stringify({
        decision,
        ...(overrides ? { overrides } : {}),
        ...(binding?.turnId ? { turn_id: binding.turnId } : {}),
        ...(binding?.sessionId ? { session_id: binding.sessionId } : {}),
        ...(binding?.requestDigest ? { request_digest: binding.requestDigest } : {}),
      }),
    }),
  /**
   * 重发一条「上次没有执行」的消息（按原话题重新提交一轮）。
   *
   * 后端用一次性 claim 抢占：同一条不可能被重发两次，已经完成的 turn 也不可能被重发。
   * 抢不到 / 不在未执行状态 → **409**，调用方必须给出可读反馈，不能静默失败。
   */
  resendInterruptedTurn: (turnId: string) =>
    request<{ ok: boolean; recovered_turn_id: string; turn_id: string; status: string }>(
      `/api/turns/${encodeURIComponent(turnId)}/resend`,
      { method: "POST" },
    ),
  /**
   * 用户选择「忽略」：不再提示，但台账记录与消息原文都保留（不删用户数据）。
   * 不在「未执行」状态同样返回 409。
   */
  dismissInterruptedTurn: (turnId: string) =>
    request<{ ok: boolean; dismissed: string }>(
      `/api/turns/${encodeURIComponent(turnId)}/dismiss`,
      { method: "POST" },
    ),
  cancelTurn: (turnId: string) =>
    request<{ ok: boolean; cancelled: boolean; turn_id: string }>(
      `/api/turns/${encodeURIComponent(turnId)}/cancel`,
      { method: "POST" },
    ),
  /**
   * 取消「当前真正在运行的主 turn」。
   * 用于收到 TURN_START 之前（还不知道 turn_id）的停止动作：后端只会取消 active，
   * 不会误伤排队中的 turn。
   */
  cancelActiveTurn: () =>
    request<{ ok: boolean; cancelled: boolean; turn_id: string | null }>(
      "/api/turns/cancel",
      { method: "POST" },
    ),
  /**
   * 权威队列快照（运行中 / 排队中 / revision）。
   * 事件流只是增量；一旦收到 RESYNC（说明事件流可能不完整），
   * 就用这个接口重新取权威状态，而不是靠猜。
   */
  getTurnQueue: () =>
    request<{
      running: { turn_id: string; message: string } | null;
      queued: { turn_id: string; message: string }[];
      cancelled: { turn_id: string; message: string }[];
      revision: number;
      instance_id?: string | null;
    }>("/api/turns/queue", { timeoutMs: API_TIMEOUT_MS.bulk }),
  /**
   * RESYNC 之后要恢复的**全部**权威状态。
   *
   * 事件流只能表达增量：断线期间错过的审批、独立任务、turn 队列都必须能查回来，
   * 否则界面会永久停在错误状态（审批永远不出现、任务卡永远「进行中」）。
   */
  getRuntimeState: () =>
    request<{
      instance_id: string;
      revision: number;
      turn_queue: {
        instance_id?: string | null;
        revision: number;
        running: { turn_id: string; message: string } | null;
        queued: { turn_id: string; message: string }[];
        cancelled: { turn_id: string; message: string }[];
      };
      /**
       * 上一次进程结束时仍没人回答的审批：不会再恢复等待，只说清「那次操作没有执行」。
       */
      interrupted_approvals?: {
        approval_id: string;
        kind: string;
        what: string;
        turn_id?: string | null;
        created_at: string;
        expires_at?: string | null;
        outcome: string;
      }[];
      /**
       * 上一次进程结束时**已经被接受、但没有执行完**的用户消息。
       *
       * 后端只给 `interrupted` 且还没被用户处理过的行（终态与系统通知轮都不在其中），
       * 前端照单渲染，不自己推断「这条算不算没做完」。
       */
      interrupted_turns?: InterruptedTurn[];
      /**
       * 孤儿重发：被抢占过（`recovered_at` 有值）、但**没有写成任何后继**
       * （`recovered_by` 为空）的记录。
       *
       * 它们不在 `interrupted_turns` 里（后端已经算它们「被处理过」），所以如果
       * 没有这个出口，那条消息就永久消失、用户点都点不到。前端把这份清单与
       * `/api/recovery/records` **并入同一个收件箱**（见 stores/restore.ts）：
       * 即使专用接口失败了，这些记录也必须在界面上看得见。
       */
      orphaned_turns?: InterruptedTurn[];
      approvals: {
        approval_id: string;
        kind: string;
        payload: Record<string, unknown>;
        turn_id?: string | null;
        session_id?: string | null;
        request_digest?: string | null;
      }[];
      tasks: {
        task_id: string;
        tool: string;
        status: "queued" | "running" | "done" | "failed";
        ok?: boolean | null;
        content_preview?: string;
        error?: string | null;
      }[];
      /**
       * 工具执行的权威事实（活工具 + 最近结束的工具）。
       *
       * `TOOL_END` 可能丢在失真区间里，但最终是 success / failed / cancelled
       * 是服务器已经知道的事实 —— 界面据此恢复真实状态，
       * 只有服务器也拿不出记录（`unknown`）时才显示「结果未收到」。
       */
      tools?: {
        turn_id?: string | null;
        tool_call_id: string;
        tool_name?: string;
        status: "running" | "success" | "failed" | "cancelled" | "unknown";
        started_at?: string | null;
        ended_at?: string | null;
        error_summary?: string | null;
      }[];
      /**
       * 本轮的执行叙事（模型文案 + 系统生成的调用摘要）。
       * 断线期间丢失的叙事在这里补齐，客户端按 `narrative_id` 去重。
       */
      narratives?: {
        narrative_id: string;
        turn_id?: string | null;
        kind?: string;
        text?: string;
        calls?: {
          call_id?: string;
          tool?: string;
          title?: string;
          status?: string;
          error?: string | null;
          duration_ms?: number | null;
        }[];
        created_at?: string | null;
      }[];
      /**
       * 当前相关轮次的结束事实（运行中 / 排队中 / 刚取消 / 上次进程留下的未完成轮）。
       * 与历史分页同一份形状：incomplete 在这里也必须原样带回来。
       */
      turn_facts?: TurnFactsRow[];
    }>("/api/runtime/state", { timeoutMs: API_TIMEOUT_MS.bulk }),
  /**
   * 可恢复记录收件箱（A01 + A03 的后端出口，见 `api/recovery_routes.py`）。
   *
   * `suffix` 由 `services/recoveryApi.ts` 拼好（limit / kinds / classes）：
   * 这里不做参数推断，避免两个地方各猜一次。返回值形状由调用方做形状校验。
   */
  getRecoveryRecords: (suffix = "") =>
    request<{
      records: unknown[];
      total: number;
      shown: number;
      truncated: boolean;
    }>(`/api/recovery/records${suffix}`, { timeoutMs: API_TIMEOUT_MS.bulk }),
  /**
   * 「继续这一条」：后端在单事务里接管 + 沿用既有可靠重发规则。
   * 抢不到（已经被处理过 / 并发）→ 409，调用方必须如实反馈，不能静默。
   */
  continueRecovery: (
    recordId: string,
    expected: { expected_class: string; expected_status?: string },
    timeoutMs: number = API_TIMEOUT_MS.write,
  ) =>
    request<{ ok: boolean; record_id: string; turn_id: string; status: string }>(
      `/api/recovery/records/${encodeURIComponent(recordId)}/continue`,
      {
        method: "POST",
        body: JSON.stringify({
          expected_class: expected.expected_class,
          ...(expected.expected_status ? { expected_status: expected.expected_status } : {}),
        }),
        timeoutMs,
      },
    ),
  /**
   * 修复孤儿重发：只作用于「interrupted + 用户行 + recovered_at 非空 +
   * recovered_by 空」这一精确状态。修复本身不改消息原文、不新建 turn。
   */
  repairOrphanRecord: (recordId: string, expected: { expected_class: string }) =>
    request<{ ok: boolean; repaired: boolean; record_id: string; reason?: string }>(
      `/api/recovery/records/${encodeURIComponent(recordId)}/repair`,
      { method: "POST", body: JSON.stringify({ expected_class: expected.expected_class }) },
    ),
  /**
   * F01：「确认写下这条无归属记录的旧执行者已经停止」。
   *
   * 旧版本（升级前）不写实例 / 心跳 / 归属，所以库里**没有证据**说明它停了；
   * 这是唯一能把这条记录从「只可见」变成「可操作」的入口（用户显式承担判断）。
   */
  confirmStoppedRecord: (recordId: string, expected: { expected_class?: string }) =>
    request<{
      ok: boolean;
      confirmed: boolean;
      already_confirmed: boolean;
      confirmed_at: string;
      record_id: string;
      kind: string;
    }>(`/api/recovery/records/${encodeURIComponent(recordId)}/confirm-stopped`, {
      method: "POST",
      body: JSON.stringify(
        expected.expected_class ? { expected_class: expected.expected_class } : {},
      ),
    }),
  /** 「忽略这一条」：标记用户已知晓，不删除原文、不产生后继。 */
  ignoreRecoveryRecord: (recordId: string, expected: { expected_class: string }) =>
    request<{ ok: boolean; ignored: boolean; record_id?: string }>(
      `/api/recovery/records/${encodeURIComponent(recordId)}/ignore`,
      { method: "POST", body: JSON.stringify({ expected_class: expected.expected_class }) },
    ),
  /**
   * 把归属已死的派生任务放回待执行。
   * `attempts` / `last_error` 由后端原样保留，迟到写由 claim_generation 挡住。
   */
  requeueDerivedRecord: (
    recordId: string,
    expected: { expected_state: string; expected_generation: number | null },
  ) =>
    request<{ ok: boolean; record_id?: string; state: string }>(
      `/api/recovery/records/${encodeURIComponent(recordId)}/requeue`,
      {
        method: "POST",
        body: JSON.stringify({
          expected_state: expected.expected_state,
          expected_generation: expected.expected_generation,
        }),
      },
    ),
  listTraces: (limit = 50, offset = 0) =>
    request<{ traces: TraceSummary[]; total: number; limit: number; offset: number }>(
      `/api/traces?limit=${limit}&offset=${offset}`,
      { timeoutMs: API_TIMEOUT_MS.bulk },
    ),
  getTrace: (turnId: string) =>
    request<TraceDetail>(`/api/traces/${encodeURIComponent(turnId)}`, {
      timeoutMs: API_TIMEOUT_MS.bulk,
    }),
  getTraceSettings: () =>
    request<{ enabled: boolean }>("/api/settings/trace"),
  updateTraceSettings: (enabled: boolean) =>
    request<{ enabled: boolean }>("/api/settings/trace", {
      method: "PUT",
      body: JSON.stringify({ enabled }),
    }),
  setAnchor: (topicId: string, fragmentId?: string | null) =>
    request<{
      ok: boolean;
      topic_id: string;
      fragment_id: string | null;
      /** 权威标题（memory_index / 摘要首句）；前端不用摘要自己拼 */
      fragment_title: string | null;
      /** 是否是「历史位置」（不是当前开放片段） */
      historic: boolean;
    }>("/api/anchor", {
      method: "POST",
      body: JSON.stringify({ topic_id: topicId, fragment_id: fragmentId ?? null }),
    }),
  /**
   * 从某段历史继续（spec 第 34 / 54 条）：旧片段保持不变，
   * 后端新建接续片段并把 Anchor 落到新片段上。
   */
  continueFromHistory: (topicId: string, fragmentId: string) =>
    request<{
      ok: boolean;
      topic_id: string;
      fragment_id: string | null;
      fragment_title: string | null;
      historic: boolean;
      created_fragment_id: string | null;
      source_fragment_id: string | null;
      /** 阶段 1：点击历史只登记意图；这两个字段说明它还没落实 */
      intent_id?: string | null;
      intent_version?: number | null;
      pending?: boolean;
    }>("/api/anchor", {
      method: "POST",
      body: JSON.stringify({
        topic_id: topicId,
        fragment_id: fragmentId,
        continue_from_history: true,
      }),
    }),
  /** 取消「下一条消息从这段历史继续」的登记（不发消息就不留痕迹）。 */
  cancelContinuation: () =>
    request<{ ok: boolean; cancelled: boolean }>("/api/anchor/continue/cancel", {
      method: "POST",
    }),
  /** 待确认切换：确认「转到这里」。 */
  confirmTopicSwitch: () =>
    request<{
      ok: boolean;
      topic_id: string | null;
      fragment_id?: string | null;
      fragment_title?: string | null;
      historic?: boolean;
    }>("/api/topic-switch/confirm", { method: "POST" }),
  /** 待确认切换：保留当前话题。 */
  rejectTopicSwitch: () =>
    request<{ ok: boolean }>("/api/topic-switch/reject", { method: "POST" }),
  getSessionContext: (limit?: number) =>
    request<{
      topic_id: string;
      topic_name: string;
      anchor_fragment: { id: string; title: string | null; historic?: boolean } | null;
      messages: {
        id: string;
        role: string;
        content: string;
        content_type: string;
        created_at: string;
        turn_id?: string | null;
        /** 叙事行的系统元数据（JSON 字符串）：kind 与系统生成的调用摘要 */
        raw?: string;
      }[];
      /** 这一页涉及的每轮结束事实（台账里确实记过的才有；含 incomplete） */
      turn_facts?: TurnFactsRow[];
      /** 这一页涉及的工具调用（预览；全文按 id 取） */
      tool_records?: ToolRecordPreview[];
      /** 还有更早的历史可以加载 */
      has_more?: boolean;
      /** 取更早历史时传回的游标 */
      next_before?: string | null;
    }>(`/api/session/context${limit ? `?limit=${limit}` : ""}`, {
      timeoutMs: API_TIMEOUT_MS.bulk,
    }),
  /**
   * 更早的一页历史（用户向上读时按需加载）。
   * `before` 是后端给的复合游标（created_at|id）：同一时刻写入的消息也不会丢或重。
   */
  getSessionMessagesBefore: (topicId: string | null, before: string, limit = 200) =>
    request<{
      topic_id: string;
      messages: {
        id: string;
        role: string;
        content: string;
        content_type: string;
        created_at: string;
        turn_id?: string | null;
        raw?: string;
      }[];
      /** 更早的这一页同样带回每轮结束事实（含 incomplete；旧记录不出现） */
      turn_facts?: TurnFactsRow[];
      tool_records?: ToolRecordPreview[];
      has_more: boolean;
      next_before: string | null;
    }>(
      `/api/session/messages?before=${encodeURIComponent(before)}&limit=${limit}` +
        (topicId ? `&topic_id=${encodeURIComponent(topicId)}` : ""),
      { timeoutMs: API_TIMEOUT_MS.bulk },
    ),
  listTopics: () =>
    request<{ topics: TopicFingerprint[] }>("/api/graph/topics"),
  getTopicDetail: (topicId: string) =>
    request<TopicDetail>(`/api/graph/topics/${encodeURIComponent(topicId)}`),
  getPositions: () => request<{ topics: TopicPosition[] }>("/api/graph/positions"),
  /** 星球第一层数据：有哪些话题可以展示（轻量、不含原文）。 */
  planetOverview: () =>
    request<{ topics: PlanetTopicSummary[]; total: number; visible_capacity: number }>(
      "/api/planet/overview",
    ),
  /** 星球第二层调用：接下来该展示哪一批（游标可前进可后退）。 */
  planetBrowse: (body: {
    cursor?: string | null;
    direction?: "forward" | "backward";
    count?: number;
    exclude?: string[];
    current_topic_id?: string | null;
    seed?: number | null;
  }) =>
    request<{
      seed: number;
      pass_index: number;
      cursor: string;
      prev_cursor: string;
      next_cursor: string;
      has_more: boolean;
      pass_changed: boolean;
      total: number;
      visible_capacity: number;
      items: PlanetTopicSummary[];
    }>("/api/planet/browse", { method: "POST", body: JSON.stringify(body) }),
  /** 第三层：只有真正展开某段历史时才按页取原文。 */
  fragmentMessages: (fragmentId: string, offset = 0, limit = 50) =>
    request<{
      fragment_id: string;
      topic_id: string;
      total: number;
      offset: number;
      limit: number;
      messages: { id: string; role: string; content: string; content_type: string; created_at: string }[];
    }>(
      `/api/fragments/${encodeURIComponent(fragmentId)}/messages?offset=${offset}&limit=${limit}`,
      { timeoutMs: API_TIMEOUT_MS.bulk },
    ),
  reviseKnowledge: (knowledgeId: string, content: string) =>
    request<{ ok: boolean; knowledge_id: string }>(
      `/api/knowledge/${encodeURIComponent(knowledgeId)}/revise`,
      { method: "POST", body: JSON.stringify({ content }) },
    ),
  revokeKnowledge: (knowledgeId: string) =>
    request<{ ok: boolean }>(`/api/knowledge/${encodeURIComponent(knowledgeId)}/revoke`, {
      method: "POST",
    }),
  listKnowledge: () => request<{ knowledge: KnowledgeItem[] }>("/api/knowledge"),
  createKnowledge: (payload: { category: string; content: string; topic_id?: string | null }) =>
    request<{ ok: boolean; knowledge: KnowledgeItem }>("/api/knowledge", {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  verifyKnowledge: (id: string) =>
    request<{ ok: boolean; knowledge: { id: string; state: string } }>(
      `/api/knowledge/${encodeURIComponent(id)}/verify`,
      { method: "POST" },
    ),
  rejectKnowledge: (id: string) =>
    request<{ ok: boolean; knowledge: { id: string; state: string } }>(
      `/api/knowledge/${encodeURIComponent(id)}/reject`,
      { method: "POST" },
    ),
  /**
   * 忽略一条高影响知识候选（对话内确认卡）。
   * 语义：用户明确不要这条长期知识 → 后端记为已忽略，同一内容不再重复提示。
   * 与 `rejectKnowledge`（打回草稿、仍留在审核列表里）不同。
   */
  ignoreKnowledge: (id: string) =>
    request<{ ok: boolean; knowledge_id: string }>(
      `/api/knowledge/${encodeURIComponent(id)}/ignore`,
      { method: "POST" },
    ),
  /** 标记「已结束」：不再是当前状态，但相关内容仍能被参考到（权重降低）。 */
  endKnowledge: (id: string) =>
    request<{ ok: boolean; knowledge: KnowledgeItem }>(
      `/api/knowledge/${encodeURIComponent(id)}/end`,
      { method: "POST", body: JSON.stringify({ reason: "user_confirmed" }) },
    ),
  resumeKnowledge: (id: string) =>
    request<{ ok: boolean; knowledge: KnowledgeItem }>(
      `/api/knowledge/${encodeURIComponent(id)}/resume`,
      { method: "POST" },
    ),
  /** 改适用范围：全局（你）或只在某个话题里生效（归属管理在知识页，不在引导里）。 */
  setKnowledgeScope: (id: string, scope: { type: "global" | "topic"; topic_id?: string }) =>
    request<{ ok: boolean; knowledge: KnowledgeItem }>(
      `/api/knowledge/${encodeURIComponent(id)}/scope`,
      { method: "POST", body: JSON.stringify(scope) },
    ),
  endTopic: (topicId: string) =>
    request<{ ok: boolean; topic_id: string }>(
      `/api/graph/topics/${encodeURIComponent(topicId)}/end`,
      { method: "POST", body: JSON.stringify({ reason: "user_confirmed" }) },
    ),
  resumeTopic: (topicId: string) =>
    request<{ ok: boolean; topic_id: string }>(
      `/api/graph/topics/${encodeURIComponent(topicId)}/resume`,
      { method: "POST" },
    ),
  listEntities: () => request<{ entities: EntityCard[] }>("/api/entities"),
  getEntity: (id: string) =>
    request<{ entity: EntityCard }>(`/api/entities/${encodeURIComponent(id)}`),
  reviseEntity: (
    id: string,
    payload: {
      attributes?: EntityAttribute[];
      aliases?: string[];
      summary?: string;
      kind?: string;
    },
  ) =>
    request<{ ok: boolean; entity: EntityCard | null }>(
      `/api/entities/${encodeURIComponent(id)}/revise`,
      { method: "POST", body: JSON.stringify(payload) },
    ),
  revokeEntity: (id: string) =>
    request<{ ok: boolean; entity_id: string }>(
      `/api/entities/${encodeURIComponent(id)}/revoke`,
      { method: "POST" },
    ),
  addEntityRelation: (id: string, payload: { type: string; target: string }) =>
    request<{ ok: boolean; entity: EntityCard | null }>(
      `/api/entities/${encodeURIComponent(id)}/relations`,
      { method: "POST", body: JSON.stringify(payload) },
    ),
  removeEntityRelation: (id: string, payload: { type: string; target: string }) =>
    request<{ ok: boolean; entity: EntityCard | null }>(
      `/api/entities/${encodeURIComponent(id)}/relations`,
      { method: "DELETE", body: JSON.stringify(payload) },
    ),

  /**
   * 记忆封块设置：字段是「轮」（用户 + 助手算一轮），不是消息条数。
   * 旧字段名 `fragment_max_messages` 描述的其实是消息数，语义与实现不一致，已由后端正名。
   */
  getMemorySettings: () =>
    request<{ fragment_max_turns: number; fragment_max_tokens: number }>(
      "/api/settings/memory",
    ),
  runMaintenance: () =>
    request<{ ok: boolean; started: boolean }>("/api/maintenance/run", {
      method: "POST",
      timeoutMs: API_TIMEOUT_MS.long,
    }),
  getMaintenanceSettings: () =>
    request<{ enabled: boolean; interval_hours: number }>("/api/settings/maintenance"),
  updateMaintenanceSettings: (body: { enabled?: boolean; interval_hours?: number }) =>
    request<{ enabled: boolean; interval_hours: number }>("/api/settings/maintenance", {
      method: "PUT",
      body: JSON.stringify(body),
    }),
  /** 只给轮数时只改轮数；只给长度时只改长度（互不牵连）。 */
  updateMemorySettings: (payload: { fragment_max_turns?: number; fragment_max_tokens?: number }) =>
    request<{ ok: boolean; fragment_max_turns: number; fragment_max_tokens: number }>(
      "/api/settings/memory",
      {
        method: "PUT",
        body: JSON.stringify(payload),
      },
    ),
  getUISettings: () =>
    request<{ typewriter_cps: number }>("/api/settings/ui"),
  updateUISettings: (typewriterCps: number) =>
    request<{ ok: boolean; typewriter_cps: number }>("/api/settings/ui", {
      method: "PUT",
      body: JSON.stringify({ typewriter_cps: typewriterCps }),
    }),
  getComputerSettings: () =>
    request<ComputerSettings>("/api/settings/computer"),
  updateComputerSettings: (body: Record<string, unknown>) =>
    request<ComputerSettings>("/api/settings/computer", {
      method: "PUT",
      body: JSON.stringify(body),
    }),
  /** 工具调用历史：一次调用的全文（参数 + 输出） */
  getToolRecord: (recordId: string) =>
    request<ToolRecordFull>(`/api/tool-records/${encodeURIComponent(recordId)}`, {
      timeoutMs: API_TIMEOUT_MS.bulk,
    }),
  getToolHistorySettings: () => request<ToolHistorySettings>("/api/settings/tools"),
  updateToolHistorySettings: (body: {
    record_outputs?: boolean;
    output_retention_days?: number;
  }) =>
    request<ToolHistorySettings>("/api/settings/tools", {
      method: "PUT",
      body: JSON.stringify(body),
    }),
  getLoopSettings: () =>
    request<LoopSettings>("/api/settings/loop"),
  updateLoopSettings: (body: Record<string, unknown>) =>
    request<LoopSettings>("/api/settings/loop", {
      method: "PUT",
      body: JSON.stringify(body),
    }),
  getSearchSettings: () =>
    request<SearchSettings>("/api/settings/search"),
  updateSearchSettings: (body: Record<string, unknown>) =>
    request<SearchSettings>("/api/settings/search", {
      method: "PUT",
      body: JSON.stringify(body),
    }),
  getOnboardingStatus: () =>
    request<OnboardingStatus>("/api/onboarding/status"),
  markOnboardingSeen: () =>
    request<OnboardingStatus>("/api/onboarding/seen", { method: "POST" }),
  saveOnboardingProfile: (payload: OnboardingProfilePayload) =>
    request<{ name: string; knowledge_id: string; entity_id: string; topics: string[] }>(
      "/api/onboarding/profile",
      { method: "POST", body: JSON.stringify(payload) },
    ),
  completeOnboarding: () =>
    request<OnboardingStatus>("/api/onboarding/complete", { method: "POST" }),
  setOnboardingHint: (dismissed: boolean) =>
    request<OnboardingStatus>("/api/onboarding/hint", {
      method: "POST",
      body: JSON.stringify({ dismissed }),
    }),
  submitOnboarding: (payload: OnboardingSubmitPayload) =>
    request<{
      name: string;
      self_card_id: string;
      written: { id: string | null; content: string }[];
      pending: { id: string; content: string }[];
      topics: string[];
    }>("/api/onboarding/submit", {
      method: "POST",
      body: JSON.stringify(payload),
      timeoutMs: API_TIMEOUT_MS.long,
    }),
  suggestFollowUps: (description: string) =>
    request<{ questions: string[] }>("/api/onboarding/followups", {
      method: "POST",
      body: JSON.stringify({ description }),
      timeoutMs: API_TIMEOUT_MS.long,
    }),
  /**
   * 工具开发任务列表（权威状态，来自工作区本身）。
   *
   * 「有未完成的任务」入口用它：刷新、重启、断线之后任务都还在，
   * 不会再出现「模型说要继续开发，界面上却找不到那个任务」。
   */
  getDevTasks: () => request<{ tasks: DevTaskRow[] }>("/api/dev/tasks"),
  /**
   * 当前的执行授权范围：用户看得到自己同意过什么。
   *
   * 授权不是一句「已允许」——它绑在（能力策略指纹 + 执行环境）上，范围还包括
   * 目录、网络与凭据引用。这里把它们如实列出来，并支持撤销。
   */
  listDevAuthorizations: () =>
    request<{ authorizations: DevAuthorizationRow[] }>("/api/dev/authorizations"),
  /** 收回某个开发任务的执行授权：下一次测试会重新征求确认。 */
  revokeDevAuthorization: (taskId: string) =>
    request<{ revoked: boolean }>(
      `/api/dev/authorizations/${encodeURIComponent(taskId)}/revoke`,
      { method: "POST" },
    ),
  /**
   * 放弃一项**没做完**的开发任务（终态，不可逆）。
   *
   * 语义边界（与「撤销授权」「删除已注册工具」都不混用）：结束这项开发、从未完成
   * 列表移除、收回它的执行授权并作废未决确认；**不删**工作区文件与记录，也**不动**
   * 已经注册的工具。
   *
   * 被拒绝时后端返回 `200 + ok:false`（正在执行 / 已经做完），调用方必须按
   * 「没有被放弃」处理：条目保留、原因就地显示。未知任务是 `404`。
   */
  abandonDevTask: (taskId: string) =>
    request<DevAbandonResult>(`/api/dev/tasks/${encodeURIComponent(taskId)}/abandon`, {
      method: "POST",
    }),
};

/** 一条开发任务的权威状态（后端 `GET /api/dev/tasks` 的一行）。 */
export interface DevTaskRow {
  id: string;
  /** 这个工具要做成什么样（后端截断到 200 字） */
  request: string;
  /** 后端给的阶段名；界面只做展示映射，不用它推断「做完了没有」 */
  phase: string | null;
  submitted: boolean;
  /** 最近一次测试的结论；`null` = 从没跑过（不是「没通过」） */
  test_passed: boolean | null;
  /**
   * 那条测试结论是否还对应**当前**的文件内容。
   * false = 测试通过之后又改过文件，那次结论不再算数。
   */
  test_evidence_current: boolean;
  updated_at: string | null;
  /** 有没有「在某个环境里跑它的测试」的执行授权（范围见 getDevAuthorizations） */
  authorized: boolean;
  /**
   * 已经放弃（终态）：不再执行、不再注册。
   *
   * 列表接口仍然会带回这一行（列表是事实清单），「没做完」是界面按
   * `!submitted && !abandoned` 过滤出来的视图。
   */
  abandoned: boolean;
  /** 放弃的时刻；没放弃过是 null */
  abandoned_at: string | null;
}

/**
 * 一次「放弃开发」的结果（`POST /api/dev/tasks/{id}/abandon`）。
 *
 * `ok === true` 才代表真的放弃了 —— 界面只认这一个字段，不做乐观移除。
 * `status` 直接用后端的词：`running`（正在跑测试/正在提交，放弃被拒绝）与
 * `submitted`（已经做完、工具已注册）都**不是**「已放弃」。
 */
export interface DevAbandonResult {
  ok: boolean;
  /**
   * 结局词。
   *
   * `persist_failed` = 放弃终态**没能可靠落盘**：任务仍是未完成，界面必须保留条目、
   * 就地显示 `message` 并允许重试（绝不能因为这次失败就把条目拿掉）。
   */
  status:
    | "abandoned"
    | "already_abandoned"
    | "running"
    | "submitted"
    | "persist_failed";
  /** 面向用户的中文一句话：界面原样显示，不改写成别的结论 */
  message: string;
  /** 是否真的收回了执行授权（含长期授权） */
  revoked: boolean;
  /** 被这次放弃作废的未决确认条数 */
  invalidated_approvals: number;
  /** 现在能不能只停止这一个任务（当前架构恒为 false：没有这个能力） */
  can_stop: boolean;
  /**
   * 放弃终态是否已经可靠落盘。
   *
   * 只有 `ok === true` 时才是 true；`ok === false` 时它表示「这次没有任何终态被写下去」，
   * 界面据此如实说明「任务还在，可以重试」。
   */
  persisted: boolean;
  /** 更新后的任务行；未知任务是 null */
  task: DevTaskRow | null;
}

/** 一条执行授权的范围（后端 `GET /api/dev/authorizations` 的一行）。 */
export interface DevAuthorizationRow {
  task_id: string;
  request: string;
  submitted: boolean;
  /** subprocess（受限子进程，不是安全沙箱）| docker（容器隔离） */
  executor: string;
  isolated: boolean;
  policy_fingerprint: string | null;
  capabilities: string[];
  filesystem: string[];
  network: boolean;
  network_allow: string[];
  credentials: string[];
  granted_at: string;
  tool_name?: string | null;
}

export interface OnboardingStatus {
  done: boolean;
  has_credential: boolean;
  has_name: boolean;
  /** 本机是否已经聊过至少一条消息：新用户不能在密钥这一步跳过 */
  has_content: boolean;
  wizard_seen: boolean;
  welcome_version: string;
  app_version: string;
  show_wizard: boolean;
  hint_dismissed: boolean;
}

export interface OnboardingSubmitPayload {
  name: string;
  background?: string;
  current_focus?: string;
  current_focus_ended?: boolean;
  interests?: string[];
  familiarity?: string;
  limits?: { dont_do?: string; how_to_talk?: string };
  preferences?: {
    kind: string;
    value: string;
    scope: { type: "global" | "topic"; topic_title?: string };
  }[];
  goals?: string[];
  inferred?: { content: string; category?: string; reason?: string }[];
}

export interface OnboardingProfilePayload {
  name: string;
  intro?: string;
  tags?: { key: string; value: string }[];
  style?: string;
  goals?: string[];
}

export interface TopicFingerprint {
  topic_id: string;
  title: string;
  keywords: string[];
  fragment_count: number;
  last_activity: string | null;
  summary_preview: string | null;
  /** 已结束的话题：不在星球主视图，只在「已结束」分组或搜索里出现 */
  ended?: boolean;
}

export interface TopicPosition {
  topic_id: string;
  name: string;
  position: [number, number, number];
  activity: number;
  updated_at: string;
}

/** 星球浏览用的话题摘要（三层接口的第一层 / 浏览批次）。 */
export interface PlanetTopicSummary {
  topic_id: string;
  title: string;
  fragment_count: number;
  last_activity: string | null;
  summary_preview?: string | null;
  /** 稳定视觉身份种子（本阶段只携带，不消费） */
  visual_seed?: number;
}

export interface TopicDetailFragment {
  fragment_id: string;
  summary: string | null;
  closed_at: string | null;
  message_count: number;
  created_at?: string;
  /** 该片段最后一次活动时间（用于「最近活动」这一层信息） */
  last_activity?: string | null;
  /** 第二层不再内联原文；原文由 fragmentMessages 按需分页读取（spec 第 42~43 条） */
  messages?: { id: string; role: string; content: string; content_type: string; created_at: string }[];
}

export interface TopicDetail {
  topic_id: string;
  name: string;
  /** 一句摘要（memory_index 的片段摘要首句；没有就不显示，不编造） */
  summary?: string | null;
  /** 话题关键词（memory_index 聚合） */
  keywords?: string[];
  /** 最近活动时间 */
  last_activity?: string | null;
  /** 话题总消息数（真实计数） */
  message_count?: number;
  fragments: TopicDetailFragment[];
  entities: { id: string; name: string }[];
  knowledge: { id: string; category: string; state: string; content: string; confidence: number | null; updated_at: string }[];
}
