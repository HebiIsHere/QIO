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
  scope: string[] | null;
  budget: number | null;
  budget_used: number;
  status: string;
  note: string | null;
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
  setAnchor: (topicId: string, fragmentId?: string | null) =>
    request<{ ok: boolean; topic_id: string; fragment_id: string | null }>("/api/anchor", {
      method: "POST",
      body: JSON.stringify({ topic_id: topicId, fragment_id: fragmentId ?? null }),
    }),
  getSessionContext: () =>
    request<{
      topic_id: string;
      topic_name: string;
      anchor_fragment: { id: string; title: string | null } | null;
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