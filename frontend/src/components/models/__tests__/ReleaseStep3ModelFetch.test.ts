/**
 * 发布前第三轮：模型获取归属 / 反馈 / Key 保存重试（F09 / F12 / F13，前端）。
 *
 * 真实组件（KeyForm / KeyAccessSection）挂载，只在服务边界（services/api、
 * services/modelCatalog）用替身。守住的边界：
 * - 表单或已保存连接换服务商 / 地址 / 协议 / Key 后，旧请求不能改当前模型列表、
 *   默认选择与获取状态；
 * - 连接保存后的自动获取把成功 / 空列表 / 业务失败 / 网络异常都说清，并结束「正在同步」；
 *   失败后重试只重新获取，不重复创建连接；
 * - 未改内容的保存重试复用同一请求身份；改内容才换身份；提交中重复点击 / 回车只发一次。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount, type VueWrapper } from "@vue/test-utils";
import KeyForm from "../KeyForm.vue";
import KeyAccessSection from "../KeyAccessSection.vue";
import type { CredentialMeta, ProviderPreset } from "../../../services/api";
import type { ModelConnection } from "../../../services/modelCatalog";

const mocks = vi.hoisted(() => ({
  api: {
    listCredentialModels: vi.fn(async () => ({ models: [] as string[] })),
    createCredential: vi.fn(),
    updateCredentialMeta: vi.fn(),
    verifyCredential: vi.fn(),
    setCredentialEnabled: vi.fn(),
    revokeCredential: vi.fn(),
    deleteCredential: vi.fn(),
    getCredentialAudit: vi.fn(async () => ({ audit: [] })),
  },
  catalog: {
    discover: vi.fn(),
    getCatalog: vi.fn(async () => ({
      connections: [] as unknown[],
      default: null,
      usages: [] as unknown[],
      discovery: {},
    })),
    verify: vi.fn(),
  },
}));

vi.mock("../../../services/api", () => {
  class ApiError extends Error {
    constructor(readonly status: number, readonly path: string, detail: string) {
      super(path + " -> " + status + ": " + detail);
      this.name = "ApiError";
    }
  }
  return { ApiError, api: mocks.api };
});
vi.mock("../../../services/modelCatalog", () => ({ modelCatalogApi: mocks.catalog }));

const provider = (overrides: Partial<ProviderPreset> = {}): ProviderPreset => ({
  id: "deepseek",
  name: "DeepSeek",
  kind: "openai",
  base_url: "https://api.deepseek.com/v1",
  suggested_model: "deepseek-v4-flash",
  category: "official",
  category_label: "官方服务",
  note: "",
  ...overrides,
});
const PROVIDERS: ProviderPreset[] = [
  provider(),
  provider({
    id: "anthropic",
    name: "Anthropic",
    kind: "anthropic",
    base_url: "https://api.anthropic.com/v1",
    suggested_model: "claude-sonnet-4",
  }),
];

function meta(overrides: Partial<CredentialMeta> = {}): CredentialMeta {
  return {
    key_id: "key_a",
    version: 1,
    tags: ["main-loop"],
    endpoint: "https://api.deepseek.com/v1",
    default_model: "deepseek-v4-flash",
    budget: null,
    budget_used: 0,
    status: "active",
    enabled: true,
    note: "工作用",
    kind: "openai",
    verify_state: "verified",
    provider_id: "deepseek",
    provider_name: "DeepSeek",
    ...overrides,
  };
}
function connection(overrides: Partial<ModelConnection> = {}): ModelConnection {
  return {
    credentialId: "key_a",
    name: "工作用",
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
    models: [],
    ...overrides,
  };
}
function discoveryResult(overrides: Record<string, unknown> = {}) {
  return {
    credentialId: "key_a",
    providerId: "deepseek",
    providerName: "DeepSeek",
    source: "provider",
    models: [],
    page: 1,
    pageSize: 50,
    total: 0,
    hasMore: false,
    error: null,
    reasonCode: null,
    stale: false,
    publicCatalog: false,
    note: "",
    ...overrides,
  };
}

const mounted: VueWrapper[] = [];
afterEach(() => {
  for (const wrapper of mounted) wrapper.unmount();
  mounted.length = 0;
});
beforeEach(() => {
  vi.clearAllMocks();
});

function buttonByText(wrapper: VueWrapper, text: string) {
  const button = wrapper.findAll("button").find((item) => item.text().trim().includes(text));
  expect(button, "找不到按钮：" + text).toBeTruthy();
  return button!;
}
async function pickProvider(wrapper: VueWrapper, label: string) {
  await wrapper.find(".qio-combo-trigger").trigger("click");
  const option = wrapper.findAll(".qio-combo-opt").find((item) => item.text().includes(label));
  expect(option, "找不到服务商：" + label).toBeTruthy();
  await option!.trigger("click");
}

describe("F09 表单获取模型：旧请求不能更新当前模型列表 / 默认选择", () => {
  it("获取期间改了 Key：旧响应不写模型列表，也不自动选中旧模型", async () => {
    let resolveOld: (v: { models: string[] }) => void = () => {};
    mocks.api.listCredentialModels.mockImplementationOnce(
      () => new Promise((resolve) => { resolveOld = resolve as never; }),
    );
    const wrapper = mount(KeyForm, { props: { providers: PROVIDERS, mode: "create" } });
    mounted.push(wrapper);
    await pickProvider(wrapper, "DeepSeek");
    await wrapper.find("#key-secret").setValue("sk-fake-first-key");
    await buttonByText(wrapper, "获取模型").trigger("click");
    await flushPromises();

    // 等待期间换 Key（= 换发送目标）
    await wrapper.find("#key-secret").setValue("sk-fake-second-key");
    resolveOld({ models: ["old-model"] });
    await flushPromises();

    expect(wrapper.find(".qio-select").exists()).toBe(false);
    expect(wrapper.text()).not.toContain("已获取 1 个模型");

    // 用新 Key 重新获取才生效
    mocks.api.listCredentialModels.mockResolvedValueOnce({ models: ["new-model"] });
    await buttonByText(wrapper, "获取模型").trigger("click");
    await flushPromises();
    expect(wrapper.text()).toContain("已获取 1 个模型");
    expect(wrapper.text()).toContain("new-model");
  });

  it("获取期间在高级选项里改了地址 / 协议：旧响应同样被丢弃", async () => {
    let resolveOld: (v: { models: string[] }) => void = () => {};
    mocks.api.listCredentialModels.mockImplementationOnce(
      () => new Promise((resolve) => { resolveOld = resolve as never; }),
    );
    const wrapper = mount(KeyForm, { props: { providers: PROVIDERS, mode: "create" } });
    mounted.push(wrapper);
    await pickProvider(wrapper, "DeepSeek");
    await wrapper.find("#key-secret").setValue("sk-fake-first-key");
    await buttonByText(wrapper, "获取模型").trigger("click");
    await flushPromises();

    await wrapper.find(".advanced-toggle").trigger("click");
    await wrapper.find("#key-endpoint").setValue("https://api.example.com/v1");
    resolveOld({ models: ["old-model"] });
    await flushPromises();

    // 高级选项里的协议选择器不是模型选择器：只断言模型结果没被写回。
    expect(wrapper.text()).not.toContain("old-model");
    expect(wrapper.text()).not.toContain("已获取 1 个模型");
  });
});

describe("F13 Key 保存重试：请求身份与重复提交", () => {
  it("双击 / 回车只发一次保存；同内容重试复用同一请求身份，改内容才换", async () => {
    mocks.api.createCredential.mockRejectedValueOnce(new Error("network lost"));
    mocks.api.createCredential.mockResolvedValueOnce({
      ok: true,
      saved: true,
      key_id: "key_new",
      version: 1,
      credential: meta({ key_id: "key_new" }),
      verify: { ok: true, state: "verified", reason_code: null, message: "", detail: "", mode: null },
    });
    const wrapper = mount(KeyForm, { props: { providers: PROVIDERS, mode: "create" } });
    mounted.push(wrapper);
    await pickProvider(wrapper, "DeepSeek");
    await wrapper.find("#key-secret").setValue("sk-fake-save-key");

    // 同一帧提交两次：只能发一次
    const first = wrapper.find("form").trigger("submit");
    const second = wrapper.find("form").trigger("submit");
    await Promise.all([first, second]);
    await flushPromises();
    expect(mocks.api.createCredential).toHaveBeenCalledTimes(1);
    const firstPayload = mocks.api.createCredential.mock.calls[0][0];
    expect(firstPayload.client_request_id).toBeTruthy();

    // 第一次是网络失败（模拟服务端可能已经保存、客户端丢了应答）：重试复用同一身份
    await wrapper.find("form").trigger("submit");
    await flushPromises();
    expect(mocks.api.createCredential).toHaveBeenCalledTimes(2);
    const retryPayload = mocks.api.createCredential.mock.calls[1][0];
    expect(retryPayload.client_request_id).toBe(firstPayload.client_request_id);
    expect(wrapper.emitted("saved")?.[0]).toEqual(["key_new"]);

    // 实际提交内容变了 → 用新身份（不能拿旧身份静默复用旧记录）
    mocks.api.createCredential.mockResolvedValueOnce({
      ok: true,
      saved: true,
      key_id: "key_new2",
      version: 1,
      credential: meta({ key_id: "key_new2" }),
      verify: { ok: true, state: "verified", reason_code: null, message: "", detail: "", mode: null },
    });
    await wrapper.find("#key-secret").setValue("sk-fake-save-key-changed");
    await wrapper.find("form").trigger("submit");
    await flushPromises();
    const thirdPayload = mocks.api.createCredential.mock.calls[2][0];
    expect(thirdPayload.client_request_id).not.toBe(firstPayload.client_request_id);
  });
});

describe("F12 连接保存后的自动获取：四种反馈都结束「正在同步」", () => {
  function mountSection(credentials: CredentialMeta[] = [meta()]) {
    const wrapper = mount(KeyAccessSection, {
      props: {
        connections: [connection()],
        credentials,
        catalog: {
          connections: [connection()],
          default: null,
          usages: [],
          discovery: {},
        },
        providers: PROVIDERS,
      },
    });
    mounted.push(wrapper);
    return wrapper;
  }

  async function triggerSaved(wrapper: VueWrapper) {
    await buttonByText(wrapper, "添加 Key").trigger("click");
    await flushPromises();
    const form = wrapper.findComponent(KeyForm);
    expect(form.exists()).toBe(true);
    form.vm.$emit("saved", "key_a");
    await flushPromises();
  }

  it("成功：说清已获取且尚未验证可用，不再停在「正在同步」", async () => {
    mocks.catalog.discover.mockResolvedValueOnce(
      discoveryResult({
        models: [
          { modelId: "a", displayName: "A", source: "discovered", capabilities: [], verifyState: "unverified", verifiedAt: null, verifyError: null, usable: true, unavailableReason: "" },
          { modelId: "b", displayName: "B", source: "discovered", capabilities: [], verifyState: "unverified", verifiedAt: null, verifyError: null, usable: true, unavailableReason: "" },
        ],
        total: 2,
      }),
    );
    const wrapper = mountSection();
    await triggerSaved(wrapper);
    expect(wrapper.text()).toContain("已获取 2 个模型");
    expect(wrapper.text()).toContain("尚未验证可用");
    expect(wrapper.text()).not.toContain("正在同步");
    expect(wrapper.text()).not.toContain("验证通过");
  });

  it("空列表 / 业务失败 / 网络异常：各有可读反馈，都结束「正在同步」", async () => {
    mocks.catalog.discover.mockResolvedValueOnce(discoveryResult({ models: [], total: 0 }));
    const wrapper = mountSection();
    await triggerSaved(wrapper);
    expect(wrapper.text()).toContain("没有返回可用的模型列表");
    expect(wrapper.text()).not.toContain("正在同步");

    mocks.catalog.discover.mockResolvedValueOnce(
      discoveryResult({ error: "服务响应超时，稍后可以重试", reasonCode: "timeout" }),
    );
    await triggerSaved(wrapper);
    expect(wrapper.text()).toContain("获取模型失败");
    expect(wrapper.text()).toContain("服务响应超时");
    expect(wrapper.text()).not.toContain("正在同步");

    mocks.catalog.discover.mockRejectedValueOnce(new Error("offline"));
    await triggerSaved(wrapper);
    expect(wrapper.text()).toContain("网络异常");
    expect(wrapper.text()).not.toContain("正在同步");
  });

  it("获取期间连接被重配：旧请求结果被忽略并提示配置已改变", async () => {
    let resolveDiscover: (v: ReturnType<typeof discoveryResult>) => void = () => {};
    mocks.catalog.discover.mockImplementationOnce(
      () => new Promise((resolve) => { resolveDiscover = resolve as never; }),
    );
    const wrapper = mountSection();
    await buttonByText(wrapper, "添加 Key").trigger("click");
    await flushPromises();
    wrapper.findComponent(KeyForm).vm.$emit("saved", "key_a");
    await flushPromises();

    // 保存后连接被再次重配（版本推进）：这次获取属于旧配置
    await wrapper.setProps({ credentials: [meta({ version: 2, endpoint: "https://api.openai.com/v1" })] });
    resolveDiscover(discoveryResult({ models: [], total: 0 }));
    await flushPromises();

    expect(wrapper.text()).toContain("配置已改变");
    expect(wrapper.text()).not.toContain("正在同步");
  });

  it("失败后重试只重新获取模型，不重复创建连接", async () => {
    mocks.catalog.discover.mockResolvedValueOnce(
      discoveryResult({ error: "服务端出错，没能取回模型列表", reasonCode: "server_error" }),
    );
    const wrapper = mountSection();
    await triggerSaved(wrapper);
    expect(wrapper.text()).toContain("获取模型失败");

    mocks.catalog.discover.mockResolvedValueOnce(discoveryResult({ models: [], total: 0 }));
    await buttonByText(wrapper, "重新获取").trigger("click");
    await flushPromises();

    expect(mocks.catalog.discover).toHaveBeenCalledTimes(2);
    expect(mocks.api.createCredential).not.toHaveBeenCalled();
    expect(mocks.api.updateCredentialMeta).not.toHaveBeenCalled();
  });
});

describe("返修：获取结果按配置归属与请求顺序写入", () => {
  function mountSection() {
    const wrapper = mount(KeyAccessSection, {
      props: {
        connections: [connection()],
        credentials: [meta()],
        catalog: { connections: [connection()], default: null, usages: [], discovery: {} },
        providers: PROVIDERS,
      },
    });
    mounted.push(wrapper);
    return wrapper;
  }
  async function triggerSaved(wrapper: VueWrapper) {
    await buttonByText(wrapper, "添加 Key").trigger("click");
    await flushPromises();
    wrapper.findComponent(KeyForm).vm.$emit("saved", "key_a");
    await flushPromises();
  }

  it("旧配置的超时不能覆盖新配置那次成功的获取状态", async () => {
    let rejectOld: (e: Error) => void = () => {};
    mocks.catalog.discover.mockImplementationOnce(
      () => new Promise((_resolve, reject) => { rejectOld = reject; }),
    );
    const wrapper = mountSection();
    await triggerSaved(wrapper); // 第一次获取（旧配置）挂起
    expect(wrapper.text()).toContain("正在同步");

    // 同一连接被重新保存：第二次获取成功，先显示成功状态
    mocks.catalog.discover.mockResolvedValueOnce(
      discoveryResult({
        models: [
          { modelId: "new-1", displayName: "new-1", source: "discovered", capabilities: [], verifyState: "unverified", verifiedAt: null, verifyError: null, usable: true, unavailableReason: "" },
          { modelId: "new-2", displayName: "new-2", source: "discovered", capabilities: [], verifyState: "unverified", verifiedAt: null, verifyError: null, usable: true, unavailableReason: "" },
        ],
        total: 2,
      }),
    );
    await triggerSaved(wrapper);
    expect(wrapper.text()).toContain("已获取 2 个模型");
    expect(wrapper.text()).not.toContain("正在同步");

    // 旧请求此刻才超时：不得改写成功状态
    rejectOld(new Error("old config timeout"));
    await flushPromises();
    expect(wrapper.text()).toContain("已获取 2 个模型");
    expect(wrapper.text()).not.toContain("网络异常");
    expect(wrapper.text()).not.toContain("配置已改变");
    wrapper.unmount();
  });

  it("同配置下较早的获取结果不能覆盖较新的获取结果", async () => {
    let resolveOld: (v: ReturnType<typeof discoveryResult>) => void = () => {};
    mocks.catalog.discover.mockImplementationOnce(
      () => new Promise((resolve) => { resolveOld = resolve as never; }),
    );
    const wrapper = mountSection();
    await triggerSaved(wrapper); // 较早的一次获取挂起

    mocks.catalog.discover.mockResolvedValueOnce(
      discoveryResult({
        models: [
          { modelId: "newer-1", displayName: "newer-1", source: "discovered", capabilities: [], verifyState: "unverified", verifiedAt: null, verifyError: null, usable: true, unavailableReason: "" },
          { modelId: "newer-2", displayName: "newer-2", source: "discovered", capabilities: [], verifyState: "unverified", verifiedAt: null, verifyError: null, usable: true, unavailableReason: "" },
        ],
        total: 2,
      }),
    );
    await triggerSaved(wrapper); // 较新的一次先成功
    expect(wrapper.text()).toContain("已获取 2 个模型");

    // 较早请求晚回来（旧结果 / 或被后端标 superseded）：不得覆盖
    resolveOld(
      discoveryResult({
        models: [
          { modelId: "older-1", displayName: "older-1", source: "discovered", capabilities: [], verifyState: "unverified", verifiedAt: null, verifyError: null, usable: true, unavailableReason: "" },
        ],
        total: 1,
      }),
    );
    await flushPromises();
    expect(wrapper.text()).toContain("已获取 2 个模型");
    expect(wrapper.text()).not.toContain("已获取 1 个模型");
    wrapper.unmount();
  });
});
