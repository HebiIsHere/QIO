/**
 * D · 设置页反馈生命周期：成功更局部更短（约 4s 自动收回），失败持久到下次操作；
 * 不出现全局顶部成功通知。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";
import { nextTick } from "vue";
import { createPinia, setActivePinia } from "pinia";
import SettingsView from "../SettingsView.vue";

const mocks = vi.hoisted(() => ({
  updateCloseBehavior: vi.fn(),
}));

vi.mock("../../services/api", () => ({
  api: {
    listCredentials: vi.fn(async () => ({ credentials: [], default_key_id: null })),
    listProviders: vi.fn(async () => ({ providers: [], model_note: "" })),
    listCredentialModels: vi.fn(async () => ({ models: [] })),
    getMemorySettings: vi.fn(async () => ({ fragment_max_turns: 10, fragment_max_tokens: 4096 })),
    updateMemorySettings: vi.fn(async () => ({ ok: true, fragment_max_turns: 10, fragment_max_tokens: 4096 })),
    getMaintenanceSettings: vi.fn(async () => ({ enabled: false, interval_hours: 24 })),
    updateMaintenanceSettings: vi.fn(async () => ({ enabled: false, interval_hours: 24 })),
    getSearchSettings: vi.fn(async () => ({
      searxng_url: "",
      bocha_has_key: false,
      top_k_default: 5,
      max_fetch_chars: 15000,
    })),
    getComputerSettings: vi.fn(async () => ({ root_dir: "", permission_mode: "default" })),
    getToolHistorySettings: vi.fn(async () => ({ record_outputs: true, output_retention_days: 90 })),
    getLoopSettings: vi.fn(async () => ({ max_iterations: 128, output_token_budget: 51200 })),
    getUISettings: vi.fn(async () => ({ typewriter_cps: 50, close_behavior: "ask" })),
    updateUISettings: vi.fn(async (cps: number) => ({ ok: true, typewriter_cps: cps })),
    updateCloseBehavior: mocks.updateCloseBehavior,
    getOnboardingStatus: vi.fn(async () => ({
      done: false,
      has_credential: false,
      has_name: false,
      has_content: false,
      wizard_seen: false,
      welcome_version: "",
      app_version: "0.1.6",
      show_wizard: false,
      hint_dismissed: false,
    })),
  },
}));

function visiblePanel(w: ReturnType<typeof mount>) {
  return w.findAll(".panel").find((p) => (p.element as HTMLElement).style.display !== "none")!;
}

async function mountSettings() {
  const pinia = createPinia();
  setActivePinia(pinia);
  const w = mount(SettingsView, {
    attachTo: document.body,
    global: { plugins: [pinia], stubs: { RouterLink: true } },
  });
  await flushPromises();
  return w;
}

beforeEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
  document.body.innerHTML = "";
  mocks.updateCloseBehavior.mockResolvedValue({ ok: true, typewriter_cps: 50, close_behavior: "tray" });
});

describe("设置页反馈生命周期", () => {
  it("成功提示约 4 秒后自动收回（成功不必长期占位）", async () => {
    const w = await mountSettings();
    const panel = visiblePanel(w);
    const system = panel.findAll(".theme-opt").find((b) => b.text() === "系统")!;
    vi.useFakeTimers();
    await system.trigger("click");
    await nextTick();
    const msg = visiblePanel(w).find(".msg");
    expect(msg.exists()).toBe(true);
    expect(msg.classes()).toContain("ok");
    vi.advanceTimersByTime(4100);
    await nextTick();
    expect(visiblePanel(w).find(".msg").exists()).toBe(false);
    vi.useRealTimers();
    w.unmount();
  });

  it("失败提示持久保留（比成功更久），不随计时自动消失", async () => {
    mocks.updateCloseBehavior.mockRejectedValueOnce(new Error("后端 500"));
    const w = await mountSettings();
    const panel = visiblePanel(w);
    const option = panel.findAll(".motion-opt").find((b) => b.text() === "最小化到托盘")!;
    await option.trigger("click");
    await flushPromises();
    const msg = visiblePanel(w).find(".msg");
    expect(msg.exists()).toBe(true);
    expect(msg.classes()).toContain("err");
    expect(msg.text()).toContain("保存关闭行为失败");

    vi.useFakeTimers();
    vi.advanceTimersByTime(10_000);
    await nextTick();
    expect(visiblePanel(w).find(".msg").classes()).toContain("err");
    vi.useRealTimers();
    w.unmount();
  });
});
