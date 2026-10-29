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
  /** 以下只有 GET /api/models/catalog?usage_key=... 才返回（服务端算好的用途维度） */
  usage_key?: string;
  usage_usable?: boolean;
  usage_reason?: string;
  capability_state?: string;
  usage_requires_capabilities?: string[];
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

/**
 * 日常对话合并行的「实际生效目标」（GET /api/models/catalog 的 effective 字段）。
 *
 * 由后端用与运行时**同一个** resolve_shared_target 计算（general-chat 显式优先），
 * 前端只展示、不自己再猜一遍优先级——两份判断分叉正是「显示一个、实际调用另一个」
 * 那个 bug 的来源。老后端不返回这个字段时按 null 处理（回退 resolved）。
 */
interface RawEffectiveTarget {
  ok: boolean;
  usage_key: string;
  credential_id: string | null;
  model_id: string | null;
  source: string;
  matched_usage_key: string;
  usable: boolean;
  unavailable_reason: string;
  reason_code: string;
  detail_code: string;
  degraded?: boolean;
  degrade_reason?: string;
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
  /** 老后端不返回；缺省按 `model` 处理（不因为缺字段就把用途误判成未接通）。 */
  delivery?: string;
  /** 独立分配会不会真的影响真实请求（没有读取点的用途为 false）。 */
  assignment_effective?: boolean;
  /** 有历史覆盖但不会生效：只保留可见性与清除入口。 */
  historical?: boolean;
  historical_target?: RawResolvedTarget | null;
  requires_capabilities?: string[];
  /** main-chat / general-chat 才有：真实生效目标；其它用途为 undefined。 */
  effective?: RawEffectiveTarget | null;
}

interface RawUsageScope {
  usage_key: string;
  label: string;
  delivery: string;
  effective: boolean;
  requires_capabilities: string[];
  note: string;
}

interface RawCatalogResponse {
  connections: RawConnection[];
  default: { credential_id: string; model_id: string } | null;
  usages: RawUsage[];
  /** 带 usage_key 请求时才有：这次目录快照按哪个用途算候选。 */
  usage?: RawUsageScope | null;
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
  /** 一般可用性：与用途无关（连接 / 模型验证层面的结论）。 */
  usable: boolean;
  unavailableReason: string;
  /** 用途维度（只有按 usage_key 请求目录时才有；服务端算，前端只呈现）。 */
  usageKey?: string;
  usageUsable?: boolean;
  usageReason?: string;
  /** supported / missing / unknown / not_required；unknown 仍可选。 */
  capabilityState?: CapabilityState;
  usageRequiresCapabilities?: string[];
}

export type CapabilityState = "supported" | "missing" | "unknown" | "not_required";

