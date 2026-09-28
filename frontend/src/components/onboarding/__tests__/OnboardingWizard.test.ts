import { beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import OnboardingWizard from "../OnboardingWizard.vue";
import { api } from "../../../services/api";

const PRESETS = [
  {
    id: "deepseek",
    name: "DeepSeek",
    kind: "openai",
    base_url: "https://api.deepseek.com/v1",
    suggested_model: "deepseek-v4-flash",
    category: "official",
    category_label: "官方服务",
    note: "",
  },
  {
    id: "custom",
    name: "其他 / 自定义服务",
    kind: "openai",
    base_url: "",
    suggested_model: "",
    category: "custom",
    category_label: "自定义服务",
    note: "",
  },
];

function credentialMeta(overrides: Record<string, unknown> = {}) {
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
    verify_state: "verified",
    is_default: true,
    ...overrides,
  };
}

const baseStatus = {
  done: false,
  has_credential: false,
  has_name: false,
  has_content: false,
  wizard_seen: false,
  welcome_version: "",
  app_version: "0.1.7",
  show_wizard: true,
  hint_dismissed: false,
};

function mountWizard(status: Record<string, unknown> = {}) {
  const merged = { ...baseStatus, ...status };
  vi.spyOn(api, "getOnboardingStatus").mockResolvedValue({ ...merged });
  vi.spyOn(api, "markOnboardingSeen").mockResolvedValue({ ...merged, wizard_seen: true });
  vi.spyOn(api, "submitOnboarding").mockResolvedValue({
    name: "祠莎",
    self_card_id: "ec_1",
    written: [],
    pending: [],
    topics: [],
  });
  vi.spyOn(api, "completeOnboarding").mockResolvedValue({ ...merged, done: true });
  vi.spyOn(api, "suggestFollowUps").mockResolvedValue({ questions: [] });
  vi.spyOn(api, "listProviders").mockResolvedValue({
    providers: PRESETS,
    model_note: "预设里的模型名只是推荐值",
  });
  vi.spyOn(api, "createCredential").mockResolvedValue({
    ok: true,
    saved: true,
    key_id: "k1",
    version: 1,
    credential: credentialMeta(),
    verify: {
      ok: true,
      state: "verified",
      reason_code: null,
      message: "模型可用",
      detail: "",
      mode: "native",
    },
  });
  vi.spyOn(api, "verifyCredential").mockResolvedValue({
    ok: true,
    key_id: "k1",
    verify: {
      ok: true,
      state: "verified",
      reason_code: null,
      message: "模型可用",
      detail: "",
      mode: "native",
    },
  });
  vi.spyOn(api, "listCredentialModels").mockResolvedValue({ models: [] });
  const pinia = createPinia();
  setActivePinia(pinia);
  const wrapper = mount(OnboardingWizard, { global: { plugins: [pinia] } });
  return wrapper;
}

async function toProfile(wrapper: ReturnType<typeof mountWizard>) {
  // 欢迎 → 连接模型（老用户可以跳过）→ 认识你
  await wrapper.find(".onboarding-actions .primary").trigger("click");
  const skip = wrapper.find(".onboarding-actions .skip");
  if (skip.exists()) await skip.trigger("click");
  // 已有可用密钥时「跳过」与主按钮合并，主按钮就是「下一步」
  else await wrapper.find(".onboarding-actions .credential-primary").trigger("click");
  await flushPromises();
}

