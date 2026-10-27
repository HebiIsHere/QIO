/**
 * uf2 · F01：首次引导（onboarding 优先级 90）的活动判断。
 *
 * 关闭询问（close-ask 100）出现时只 v-show 隐藏、步骤与已填内容保留；取消后回到原步骤。
 * 修复后统一用 arbiter.isActive（已打开 && 无挂起 && 栈顶）。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";

vi.mock("../../../services/api", () => ({
  api: {
    getOnboardingStatus: vi.fn(async () => ({
      done: false,
      has_credential: false,
      has_name: false,
      has_content: false,
      wizard_seen: false,
      welcome_version: "",
      app_version: "0.1.7",
      show_wizard: true,
      hint_dismissed: false,
    })),
    markOnboardingSeen: vi.fn(async () => ({
      done: false,
      has_credential: false,
      has_name: false,
      has_content: false,
      wizard_seen: true,
      welcome_version: "",
      app_version: "0.1.7",
      show_wizard: true,
      hint_dismissed: false,
    })),
    listProviders: vi.fn(async () => ({ providers: [], model_note: "" })),
    listCredentialModels: vi.fn(async () => ({ models: [] })),
    suggestFollowUps: vi.fn(async () => ({ questions: [] })),
  },
}));

import OnboardingWizard from "../OnboardingWizard.vue";
import { useModalArbiterStore } from "../../../stores/modalArbiter";

async function setup() {
  const pinia = createPinia();
  setActivePinia(pinia);
  const w = mount(OnboardingWizard, { global: { plugins: [pinia] }, attachTo: document.body });
  await flushPromises();
  return { w, arbiter: useModalArbiterStore() };
}

function wizardDisplay(w: Awaited<ReturnType<typeof setup>>["w"]): string {
  return (w.find(".onboarding").element as HTMLElement).style.display;
}

beforeEach(() => {
  document.body.innerHTML = "";
  try {
    localStorage.clear();
  } catch {
    // 存储不可用不影响本用例
  }
});

describe("uf2-F01 首次引导的活动判断", () => {
  it("关闭询问出现：引导只隐藏（步骤与已填内容保留）；关闭询问取消后回到同一步", async () => {
    const { w, arbiter } = await setup();
    expect(arbiter.isOpen("qio-onboarding")).toBe(true);
    expect(wizardDisplay(w)).toBe("");

    // 走到第二步（连接模型），保留可见步骤与已填内容
    await w.find(".onboarding-actions .primary").trigger("click");
    await flushPromises();
    const stepBefore = w.find(".onboarding-steps .step.current").text();
    expect(stepBefore).toContain("连接模型");

    arbiter.open({ id: "qio-close-ask", kind: "close-ask", dismissible: true });
    arbiter.suspendAll("close-ask");
    await flushPromises();
    expect(wizardDisplay(w)).toBe("none");
    expect(w.find(".onboarding").exists()).toBe(true);

    arbiter.close("qio-close-ask");
    arbiter.resumeAll();
    await flushPromises();
    expect(wizardDisplay(w)).toBe("");
    expect(w.find(".onboarding-steps .step.current").text()).toContain("连接模型");
    w.unmount();
  });
});
