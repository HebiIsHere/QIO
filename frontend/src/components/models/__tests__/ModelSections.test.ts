/**
 * 模型目录 / 用途分配 / Key 管理的组件行为（不联网，全部用假数据）。
 *
 * 覆盖任务书 §8 要求的五条：正常接入流程之外的四条界面硬要求 ——
 * 候选不可用原因、恢复跟随默认、删除引用保护、以及「沿用现有处理方式」不被伪装成继承。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount, type DOMWrapper, type VueWrapper } from "@vue/test-utils";
import CandidatePicker from "../CandidatePicker.vue";
import DefaultModelSection from "../DefaultModelSection.vue";
import UsageAssignmentSection from "../UsageAssignmentSection.vue";
import KeyAccessSection from "../KeyAccessSection.vue";
import { api, type CredentialMeta } from "../../../services/api";
import type {
  CatalogResponse,
  CatalogModel,
  ModelConnection,
  UsageAssignment,
} from "../../../services/modelCatalog";

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
    setDefault: vi.fn(),
    verify: vi.fn(),
    getPublicCatalog: vi.fn(),
    resolveUsage: vi.fn(),
  },
  modelErrorMessage: (error: unknown) =>
    error instanceof Error ? error.message : String(error),
}));

const model = (overrides: Partial<CatalogModel> = {}): CatalogModel => ({
  modelId: "m1",
  displayName: "M1",
  source: "discovered",
  capabilities: [],
  verifyState: "verified",
  verifiedAt: "2026-10-27T10:00:00+00:00",
  verifyError: null,
  usable: true,
  unavailableReason: "",
  ...overrides,
});

const connection = (overrides: Partial<ModelConnection> = {}): ModelConnection => ({
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

const usage = (overrides: Partial<UsageAssignment> = {}): UsageAssignment => ({
  usageKey: "main-chat",
  label: "主对话",
  kind: "chat",
  group: "primary",
  modelId: null,
  credentialId: null,
  followsDefault: true,
  inheritsOk: true,
  resolved: {
    credentialId: "key_a",
    modelId: "m1",
    source: "default",
    usable: true,
    unavailableReason: "",
    credentialName: "工作用 DeepSeek",
  },
  note: "",
  daily: true,
  delivery: "model",
  ...overrides,
});

function catalog(overrides: Partial<CatalogResponse> = {}): CatalogResponse {
  return {
    connections: [connection()],
    default: null,
    usages: [usage()],
    discovery: {},
    ...overrides,
  };
}

const meta = (overrides: Partial<CredentialMeta> = {}): CredentialMeta => ({
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
function buttonByText(
  wrapper: { findAll(selector: string): DOMWrapper<Element>[] },
  text: string,
) {
  const button = wrapper.findAll("button").find((item) => item.text().trim().includes(text));
  expect(button, "找不到按钮：" + text).toBeTruthy();
  return button!;
}

afterEach(() => {
  for (const wrapper of mounted) wrapper.unmount();
  mounted.length = 0;
});
beforeEach(() => {
  vi.clearAllMocks();
});

describe("候选列表 · 不可用原因", () => {
  it("不可用候选照样列出并写明原因，不会静默过滤", async () => {
    const wrapper = mountIt(CandidatePicker, {
      connections: [
        connection({
          models: [
            model({ modelId: "good-model", displayName: "Good" }),
            model({
              modelId: "bad-model",
              displayName: "Bad",
              usable: false,
              verifyState: "failed",
              unavailableReason: "这个模型在当前配置上验证没通过",
            }),
          ],
        }),
      ],
      selected: null,
    });
    expect(wrapper.text()).toContain("Bad");
    expect(wrapper.text()).toContain("这个模型在当前配置上验证没通过");
    const disabled = wrapper.findAll("button.model").find((item) => item.text().includes("Bad"));
    expect(disabled!.attributes("disabled")).toBeDefined();
    const good = wrapper.findAll("button.model").find((item) => item.text().includes("Good"));
    await good!.trigger("click");
    expect(wrapper.emitted("pick")?.[0]).toEqual([
      { credentialId: "key_a", modelId: "good-model" },
    ]);
  });
});

describe("默认模型 · 跟随默认与恢复", () => {
  it("没有默认模型时如实说明会回退到兼容规则", () => {
    const wrapper = mountIt(DefaultModelSection, {
      connections: [connection()],
      catalog: catalog({ usages: [usage({ resolved: null, inheritsOk: false })] }),
      busy: false,
      error: "",
    });
    expect(wrapper.text()).toContain("还没有设置默认模型");
    expect(wrapper.text()).toContain("回退到兼容的旧凭据选择规则");
  });

  it("选择候选后向上抛出保存默认模型的请求", async () => {
    const wrapper = mountIt(DefaultModelSection, {
      connections: [
        connection({
          models: [
            model({ modelId: "m1", displayName: "M1" }),
            model({ modelId: "m2", displayName: "M2" }),
          ],
        }),
      ],
      catalog: catalog({
        default: { credentialId: "key_a", modelId: "m1" },
        usages: [usage({ modelId: "m1", credentialId: "key_a", followsDefault: false })],
      }),
      busy: false,
      error: "",
    });
    await buttonByText(wrapper, "更换默认模型").trigger("click");
    const target = wrapper.findAll("button.model").find((item) => item.text().includes("M2"));
    await target!.trigger("click");
    expect(wrapper.emitted("save")?.[0]).toEqual([{ credentialId: "key_a", modelId: "m2" }]);
  });
});

describe("用途分配 · 合并日常入口 / 恢复跟随默认 / 沿用现有处理方式", () => {
  const usages: UsageAssignment[] = [
    usage({ usageKey: "main-chat", label: "主对话" }),
    usage({ usageKey: "general-chat", label: "通用对话" }),
    usage({
      usageKey: "vision",
      label: "看图 / 视觉",
      kind: "vision",
      group: "special",
      followsDefault: false,
      modelId: "m1",
      credentialId: "key_a",
      resolved: {
        credentialId: "key_a",
        modelId: "m1",
        source: "explicit",
        usable: true,
        unavailableReason: "",
        credentialName: "工作用 DeepSeek",
      },
    }),
    // 发布第二轮 B：这三类没有独立调用路径，界面只读（delivery 来自后端）。
    usage({
      usageKey: "video",
      label: "视频",
      kind: "video",
      group: "special",
      delivery: "existing",
      assignmentEffective: false,
      followsDefault: false,
      resolved: null,
      inheritsOk: false,
    }),
    usage({
      usageKey: "research",
      label: "研究",
      kind: "chat",
      group: "primary",
      delivery: "unsupported",
      assignmentEffective: false,
      followsDefault: false,
      resolved: null,
      inheritsOk: false,
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
      inheritsOk: false,
      credentialId: "key_a",
      modelId: "m1",
      historical: true,
      historicalTarget: {
        credentialId: "key_a",
        modelId: "m1",
        source: "historical",
        usable: true,
        unavailableReason: "",
        credentialName: "工作用 DeepSeek",
      },
      note: "历史配置（未生效）：这个用途没有独立的模型调用路径，历史覆盖只保留可见性与清除入口，不会影响真实请求",
    }),
  ];

  function mountSection() {
    return mountIt(UsageAssignmentSection, {
      connections: [
        connection({
          models: [
            model({ modelId: "m1", displayName: "M1" }),
            model({ modelId: "m2", displayName: "M2" }),
          ],
        }),
      ],
      catalog: catalog({ usages }),
      busyKey: null,
      errors: {},
    });
  }

  it("主对话与通用对话合并成一行；恢复跟随默认会把显式分配清掉", async () => {
    const wrapper = mountSection();
    expect(wrapper.text()).toContain("日常对话");
    expect(wrapper.text()).toContain("主对话 / 通用对话合并显示");
    expect(wrapper.text()).not.toContain("通用对话\n");

    const visionRow = wrapper.findAll(".row").find((row) => row.text().includes("看图 / 视觉"));
    const restore = visionRow!.findAll("button").find((button) => button.text().includes("恢复跟随默认"));
    expect(restore, "显式指定的用途应提供恢复跟随默认").toBeTruthy();
    await restore!.trigger("click");
    expect(wrapper.emitted("assign")?.[0]).toEqual([["vision"], null]);
  });

  it("video 显示「沿用现有处理方式」，不会被伪装成继承默认对话模型", () => {
    const wrapper = mountSection();
    const videoRow = wrapper.findAll(".row").find((row) => row.text().includes("视频"));
    expect(videoRow!.text()).toContain("沿用现有处理方式");
  });

  it("research 如实标注尚未接通独立调用路径", () => {
    const wrapper = mountSection();
    const researchRow = wrapper.findAll(".row").find((row) => row.text().includes("研究"));
    expect(researchRow!.text()).toContain("未接通独立调用路径");
  });

  it("embedding 改为只读说明：没有独立调用路径，历史覆盖标未生效且可清除", async () => {
    const wrapper = mountSection();
    const embeddingRow = wrapper.findAll(".row").find((row) => row.text().includes("向量 / 嵌入"));
    // 不再提供「指定模型」：换外部嵌入模型并不会被真实调用读取。
    const picker = embeddingRow!
      .findAll("button")
      .find((button) => button.text().includes("指定模型"));
    expect(picker, "embedding 不应再有独立模型选择").toBeFalsy();
    expect(embeddingRow!.text()).toContain("沿用现有处理方式");
    expect(embeddingRow!.text()).toContain("历史配置（未生效）");
    await buttonByText(embeddingRow!, "清除历史覆盖").trigger("click");
    expect(wrapper.emitted("assign")).toEqual([[["embedding"], null]]);
  });
});

describe("Key 管理 · 删除引用保护", () => {
  async function openManageAndDelete(wrapper: VueWrapper) {
    const manageTab = wrapper.findAll(".tab").find((tab) => tab.text().includes("管理"));
    await manageTab!.trigger("click");
    await buttonByText(wrapper, "删除连接").trigger("click");
    await flushPromises();
  }

  it("仍被已保存用途引用的连接不删，弹窗列出引用", async () => {
    const wrapper = mountIt(KeyAccessSection, {
      connections: [connection()],
      credentials: [meta()],
      catalog: catalog({
        usages: [
          usage({
            usageKey: "vision",
            label: "看图 / 视觉",
            kind: "vision",
            followsDefault: false,
            credentialId: "key_a",
            modelId: "m1",
          }),
        ],
      }),
      providers: [],
      draftRefs: [],
    });
    await openManageAndDelete(wrapper);
    expect(wrapper.find(".qio-confirm").text()).toContain("还被用途引用");
    expect(wrapper.find(".qio-confirm").text()).toContain("看图 / 视觉");
    expect(api.deleteCredential).not.toHaveBeenCalled();
  });

  it("未保存草稿里的引用同样阻止删除", async () => {
    const wrapper = mountIt(KeyAccessSection, {
      connections: [connection()],
      credentials: [meta()],
      catalog: catalog({ usages: [] }),
      providers: [],
      draftRefs: ["key_a"],
    });
    await openManageAndDelete(wrapper);
    expect(wrapper.find(".qio-confirm").text()).toContain("尚未保存的用途改动");
    expect(api.deleteCredential).not.toHaveBeenCalled();
  });

  it("没有引用时确认后才真正删除", async () => {
    (api.deleteCredential as ReturnType<typeof vi.fn>).mockResolvedValue({ ok: true, key_id: "key_a" });
    const wrapper = mountIt(KeyAccessSection, {
      connections: [connection()],
      credentials: [meta()],
      catalog: catalog({ usages: [usage({ resolved: null, inheritsOk: false })] }),
      providers: [],
      draftRefs: [],
    });
    await openManageAndDelete(wrapper);
    const confirm = wrapper.find(".qio-confirm");
    expect(confirm.text()).toContain("删除这条连接");
    await confirm.find('[data-qio-confirm-action="confirm"]').trigger("click");
    await flushPromises();
    expect(api.deleteCredential).toHaveBeenCalledWith("key_a");
    expect(wrapper.emitted("changed")).toBeTruthy();
  });
});