/** 目录快照按哪个用途算的候选（带 usage_key 请求时由后端给出）。 */
export interface UsageScope {
  usageKey: string;
  label: string;
  delivery: "model" | "existing" | "unsupported";
  effective: boolean;
  requiresCapabilities: string[];
  note: string;
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

/** 真实生效目标（合并行展示用）：与后端运行时解析结果一致，不是前端推断。 */
export interface EffectiveTarget {
  ok: boolean;
  usageKey: string;
  credentialId: string | null;
  modelId: string | null;
  source: string;
  matchedUsageKey: string;
  usable: boolean;
  unavailableReason: string;
  reasonCode: string;
  detailCode: string;
  degraded: boolean;
  degradeReason: string;
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
  /**
   * 只有 main-chat / general-chat 才有：后端算好的真实生效目标。
   * 合并行优先显示它，没有时回退 `resolved`（老后端兼容）。
   */
  effective?: EffectiveTarget | null;
  note: string;
  daily: boolean;
  /**
   * 这个用途到底怎么被用：`model` 分配会真的生效；`existing` 沿用现有处理方式
   * （视频 / 语音 / 嵌入，不继承默认对话模型）；`unsupported` 当前没有独立调用路径。
   * 界面不得把后两者画成已生效的选择器。
   */
  delivery: "model" | "existing" | "unsupported";
  /**
   * 独立分配**是否真的影响真实请求**（后端核对真实读取路径后的唯一事实）。
   * 老后端不返回：按「会生效」处理，不把正常用途降级成只读。
   */
  assignmentEffective?: boolean;
  /** 有历史覆盖但不会生效：只读展示 + 清除入口，不静默删除。 */
  historical?: boolean;
  /** 历史覆盖指向的目标（只用于如实展示，不代表当前实际使用）。 */
  historicalTarget?: ResolvedTarget | null;
  requiresCapabilities?: string[];
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
  /** 带 usageKey 读取时才有：这次候选按哪个用途算。 */
  usageScope?: UsageScope | null;
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

function toCapabilityState(raw: unknown): CapabilityState | undefined {
  return raw === "supported" || raw === "missing" || raw === "unknown" || raw === "not_required"
    ? raw
    : undefined;
}

function toCatalogModel(raw: RawCatalogModel): CatalogModel {
  const model: CatalogModel = {
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
  // 用途维度字段只在按 usage_key 请求目录时出现；缺省不猜（前端只呈现后端算的）。
  if (typeof raw.usage_usable === "boolean") {
    model.usageKey = raw.usage_key || "";
    model.usageUsable = raw.usage_usable;
    model.usageReason = raw.usage_reason || "";
    model.capabilityState = toCapabilityState(raw.capability_state);
    model.usageRequiresCapabilities = Array.isArray(raw.usage_requires_capabilities)
      ? raw.usage_requires_capabilities
      : [];
  }
  return model;
}

function toUsageScope(raw: RawUsageScope | null | undefined): UsageScope | null {
  if (!raw || !raw.usage_key) return null;
  const delivery: UsageScope["delivery"] =
    raw.delivery === "existing" || raw.delivery === "unsupported" ? raw.delivery : "model";
  return {
    usageKey: raw.usage_key,
    label: raw.label || raw.usage_key,
    delivery,
    effective: Boolean(raw.effective),
    requiresCapabilities: Array.isArray(raw.requires_capabilities)
      ? raw.requires_capabilities
      : [],
    note: raw.note || "",
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

function toEffectiveTarget(raw: RawEffectiveTarget | null | undefined): EffectiveTarget | null {
  if (!raw || typeof raw.ok !== "boolean") return null;
  return {
    ok: raw.ok,
    usageKey: raw.usage_key,
    credentialId: raw.credential_id,
    modelId: raw.model_id,
    source: raw.source || "",
    matchedUsageKey: raw.matched_usage_key || raw.usage_key,
    usable: raw.usable,
    unavailableReason: raw.unavailable_reason || "",
    reasonCode: raw.reason_code || "",
    detailCode: raw.detail_code || "",
    degraded: Boolean(raw.degraded),
    degradeReason: raw.degrade_reason || "",
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
    effective: toEffectiveTarget(raw.effective),
    note: raw.note || "",
    daily: raw.daily !== false,
    delivery:
      raw.delivery === "existing" || raw.delivery === "unsupported"
        ? raw.delivery
        : "model",
    // 老后端不返回时按「会生效」处理（不因为缺字段就把正常用途降级成只读）。
    assignmentEffective: raw.assignment_effective !== false,
    historical: raw.historical === true,
    historicalTarget: toResolvedTarget(raw.historical_target ?? null),
    requiresCapabilities: Array.isArray(raw.requires_capabilities)
      ? raw.requires_capabilities
      : [],
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

/**
 * 把模型接口的失败翻译成一句人话（保存 / 读取共用）。
 *
 * 用途分配被拒时后端返回结构化 4xx（{code, detail, ...}）：这里读 detail，
 * 不把 JSON 原文丢给用户；老接口的字符串 detail 仍然取 ApiError.message 的后半段。
 */
export function modelErrorMessage(error: unknown): string {
  const body = (error as { body?: unknown } | null)?.body;
  if (body && typeof body === "object") {
    const record = body as Record<string, unknown>;
    const detail = record.detail;
    if (typeof detail === "string" && detail.trim()) return detail.trim().slice(0, 300);
    if (detail && typeof detail === "object") {
      const nested = (detail as Record<string, unknown>).message;
      if (typeof nested === "string" && nested.trim()) return nested.trim().slice(0, 300);
    }
  }
  const text = error instanceof Error ? error.message : String(error);
  const marker = text.lastIndexOf(" -> ");
  if (marker >= 0) {
    const after = text.slice(marker + 4);
    const sep = after.indexOf(": ");
    if (sep >= 0) return after.slice(sep + 2).trim().slice(0, 300);
  }
  return text.trim().slice(0, 300);
}

export const modelCatalogApi = {
  /** 目录快照（连接 / 模型 / 用途 / 默认模型）。只读，不联网。 */
  async getCatalog(options: { refresh?: boolean; usageKey?: string; signal?: AbortSignal } = {}): Promise<CatalogResponse> {
    const params = new URLSearchParams();
    if (options.refresh) params.set("refresh", "1");
    // usage_key：让服务端按当前用途算候选（能力缺失 / 未知 / 连接不可用），
    // 前端只呈现结论；这次读仍然只读本地事实（不发验证、不联网）。
    if (options.usageKey) params.set("usage_key", options.usageKey);
    const query = params.toString() ? "?" + params.toString() : "";
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
      usageScope: toUsageScope(raw.usage),
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

  /**
   * 一次原子保存多项用途分配（合并行必须整体成功或整体失败）。
   *
   * 为什么不能继续循环 `setUsage`：两次顺序请求会在第二次失败时留下半个配置
   * （主对话换了、通用对话没换）。后端在**一个 SQLite transaction** 内写完整批，
   * 任一项失败整批回滚；前端这里只发**一次**请求，不自己拼回滚。
   */
  async setUsages(payload: {
    assignments: { usageKey: string; credentialId: string | null; modelId: string | null }[];
  }): Promise<{ ok: boolean; usages: UsageAssignment[] }> {
    const raw = await request<{ ok: boolean; usages?: RawUsage[] }>("/api/models/usages", {
      method: "POST",
      body: JSON.stringify({
        assignments: payload.assignments.map((item) => ({
          usage_key: item.usageKey,
          credential_id: item.credentialId,
          model_id: item.modelId,
        })),
      }),
    });
    return {
      ok: raw.ok,
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
