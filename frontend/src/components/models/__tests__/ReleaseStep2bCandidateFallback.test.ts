/**
 * C（F07 返修）：用途判定「读取中 / 读取失败」时，明确只有文字能力的模型仍不可选；
 * 能力真正未知的模型沿用既定规则（仍可选，标注「能力未确认」）。
 *
 * 触发条件：打开候选时先渲染的是**不带用途维度**的连接列表（服务端判定还没回来），
 * 旧实现于是把「一般可用」当成「适用于该用途」，看图时能选中只有文字能力的模型。
 * 修复：候选组件用用途要求的必备能力做本地兜底（只看模型已声明能力这个事实，不按名字猜）。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";
import CandidatePicker from "../CandidatePicker.vue";
import UsageAssignmentSection from "../UsageAssignmentSection.vue";
import { modelCatalogApi } from "../../../services/modelCatalog";
import type { CatalogModel, ModelConnection } from "../../../services/modelCatalog";

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
  modelErrorMessage: (error: unknown) => (error instanceof Error ? error.message : String(error)),
}));

function model(over: Partial<CatalogModel> & { modelId: string }): CatalogModel {
  return {
    displayName: over.modelId,
    source: "discovered",
    capabilities: [],
    verifyState: "verified",
    verifiedAt: null,
    verifyError: null,
    usable: true,
    unavailableReason: "",
    ...over,
  };
}

const connections: ModelConnection[] = [
  {
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
    models: [
      model({ modelId: "vision-ok", capabilities: ["vision"] }),
      model({ modelId: "text-only", capabilities: ["text"] }),
      model({ modelId: "unknown-caps" }),
    ],
  },
];

function mountPicker(over: Record<string, unknown> = {}) {
  return mount(CandidatePicker, {
    props: {
      connections,
      usageLabel: "看图 / 视觉",
      usageKey: "vision",
      requiredCapabilities: ["vision"],
      ...over,
    },
  });
}

function modelButton(wrapper: ReturnType<typeof mountPicker>, modelId: string) {
  return wrapper
    .findAll("button.model")
    .find((button) => button.text().includes(modelId));
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe("候选：用途判定未就绪时仍按已声明能力兜底", () => {
  it("读取中：明确缺少 vision 的模型不可选并说明原因，未知能力仍可选", () => {
    const wrapper = mountPicker({ usagePending: true });
    expect(wrapper.text()).toContain("正在读取");

    const textOnly = modelButton(wrapper, "text-only");
    expect(textOnly, "找不到 text-only 候选").toBeTruthy();
    expect(textOnly!.attributes("disabled")).toBeDefined();
    expect(textOnly!.text()).toContain("缺少");

    const unknown = modelButton(wrapper, "unknown-caps");
    expect(unknown, "找不到 unknown-caps 候选").toBeTruthy();
    expect(unknown!.attributes("disabled")).toBeUndefined();
    expect(unknown!.text()).toContain("能力未确认");

    const vision = modelButton(wrapper, "vision-ok");
    expect(vision!.attributes("disabled")).toBeUndefined();
  });

  it("读取失败：仍按已声明能力兜底，不把明确缺能力的模型放出来", () => {
    const wrapper = mountPicker({
      usagePending: false,
      usageError: "没能读到这个用途的候选判定：网络错误",
    });
    expect(wrapper.text()).toContain("没能读到这个用途的候选判定");
    expect(modelButton(wrapper, "text-only")!.attributes("disabled")).toBeDefined();
    expect(modelButton(wrapper, "unknown-caps")!.attributes("disabled")).toBeUndefined();
  });

  it("服务端用途判定到达后以服务端结论为准（不再本地推断）", () => {
    const scoped: ModelConnection[] = [
      {
        ...connections[0],
        models: [
          model({ modelId: "vision-ok", capabilities: ["vision"], usageKey: "vision", usageUsable: true, capabilityState: "supported" }),
          model({ modelId: "text-only", capabilities: ["text"], usageKey: "vision", usageUsable: false, capabilityState: "missing", usageReason: "能力不满足当前用途（缺少 vision）" }),
          model({ modelId: "unknown-caps", usageKey: "vision", usageUsable: true, capabilityState: "unknown" }),
        ],
      },
    ];
    const wrapper = mountPicker({ connections: scoped, usagePending: false });
    expect(modelButton(wrapper, "text-only")!.attributes("disabled")).toBeDefined();
    expect(modelButton(wrapper, "text-only")!.text()).toContain("缺少 vision");
    expect(modelButton(wrapper, "unknown-caps")!.attributes("disabled")).toBeUndefined();
    expect(modelButton(wrapper, "vision-ok")!.text()).toContain("该用途能力已声明");
  });

  it("没有能力要求的用途（日常对话）不受影响：一般可用即可选", () => {
    const wrapper = mountPicker({
      usageKey: "main-chat",
      usageLabel: "日常对话",
      requiredCapabilities: [],
      usagePending: true,
    });
    expect(modelButton(wrapper, "text-only")!.attributes("disabled")).toBeUndefined();
    expect(modelButton(wrapper, "unknown-caps")!.attributes("disabled")).toBeUndefined();
  });
});
describe("用途分配里的候选面板：读取中/失败时也按已声明能力兜底", () => {
  function visionCatalog(connectionsInput: ModelConnection[]) {
    return {
      connections: connectionsInput,
      default: null,
      discovery: {},
      usages: [
        {
          usageKey: "vision",
          label: "看图 / 视觉",
          kind: "vision",
          group: "special",
          modelId: null,
          credentialId: null,
          followsDefault: true,
          inheritsOk: false,
          resolved: null,
          note: "只有真能读图的模型才该放这里",
          daily: false,
          delivery: "model" as const,
          assignmentEffective: true,
          requiresCapabilities: ["vision"],
        },
      ],
    };
  }

  it("打开候选时先显示读取中，明确缺能力的模型已经不可选；服务端结论到达后按服务端算", async () => {
    let release: ((value: unknown) => void) | null = null;
    const pending = new Promise((resolve) => {
      release = resolve;
    });
    (modelCatalogApi.getCatalog as unknown as { mockImplementationOnce: (fn: () => unknown) => void })
      .mockImplementationOnce(() => pending);

    const wrapper = mount(UsageAssignmentSection, {
      props: { connections, catalog: visionCatalog(connections) },
    });
    const visionRow = wrapper.findAll(".row").find((row) => row.text().includes("看图 / 视觉"));
    expect(visionRow, "找不到看图行").toBeTruthy();
    const pickerButton = visionRow!
      .findAll("button")
      .find((button) => button.text().includes("指定模型"));
    expect(pickerButton, "看图用途应可选").toBeTruthy();
    await pickerButton!.trigger("click");
    await flushPromises();

    // 服务端判定还没回来：本地兜底先挡住明确缺能力的候选
    expect(wrapper.text()).toContain("正在读取这个用途的候选判定");
    const textOnly = wrapper
      .findAll("button.model")
      .find((button) => button.text().includes("text-only"));
    expect(textOnly, "找不到 text-only 候选").toBeTruthy();
    expect(textOnly!.attributes("disabled")).toBeDefined();

    // 服务端按用途算过的结论到达：以服务端为准（能力未知仍可选）
    const scoped: ModelConnection[] = [
      {
        ...connections[0],
        models: [
          model({ modelId: "text-only", capabilities: ["text"], usageKey: "vision", usageUsable: false, capabilityState: "missing", usageReason: "能力不满足当前用途（缺少 vision）" }),
          model({ modelId: "unknown-caps", usageKey: "vision", usageUsable: true, capabilityState: "unknown" }),
        ],
      },
    ];
    release!({ connections: scoped });
    await flushPromises();
    expect(wrapper.text()).not.toContain("正在读取这个用途的候选判定");
    const textOnlyAfter = wrapper
      .findAll("button.model")
      .find((button) => button.text().includes("text-only"));
    expect(textOnlyAfter!.attributes("disabled")).toBeDefined();
    const unknownAfter = wrapper
      .findAll("button.model")
      .find((button) => button.text().includes("unknown-caps"));
    expect(unknownAfter!.attributes("disabled")).toBeUndefined();
  });
});
