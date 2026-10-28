/**
 * 模型目录 / 用途分配 / 默认模型的前端服务（契约 _STG-COMMON.md §6.4）。
 *
 * 为什么单独一个服务文件：设置页的「模型与服务」分区由 B 提供，A 的壳只挂载面板；
 * 面板内部所有网络调用都从这里走，字段在这里一次性从后端的下划线命名翻成 camelCase，
 * 组件里不再做字段翻译（否则同一字段两套名字，迟早漂移）。
 *
 * 几条不能靠界面自觉的边界（这里只做形状转换，判断在后端）：
 * - 发现（discover）是**唯一**会把 Key 发出去的入口，且只发给已保存的那条连接；
 * - 读目录（getCatalog）不联网，返回的 usable / unavailableReason 是后端算好的事实；
 * - 验证结论带 appliesToCurrentConfig：false 表示「这是改动前配置的历史结果」，
 *   界面必须如实显示，不能当成当前配置可用。
 */

import { API_TIMEOUT_MS, request } from "./api";

// ── 后端原始形状（只有本文件读它们） ─────────────────────────────────────────

interface RawCatalogModel {
  model_id: string;
  display_name: string;
  source: string;
  capabilities: string[];
  verify_state: string;
  verified_at: string | null;
  verify_error: string | null;
  usable: boolean;
  unavailable_reason: string;
}

interface RawConnection {
  credential_id: string;
  name: string;
  endpoint: string | null;
  kind: string;
  provider_id: string | null;
  provider_name: string | null;
  enabled: boolean;
  status: string;
  verify_state: string;
  budget: number | null;
  budget_used: number;
  budget_left: number | null;
  models: RawCatalogModel[];
}

interface RawResolvedTarget {
  credential_id: string | null;
  model_id: string | null;
  source: string;
  usable: boolean;
  unavailable_reason: string;
  credential_name?: string;
}

interface RawUsage {
  usage_key: string;
  label: string;
  kind: string;
  group: string;
  model_id: string | null;
  credential_id: string | null;
  follows_default: boolean;
  inherits_ok: boolean;
  resolved: RawResolvedTarget | null;
  note: string;
  daily?: boolean;
}

interface RawCatalogResponse {
  connections: RawConnection[];
  default: { credential_id: string; model_id: string } | null;
  usages: RawUsage[];
  discovery: Record<
    string,
    {
      checked_at: string | null;
      error: string | null;
      source: string | null;
      provider_id: string | null;
      model_count: number;
    }
  >;
}

interface RawDiscoveryResult {
  credential_id: string;
  provider_id: string | null;
  provider_name: string | null;
  source: string;
  models: RawCatalogModel[];
  page: number;
  page_size: number;
  total: number;
  has_more: boolean;
  error: string | null;
  reason_code: string | null;
  public_catalog: boolean;
  note: string;
}

// ── 对组件暴露的类型（camelCase，与契约一致） ────────────────────────────────

export interface CatalogModel {
  modelId: string;
  displayName: string;
  source: string;
  capabilities: string[];
  verifyState: string;
  verifiedAt: string | null;
  verifyError: string | null;
  usable: boolean;
  unavailableReason: string;
}

export interface ModelConnection {
  credentialId: string;
  name: string;
  endpoint: string;
  kind: string;
  providerId: string | null;
  providerName: string | null;
  enabled: boolean;
  status: string;
  verifyState: string;
  budget: number | null;
  budgetUsed: number;
  budgetLeft: number | null;
  models: CatalogModel[];
}

export interface ResolvedTarget {
  credentialId: string;
  modelId: string;
  source: string;
  usable: boolean;
  unavailableReason: string;
  credentialName: string;
}

export interface UsageAssignment {
  usageKey: string;
  label: string;
  kind: string;
  group: string;
  modelId: string | null;
  credentialId: string | null;
  followsDefault: boolean;
  inheritsOk: boolean;
  resolved: ResolvedTarget | null;
  note: string;
  daily: boolean;
}

export interface DiscoveryState {
  checkedAt: string | null;
  error: string | null;
  source: string | null;
  providerId: string | null;
  modelCount: number;
}

export interface DefaultModel {
  credentialId: string;
  modelId: string;
}

export interface CatalogResponse {
  connections: ModelConnection[];
  default: DefaultModel | null;
  usages: UsageAssignment[];
  discovery: Record<string, DiscoveryState>;
}

export interface DiscoveryResult {
  credentialId: string;
  providerId: string | null;
  providerName: string | null;
  source: string;
  models: CatalogModel[];
  page: number;
  pageSize: number;
  total: number;
  hasMore: boolean;
  error: string | null;
  reasonCode: string | null;
  publicCatalog: boolean;
  note: string;
}

