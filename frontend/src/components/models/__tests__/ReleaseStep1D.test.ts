/**
 * D：日常对话合并行一次保存（后端原子批量）+ 显示真实生效目标（effective）。
 *
 * 发布前第一轮修复任务 D 的两个界面问题：
 * 1. assignUsage 把 main-chat / general-chat 分两次 POST /api/models/usage：第一次成功、
 *    第二次失败就留下半个配置。现在必须只发**一次** POST /api/models/usages
 *    （body.assignments 同时含两个 key），失败时如实报错、不 reload。
 * 2. 合并行以前显示 main-chat 自己的 resolved，而真实运行规则是
 *    general-chat 显式优先（resolve_shared_target(["general-chat","main-chat"],
 *    primary_key="main-chat")）。现在优先展示后端算好的 effective，
 *    「当前生效参数」只读区的 main-chat 目标也来自 effective。
 *
 * 全部 fake：不联网、不用真实 Key。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount, type DOMWrapper, type VueWrapper } from "@vue/test-utils";
import ModelServicesPanel from "../ModelServicesPanel.vue";
import UsageAssignmentSection from "../UsageAssignmentSection.vue";
import { api } from "../../../services/api";
import { modelCatalogApi } from "../../../services/modelCatalog";
import { modelFacts, resetModelFacts } from "../../../views/settings/modelFacts";

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
      listCredentials: vi.fn(async () => ({ credentials: [], default_key_id: null })),
      listProviders: vi.fn(async () => ({ providers: [], model_note: "" })),
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
    setUsages: vi.fn(),
    setDefault: vi.fn(),
    verify: vi.fn(),
    getPublicCatalog: vi.fn(),
    resolveUsage: vi.fn(),
  },
  modelErrorMessage: (error: unknown) =>
    error instanceof Error ? error.message : String(error),
}));

type Mock = ReturnType<typeof vi.fn>;

const model = (overrides: Record<string, unknown> = {}) => ({
  modelId: "model-a",
  displayName: "M-A",
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
  name: "工作用 A",
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

const resolved = (overrides: Record<string, unknown> = {}) => ({
  credentialId: "key_a",
  modelId: "model-a",
  source: "default",
  usable: true,
  unavailableReason: "",
  credentialName: "工作用 A",
  ...overrides,
});

const effective = (overrides: Record<string, unknown> = {}) => ({
  ok: true,
  usageKey: "main-chat",
  credentialId: "key_b",
  modelId: "model-b",
  source: "explicit",
  matchedUsageKey: "general-chat",
  usable: true,
  unavailableReason: "",
  reasonCode: "",
  detailCode: "",
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
  resolved: resolved(),
  note: "",
  daily: true,
  delivery: "model",
  ...overrides,
});

const twoConnections = () => [
  connection({
    models: [model({ modelId: "model-a", displayName: "M-A" }), model({ modelId: "model-a2", displayName: "M-A2" })],
  }),
  connection({
    credentialId: "key_b",
    name: "工作用 B",
    endpoint: "https://api.openai.com/v1",
    providerId: "openai",
    providerName: "OpenAI",
    models: [model({ modelId: "model-b", displayName: "M-B" })],
  }),
];

function catalog(overrides: Record<string, unknown> = {}) {
  return {
    connections: twoConnections(),
    default: { credentialId: "key_a", modelId: "model-a" },
    usages: [
      usage({ usageKey: "main-chat", label: "主对话" }),
      usage({ usageKey: "general-chat", label: "通用对话" }),
    ],
    discovery: {},
    ...overrides,
  };
}

const mounted: VueWrapper[] = [];
function mountIt(component: unknown, props: Record<string, unknown> = {}): VueWrapper {
  const wrapper = mount(component as never, { props: props as never });
  mounted.push(wrapper as VueWrapper);
  return wrapper as VueWrapper;
}

function rowByText(wrapper: VueWrapper, text: string): DOMWrapper<Element> {
  const row = wrapper.findAll(".row").find((item) => item.text().includes(text));
  expect(row, "找不到用途行：" + text).toBeTruthy();
  return row!;
}

function buttonByText(
  wrapper: { findAll(selector: string): DOMWrapper<Element>[] },
  text: string,
): DOMWrapper<Element> {
  const button = wrapper.findAll("button").find((item) => item.text().trim().includes(text));
  expect(button, "找不到按钮：" + text).toBeTruthy();
  return button!;
}

async function mountPanel(): Promise<VueWrapper> {
  (modelCatalogApi.getCatalog as Mock).mockResolvedValue(catalog());
  (api.listCredentials as Mock).mockResolvedValue({ credentials: [], default_key_id: null });
  (api.listProviders as Mock).mockResolvedValue({ providers: [], model_note: "" });
  const wrapper = mountIt(ModelServicesPanel, {});
  await flushPromises();
  return wrapper;
}

async function pickModel(
  wrapper: VueWrapper,
  row: DOMWrapper<Element>,
  modelText: string,
): Promise<void> {
  await buttonByText(row, "指定模型").trigger("click");
  await flushPromises();
  const target = wrapper.findAll("button.model").find((item) => item.text().includes(modelText));
  expect(target, "找不到候选模型：" + modelText).toBeTruthy();
  await target!.trigger("click");
  await flushPromises();
}

beforeEach(() => {
  vi.clearAllMocks();
  resetModelFacts();
});

afterEach(() => {
  for (const wrapper of mounted) wrapper.unmount();
  mounted.length = 0;
  resetModelFacts();
});

describe("合并行保存 · 一次原子批量请求", () => {
  it("选一次模型只发一次 POST /api/models/usages，两个 key 一起提交", async () => {
    const wrapper = await mountPanel();
    await pickModel(wrapper, rowByText(wrapper, "日常对话"), "M-A2");

    expect(modelCatalogApi.setUsages).toHaveBeenCalledTimes(1);
    expect((modelCatalogApi.setUsages as Mock).mock.calls[0][0]).toEqual({
      assignments: [
        { usageKey: "main-chat", credentialId: "key_a", modelId: "model-a2" },
        { usageKey: "general-chat", credentialId: "key_a", modelId: "model-a2" },
      ],
    });
    // 旧的分两次保存路径不再使用。
    expect(modelCatalogApi.setUsage).not.toHaveBeenCalled();
    // 成功后读回完整新配置（reload 目录），而不是只相信请求成功。
    expect((modelCatalogApi.getCatalog as Mock).mock.calls.length).toBeGreaterThanOrEqual(2);
  });

  it("批量保存失败：如实报错、不 reload 成成功、不调用单用途接口", async () => {
    (modelCatalogApi.setUsages as Mock).mockRejectedValue(
      new Error("批量保存失败：第 2 项用途分配无效"),
    );
    const wrapper = await mountPanel();
    // 打开候选会按用途取一次只读目录（第二轮 C）；它不是「保存成功后 reload」。
    await buttonByText(rowByText(wrapper, "日常对话"), "指定模型").trigger("click");
    await flushPromises();
    const loadsAfterOpen = (modelCatalogApi.getCatalog as Mock).mock.calls.length;
    const target = wrapper.findAll("button.model").find((item) => item.text().includes("M-A2"));
    expect(target, "找不到候选模型：M-A2").toBeTruthy();
    await target!.trigger("click");
    await flushPromises();

    expect(modelCatalogApi.setUsages).toHaveBeenCalledTimes(1);
    expect(modelCatalogApi.setUsage).not.toHaveBeenCalled();
    // 失败 = 不 reload（没有「保存成功」的假象），并保留真实错误。
    expect((modelCatalogApi.getCatalog as Mock).mock.calls.length).toBe(loadsAfterOpen);
    const merged = rowByText(wrapper, "日常对话");
    expect(merged.text()).toContain("批量保存失败");
  });

  it("恢复跟随默认：一次批量请求给两个 key 都传 null", async () => {
    (modelCatalogApi.getCatalog as Mock).mockResolvedValue(
      catalog({
        usages: [
          usage({
            usageKey: "main-chat",
            label: "主对话",
            followsDefault: false,
            credentialId: "key_a",
            modelId: "model-a",
            resolved: resolved({ source: "explicit", modelId: "model-a" }),
          }),
          usage({
            usageKey: "general-chat",
            label: "通用对话",
            followsDefault: false,
            credentialId: "key_a",
            modelId: "model-a",
            resolved: resolved({ source: "explicit", modelId: "model-a" }),
          }),
        ],
      }),
    );
    (api.listCredentials as Mock).mockResolvedValue({ credentials: [], default_key_id: null });
    (api.listProviders as Mock).mockResolvedValue({ providers: [], model_note: "" });
    const wrapper = mountIt(ModelServicesPanel, {});
    await flushPromises();

    const merged = rowByText(wrapper, "日常对话");
    await buttonByText(merged, "恢复跟随默认").trigger("click");
    await flushPromises();

    expect(modelCatalogApi.setUsages).toHaveBeenCalledTimes(1);
    expect((modelCatalogApi.setUsages as Mock).mock.calls[0][0]).toEqual({
      assignments: [
        { usageKey: "main-chat", credentialId: null, modelId: null },
        { usageKey: "general-chat", credentialId: null, modelId: null },
      ],
    });
    expect(modelCatalogApi.setUsage).not.toHaveBeenCalled();
  });

  it("非合并用途（vision）仍然是一次批量请求、只含它自己", async () => {
    (modelCatalogApi.getCatalog as Mock).mockResolvedValue(
      catalog({
        usages: [
          usage({ usageKey: "main-chat", label: "主对话" }),
          usage({ usageKey: "general-chat", label: "通用对话" }),
          usage({
            usageKey: "vision",
            label: "看图 / 视觉",
            kind: "vision",
            group: "special",
            followsDefault: false,
            credentialId: "key_a",
            modelId: "model-a",
            resolved: resolved({ source: "explicit", modelId: "model-a" }),
          }),
        ],
      }),
    );
    (api.listCredentials as Mock).mockResolvedValue({ credentials: [], default_key_id: null });
    (api.listProviders as Mock).mockResolvedValue({ providers: [], model_note: "" });
    const wrapper = mountIt(ModelServicesPanel, {});
    await flushPromises();

    await pickModel(wrapper, rowByText(wrapper, "看图 / 视觉"), "M-A2");

    expect(modelCatalogApi.setUsages).toHaveBeenCalledTimes(1);
    expect((modelCatalogApi.setUsages as Mock).mock.calls[0][0]).toEqual({
      assignments: [{ usageKey: "vision", credentialId: "key_a", modelId: "model-a2" }],
    });
    expect(modelCatalogApi.setUsage).not.toHaveBeenCalled();
  });

  it("迟到失败不覆盖后发请求的成功状态（seq 保护保留）", async () => {
    let rejectFirst: (reason: unknown) => void = () => {};
    (modelCatalogApi.setUsages as Mock)
      .mockImplementationOnce(
        () =>
          new Promise((_resolve, reject) => {
            rejectFirst = reject;
          }),
      )
      .mockResolvedValueOnce({ ok: true, assignments: [], usages: [] });

    const wrapper = await mountPanel();
    // 第一次保存：请求挂起（旧请求）。
    await pickModel(wrapper, rowByText(wrapper, "日常对话"), "M-A2");
    // 第二次保存：后发先至，成功。
    await pickModel(wrapper, rowByText(wrapper, "日常对话"), "M-A");
    await flushPromises();

    expect(modelCatalogApi.setUsages).toHaveBeenCalledTimes(2);
    const merged = rowByText(wrapper, "日常对话");
    // 旧请求此刻才失败：不得把它写进 UI（不能覆盖后发请求的成功状态）。
    rejectFirst(new Error("旧请求失败"));
    await flushPromises();
    expect(merged.text()).not.toContain("旧请求失败");
    expect(merged.text()).not.toContain("正在保存");
  });
});

describe("合并行显示 · 真实生效目标 effective", () => {
  function section(overrides: Record<string, unknown> = {}) {
    return mountIt(UsageAssignmentSection, {
      connections: twoConnections(),
      catalog: catalog(overrides),
      busyKey: null,
      errors: {},
    });
  }

  it("main-chat 与 general-chat 配置不一致时显示 effective（general-chat 优先）", () => {
    const wrapper = section({
      usages: [
        usage({
          usageKey: "main-chat",
          label: "主对话",
          resolved: resolved({ credentialId: "key_a", modelId: "model-a", credentialName: "工作用 A" }),
          effective: effective({ credentialId: "key_b", modelId: "model-b", matchedUsageKey: "general-chat" }),
        }),
        usage({
          usageKey: "general-chat",
          label: "通用对话",
          followsDefault: false,
          credentialId: "key_b",
          modelId: "model-b",
          resolved: resolved({ credentialId: "key_b", modelId: "model-b", credentialName: "工作用 B" }),
          effective: effective({ credentialId: "key_b", modelId: "model-b", matchedUsageKey: "general-chat" }),
        }),
      ],
    });
    const merged = rowByText(wrapper, "日常对话");
    // 真实调用走 general-chat 的 key_b/model-b：显示必须与之一致。
    expect(merged.text()).toContain("工作用 B");
    expect(merged.text()).toContain("model-b");
    expect(merged.text()).not.toContain("工作用 A · model-a");
  });

  it("没有 effective 时合并行回退 resolved（老后端兼容）", () => {
    const wrapper = section();
    const merged = rowByText(wrapper, "日常对话");
    expect(merged.text()).toContain("工作用 A · model-a");
  });

  it("effective 不可用时如实显示不可用原因（不显示旧 resolved 目标）", () => {
    const wrapper = section({
      usages: [
        usage({
          usageKey: "main-chat",
          label: "主对话",
          resolved: resolved(),
          effective: effective({
            ok: false,
            credentialId: null,
            modelId: null,
            usable: false,
            unavailableReason: "通用对话已显式指定 key_b，但它现在不可用：连接已停用",
            reasonCode: "explicit_unavailable",
          }),
        }),
        usage({ usageKey: "general-chat", label: "通用对话" }),
      ],
    });
    const merged = rowByText(wrapper, "日常对话");
    expect(merged.text()).toContain("连接已停用");
    expect(merged.text()).not.toContain("工作用 A · model-a");
  });
});

describe("当前生效参数 · 只读区使用 effective", () => {
  it("main-chat 的 credential/model 来自 effective（与真实调用一致）", async () => {
    (modelCatalogApi.getCatalog as Mock).mockResolvedValue(
      catalog({
        usages: [
          usage({ usageKey: "main-chat", label: "主对话" }),
          usage({
            usageKey: "general-chat",
            label: "通用对话",
            followsDefault: false,
            credentialId: "key_b",
            modelId: "model-b",
            resolved: resolved({ credentialId: "key_b", modelId: "model-b", credentialName: "工作用 B" }),
            effective: effective({ credentialId: "key_b", modelId: "model-b" }),
          }),
        ],
      }),
    );
    (api.listCredentials as Mock).mockResolvedValue({ credentials: [], default_key_id: null });
    (api.listProviders as Mock).mockResolvedValue({ providers: [], model_note: "" });
    mountIt(ModelServicesPanel, {});
    await flushPromises();

    const mainChat = modelFacts.usages.find((item) => item.usageKey === "main-chat");
    expect(mainChat).toBeTruthy();
    expect(mainChat).toMatchObject({
      credentialId: "key_b",
      modelId: "model-b",
      connectionName: "工作用 B",
    });
    expect(mainChat!.resolved).toEqual({ credentialId: "key_b", modelId: "model-b" });
  });

  it("effective 不可用时只读区如实说不可用，不把旧 resolved 目标说成当前生效", async () => {
    (modelCatalogApi.getCatalog as Mock).mockResolvedValue(
      catalog({
        usages: [
          usage({
            usageKey: "main-chat",
            label: "主对话",
            effective: effective({
              ok: false,
              credentialId: null,
              modelId: null,
              usable: false,
              unavailableReason: "连接已停用",
              reasonCode: "explicit_unavailable",
            }),
          }),
          usage({ usageKey: "general-chat", label: "通用对话" }),
        ],
      }),
    );
    (api.listCredentials as Mock).mockResolvedValue({ credentials: [], default_key_id: null });
    (api.listProviders as Mock).mockResolvedValue({ providers: [], model_note: "" });
    mountIt(ModelServicesPanel, {});
    await flushPromises();

    const mainChat = modelFacts.usages.find((item) => item.usageKey === "main-chat");
    expect(mainChat).toBeTruthy();
    expect(mainChat!.credentialId).toBeNull();
    expect(mainChat!.modelId).toBeNull();
    expect(String(mainChat!.note ?? "")).toContain("不可用");
    expect(String(mainChat!.note ?? "")).toContain("连接已停用");
  });
});