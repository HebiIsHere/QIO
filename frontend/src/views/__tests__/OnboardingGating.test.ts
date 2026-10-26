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

// 启动握手是外壳/后端的事，这里把它当作已就绪 —— 这几条用例只关心欢迎页的展开规则
const boot = vi.hoisted(() => ({ waitForBackend: vi.fn() }));
vi.mock("../../services/boot", () => ({ waitForBackend: boot.waitForBackend, BOOT_SLOW_MS: 10_000 }));

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
      stubs: { RouterView: true, NonChatApprovalEntry: true, DevTaskEntry: true },
    },
  });
  await flushPromises();
  return wrapper;
}

async function mountConversation() {
  const pinia = createPinia();
  setActivePinia(pinia);
  // 先把首屏状态读好再挂载：这条用例断言的是「已读到未完成状态后提示怎么显示/怎么关」，
  // 与挂载时序无关。并行跑多个测试文件时，先挂载后 load 会让提示的首次渲染依赖
  // 调度顺序（组件已经挂载、状态稍后才到），断言会间歇性看不到提示。
  const onboarding = useOnboardingStore();
  await onboarding.load();
  const wrapper = mount(ConversationView, {
    global: {
      plugins: [pinia],
      stubs: {
        MessageStream: true,
        Composer: true,
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
    boot.waitForBackend.mockResolvedValue({ base: "http://127.0.0.1:1", token: "tk" });
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

  it("未完成时对话页不再渲染设置提示（顶部一次性轻提醒由 D 承接）；关闭与重开能力保留", async () => {
    stubStatus({ done: false, hint_dismissed: false });
    const w = await mountConversation();
    const store = useOnboardingStore();
    await store.load();
    await flushPromises();

    // 生产挂载已移除：主对话不再有 .setup-hint（引导门槛/必填/迁移/后端 hint 字段都没改）
    expect(w.find(".setup-hint").exists()).toBe(false);
    expect(store.hintVisible, "数据仍如实反映「未完成且未关闭」").toBe(true);

    // 关闭能力保留：仍然调用既有的后端字段写入
    await store.setHintDismissed(true);
    await flushPromises();
    expect(api.setOnboardingHint).toHaveBeenCalledWith(true);
    expect(store.hintVisible).toBe(false);

    // 重新展开能力保留（设置页「重新运行设置助手」与旧入口共用）
    store.reopen();
    expect(store.showWizard).toBe(true);
  });
});

