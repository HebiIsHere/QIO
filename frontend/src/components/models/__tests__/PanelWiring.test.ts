/**
 * 模型与服务面板的接线冒烟：A 的设置页只挂这一个组件，这里守住
 * 「面板自己读取数据、三块都渲染、响应里没有密钥」三件事。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount, type DOMWrapper, type VueWrapper } from "@vue/test-utils";
import { api } from "../../../services/api";

vi.mock("../../../services/api", () => {
  class ApiError extends Error {
    constructor(
      readonly status: number,
      readonly path: string,
      detail: string,
    ) {
      super(path + " -> " + status + ": " + detail);
      this.name = "ApiError";
    }
  }
  return {
    ApiError,
    api: {
      listCredentials: vi.fn(),
      listProviders: vi.fn(),
      listCredentialModels: vi.fn(async () => ({ models: [] })),
      createCredential: vi.fn(),
      updateCredentialMeta: vi.fn(),
      verifyCredential: vi.fn(),
      setCredentialEnabled: vi.fn(),
      revokeCredential: vi.fn(),
      deleteCredential: vi.fn(),
      getCredentialAudit: vi.fn(async () => ({ ok: true, audit: [] })),
    },
  };
});

vi.mock("../../../services/modelCatalog", () => ({
  modelCatalogApi: {
    getCatalog: vi.fn(),
    discover: vi.fn(),
    setUsage: vi.fn(),
    setDefault: vi.fn(),
    verify: vi.fn(),
    getPublicCatalog: vi.fn(),
    resolveUsage: vi.fn(),
  },
}));

const model = (overrides: Record<string, unknown> = {}) => ({
  modelId: "m1",
  displayName: "M1",
  source: "discovered",
  capabilities: [],
  verifyState: "verified",
  verifiedAt: null,
  verifyError: null,
  usable: true,
  unavailableReason: "",
  ...overrides,
});

const connection = (overrides: Record<string, unknown> = {}) => ({
  credentialId: "key_a",
  name: "工作用 DeepSeek",
  endpoint: "https://api.deepseek.com/v1",
  kind: "openai",
  providerId: "deepseek",
  providerName: "DeepSeek",
  enabled: true,
  status: "active",
  verifyState: "verified",
  budget: null,
  budgetUsed: 0,
  budgetLeft: null,
  models: [model()],
  ...overrides,
});

const usage = (overrides: Record<string, unknown> = {}) => ({
  usageKey: "main-chat",
  label: "主对话",
  kind: "chat",
  group: "primary",
  modelId: null,
  credentialId: null,
  followsDefault: true,
  inheritsOk: true,
  resolved: null,
  note: "",
  daily: true,
  ...overrides,
});

function catalog(overrides: Record<string, unknown> = {}) {
  return {
    connections: [connection()],
    default: null,
    usages: [usage()],
    discovery: {},
    ...overrides,
  };
}

const meta = (overrides: Record<string, unknown> = {}) => ({
  key_id: "key_a",
  version: 2,
  tags: ["main-loop"],
  endpoint: "https://api.deepseek.com/v1",
  default_model: "m1",
  budget: 1000,
  budget_used: 100,
  status: "active",
  enabled: true,
  note: "工作用 DeepSeek",
  kind: "openai",
  verify_state: "verified",
  provider_id: "deepseek",
  provider_name: "DeepSeek",
  ...overrides,
});

const mounted: VueWrapper[] = [];
function mountIt(component: unknown, props: Record<string, unknown> = {}): VueWrapper {
  // 组件 props 在测试里是逐用例构造的（不同组件形状不同），这里统一收窄成人造签名。
  const wrapper = mount(component as never, { props: props as never });
  mounted.push(wrapper as VueWrapper);
  return wrapper as VueWrapper;
}

afterEach(() => {
  for (const wrapper of mounted) wrapper.unmount();
  mounted.length = 0;
});
beforeEach(() => {
  vi.clearAllMocks();
});

import ModelServicesPanel from "../ModelServicesPanel.vue";
import { modelCatalogApi } from "../../../services/modelCatalog";

describe("模型与服务面板 · 接线冒烟", () => {
  it("自己读取目录与凭据，渲染 Key 接入 / 默认模型 / 用途分配三块，且不含密钥", async () => {
    (modelCatalogApi.getCatalog as ReturnType<typeof vi.fn>).mockResolvedValue(
      catalog({
        default: { credentialId: "key_a", modelId: "m1" },
        usages: [
          usage({ usageKey: "main-chat", label: "主对话", resolved: null, inheritsOk: false }),
          usage({ usageKey: "general-chat", label: "通用对话", resolved: null, inheritsOk: false }),
        ],
      }),
    );
    (api.listCredentials as ReturnType<typeof vi.fn>).mockResolvedValue({
      credentials: [meta()],
      default_key_id: "key_a",
    });
    (api.listProviders as ReturnType<typeof vi.fn>).mockResolvedValue({
      providers: [
        {
          id: "deepseek",
          name: "DeepSeek",
          kind: "openai",
          base_url: "https://api.deepseek.com/v1",
          suggested_model: "m1",
          category: "official",
          category_label: "官方服务",
          note: "",
        },
      ],
      model_note: "",
    });
    const wrapper = mountIt(ModelServicesPanel, {});
    await flushPromises();
    expect(wrapper.text()).toContain("Key 接入与管理");
    expect(wrapper.text()).toContain("工作用 DeepSeek");
    expect(wrapper.text()).toContain("默认模型");
    expect(wrapper.text()).toContain("用途分配");
    expect(wrapper.text()).not.toContain("sk-");
    expect(wrapper.text()).not.toContain("secret");
  });
});