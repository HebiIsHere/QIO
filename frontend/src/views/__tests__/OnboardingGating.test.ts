import { beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import App from "../../App.vue";
import ConversationView from "../ConversationView.vue";
import { useOnboardingStore } from "../../stores/onboarding";
import { api } from "../../services/api";

vi.mock("../../services/api", () => ({
  api: {
    getOnboardingStatus: vi.fn(),
    markOnboardingSeen: vi.fn(),
    saveOnboardingProfile: vi.fn(),
    completeOnboarding: vi.fn(),
    setOnboardingHint: vi.fn(),
    listCredentials: vi.fn(async () => ({ credentials: [] })),
    createCredential: vi.fn(),
    testCredential: vi.fn(),
    loadHistory: vi.fn(async () => ({ messages: [], anchor: null })),
    getUISettings: vi.fn(async () => ({ typewriter_cps: 50 })),
  },
}));

vi.mock("../../stores/events", () => ({
  useEventStore: () => ({ connect: vi.fn() }),
}));

vi.mock("../../stores/ui", () => ({
  useUiStore: () => ({ load: vi.fn() }),
}));

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

function stubStatus(status: Record<string, unknown>) {
  (api.getOnboardingStatus as unknown as { mockResolvedValue: (v: unknown) => void })
    .mockResolvedValue({ ...baseStatus, ...status });
  (api.markOnboardingSeen as unknown as { mockResolvedValue: (v: unknown) => void })
    .mockResolvedValue({ ...baseStatus, ...status, wizard_seen: true, show_wizard: false });
  (api.setOnboardingHint as unknown as { mockResolvedValue: (v: unknown) => void })
    .mockResolvedValue({ ...baseStatus, ...status, hint_dismissed: true });
}

async function mountApp() {
  const pinia = createPinia();
  setActivePinia(pinia);
  const wrapper = mount(App, {
    global: {
      plugins: [pinia],
      stubs: { RouterView: true, ApprovalEntry: true, ApprovalModal: true },
    },
  });
  await flushPromises();
  return wrapper;
}

async function mountConversation() {
  const pinia = createPinia();
  setActivePinia(pinia);
  const wrapper = mount(ConversationView, {
    global: {
      plugins: [pinia],
      stubs: {
        MessageStream: true,
        Composer: true,
        SettingsFloat: true,
        PlanetDock: true,
        PlanetView: true,
        PlanetBoot: true,
      },
    },
  });
  await flushPromises();
  return wrapper;
}

describe("欢迎页 gating", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.clear();
  });

  it("首次启动（show_wizard=true）→ 展开全屏向导", async () => {
    stubStatus({});
    const w = await mountApp();
    expect(w.find(".onboarding").exists()).toBe(true);
  });

  it("本版本已展示过（show_wizard=false）→ 不展开", async () => {
    stubStatus({ wizard_seen: true, welcome_version: "0.1.6", show_wizard: false });
    const w = await mountApp();
    expect(w.find(".onboarding").exists()).toBe(false);
  });

  it("刚更新到这个版本（done=true 但 show_wizard=true）→ 仍然展开一次", async () => {
    stubStatus({
      done: true,
      has_name: true,
      wizard_seen: true,
      welcome_version: "0.1.5",
      show_wizard: true,
    });
    const w = await mountApp();
    expect(w.find(".onboarding").exists()).toBe(true);
  });

  it("未完成且未关闭 → 对话页「继续设置」；点 × 记录关闭；点「继续设置」重新展开", async () => {
    stubStatus({ done: false, hint_dismissed: false });
    const w = await mountConversation();
    const store = useOnboardingStore();
    await store.load();
    await flushPromises();

    const hint = w.find(".setup-hint");
    expect(hint.exists()).toBe(true);
    expect(hint.text()).toContain("继续设置");

    await hint.find(".close").trigger("click");
    await flushPromises();
    expect(api.setOnboardingHint).toHaveBeenCalledWith(true);

    // 重新展开：点击「继续设置」后向导应当出现
    await hint.find(".resume").trigger("click");
    expect(store.showWizard).toBe(true);
  });
});
