/**
 * 设置页测试用的 API 替身（fake，不联网、不用真实 Key）。
 *
 * 所有默认值都是「已经配置好、可以正常保存」的中性状态；
 * 单个用例需要失败/延迟时再对具体方法 mockRejectedValueOnce / mockImplementationOnce。
 */
import { vi } from "vitest";

export function createSettingsApiMock() {
  return {
    listCredentials: vi.fn(async () => ({ credentials: [], default_key_id: null })),
    listProviders: vi.fn(async () => ({ providers: [], model_note: "" })),
    getMemorySettings: vi.fn(async () => ({ fragment_max_turns: 10, fragment_max_tokens: 4096 })),
    updateMemorySettings: vi.fn(
      async (payload: { fragment_max_turns?: number; fragment_max_tokens?: number }) => ({
        ok: true,
        fragment_max_turns: payload.fragment_max_turns ?? 10,
        fragment_max_tokens: payload.fragment_max_tokens ?? 4096,
      }),
    ),
    getMaintenanceSettings: vi.fn(async () => ({ enabled: true, interval_hours: 24 })),
    updateMaintenanceSettings: vi.fn(
      async (body: { enabled?: boolean; interval_hours?: number }) => ({
        enabled: body.enabled ?? true,
        interval_hours: body.interval_hours ?? 24,
      }),
    ),
    runMaintenance: vi.fn(async () => ({ ok: true, started: true })),
    getSearchSettings: vi.fn(async () => ({
      searxng_url: "",
      bocha_has_key: false,
      keyless_fallback: true,
      top_k_default: 5,
      max_fetch_chars: 15000,
    })),
    updateSearchSettings: vi.fn(async (body: Record<string, unknown>) => ({
      searxng_url: String(body.searxng_url ?? ""),
      bocha_has_key:
        body.bocha_api_key === undefined
          ? false
          : String(body.bocha_api_key).trim().length > 0,
      keyless_fallback: body.keyless_fallback === undefined ? true : Boolean(body.keyless_fallback),
      top_k_default: Number(body.top_k_default ?? 5),
      max_fetch_chars: Number(body.max_fetch_chars ?? 15000),
    })),
    getSearchStatus: vi.fn(async () => ({ channels: [] })),
    checkSearch: vi.fn(async () => ({ ok: true, channels: [] })),
    getUISettings: vi.fn(async () => ({ typewriter_cps: 50, close_behavior: "ask" })),
    updateUISettings: vi.fn(async (cps: number) => ({ ok: true, typewriter_cps: cps })),
    updateCloseBehavior: vi.fn(async (behavior: string) => ({
      ok: true,
      typewriter_cps: 50,
      close_behavior: behavior,
    })),
    getComputerSettings: vi.fn(async () => ({ root_dir: "", permission_mode: "default" })),
    updateComputerSettings: vi.fn(async (body: Record<string, unknown>) => ({
      root_dir: String(body.root_dir ?? ""),
      permission_mode: String(body.permission_mode ?? "default"),
    })),
    getToolHistorySettings: vi.fn(async () => ({
      record_outputs: true,
      output_retention_days: 90,
      record_retention_days: 0,
    })),
    updateToolHistorySettings: vi.fn(
      async (body: {
        record_outputs?: boolean;
        output_retention_days?: number;
        record_retention_days?: number;
      }) => ({
        record_outputs: body.record_outputs ?? true,
        output_retention_days: body.output_retention_days ?? 90,
        record_retention_days: body.record_retention_days ?? 0,
        purged: 0,
      }),
    ),
    getLoopSettings: vi.fn(async () => ({ max_iterations: 128, output_token_budget: 51200 })),
    updateLoopSettings: vi.fn(async (body: Record<string, unknown>) => ({
      max_iterations: Number(body.max_iterations ?? 128),
      output_token_budget: Number(body.output_token_budget ?? 51200),
    })),
    getOnboardingStatus: vi.fn(async () => ({
      done: true,
      has_credential: true,
      has_name: true,
      has_content: true,
      wizard_seen: true,
      welcome_version: "",
      app_version: "0.2.10",
      show_wizard: false,
      hint_dismissed: true,
    })),
  };
}

export type SettingsApiMock = ReturnType<typeof createSettingsApiMock>;

/**
 * 与真实 `ApiError` 同名的测试替身。
 *
 * 为什么必须提供：设置页挂载后会渲染「模型与服务」的真实面板，面板的失败分支走
 * `services/credentials.ts::readableError`，那里做 `error instanceof ApiError`。
 * 只 mock 了 `api` 而漏掉 `ApiError` 时，`instanceof` 会撞到「mock 上没有这个导出」
 * 并抛出未处理拒绝 —— 看起来像测试失败，实际是替身不完整。
 */
export class FakeApiError extends Error {}

/** `services/api` 的完整模块替身（api + ApiError），各测试文件用它构造 vi.mock 返回值。 */
export function settingsApiModule() {
  return { api: createSettingsApiMock(), ApiError: FakeApiError };
}

/**
 * `services/modelCatalog` 的替身：默认「没有任何连接」的确定性状态。
 *
 * 设置页里「模型与服务」面板是真实组件（不是占位），不 mock 它就会真的发请求，
 * 于是每个设置页测试都要依赖网络失败的时序 —— 那是测试脆弱性的来源。
 */
export function createModelCatalogMock() {
  const emptyCatalog = {
    connections: [] as unknown[],
    default: null,
    usages: [] as unknown[],
    discovery: {},
  };
  return {
    modelCatalogApi: {
      getCatalog: vi.fn(async () => ({ ...emptyCatalog })),
      discover: vi.fn(async () => ({
        credentialId: "",
        providerId: null,
        providerName: null,
        source: "provider",
        models: [],
        page: 1,
        pageSize: 50,
        total: 0,
        hasMore: false,
        error: null,
      })),
      setDefault: vi.fn(async () => ({ credentialId: "", modelId: "" })),
      setUsage: vi.fn(async () => ({ ...emptyCatalog })),
      verify: vi.fn(async () => ({
        ok: false,
        state: "failed",
        reasonCode: "not_run",
        message: "",
        appliesToCurrentConfig: false,
        verifiedAt: null,
        modelId: "",
        credentialId: "",
      })),
    },
  };
}

/** `services/modelCatalog` 的 vi.mock 工厂体（各测试文件里 await import 后返回）。 */
export function modelCatalogModule() {
  return createModelCatalogMock();
}
