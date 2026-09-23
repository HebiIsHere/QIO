import { beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import OnboardingWizard from "../OnboardingWizard.vue";
import { api } from "../../../services/api";

const baseStatus = {
  done: false,
  has_credential: false,
  has_name: false,
  wizard_seen: false,
  welcome_version: "",
  app_version: "0.1.6",
  show_wizard: true,
  hint_dismissed: false,
};

function mountWizard() {
  const pinia = createPinia();
  setActivePinia(pinia);
  return mount(OnboardingWizard, { global: { plugins: [pinia] } });
}

describe("OnboardingWizard", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    localStorage.clear();
    vi.spyOn(api, "getOnboardingStatus").mockResolvedValue({ ...baseStatus });
    vi.spyOn(api, "markOnboardingSeen").mockResolvedValue({
      ...baseStatus,
      wizard_seen: true,
      welcome_version: "0.1.6",
      show_wizard: false,
    });
    vi.spyOn(api, "saveOnboardingProfile").mockResolvedValue({
      name: "小舟",
      knowledge_id: "k1",
      entity_id: "e1",
      topics: ["学习"],
    });
    vi.spyOn(api, "completeOnboarding").mockResolvedValue({ ...baseStatus, done: true });
    vi.spyOn(api, "listCredentials").mockResolvedValue({ credentials: [] });
  });

  it("首屏是欢迎步骤：品牌 + 定位 + 开始设置，进度条 6 段", async () => {
    const w = mountWizard();
    await flushPromises();
    expect(w.find(".onboarding").exists()).toBe(true);
    expect(w.text()).toContain("你的私人记忆星球");
    expect(w.find(".onboarding-actions .primary").text()).toBe("开始设置");
    expect(w.findAll(".onboarding-steps .step")).toHaveLength(6);
    expect(w.find(".onboarding-steps .step.current").attributes("aria-current")).toBe("step");
  });

  it("挂载即记「本版本已展示过欢迎页」", async () => {
    mountWizard();
    await flushPromises();
    expect(api.markOnboardingSeen).toHaveBeenCalledTimes(1);
  });

  it("开始设置 → 连接模型；跳过 → 认识你", async () => {
    const w = mountWizard();
    await flushPromises();
    await w.find(".onboarding-actions .primary").trigger("click");
    expect(w.text()).toContain("连接模型");
    expect(w.find("input.qio-input").exists()).toBe(true);
    await w.find(".onboarding-actions .skip").trigger("click");
    expect(w.find(".onboarding-steps .step.current").text()).toContain("认识你");
  });

  it("认识你没填称呼点下一步 → 停在原地并提示", async () => {
    const w = mountWizard();
    await flushPromises();
    await w.find(".onboarding-actions .primary").trigger("click");
    await w.find(".onboarding-actions .skip").trigger("click");
    expect(w.find(".onboarding-steps .step.current").text()).toContain("认识你");
    await w.find(".onboarding-actions .primary").trigger("click");
    await flushPromises();
    expect(w.find(".onboarding-steps .step.current").text()).toContain("认识你");
    expect(w.text()).toContain("称呼是完成设置的必填项");
    expect(api.saveOnboardingProfile).not.toHaveBeenCalled();
  });

  it("填称呼 + 选目标 → 完成页「进入对话」→ emit done", async () => {
    const w = mountWizard();
    await flushPromises();
    await w.find(".onboarding-actions .primary").trigger("click"); // 开始设置 → 连接模型
    await w.find(".onboarding-actions .skip").trigger("click"); // 跳过 → 认识你
    await w.find("input.qio-input").setValue("小舟");
    await w.find(".onboarding-actions .primary").trigger("click"); // 下一步 → 偏好
    await flushPromises();
    expect(api.saveOnboardingProfile).toHaveBeenCalled();
    await w.find(".onboarding-actions .primary").trigger("click"); // 偏好 → 目标
    await w.find(".goal").trigger("click"); // 勾一个目标
    await w.find(".onboarding-actions .primary").trigger("click"); // 目标 → 完成
    await flushPromises();
    expect(w.text()).toContain("进入对话");
    await w.find(".onboarding-actions .primary").trigger("click");
    await flushPromises();
    expect(api.completeOnboarding).toHaveBeenCalledTimes(1);
    expect(w.emitted("done")).toHaveLength(1);
  });
});
