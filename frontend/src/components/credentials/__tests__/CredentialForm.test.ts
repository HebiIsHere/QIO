/**
 * 共用凭据表单：基础区只问「厂商 / Key / 用途」，其余收进高级设置。
 *
 * 这一份用例同时守护首次引导与设置页共用的行为：默认用途、选厂商补齐字段、
 * 保存与验证分开反馈、防重复提交、换钥失败不动原凭据、改发送目标必须确认。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";
import CredentialForm from "../CredentialForm.vue";
import type { CredentialMeta, VerifyReport } from "../../../services/api";

const provider = (overrides: Record<string, unknown> = {}) => ({
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

const CUSTOM = provider({
  id: "custom",
  name: "其他 / 自定义服务",
  base_url: "",
  suggested_model: "",
  category: "custom",
  category_label: "自定义服务",
});

const OPENAI = provider({
  id: "openai",
  name: "OpenAI",
  base_url: "https://api.openai.com/v1",
  suggested_model: "gpt-5.6-luna",
});

function report(ok: boolean, overrides: Partial<VerifyReport> = {}): VerifyReport {
  return {
    ok,
    state: ok ? "verified" : "failed",
    reason_code: ok ? null : "invalid_key",
    message: ok ? "模型可用" : "API Key 无效或已被停用，请确认后重新填写",
    detail: ok ? "" : "401 unauthorized",
    mode: ok ? "native" : null,
    ...overrides,
  } as VerifyReport;
}

function meta(overrides: Partial<CredentialMeta> = {}): CredentialMeta {
  return {
    key_id: "k1",
    version: 1,
    tags: ["main-loop"],
    endpoint: "https://api.deepseek.com/v1",
    default_model: "deepseek-v4-flash",
    budget: null,
    budget_used: 0,
    status: "active",
    enabled: true,
    note: "DeepSeek",
    kind: "openai",
    verify_state: "verified",
    provider_id: "deepseek",
    provider_name: "DeepSeek",
    ...overrides,
  };
}

vi.mock("../../../services/api", () => {
  class ApiError extends Error {
    constructor(
      readonly status: number,
      readonly path: string,
      detail: string,
    ) {
      super(`${path} -> ${status}: ${detail}`);
      this.name = "ApiError";
    }
  }
  return {
    ApiError,
    api: {
      listProviders: vi.fn(async () => ({ providers: [], model_note: "" })),
      listCredentialModels: vi.fn(async () => ({ models: [] })),
      createCredential: vi.fn(),
      verifyCredential: vi.fn(),
      verifyCredentialDraft: vi.fn(),
      updateCredentialMeta: vi.fn(),
    },
  };
});

async function setup(props: { mode: "create" | "edit" | "rotate"; initial?: CredentialMeta | null }) {
  const { api } = await import("../../../services/api");
  (api.listProviders as ReturnType<typeof vi.fn>).mockResolvedValue({
    providers: [provider(), OPENAI, CUSTOM],
    model_note: "预设里的模型名只是推荐值",
  });
  const w = mount(CredentialForm, { props: { mode: props.mode, initial: props.initial ?? null } });
  await flushPromises();
  return { w, api };
}

async function pickProvider(w: Awaited<ReturnType<typeof setup>>["w"], label: string) {
  await w.find(".qio-combo-trigger").trigger("click");
  const option = w.findAll(".qio-combo-opt").find((o) => o.text().includes(label));
  expect(option, `找不到厂商「${label}」`).toBeTruthy();
  await option!.trigger("click");
}

beforeEach(() => {
  vi.clearAllMocks();
  vi.restoreAllMocks();
});

describe("凭据表单 · 基础流程", () => {
  it("基础区只有厂商 / API Key / 用途 / 保存与取消；高级设置默认收起", async () => {
    const { w } = await setup({ mode: "create" });
    expect(w.find(".qio-combo").exists()).toBe(true);
    expect(w.find("#cred-secret").attributes("type")).toBe("password");
    // 用途默认就是「主对话」，不需要用户选
    expect(w.find(".usage-value").text()).toBe("主对话");
    expect(w.find(".advanced-toggle").attributes("aria-expanded")).toBe("false");
    expect(w.find(".collapse.open").exists()).toBe(false);
    // 主按钮是「保存」，不是「连接」或「保存并测试」
    expect(w.find(".btn-submit").text()).toBe("保存");
    expect(w.find(".btn-cancel").text()).toBe("取消");
  });

  it("API Key 支持显示 / 隐藏", async () => {
    const { w } = await setup({ mode: "create" });
    await w.find(".btn-reveal").trigger("click");
    expect(w.find("#cred-secret").attributes("type")).toBe("text");
    await w.find(".btn-reveal").trigger("click");
    expect(w.find("#cred-secret").attributes("type")).toBe("password");
  });

  it("选厂商就自动补齐地址、协议、模型与显示名称（普通用户不用碰高级设置）", async () => {
    const { w } = await setup({ mode: "create" });
    await pickProvider(w, "DeepSeek");
    await w.find(".advanced-toggle").trigger("click");
    expect((w.find("#cred-endpoint").element as HTMLInputElement).value).toBe(
      "https://api.deepseek.com/v1",
    );
    expect((w.find("#cred-model").element as HTMLInputElement).value).toBe("deepseek-v4-flash");
    expect((w.find("#cred-note").element as HTMLInputElement).value).toBe("DeepSeek");
    expect((w.find("#cred-kind").element as HTMLSelectElement).value).toBe("openai");
    // 发送目标要说明白
    expect(w.text()).toContain("官方服务");
  });

  it("保存时带上用途、身份标识与 idempotency key，并显示「已保存，模型可用」", async () => {
    const { w, api } = await setup({ mode: "create" });
    (api.createCredential as ReturnType<typeof vi.fn>).mockResolvedValue({
      ok: true,
      saved: true,
      key_id: "k9",
      version: 1,
      credential: meta({ key_id: "k9" }),
      verify: report(true),
    });
    await pickProvider(w, "DeepSeek");
    await w.find("#cred-secret").setValue("sk-live-abcdefghijklmnop");
    await w.find(".btn-submit").trigger("click");
    await flushPromises();
    const payload = (api.createCredential as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(payload.tags).toEqual(["main-loop"]);
    expect(payload.endpoint).toBe("https://api.deepseek.com/v1");
    expect(payload.default_model).toBe("deepseek-v4-flash");
    expect(payload.provider).toBe("deepseek");
    expect(typeof payload.client_request_id).toBe("string");
    expect(w.find(".qio-feedback").text()).toContain("已保存，模型可用");
    expect(w.emitted("saved")).toHaveLength(1);
  });

  it("保存成功但验证失败：说清「已保存，尚未通过验证」并给出重试入口", async () => {
    const { w, api } = await setup({ mode: "create" });
    (api.createCredential as ReturnType<typeof vi.fn>).mockResolvedValue({
      ok: true,
      saved: true,
      key_id: "k9",
      version: 1,
      credential: meta({ key_id: "k9", verify_state: "failed" }),
      verify: report(false),
    });
    (api.verifyCredential as ReturnType<typeof vi.fn>).mockResolvedValue({
      ok: true,
      key_id: "k9",
      verify: report(true),
    });
    await pickProvider(w, "DeepSeek");
    await w.find("#cred-secret").setValue("sk-bad-key-value-123456");
    await w.find(".btn-submit").trigger("click");
    await flushPromises();
    expect(w.find(".qio-feedback").text()).toContain("已保存，尚未通过验证");
    expect(w.find(".qio-feedback").classes()).toContain("warn");
    // 技术细节可以展开，但默认收起
    expect(w.find("details.tech").exists()).toBe(true);
    // 重试作用在同一条记录上：不再创建新凭据
    await w.find(".btn-retry").trigger("click");
    await flushPromises();
    expect(api.verifyCredential).toHaveBeenCalledWith("k9");
    expect(api.createCredential).toHaveBeenCalledTimes(1);
    expect(w.find(".qio-feedback").text()).toContain("已保存，模型可用");
  });

  it("写入失败时显示「保存失败」，绝不显示成功", async () => {
    const { w, api } = await setup({ mode: "create" });
    const { ApiError } = await import("../../../services/api");
    (api.createCredential as ReturnType<typeof vi.fn>).mockRejectedValue(
      new ApiError(500, "/api/credentials", "保存失败：系统的安全凭据库在写入时出错，密钥没有保存"),
    );
    await pickProvider(w, "DeepSeek");
    await w.find("#cred-secret").setValue("sk-live-abcdefghijklmnop");
    await w.find(".btn-submit").trigger("click");
    await flushPromises();
    expect(w.find(".qio-feedback").text()).toContain("保存失败");
    expect(w.find(".qio-feedback").classes()).toContain("err");
    expect(w.emitted("saved")).toBeUndefined();
  });

  it("保存后验证没过：改 Key 再保存作用在同一条记录上，不会新建第二条", async () => {
    const { w, api } = await setup({ mode: "create" });
    (api.createCredential as ReturnType<typeof vi.fn>).mockResolvedValue({
      ok: true,
      saved: true,
      key_id: "k9",
      version: 1,
      credential: meta({ key_id: "k9", verify_state: "failed" }),
      verify: report(false),
    });
    (api.updateCredentialMeta as ReturnType<typeof vi.fn>).mockResolvedValue({
      ok: true,
      credential: meta({ key_id: "k9", version: 2, verify_state: "verified" }),
      verify: report(true),
    });
    await pickProvider(w, "DeepSeek");
    await w.find("#cred-secret").setValue("sk-typo-key-1234567890");
    await w.find(".btn-submit").trigger("click");
    await flushPromises();

    // 用户改掉打错的 Key 再点保存：应该更新 k9，而不是再建一条
    await w.find("#cred-secret").setValue("sk-fixed-key-1234567890");
    await w.find(".btn-submit").trigger("click");
    await flushPromises();
    expect(api.createCredential).toHaveBeenCalledTimes(1);
    expect(api.updateCredentialMeta).toHaveBeenCalledTimes(1);
    const [keyId, payload] = (api.updateCredentialMeta as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(keyId).toBe("k9");
    expect(payload.secret).toBe("sk-fixed-key-1234567890");
    expect(payload.client_request_id).toBeUndefined();
    expect(w.find(".qio-feedback").text()).toContain("已保存，模型可用");
  });

  it("保存后验证没过：Key 没变再点保存只是重试验证（不写库）", async () => {
    const { w, api } = await setup({ mode: "create" });
    (api.createCredential as ReturnType<typeof vi.fn>).mockResolvedValue({
      ok: true,
      saved: true,
      key_id: "k9",
      version: 1,
      credential: meta({ key_id: "k9", verify_state: "failed" }),
      verify: report(false),
    });
    (api.verifyCredential as ReturnType<typeof vi.fn>).mockResolvedValue({
      ok: true,
      key_id: "k9",
      verify: report(true),
    });
    await pickProvider(w, "DeepSeek");
    await w.find("#cred-secret").setValue("sk-same-key-1234567890");
    await w.find(".btn-submit").trigger("click");
    await flushPromises();
    await w.find(".btn-submit").trigger("click");
    await flushPromises();
    expect(api.createCredential).toHaveBeenCalledTimes(1);
    expect(api.updateCredentialMeta).not.toHaveBeenCalled();
    expect(api.verifyCredential).toHaveBeenCalledWith("k9", expect.anything());
  });

  it("提交中禁用主按钮，防止重复提交", async () => {
    const { w, api } = await setup({ mode: "create" });
    let resolve!: (value: unknown) => void;
    (api.createCredential as ReturnType<typeof vi.fn>).mockImplementation(
      () => new Promise((r) => (resolve = r)),
    );
    await pickProvider(w, "DeepSeek");
    await w.find("#cred-secret").setValue("sk-live-abcdefghijklmnop");
    await w.find(".btn-submit").trigger("click");
    await flushPromises();
    expect(w.find(".btn-submit").text()).toBe("保存中…");
    expect(w.find(".btn-submit").attributes("disabled")).toBeDefined();
    expect(w.find(".btn-submit").attributes("aria-busy")).toBe("true");
    await w.find(".btn-submit").trigger("click");
    expect(api.createCredential).toHaveBeenCalledTimes(1);
    resolve({ ok: true, saved: true, key_id: "k1", version: 1, credential: meta(), verify: report(true) });
    await flushPromises();
  });

  it("自定义服务：必填项直接展开，缺地址或模型不给保存", async () => {
    const { w, api } = await setup({ mode: "create" });
    await pickProvider(w, "其他 / 自定义服务");
    expect(w.find(".collapse.open").exists()).toBe(true);
    await w.find("#cred-secret").setValue("sk-custom-abcdefghijkl");
    await w.find(".btn-submit").trigger("click");
    await flushPromises();
    expect(api.createCredential).not.toHaveBeenCalled();
    expect(w.text()).toContain("请填写服务地址");
    await w.find("#cred-endpoint").setValue("https://gateway.example/v1");
    await w.find(".btn-submit").trigger("click");
    await flushPromises();
    expect(w.text()).toContain("请选择或填写模型名称");
  });

  it("拿不到模型列表时可以手填模型名称", async () => {
    const { w, api } = await setup({ mode: "create" });
    (api.listCredentialModels as ReturnType<typeof vi.fn>).mockResolvedValue({ models: [] });
    await pickProvider(w, "其他 / 自定义服务");
    await w.find("#cred-endpoint").setValue("https://gateway.example/v1");
    await w.find("#cred-model").setValue("my-local-model");
    expect((w.find("#cred-model").element as HTMLInputElement).value).toBe("my-local-model");
  });

  it("后台取模型列表不会顶掉正在进行的保存（按钮不会卡在「保存中…」）", async () => {
    const { w, api } = await setup({ mode: "create" });
    // 保存还没回来时，后台的「取模型列表」先完成了 —— 如果两者共用同一个请求序号，
    // 保存结果会被判成过期请求丢掉，界面就永远停在「保存中…」。
    let resolveCreate!: (value: unknown) => void;
    (api.createCredential as ReturnType<typeof vi.fn>).mockImplementation(
      () => new Promise((resolve) => (resolveCreate = resolve)),
    );
    (api.listCredentialModels as ReturnType<typeof vi.fn>).mockImplementation(
      () => new Promise((resolve) => setTimeout(() => resolve({ models: ["local-test-model"] }), 10)),
    );
    await pickProvider(w, "其他 / 自定义服务");
    await w.find("#cred-model").setValue("local-test-model");
    await w.find("#cred-endpoint").setValue("http://127.0.0.1:8799/v1");
    await w.find("#cred-secret").setValue("sk-local-bad-key");
    await w.find(".btn-submit").trigger("click");
    await flushPromises();
    // 等后台模型列表的防抖（450ms）与请求都跑完
    await new Promise((resolve) => setTimeout(resolve, 600));
    await flushPromises();
    expect(w.find(".btn-submit").text()).toBe("保存中…");
    resolveCreate({
      ok: true,
      saved: true,
      key_id: "k9",
      version: 1,
      credential: meta({ key_id: "k9", verify_state: "failed" }),
      verify: report(false),
    });
    await flushPromises();
    expect(w.find(".qio-feedback").text()).toContain("已保存，尚未通过验证");
    expect(w.find(".btn-submit").text()).toBe("保存");
  });

  it("关闭表单会清掉已输入的 API Key", async () => {
    const { w } = await setup({ mode: "create" });
    await w.find("#cred-secret").setValue("sk-should-be-cleared-1234");
    await w.find(".btn-cancel").trigger("click");
    expect((w.find("#cred-secret").element as HTMLInputElement).value).toBe("");
    expect(w.emitted("cancel")).toHaveLength(1);
  });
});

describe("凭据表单 · 提示与编辑", () => {
  it("Key 前缀只做本地提示：足够明确才预选厂商", async () => {
    const { w } = await setup({ mode: "create" });
    await w.find("#cred-secret").setValue("sk-proj-abcdefghijklmnop");
    // 提示有 400ms 防抖；这里等它自然触发，避免用假计时器与 flushPromises 互相卡住
    await new Promise((resolve) => setTimeout(resolve, 550));
    await flushPromises();
    expect(w.find(".qio-combo-value").text()).toContain("OpenAI");
    expect(w.text()).toContain("预选");
  });

  it("含糊前缀（sk-）只提示，不替用户改厂商", async () => {
    const { w } = await setup({ mode: "create" });
    await pickProvider(w, "OpenAI");
    await w.find("#cred-secret").setValue("sk-live-abcdefghijklmnop");
    await new Promise((resolve) => setTimeout(resolve, 550));
    await flushPromises();
    expect(w.find(".qio-combo-value").text()).toContain("OpenAI");
    expect(w.text()).toContain("当前仍按你选择的厂商发送");
  });

  it("编辑已有凭据保留原用途，不套用新建默认值", async () => {
    const { w } = await setup({
      mode: "edit",
      initial: meta({ tags: ["vision", "research"], note: "看图专用" }),
    });
    expect(w.find(".usage-value").text()).toContain("看图/视觉");
    expect(w.find(".usage-value").text()).toContain("研究");
    expect(w.find(".usage-value").text()).not.toContain("主对话");
  });

  it("编辑老凭据（协议列为空）只改备注：不提交协议字段，也不要求重新确认", async () => {
    const { w, api } = await setup({
      mode: "edit",
      // 历史数据没有 kind：表单必须按地址判断成 Anthropic，而不是默认 OpenAI
      initial: meta({ kind: null, endpoint: "https://api.anthropic.com/v1" }),
    });
    await w.find(".advanced-toggle").trigger("click");
    expect((w.find("#cred-kind").element as HTMLSelectElement).value).toBe("anthropic");
    (api.updateCredentialMeta as ReturnType<typeof vi.fn>).mockResolvedValue({
      ok: true,
      credential: meta(),
      verify: null,
    });
    await w.find(".btn-submit").trigger("click");
    await flushPromises();
    const payload = (api.updateCredentialMeta as ReturnType<typeof vi.fn>).mock.calls[0][1];
    expect("kind" in payload).toBe(false);
    expect("confirm_reconfigure" in payload).toBe(false);
    expect(w.find(".qio-feedback").text()).toContain("已保存");
  });

  it("编辑时改了发送目标：必须重填 Key 并显式确认，否则不给保存", async () => {
    const { w, api } = await setup({ mode: "edit", initial: meta() });
    await w.find(".advanced-toggle").trigger("click");
    await w.find("#cred-endpoint").setValue("https://attacker.example/v1");
    await flushPromises();
    expect(w.text()).toContain("这把 Key 会被发送到新的地址");
    await w.find(".btn-submit").trigger("click");
    await flushPromises();
    expect(api.updateCredentialMeta).not.toHaveBeenCalled();
    // 先要求重新输入 Key
    expect(w.text()).toContain("请填写 API Key");

    // 填了 Key 但没勾确认：仍然不发请求
    await w.find("#cred-secret").setValue("sk-again-abcdefghijkl");
    await w.find(".btn-submit").trigger("click");
    await flushPromises();
    expect(api.updateCredentialMeta).not.toHaveBeenCalled();
    expect(w.text()).toContain("请勾选确认");

    await w.find(".target-warning input[type=checkbox]").setValue(true);
    (api.updateCredentialMeta as ReturnType<typeof vi.fn>).mockResolvedValue({
      ok: true,
      credential: meta({ endpoint: "https://attacker.example/v1" }),
      verify: report(true),
    });
    await w.find(".btn-submit").trigger("click");
    await flushPromises();
    const payload = (api.updateCredentialMeta as ReturnType<typeof vi.fn>).mock.calls[0][1];
    expect(payload.confirm_reconfigure).toBe(true);
    expect(payload.secret).toBe("sk-again-abcdefghijkl");
    expect(payload.endpoint).toBe("https://attacker.example/v1");
  });

  it("换钥：先验证新 Key，验证不过就完全不碰原凭据", async () => {
    const { w, api } = await setup({ mode: "rotate", initial: meta() });
    (api.verifyCredentialDraft as ReturnType<typeof vi.fn>).mockResolvedValue({
      ok: false,
      verify: report(false),
    });
    await w.find("#cred-secret").setValue("sk-bad-new-key-123456");
    await w.find(".btn-submit").trigger("click");
    await flushPromises();
    expect(api.verifyCredentialDraft).toHaveBeenCalled();
    expect(api.updateCredentialMeta).not.toHaveBeenCalled();
    expect(w.text()).toContain("原凭据仍然可用");
  });

  it("换钥成功：走一次原子更新，保留用途与显示名称", async () => {
    const { w, api } = await setup({
      mode: "rotate",
      initial: meta({ tags: ["main-loop", "code"], note: "主力 Key" }),
    });
    (api.verifyCredentialDraft as ReturnType<typeof vi.fn>).mockResolvedValue({
      ok: true,
      verify: report(true),
    });
    (api.updateCredentialMeta as ReturnType<typeof vi.fn>).mockResolvedValue({
      ok: true,
      credential: meta({ version: 2 }),
      verify: report(true),
    });
    await w.find("#cred-secret").setValue("sk-new-good-key-123456");
    await w.find(".btn-submit").trigger("click");
    await flushPromises();
    const payload = (api.updateCredentialMeta as ReturnType<typeof vi.fn>).mock.calls[0][1];
    expect(payload.tags).toEqual(["main-loop", "code"]);
    expect(payload.note).toBe("主力 Key");
    expect(payload.secret).toBe("sk-new-good-key-123456");
    expect(w.find(".qio-feedback").text()).toContain("已保存，模型可用");
  });
});
