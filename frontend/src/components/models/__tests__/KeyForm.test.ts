/**
 * Key 接入表单：正常流程（选服务商 → 填 Key → 获取模型 → 保存）与高级入口收起。
 *
 * 这些用例守护三条产品要求：
 * - 正常情况下不要求填地址 / 协议 / 模型名；
 * - 本地 Key 前缀提示**不是**验证成功，只是本地猜测；
 * - 改地址 / 协议必须重新填 Key 并显式确认（与后端规则一致）。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { flushPromises, mount, type VueWrapper } from "@vue/test-utils";
import KeyForm from "../KeyForm.vue";
import { api } from "../../../services/api";
import type { CredentialMeta, ProviderPreset } from "../../../services/api";

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
      listProviders: vi.fn(async () => ({ providers: [], model_note: "" })),
      listCredentialModels: vi.fn(async () => ({ models: [] })),
      createCredential: vi.fn(),
      updateCredentialMeta: vi.fn(),
      verifyCredential: vi.fn(),
      verifyCredentialDraft: vi.fn(),
    },
  };
});

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
  provider({
    id: "custom",
    name: "其他 / 自定义服务",
    kind: "openai",
    base_url: "",
    suggested_model: "",
    category: "custom",
    category_label: "自定义服务",
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
    note: "工作用 DeepSeek",
    kind: "openai",
    verify_state: "verified",
    provider_id: "deepseek",
    provider_name: "DeepSeek",
    ...overrides,
  };
}

const mounted: VueWrapper[] = [];
function mountForm(props: { mode: "create" | "edit" | "rotate"; initial?: CredentialMeta | null }) {
  const wrapper = mount(KeyForm, {
    props: { providers: PROVIDERS, mode: props.mode, initial: props.initial ?? null },
  });
  mounted.push(wrapper);
  return wrapper;
}

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

afterEach(() => {
  for (const wrapper of mounted) wrapper.unmount();
  mounted.length = 0;
});
beforeEach(() => {
  vi.clearAllMocks();
});

describe("Key 接入 · 正常流程", () => {
  it("选服务商 → 填 Key → 获取模型 → 保存，全程不需要填地址 / 协议 / 模型名", async () => {
    (api.listCredentialModels as ReturnType<typeof vi.fn>).mockResolvedValue({
      models: ["deepseek-v4-flash", "deepseek-reasoner"],
    });
    (api.createCredential as ReturnType<typeof vi.fn>).mockResolvedValue({
      ok: true,
      saved: true,
      key_id: "key_new",
      version: 1,
      credential: meta({ key_id: "key_new" }),
      verify: { ok: true, state: "verified", reason_code: null, message: "模型可用", detail: "", mode: "native" },
    });

    const wrapper = mountForm({ mode: "create" });
    await pickProvider(wrapper, "DeepSeek");
    await wrapper.find("#key-secret").setValue("sk-fake-normal-flow-key");
    await buttonByText(wrapper, "获取模型").trigger("click");
    await flushPromises();

    const fetchPayload = (api.listCredentialModels as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(fetchPayload).toMatchObject({
      endpoint: "https://api.deepseek.com/v1",
      kind: "openai",
      secret: "sk-fake-normal-flow-key",
    });
    expect(wrapper.text()).toContain("已获取 2 个模型");

    expect(buttonByText(wrapper, "添加 Key").exists()).toBe(true);
    await wrapper.find("form").trigger("submit");
    await flushPromises();
    const payload = (api.createCredential as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(payload).toMatchObject({
      provider: "deepseek",
      endpoint: "https://api.deepseek.com/v1",
      kind: "openai",
      default_model: "deepseek-v4-flash",
      tags: ["main-loop"],
    });
    expect(wrapper.emitted("saved")?.[0]).toEqual(["key_new"]);
  });

  it("高级选项默认收起；展开后才有地址 / 协议 / 手动模型名 / 用量上限", async () => {
    const wrapper = mountForm({ mode: "create" });
    expect(wrapper.find(".advanced-toggle").attributes("aria-expanded")).toBe("false");
    expect(wrapper.find("#key-endpoint").exists()).toBe(false);
    expect(wrapper.find("#key-manual-model").exists()).toBe(false);
    expect(wrapper.find("#key-note").exists()).toBe(false);

    await wrapper.find(".advanced-toggle").trigger("click");
    expect(wrapper.find(".advanced-toggle").attributes("aria-expanded")).toBe("true");
    expect(wrapper.find("#key-endpoint").exists()).toBe(true);
    expect(wrapper.find("#key-manual-model").exists()).toBe(true);
    expect(wrapper.find("#key-note").exists()).toBe(true);
  });

  it("本地 Key 前缀提示写明「未验证」，不声称已经识别成功", async () => {
    const wrapper = mountForm({ mode: "create" });
    await wrapper.find("#key-secret").setValue("sk-ant-api03-fake");
    expect(wrapper.text()).toContain("看起来像 Anthropic 的 Key（只是本地提示，未验证）");
  });
});

describe("Key 接入 · 编辑与发送目标变更", () => {
  it("只改显示名称 / 用量上限时不要求重新填 Key", async () => {
    (api.updateCredentialMeta as ReturnType<typeof vi.fn>).mockResolvedValue({
      ok: true,
      credential: meta(),
      verify: null,
    });
    const wrapper = mountForm({ mode: "edit", initial: meta() });
    await wrapper.find(".advanced-toggle").trigger("click");
    await wrapper.find("#key-note").setValue("改过的名字");
    await wrapper.find("form").trigger("submit");
    await flushPromises();
    const [keyId, payload] = (api.updateCredentialMeta as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(keyId).toBe("key_a");
    expect(payload).toMatchObject({ note: "改过的名字" });
    expect(payload.secret).toBeUndefined();
  });

  it("改服务地址必须填 Key 并勾选确认，否则不提交", async () => {
    const wrapper = mountForm({ mode: "edit", initial: meta() });
    await wrapper.find(".advanced-toggle").trigger("click");
    await wrapper.find("#key-endpoint").setValue("https://gateway.example.com/v1");
    await wrapper.find("#key-secret").setValue("sk-fake-reentered-key");
    await wrapper.find("form").trigger("submit");
    await flushPromises();
    expect(api.updateCredentialMeta).not.toHaveBeenCalled();
    expect(wrapper.text()).toContain("换了发送目标");
  });
});

/**
 * #17 / #18 / #89 的文案与底部动作。
 * 断言只针对本表单自己的 DOM 与源码（jsdom 不做排版，等宽/同高/居中由 CDP 实测证据守护）。
 */