export interface VerifyModelResult {
  ok: boolean;
  state: string;
  verifyState: string;
  reasonCode: string | null;
  message: string;
  detail: string;
  mode: string | null;
  appliesToCurrentConfig: boolean;
  resultState: string;
  verifiedAt: string | null;
  credentialId: string;
  modelId: string;
}

export interface PublicCatalogProvider {
  providerId: string;
  providerName: string;
  modelsPath: string;
  authPath: string;
  publicCatalog: boolean;
  discoveryNote: string;
  note: string;
}

export interface PublicCatalogInfo {
  providers: PublicCatalogProvider[];
  note: string;
}

export interface UsageResolveResult {
  usageKey: string;
  label: string;
  supported: boolean;
  resolved: ResolvedTarget | null;
  source: string | null;
  note: string;
}

// ── 形状转换 ────────────────────────────────────────────────────────────────

function toCatalogModel(raw: RawCatalogModel): CatalogModel {
  return {
    modelId: raw.model_id,
    displayName: raw.display_name || raw.model_id,
    source: raw.source,
    capabilities: Array.isArray(raw.capabilities) ? raw.capabilities : [],
    verifyState: raw.verify_state,
    verifiedAt: raw.verified_at,
    verifyError: raw.verify_error,
    usable: raw.usable,
    unavailableReason: raw.unavailable_reason || "",
  };
}

function toResolvedTarget(raw: RawResolvedTarget | null): ResolvedTarget | null {
  if (!raw || !raw.credential_id || !raw.model_id) return null;
  return {
    credentialId: raw.credential_id,
    modelId: raw.model_id,
    source: raw.source,
    usable: raw.usable,
    unavailableReason: raw.unavailable_reason || "",
    credentialName: raw.credential_name || "",
  };
}

function toUsage(raw: RawUsage): UsageAssignment {
  return {
    usageKey: raw.usage_key,
    label: raw.label,
    kind: raw.kind,
    group: raw.group,
    modelId: raw.model_id,
    credentialId: raw.credential_id,
    followsDefault: raw.follows_default,
    inheritsOk: raw.inherits_ok,
    resolved: toResolvedTarget(raw.resolved),
    note: raw.note || "",
    daily: raw.daily !== false,
  };
}

function toDiscoveryState(raw: RawCatalogResponse["discovery"][string]): DiscoveryState {
  return {
    checkedAt: raw.checked_at,
    error: raw.error,
    source: raw.source,
    providerId: raw.provider_id,
    modelCount: raw.model_count ?? 0,
  };
}

function toConnection(raw: RawConnection): ModelConnection {
  return {
    credentialId: raw.credential_id,
    name: raw.name,
    endpoint: raw.endpoint || "",
    kind: raw.kind || "",
    providerId: raw.provider_id,
    providerName: raw.provider_name,
    enabled: raw.enabled,
    status: raw.status,
    verifyState: raw.verify_state,
    budget: raw.budget,
    budgetUsed: raw.budget_used ?? 0,
    budgetLeft: raw.budget_left,
    models: (raw.models || []).map(toCatalogModel),
  };
}

function toDiscoveryResult(raw: RawDiscoveryResult): DiscoveryResult {
  return {
    credentialId: raw.credential_id,
    providerId: raw.provider_id,
    providerName: raw.provider_name,
    source: raw.source,
    models: (raw.models || []).map(toCatalogModel),
    page: raw.page,
    pageSize: raw.page_size,
    total: raw.total,
    hasMore: raw.has_more,
    error: raw.error,
    reasonCode: raw.reason_code,
    publicCatalog: raw.public_catalog,
    note: raw.note || "",
  };
}

// ── API ─────────────────────────────────────────────────────────────────────

