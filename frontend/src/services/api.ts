/** 后端 API 客户端（localhost HTTP）。 */
import { BACKEND_BASE } from "./events_const";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const resp = await fetch(`${BACKEND_BASE}${path}`, {
    headers: { "Content-Type": "application/json" },
    ...init,
  });
  if (!resp.ok) {
    const text = await resp.text();
    throw new Error(`${path} -> ${resp.status}: ${text.slice(0, 200)}`);
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
    request<{ ok: boolean; topic_id: string | null }>("/api/turns", {
      method: "POST",
      body: JSON.stringify({ message, topic_id: topicId ?? null }),
    }),
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

  getMemorySettings: () =>
    request<{ fragment_max_messages: number }>("/api/settings/memory"),
  runMaintenance: () =>
    request<{ ok: boolean; started: boolean }>("/api/maintenance/run", { method: "POST" }),
  getMaintenanceSettings: () =>
    request<{ enabled: boolean; interval_hours: number }>("/api/settings/maintenance"),
  updateMaintenanceSettings: (body: { enabled?: boolean; interval_hours?: number }) =>
    request<{ enabled: boolean; interval_hours: number }>("/api/settings/maintenance", {
      method: "PUT",
      body: JSON.stringify(body),
    }),
  updateMemorySettings: (fragmentMaxMessages: number) =>
    request<{ ok: boolean; fragment_max_messages: number }>("/api/settings/memory", {
      method: "PUT",
      body: JSON.stringify({ fragment_max_messages: fragmentMaxMessages }),
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

export interface TopicDetailFragment {
  fragment_id: string;
  summary: string | null;
  closed_at: string | null;
  message_count: number;
  messages: { id: string; role: string; content: string; content_type: string; created_at: string }[];
}

export interface TopicDetail {
  topic_id: string;
  name: string;
  fragments: TopicDetailFragment[];
  entities: { id: string; name: string }[];
  knowledge: { id: string; category: string; state: string; content: string; confidence: number | null; updated_at: string }[];
}
