/**
 * 发布第二轮 B+C：用途分配真实生效性 + 候选按用途能力判断（全部 fake，不联网）。
 *
 * B（F05/F06）：没有真实读取点的用途（研究 / 视频 / 语音 / 嵌入 / 写代码 / 自定义标签）
 * 不再提供独立模型选择；历史配置仍可读并标「历史配置（未生效）」，清除入口保留。
 * C（F07）：候选按**当前用途**判断能力，四类结果正确；提交与真实调用同一份判断。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount, type DOMWrapper, type VueWrapper } from "@vue/test-utils";
import CandidatePicker from "../CandidatePicker.vue";
import UsageAssignmentSection from "../UsageAssignmentSection.vue";
import ModelServicesPanel from "../ModelServicesPanel.vue";
import { api } from "../../../services/api";
import { modelCatalogApi } from "../../../services/modelCatalog";
import { modelFacts, resetModelFacts } from "../../../views/settings/modelFacts";

vi.mock("../../../services/api", () => {
  class ApiError extends Error {
    constructor(
      readonly status: number,
      readonly path: string,
      detail: string,
      readonly body?: unknown,
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
  modelErrorMessage: (error: unknown) => {
    const body = (error as { body?: { detail?: unknown } } | null)?.body;
    if (body && typeof body.detail === "string" && body.detail.trim()) {
      return body.detail;
    }
    const text = error instanceof Error ? error.message : String(error);
    const marker = text.lastIndexOf(": ");
    return marker >= 0 && text.slice(0, marker).includes("->")
      ? text.slice(marker + 2)
      : text;
  },
}));

type Mock = ReturnType<typeof vi.fn>;

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
  modelId: "m1",
  source: "default",
  usable: true,
  unavailableReason: "",
  credentialName: "工作用 A",
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
  assignmentEffective: true,
  historical: false,
  historicalTarget: null,
  requiresCapabilities: [],
  ...overrides,
});

const baseUsages = () => [
  usage({ usageKey: "main-chat", label: "主对话" }),
  usage({ usageKey: "general-chat", label: "通用对话" }),
  usage({
    usageKey: "vision",
    label: "看图 / 视觉",
    kind: "vision",
    group: "special",
    followsDefault: false,
    credentialId: "key_a",
    modelId: "m1",
    resolved: resolved({ source: "explicit", modelId: "m1" }),
    requiresCapabilities: ["vision"],
  }),
  usage({
    usageKey: "video",
    label: "视频",
    kind: "video",
    group: "special",
    delivery: "existing",
    assignmentEffective: false,
    followsDefault: false,
    resolved: null,
    note: "未接通独立模型调用路径：沿用现有处理方式，不继承默认对话模型",
  }),
  usage({
    usageKey: "research",
    label: "研究",
    group: "primary",
    delivery: "unsupported",
    assignmentEffective: false,
    followsDefault: false,
    resolved: null,
    note: "当前没有独立的研究模型调用路径：只保留历史标签与策略回退",
  }),
  usage({ usageKey: "subagent", label: "子任务" }),
  usage({
    usageKey: "memory-organize",
    label: "记忆整理",
    group: "background",
  }),
  usage({
    usageKey: "knowledge-distill",
    label: "知识提炼",
    group: "background",
  }),
  usage({
    usageKey: "entity-extract",
    label: "实体提炼",
    group: "background",
  }),
  usage({
    usageKey: "code",
    label: "写代码",
    group: "primary",
    delivery: "unsupported",
    assignmentEffective: false,
    followsDefault: false,
    resolved: null,
    note: "当前没有独立的代码模型调用路径：写代码由日常对话的模型完成，跟随日常对话",
  }),
  usage({
    usageKey: "embedding",
    label: "向量 / 嵌入",
    kind: "embedding",
    group: "special",
    delivery: "existing",
    assignmentEffective: false,
    followsDefault: false,
    resolved: null,
    credentialId: "key_a",
    modelId: "m1",
    historical: true,
    historicalTarget: resolved({ source: "historical", modelId: "m1" }),
    note: "历史配置（未生效）：这个用途没有独立的模型调用路径，历史覆盖只保留可见性与清除入口，不会影响真实请求",
  }),
  usage({
    usageKey: "custom:写作助手",
    label: "写作助手",
    group: "legacy",
    delivery: "unsupported",
    assignmentEffective: false,
    followsDefault: false,
    resolved: null,
    credentialId: "key_a",
    modelId: "m1",
    historical: true,
    historicalTarget: resolved({ source: "historical", modelId: "m1" }),
    note: "历史配置（未生效）：这个用途没有独立的模型调用路径，历史覆盖只保留可见性与清除入口，不会影响真实请求",
  }),
];

const catalog = (overrides: Record<string, unknown> = {}) => ({
  connections: [connection()],
  default: { credentialId: "key_a", modelId: "m1" },
  usages: baseUsages(),
  discovery: {},
  usageScope: null,
  ...overrides,
});

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

function section(overrides: Record<string, unknown> = {}): VueWrapper {
  return mountIt(UsageAssignmentSection, {
    connections: [connection()],
    catalog: catalog(overrides),
    busyKey: null,
    errors: {},
  });
}

async function mountPanel(): Promise<VueWrapper> {
  (modelCatalogApi.getCatalog as Mock).mockResolvedValue(catalog());
  (api.listCredentials as Mock).mockResolvedValue({ credentials: [], default_key_id: null });
  (api.listProviders as Mock).mockResolvedValue({ providers: [], model_note: "" });
  const wrapper = mountIt(ModelServicesPanel, {});
  await flushPromises();
  return wrapper;
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

// ---------------------------------------------------------------------------
// C：候选按当前用途判断（四类结果）
// ---------------------------------------------------------------------------

describe("候选按当前用途判断能力", () => {
  function picker(models: Record<string, unknown>[]): VueWrapper {
    return mountIt(CandidatePicker, {
      connections: [connection({ models })],
      selected: null,
      usageLabel: "看图 / 视觉",
      usageKey: "vision",
    });
  }

  it("明确支持该用途能力：可选，并说清依据", async () => {
    const wrapper = picker([
      model({
        modelId: "vision-capable",
        displayName: "Vision",
        capabilities: ["vision"],
        usageKey: "vision",
        usageUsable: true,
        usageReason: "",
        capabilityState: "supported",
      }),
    ]);
    const button = wrapper.findAll("button.model")[0];
    expect(button.attributes("disabled")).toBeUndefined();
    expect(wrapper.text()).toContain("该用途能力已声明");
    await button.trigger("click");
    expect(wrapper.emitted("pick")?.[0]).toEqual([
      { credentialId: "key_a", modelId: "vision-capable" },
    ]);
  });

  it("明确缺少所需能力：禁止选择 + 原因，并同时显示「一般可用」这条不同事实", () => {
    const wrapper = picker([
      model({
        modelId: "text-only",
        displayName: "TextOnly",
        capabilities: ["text"],
        usageKey: "vision",
        usageUsable: false,
        usageReason: "该模型没有声明这个用途需要的能力（缺少 vision）",
        capabilityState: "missing",
      }),
    ]);
    const button = wrapper.findAll("button.model")[0];
    expect(button.attributes("disabled")).toBeDefined();
    expect(wrapper.text()).toContain("不适用于该用途");
    expect(wrapper.text()).toContain("缺少 vision");
    // 同一个模型「一般可用、对该用途不适用」是两条不同事实，都要显示出来。
    expect(wrapper.text()).toContain("一般可用");
  });

  it("能力未知：仍可选，只标注未确认（不能判为不支持）", async () => {
    const wrapper = picker([
      model({
        modelId: "unknown-caps",
        displayName: "Unknown",
        capabilities: [],
        usageKey: "vision",
        usageUsable: true,
        usageReason: "",
        capabilityState: "unknown",
      }),
    ]);
    const button = wrapper.findAll("button.model")[0];
    expect(button.attributes("disabled")).toBeUndefined();
    expect(wrapper.text()).toContain("能力未确认");
    await button.trigger("click");
    expect(wrapper.emitted("pick")?.[0]).toEqual([
      { credentialId: "key_a", modelId: "unknown-caps" },
    ]);
  });

  it("连接本身不可用：禁止选择 + 现有原因，不显示能力结论", () => {
    const wrapper = picker([
      model({
        modelId: "disabled-key-model",
        displayName: "Disabled",
        capabilities: ["vision"],
        usable: false,
        unavailableReason: "连接已停用",
        usageKey: "vision",
        usageUsable: false,
        usageReason: "连接已停用",
        capabilityState: "supported",
      }),
    ]);
    const button = wrapper.findAll("button.model")[0];
    expect(button.attributes("disabled")).toBeDefined();
    expect(wrapper.text()).toContain("连接已停用");
    expect(wrapper.text()).not.toContain("该用途能力已声明");
    expect(wrapper.text()).not.toContain("能力未确认");
  });

  it("没有用途维度数据时退回一般可用性（默认模型候选与老后端兼容）", async () => {
    const wrapper = mountIt(CandidatePicker, {
      connections: [connection({ models: [model({ modelId: "plain", displayName: "Plain" })] })],
      selected: null,
    });
    const button = wrapper.findAll("button.model")[0];
    expect(button.attributes("disabled")).toBeUndefined();
    expect(wrapper.text()).not.toContain("能力未确认");
    await button.trigger("click");
    expect(wrapper.emitted("pick")?.[0]).toEqual([{ credentialId: "key_a", modelId: "plain" }]);
  });
});

// ---------------------------------------------------------------------------
// B：不生效用途只读展示 + 历史配置标注
// ---------------------------------------------------------------------------

describe("用途分配 · 不生效的用途改为只读说明", () => {
  it("不提供独立模型选择，并给出一句话当前行为", () => {
    const wrapper = section();
    for (const label of ["视频", "研究", "写代码"]) {
      const row = rowByText(wrapper, label);
      const pickerButton = row
        .findAll("button")
        .find((button) => button.text().includes("指定模型"));
      expect(pickerButton, label + " 不应再有独立的模型选择入口").toBeFalsy();
    }
    expect(rowByText(wrapper, "视频").text()).toContain("沿用现有处理方式");
    expect(rowByText(wrapper, "研究").text()).toContain("未接通独立调用路径");
    expect(rowByText(wrapper, "写代码").text()).toContain("跟随日常对话");
  });

  it("历史配置如实标「历史配置（未生效）」，并保留清除入口", async () => {
    const wrapper = section();
    const row = rowByText(wrapper, "向量 / 嵌入");
    expect(row.text()).toContain("历史配置（未生效）");
    expect(row.text()).toContain("m1");
    await buttonByText(row, "清除历史覆盖").trigger("click");
    expect(wrapper.emitted("assign")?.[0]).toEqual([["embedding"], null]);
  });

  it("历史自定义标签同样只读且可清除", async () => {
    const wrapper = section();
    const row = rowByText(wrapper, "写作助手");
    expect(row.text()).toContain("历史配置（未生效）");
    await buttonByText(row, "清除历史覆盖").trigger("click");
    expect(wrapper.emitted("assign")?.[0]).toEqual([["custom:写作助手"], null]);
  });

  it("真正生效的用途仍然提供独立选择", () => {
    const wrapper = section();
    // 核对过真实读取路径的生效用途：合并日常对话行、子任务、看图、后台三类。
    for (const label of ["日常对话", "子任务", "看图 / 视觉", "记忆整理", "知识提炼", "实体提炼"]) {
      const row = rowByText(wrapper, label);
      const pickerButton = row
        .findAll("button")
        .find((button) => button.text().includes("指定模型"));
      expect(pickerButton, label + " 应当仍可单独指定模型").toBeTruthy();
    }
  });

  it("打开候选时按该用途向服务端取候选判定（只读本地事实，不发验证）", async () => {
    (modelCatalogApi.getCatalog as Mock).mockResolvedValue(
      catalog({
        connections: [
          connection({
            models: [
              model({
                modelId: "text-only",
                displayName: "TextOnly",
                capabilities: ["text"],
                usageKey: "vision",
                usageUsable: false,
                usageReason: "该模型没有声明这个用途需要的能力（缺少 vision）",
                capabilityState: "missing",
              }),
            ],
          }),
        ],
        usageScope: {
          usageKey: "vision",
          label: "看图 / 视觉",
          delivery: "model",
          effective: true,
          requiresCapabilities: ["vision"],
          note: "",
        },
      }),
    );
    const wrapper = section();
    await buttonByText(rowByText(wrapper, "看图 / 视觉"), "指定模型").trigger("click");
    await flushPromises();

    const calls = (modelCatalogApi.getCatalog as Mock).mock.calls;
    const usageCall = calls.find(
      (call) => (call[0] as { usageKey?: string } | undefined)?.usageKey === "vision",
    );
    expect(usageCall, "必须按用途请求候选判定").toBeTruthy();

    const buttons = rowByText(wrapper, "看图 / 视觉").findAll("button.model");
    expect(buttons.length).toBeGreaterThan(0);
    expect(buttons[0].attributes("disabled")).toBeDefined();
    expect(rowByText(wrapper, "看图 / 视觉").text()).toContain("缺少 vision");
  });
});

// ---------------------------------------------------------------------------
// 保存失败：结构化 4xx 显示人话（不把 JSON 当字符串）
// ---------------------------------------------------------------------------

describe("用途分配 · 后端拒绝时显示人话", () => {
  async function pickModel(wrapper: VueWrapper, rowText: string, modelText: string) {
    await buttonByText(rowByText(wrapper, rowText), "指定模型").trigger("click");
    await flushPromises();
    const target = wrapper
      .findAll("button.model")
      .find((item) => item.text().includes(modelText));
    expect(target, "找不到候选模型：" + modelText).toBeTruthy();
    await target!.trigger("click");
    await flushPromises();
  }

  it("结构化 422 的 detail 直接呈现，不显示原始 JSON", async () => {
    (modelCatalogApi.setUsages as Mock).mockRejectedValue({
      status: 422,
      body: {
        ok: false,
        code: "usage_not_effective",
        detail: "「视频」沿用现有处理方式，没有独立的模型调用路径：这里的模型分配不会影响真实请求",
      },
    });
    const wrapper = await mountPanel();
    await pickModel(wrapper, "日常对话", "M1");
    const merged = rowByText(wrapper, "日常对话");
    expect(merged.text()).toContain("这里的模型分配不会影响真实请求");
    expect(merged.text()).not.toContain('"code"');
  });
});

// ---------------------------------------------------------------------------
// 当前生效参数：不生效用途不得把历史模型说成当前生效
// ---------------------------------------------------------------------------

describe("当前生效参数 · 不生效用途不冒充当前目标", () => {
  it("历史配置只出现在用途分配里，不写进「当前生效参数」", async () => {
    await mountPanel();
    const embedding = modelFacts.usages.find((item) => item.usageKey === "embedding");
    expect(embedding).toBeTruthy();
    expect(embedding!.credentialId).toBeNull();
    expect(embedding!.modelId).toBeNull();
    expect(embedding!.status).toBe("existing");
    expect(String(embedding!.note)).toContain("历史配置（未生效）");
  });
});