describe("首次引导向导 v2", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.restoreAllMocks();
    localStorage.clear();
  });

  it("七步进度条，首屏是欢迎页", async () => {
    const w = mountWizard();
    await flushPromises();
    expect(w.findAll(".onboarding-steps .step")).toHaveLength(7);
    expect(w.find(".onboarding-steps .step.current").attributes("aria-current")).toBe("step");
    expect(w.find(".onboarding-actions .primary").text()).toBe("开始设置");
    expect(api.markOnboardingSeen).toHaveBeenCalledTimes(1);
  });

  it("连接模型用的是与设置页相同的表单：只需要选厂商、填 Key、保存", async () => {
    const w = mountWizard({ has_content: false });
    await flushPromises();
    await w.find(".onboarding-actions .primary").trigger("click"); // → 连接模型

    // 这一步只有一个主按钮：表单自己不再另起一行「取消 / 保存」
    expect(w.find(".onboarding-body .btn-submit").exists()).toBe(false);
    expect(w.find(".onboarding-actions .credential-primary").text()).toBe("保存");

    // 选厂商（可搜索的下拉）
    await w.find(".onboarding-body .qio-combo-trigger").trigger("click");
    const option = w.findAll(".qio-combo-opt").find((o) => o.text().includes("DeepSeek"));
    expect(option).toBeTruthy();
    await option!.trigger("click");

    await w.find("#cred-secret").setValue("sk-abcdefghijklmnopqrstuvwxyz012345");
    await w.find(".onboarding-actions .credential-primary").trigger("click");
    await flushPromises();

    const payload = (api.createCredential as unknown as { mock: { calls: unknown[][] } }).mock
      .calls[0][0] as Record<string, unknown>;
    expect(payload.tags).toEqual(["main-loop"]);
    // 选完厂商就自动补齐地址、协议与默认模型，普通用户不用碰这些
    expect(payload.endpoint).toBe("https://api.deepseek.com/v1");
    expect(payload.default_model).toBe("deepseek-v4-flash");
    expect(payload.kind).toBe("openai");
    expect(typeof payload.client_request_id).toBe("string");
    // 保存通过就自动进入下一步：同一个按钮同时完成保存与前进
    // （「已保存，模型可用」这段反馈由 CredentialForm 自己的用例守着；
    //   引导页这里成功后立刻换步，所以状态区只会在瞬间出现）
    expect(w.find(".onboarding-steps .step.current").text()).toContain("认识你");
    expect(w.find(".onboarding-body .btn-submit").exists()).toBe(false);
  });

  it("连接模型：验证没通过时停在原地，按钮仍是「保存」并给出重试", async () => {
    const w = mountWizard({ has_content: false });
    await flushPromises();
    await w.find(".onboarding-actions .primary").trigger("click"); // → 连接模型
    (api.createCredential as ReturnType<typeof vi.fn>).mockResolvedValue({
      ok: true,
      saved: true,
      key_id: "k1",
      version: 1,
      credential: credentialMeta({ verify_state: "failed", is_default: false }),
      verify: {
        ok: false,
        state: "failed",
        reason_code: "invalid_key",
        message: "API Key 无效或已被停用，请确认后重新填写",
        detail: "401",
        mode: null,
      },
    });
    await w.find(".onboarding-body .qio-combo-trigger").trigger("click");
    const option = w.findAll(".qio-combo-opt").find((o) => o.text().includes("DeepSeek"));
    await option!.trigger("click");
    await w.find("#cred-secret").setValue("sk-bad-key-abcdefghijkl");
    await w.find(".onboarding-actions .credential-primary").trigger("click");
    await flushPromises();

    expect(w.find(".onboarding-steps .step.current").text()).toContain("连接模型");
    expect(w.find(".onboarding-actions .credential-primary").text()).toBe("保存");
    expect(w.find(".onboarding-body .qio-feedback").text()).toContain("已保存，尚未通过验证");
    expect(w.find(".onboarding-body .btn-retry").exists()).toBe(true);
  });

  it("连接模型：已经有可用凭据且没在填新的时，按钮是「下一步」", async () => {
    const w = mountWizard({ has_content: false, has_credential: true });
    await flushPromises();
    await w.find(".onboarding-actions .primary").trigger("click"); // → 连接模型
    expect(w.find(".onboarding-actions .credential-primary").text()).toBe("下一步");
    // 「下一步」和「跳过」在这一刻做的是同一件事：只留一个
    expect(w.find(".onboarding-actions .skip").exists()).toBe(false);
    await w.find(".onboarding-actions .credential-primary").trigger("click");
    await flushPromises();
    expect(w.find(".onboarding-steps .step.current").text()).toContain("认识你");
  });

  it("连接模型：开始填新的凭据后才出现「跳过」（此时它与「保存」不是一回事）", async () => {
    const w = mountWizard({ has_content: true, has_credential: true });
    await flushPromises();
    await w.find(".onboarding-actions .primary").trigger("click"); // → 连接模型
    expect(w.find(".onboarding-actions .skip").exists()).toBe(false);
    await w.find("#cred-secret").setValue("sk-want-to-replace-1234");
    await flushPromises();
    expect(w.find(".onboarding-actions .credential-primary").text()).toBe("保存");
    expect(w.find(".onboarding-actions .skip").exists()).toBe(true);
  });

  it("连接模型：没有可用凭据时，「跳过」仍然是把这一步放过去的出口", async () => {
    const w = mountWizard({ has_content: true, has_credential: false });
    await flushPromises();
    await w.find(".onboarding-actions .primary").trigger("click"); // → 连接模型
    expect(w.find(".onboarding-actions .credential-primary").text()).toBe("保存");
    const skip = w.find(".onboarding-actions .skip");
    expect(skip.exists()).toBe(true);
    await skip.trigger("click");
    await flushPromises();
    expect(w.find(".onboarding-steps .step.current").text()).toContain("认识你");
  });

  it("新用户（主页没有内容）不能离开连接模型这一步", async () => {
    const w = mountWizard({ has_content: false });
    await flushPromises();
    await w.find(".onboarding-actions .primary").trigger("click");
    expect(w.find(".onboarding-steps .step.current").text()).toContain("连接模型");

    await w.find(".onboarding-actions .primary").trigger("click");
    await flushPromises();
    expect(w.find(".onboarding-steps .step.current").text()).toContain("连接模型");
    // 没填任何东西就点保存：错误出现在表单里（当前表单项旁），不是另开一句提示
    expect(w.text()).toContain("请先选择服务厂商");
    // 新用户看不到关闭按钮
    expect(w.find(".onboarding-close").exists()).toBe(false);
  });

  it("老用户（主页已有内容）可以关闭，也可以跳过密钥", async () => {
    const w = mountWizard({ has_content: true, has_credential: true });
    await flushPromises();
    expect(w.find(".onboarding-close").exists()).toBe(true);

    await w.find(".onboarding-close").trigger("click");
    expect(w.emitted("done")).toHaveLength(1);
  });

  it("曾经完成过设置的老用户再次运行助手：即使暂时量不到内容也可以关闭", async () => {
    const w = mountWizard({ has_content: false, done: true, has_credential: true });
    await flushPromises();
    expect(w.find(".onboarding-close").exists()).toBe(true);
  });

  it("已经有一把可用密钥时：说明清楚，并且可以直接继续", async () => {
    const w = mountWizard({ has_content: false, has_credential: true });
    await flushPromises();
    await w.find(".onboarding-actions .primary").trigger("click"); // → 连接模型
    expect(w.text()).toContain("已有一把通过验证的密钥");

    await w.find(".onboarding-actions .credential-primary").trigger("click"); // → 认识你
    await flushPromises();
    expect(w.find(".onboarding-steps .step.current").text()).toContain("认识你");
  });

  it("认识你没填称呼就停在原地", async () => {
    const w = mountWizard({ has_content: true });
    await flushPromises();
    await toProfile(w);
    await w.find(".onboarding-actions .primary").trigger("click");
    await flushPromises();
    expect(w.find(".onboarding-steps .step.current").text()).toContain("认识你");
    expect(w.text()).toContain("称呼是完成设置的必填项");
  });

  it("追问按描述现问：跳过不进清单，答了才进清单", async () => {
    const w = mountWizard({ has_content: true, has_credential: true });
    vi.spyOn(api, "suggestFollowUps").mockResolvedValue({ questions: ["你最想先解决什么？"] });
    await flushPromises();
    await toProfile(w);
    const inputs = w.findAll("input.qio-input");
    await inputs[0].setValue("祠莎");
    await inputs[1].setValue("开发 QIO"); // 有了描述，追问才会现问
    await w.find(".onboarding-actions .primary").trigger("click"); // → 偏好
    await w.find(".onboarding-actions .primary").trigger("click"); // → 目标
    await w.find(".onboarding-actions .primary").trigger("click"); // → 追问
    await flushPromises();

    expect(api.suggestFollowUps).toHaveBeenCalled();
    expect(w.text()).toContain("你最想先解决什么？");
    await w.find(".onboarding-actions .skip-followup").trigger("click"); // 跳过追问
    expect(w.text()).not.toContain("你最想先解决什么？"); // 跳过的追问不进清单
    await w.find(".onboarding-actions .primary").trigger("click"); // 完成设置
    await flushPromises();
    expect(api.submitOnboarding).toHaveBeenCalled();
  });

  it("每个偏好维度都能选「其他」并自己填，填的内容按自定义值提交", async () => {
    const w = mountWizard({ has_content: true, has_credential: true });
    await flushPromises();
    await toProfile(w);
    await w.find("input.qio-input").setValue("祠莎");
    await w.find(".onboarding-actions .primary").trigger("click"); // 认识你 → 偏好

    const verbosity = w.findAll(".pref-row")[0];
    const other = verbosity.findAll(".chip").find((chip) => chip.text() === "其他");
    expect(other).toBeTruthy();
    await other!.trigger("click");
    const custom = verbosity.find("input.qio-input");
    expect(custom.exists()).toBe(true);
    await custom.setValue("一句话说完");

    await w.find(".onboarding-actions .primary").trigger("click"); // 偏好 → 目标
    await w.find(".onboarding-actions .primary").trigger("click"); // 目标 → 追问
    await flushPromises();
    await w.find(".onboarding-actions .skip-followup").trigger("click");
    await w.find(".onboarding-actions .finish").trigger("click");
    await flushPromises();

    const payload = (api.submitOnboarding as unknown as { mock: { calls: unknown[][] } }).mock
      .calls[0][0] as { preferences: { kind: string; value: string }[] };
    expect(payload.preferences).toEqual([
      { kind: "verbosity", value: "一句话说完", scope: { type: "global" } },
    ]);
  });

  it("清单里删掉一条就不写进去，改过的按新值写", async () => {
    const w = mountWizard({ has_content: true, has_credential: true });
    await flushPromises();
    await toProfile(w);
    const inputs = w.findAll("input.qio-input");
    await inputs[0].setValue("祠莎");
    await inputs[1].setValue("开发 QIO");
    await inputs[2].setValue("学生");
    await w.find(".onboarding-actions .primary").trigger("click"); // → 偏好
    await w.find(".onboarding-actions .primary").trigger("click"); // → 目标
    await w.find(".onboarding-actions .primary").trigger("click"); // → 追问
    await flushPromises();
    await w.find(".onboarding-actions .skip-followup").trigger("click");

    // 清单：删掉「学习 / 工作背景」
    const rows = w.findAll(".review-item");
    const backgroundRow = rows.find((row) => row.text().includes("学习 / 工作背景"));
    expect(backgroundRow).toBeTruthy();
    await backgroundRow!.find(".remove").trigger("click");

    // 改「称呼」
    const nameRow = w.findAll(".review-item").find((row) => row.text().includes("称呼"));
    await nameRow!.find(".edit").trigger("click");
    await nameRow!.find("input").setValue("柯莎");
    await nameRow!.find("button").trigger("click");

    await w.find(".onboarding-actions .finish").trigger("click");
    await flushPromises();

    const payload = (api.submitOnboarding as unknown as { mock: { calls: unknown[][] } }).mock
      .calls[0][0] as Record<string, unknown>;
    expect(payload.name).toBe("柯莎");
    expect(payload.background).toBeUndefined();
    expect(w.emitted("done")).toHaveLength(1);
  });
});