export const modelCatalogApi = {
  /** 目录快照（连接 / 模型 / 用途 / 默认模型）。只读，不联网。 */
  async getCatalog(options: { refresh?: boolean; signal?: AbortSignal } = {}): Promise<CatalogResponse> {
    const query = options.refresh ? "?refresh=1" : "";
    const raw = await request<RawCatalogResponse>(`/api/models/catalog${query}`, {
      signal: options.signal,
      timeoutMs: API_TIMEOUT_MS.read,
    });
    const discovery: Record<string, DiscoveryState> = {};
    for (const [credentialId, state] of Object.entries(raw.discovery || {})) {
      discovery[credentialId] = toDiscoveryState(state);
    }
    return {
      connections: (raw.connections || []).map(toConnection),
      default: raw.default
        ? { credentialId: raw.default.credential_id, modelId: raw.default.model_id }
        : null,
      usages: (raw.usages || []).map(toUsage),
      discovery,
    };
  },

  /** 获取一条已保存连接的模型列表（**唯一**会把 Key 发给服务商的入口）。 */
  async discover(
    payload: {
      credentialId: string;
      page?: number;
      pageSize?: number;
      refresh?: boolean;
    },
    signal?: AbortSignal,
  ): Promise<DiscoveryResult> {
    const raw = await request<RawDiscoveryResult>("/api/models/discover", {
      method: "POST",
      body: JSON.stringify({
        credential_id: payload.credentialId,
        page: payload.page ?? 1,
        page_size: payload.pageSize ?? 50,
        refresh: payload.refresh ?? false,
      }),
      signal,
      // 这是一次真实网络请求：套用长超时，不能按普通写请求的短超时掐断。
      timeoutMs: API_TIMEOUT_MS.long,
    });
    return toDiscoveryResult(raw);
  },

  /** 分配用途；credentialId / modelId 都传 null = 恢复跟随默认。 */
  async setUsage(payload: {
    usageKey: string;
    credentialId: string | null;
    modelId: string | null;
  }): Promise<{ ok: boolean; usage: RawUsage | null; usages: UsageAssignment[] }> {
    const raw = await request<{ ok: boolean; usage: RawUsage; usages: RawUsage[] }>(
      "/api/models/usage",
      {
        method: "POST",
        body: JSON.stringify({
          usage_key: payload.usageKey,
          credential_id: payload.credentialId,
          model_id: payload.modelId,
        }),
      },
    );
    return {
      ok: raw.ok,
      usage: raw.usage ?? null,
      usages: (raw.usages || []).map(toUsage),
    };
  },

  /** 设置默认模型（日常对话用哪条连接的哪个模型）。 */
  async setDefault(payload: { credentialId: string; modelId: string }): Promise<{
    ok: boolean;
    default: DefaultModel | null;
    mainChat: UsageAssignment | null;
  }> {
    const raw = await request<{
      ok: boolean;
      default: { credential_id: string; model_id: string } | null;
      main_chat: RawUsage | null;
    }>("/api/models/default", {
      method: "POST",
      body: JSON.stringify({
        credential_id: payload.credentialId,
        model_id: payload.modelId,
      }),
    });
    return {
      ok: raw.ok,
      default: raw.default
        ? { credentialId: raw.default.credential_id, modelId: raw.default.model_id }
        : null,
      mainChat: raw.main_chat ? toUsage(raw.main_chat) : null,
    };
  },

  /** 对单个模型做一次真实调用验证（不影响同 Key 其它模型）。 */
  async verify(
    payload: { credentialId: string; modelId: string },
    signal?: AbortSignal,
  ): Promise<VerifyModelResult> {
    const raw = await request<{
      ok: boolean;
      state: string;
      verify_state?: string;
      reason_code: string | null;
      message: string;
      detail: string;
      mode: string | null;
      applies_to_current_config: boolean;
      result_state: string;
      verified_at: string | null;
      credential_id: string;
      model_id: string;
    }>("/api/models/verify", {
      method: "POST",
      body: JSON.stringify({ credential_id: payload.credentialId, model_id: payload.modelId }),
      signal,
      timeoutMs: API_TIMEOUT_MS.long,
    });
    return {
      ok: raw.ok,
      state: raw.state,
      verifyState: raw.verify_state || raw.state,
      reasonCode: raw.reason_code,
      message: raw.message,
      detail: raw.detail || "",
      mode: raw.mode,
      appliesToCurrentConfig: raw.applies_to_current_config,
      resultState: raw.result_state,
      verifiedAt: raw.verified_at,
      credentialId: raw.credential_id,
      modelId: raw.model_id,
    };
  },

  /** 公共模型目录说明（哪些服务商的目录是公共的；不联网）。 */
  async getPublicCatalog(signal?: AbortSignal): Promise<PublicCatalogInfo> {
    const raw = await request<{
      providers: {
        provider_id: string;
        provider_name: string;
        models_path: string;
        auth_path: string;
        public_catalog: boolean;
        discovery_note: string;
        note: string;
      }[];
      note: string;
    }>("/api/models/catalog/public", { signal });
    return {
      providers: (raw.providers || []).map((item) => ({
        providerId: item.provider_id,
        providerName: item.provider_name,
        modelsPath: item.models_path,
        authPath: item.auth_path,
        publicCatalog: item.public_catalog,
        discoveryNote: item.discovery_note,
        note: item.note,
      })),
      note: raw.note,
    };
  },

  /** 调试 / 展示用：一个用途现在实际会打到哪条连接的哪个模型。 */
  async resolveUsage(usageKey: string, signal?: AbortSignal): Promise<UsageResolveResult> {
    const raw = await request<{
      usage_key: string;
      label: string;
      supported: boolean;
      resolved: RawResolvedTarget | null;
      source: string | null;
      note: string;
    }>(`/api/models/usage-resolve?usage_key=${encodeURIComponent(usageKey)}`, { signal });
    return {
      usageKey: raw.usage_key,
      label: raw.label,
      supported: raw.supported,
      resolved: toResolvedTarget(raw.resolved),
      source: raw.source,
      note: raw.note,
    };
  },
};

export default modelCatalogApi;
