import { beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import OnboardingWizard from "../OnboardingWizard.vue";
import { api } from "../../../services/api";
import { identifyCredential } from "../../../services/identify";

vi.mock("../../../services/identify", () => ({ identifyCredential: vi.fn() }));

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
  vi.spyOn(api, "createCredential").mockResolvedValue({ ok: true, key_id: "k1", version: 1 });
  vi.spyOn(api, "testCredential").mockResolvedValue({
    key_id: "k1",
    probe: { mode: "native", detail: "ok" },
  });
  const pinia = createPinia();
  setActivePinia(pinia);
  const wrapper = mount(OnboardingWizard, { global: { plugins: [pinia] } });
  return wrapper;
}

async function toProfile(wrapper: ReturnType<typeof mountWizard>) {
  // 欢迎 → 连接模型（老用户可以跳过）→ 认识你
  await wrapper.find(".onboarding-actions .primary").trigger("click");
  await wrapper.find(".onboarding-actions .skip").trigger("click");
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

  it("新用户（主页没有内容）不能离开连接模型这一步", async () => {
    const w = mountWizard({ has_content: false });
    await flushPromises();
    await w.find(".onboarding-actions .primary").trigger("click");
    expect(w.find(".onboarding-steps .step.current").text()).toContain("连接模型");

    await w.find(".onboarding-actions .primary").trigger("click");
    await flushPromises();
    expect(w.find(".onboarding-steps .step.current").text()).toContain("连接模型");
    expect(w.text()).toContain("没有可用的密钥就无法继续");
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
