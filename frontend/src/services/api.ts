/** 后端 API 客户端（本机 HTTP + 会话令牌）。 */
import { authHeaders, resolveBackend } from "./backend";

/** API 错误：带上状态码，调用方才能区分「没权限」和「真的坏了」。 */
export class ApiError extends Error {
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
    this.name = "ApiError";
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
    const text = await resp.text();
    throw new ApiError(resp.status, path, text.slice(0, 200));
  }
  return resp.json() as Promise<T>;
}

export interface CredentialMeta {
  key_id: string;
  version: number;
  tags: string[];
  endpoint: string | null;
  default_model: string | null;
  budget: number | null;
  budget_used: number;
  status: string;
  enabled: boolean;
  note: string | null;
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

export const api = {
  listCredentials: () =>
    request<{ credentials: CredentialMeta[] }>("/api/credentials"),
  createCredential: (payload: Record<string, unknown>) =>
    request<{ ok: boolean; key_id: string; version: number }>("/api/credentials", {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  revokeCredential: (keyId: string) =>
    request<{ ok: boolean }>(`/api/credentials/${encodeURIComponent(keyId)}/revoke`, {
      method: "POST",
    }),
  deleteCredential: (keyId: string) =>
    request<{ ok: boolean; key_id: string }>(
      `/api/credentials/${encodeURIComponent(keyId)}`,
      { method: "DELETE" },
    ),
  updateCredentialMeta: (keyId: string, payload: Record<string, unknown>) =>
    request<{ ok: boolean; credential: CredentialMeta }>(
      `/api/credentials/${encodeURIComponent(keyId)}`,
      { method: "PATCH", body: JSON.stringify(payload) },
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
    request<{ key_id: string; probe: { mode: string; detail: string } }>(
      `/api/credentials/${encodeURIComponent(keyId)}/test`,
      { method: "POST" },
    ),
  sendTurn: (message: string, topicId?: string | null) =>
    request<{
      ok: boolean;
      accepted: boolean;
      /** 受理时就有：乐观消息关联与「停止」都直接用它，不必等 TURN_START */
      turn_id: string;
      status: string;
      topic_id: string | null;
    }>("/api/turns", {
      method: "POST",
      body: JSON.stringify({ message, topic_id: topicId ?? null }),
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
    }>("/api/turns/queue"),
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
    }>("/api/runtime/state"),
  listTraces: (limit = 50, offset = 0) =>
    request<{ traces: TraceSummary[]; total: number; limit: number; offset: number }>(
      `/api/traces?limit=${limit}&offset=${offset}`,
    ),
  getTrace: (turnId: string) =>
    request<TraceDetail>(`/api/traces/${encodeURIComponent(turnId)}`),
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
        /** 叙事行的系统元数据（JSON 字符串）：kind 与系统生成的调用摘要 */
        raw?: string;
      }[];
      /** 还有更早的历史可以加载 */
      has_more?: boolean;
      /** 取更早历史时传回的游标 */
      next_before?: string | null;
    }>(`/api/session/context${limit ? `?limit=${limit}` : ""}`),
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
        raw?: string;
      }[];
      has_more: boolean;
      next_before: string | null;
    }>(
      `/api/session/messages?before=${encodeURIComponent(before)}&limit=${limit}` +
        (topicId ? `&topic_id=${encodeURIComponent(topicId)}` : ""),
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
    request<{ ok: boolean; started: boolean }>("/api/maintenance/run", { method: "POST" }),
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
    }>("/api/onboarding/submit", { method: "POST", body: JSON.stringify(payload) }),
  suggestFollowUps: (description: string) =>
    request<{ questions: string[] }>("/api/onboarding/followups", {
      method: "POST",
      body: JSON.stringify({ description }),
    }),
};

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