describe("Key 接入 · 文案与底部动作（#17 / #18 / #89）", () => {
  it("#17 服务商占位写「选择服务商」；自定义服务地址提示指向「高级选项」", async () => {
    const wrapper = mountForm({ mode: "create" });
    expect(wrapper.find(".qio-combo-trigger").text()).toContain("选择服务商");
    expect(wrapper.text()).not.toContain("或下面的自定义服务地址");
    await pickProvider(wrapper, "其他 / 自定义服务");
    expect(wrapper.text()).toContain("「高级选项」里填写");
  });

  it("#18 累计用量上限：标题不重复 token 单位、数字框保留单位、「留空不限」只写一次", async () => {
    const wrapper = mountForm({ mode: "create" });
    await wrapper.find(".advanced-toggle").trigger("click");
    const labels = wrapper.findAll(".field__label").map((item) => item.text());
    expect(labels).toContain("累计用量上限");
    expect(wrapper.text()).not.toContain("累计用量上限（token");
    expect(wrapper.find(".q-number-unit").text()).toBe("token");
    const occurrences = wrapper.text().match(/不限/g) ?? [];
    expect(occurrences).toHaveLength(1);
    expect(wrapper.text()).toContain("留空表示不限");
  });

  it("#89 底部动作走共享按钮组规则，保留主次颜色与等宽同高来源", () => {
    const source = readFileSync(join(process.cwd(), "src", "components", "models", "KeyForm.vue"), "utf8");
    // 容器用共享 .qio-modal-actions（居中 / 等宽 / 同高 / 窄窗纵排都由 base.css 提供）
    expect(source).toContain('class="actions qio-modal-actions"');
    // 两个按钮仍是共享按钮原语：次级 .qio-btn、主操作 .qio-btn.primary
    expect(source).toMatch(/class="qio-btn" :disabled="busy" @click="emit\('cancel'\)"/);
    expect(source).toMatch(/class="qio-btn primary"/);
    // KeyForm 只收口尺寸：等分 + 36px，不新增宽度魔法数字
    //（选择器写成「容器 + 重复类名」，权重压过上面的 :deep(.qio-btn)，不引入 !important）
    expect(source).toMatch(/\.actions\.qio-modal-actions \.qio-btn\.qio-btn\s*\{[^}]*flex:\s*1 1 0/);
    expect(source).toMatch(/\.actions\.qio-modal-actions \.qio-btn\.qio-btn\s*\{[^}]*height:\s*36px/);
    // 窄窗（≤480）：共享规则换成纵排 + 撑满；上面高权重的尺寸规则必须把宽度交还，
    // 否则两个按钮会缩成内容宽贴左排（实测 leftGap=1 / rightGap=143）
    expect(source).toMatch(/@media \(max-width: 480px\)[\s\S]*?\.actions\.qio-modal-actions \.qio-btn\.qio-btn\s*\{[^}]*width:\s*100%/);
    // 不再把动作组钉在右边（#89 的同轴要求）
    expect(source).not.toMatch(/\.actions\s*\{[^}]*justify-content:\s*flex-end/);
  });
});
