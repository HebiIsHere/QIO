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
  respondApproval: (approvalId: string, decision: string, overrides?: Record<string, unknown>) =>
    request<{ ok: boolean }>(`/api/approvals/${encodeURIComponent(approvalId)}/respond`, {
      method: "POST",
      body: JSON.stringify(overrides ? { decision, overrides } : { decision }),
    }),
  cancelTurn: (turnId: string) =>
    request<{ ok: boolean; cancelled: boolean; turn_id: string }>(
      `/api/turns/${encodeURIComponent(turnId)}/cancel`,
      { method: "POST" },
    ),
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
    }>("/api/anchor", {
      method: "POST",
      body: JSON.stringify({
        topic_id: topicId,
        fragment_id: fragmentId,
        continue_from_history: true,
      }),
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
  getSessionContext: () =>
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
      }[];
    }>("/api/session/context"),
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
    request<{ fragment_max_turns: number }>("/api/settings/memory"),
  runMaintenance: () =>
    request<{ ok: boolean; started: boolean }>("/api/maintenance/run", { method: "POST" }),
  getMaintenanceSettings: () =>
    request<{ enabled: boolean; interval_hours: number }>("/api/settings/maintenance"),
  updateMaintenanceSettings: (body: { enabled?: boolean; interval_hours?: number }) =>
    request<{ enabled: boolean; interval_hours: number }>("/api/settings/maintenance", {
      method: "PUT",
      body: JSON.stringify(body),
    }),
  updateMemorySettings: (fragmentMaxTurns: number) =>
    request<{ ok: boolean; fragment_max_turns: number }>("/api/settings/memory", {
      method: "PUT",
      body: JSON.stringify({ fragment_max_turns: fragmentMaxTurns }),
    }),
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
};

export interface TopicFingerprint {
  topic_id: string;
  title: string;
  keywords: string[];
  fragment_count: number;
  last_activity: string | null;
  summary_preview: string | null;
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
