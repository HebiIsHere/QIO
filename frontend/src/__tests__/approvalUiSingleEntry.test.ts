/**
 * 同一时刻只允许**一套**审批界面（A-CP1 迁移后的结构性断言）。
 *
 * 旧实现由 App 挂全局 ApprovalModal + ApprovalEntry，再靠 approval_id 门控与内联卡
 * 互斥；迁移后非聊天路由使用统一入口 + 统一弹窗（NonChatApprovalEntry +
 * UnifiedApprovalLayer），聊天页由各自视图挂载同一个 UnifiedApprovalLayer。
 * 这里锁定：旧展示层不再出现在 App 里、入口只出现一份、弹窗只挂载一份。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { createRouter, createMemoryHistory } from "vue-router";
import App from "../App.vue";
import NonChatApprovalEntry from "../components/approval/NonChatApprovalEntry.vue";
import UnifiedApprovalLayer from "../components/conversation/UnifiedApprovalLayer.vue";
import { useApprovalsStore } from "../stores/approvals";

/**
 * 旧展示层的 DOM 痕迹（不 import 旧组件 —— 那些文件会在 A-CP1 后由 C 删除）：
 * 旧 ApprovalModal 的遮罩 class 是 .modal-mask（弹窗主体 .modal），
 * 旧 ApprovalEntry 的入口 class 是 .approval-entry。
 * 新统一弹窗的遮罩是 .ua-mask / data-unified-approval-modal。
 */
function legacyApprovalMarkers(wrapper: any) {
  return {
    oldModal: wrapper.find(".modal-mask").exists() || wrapper.find(".modal").exists(),
    oldEntry: wrapper.find(".approval-entry").exists(),
  };
}

const boot = vi.hoisted(() => ({ waitForBackend: vi.fn() }));
vi.mock("../services/boot", () => ({ waitForBackend: boot.waitForBackend, BOOT_SLOW_MS: 10_000 }));

vi.mock("../stores/events", () => ({ useEventStore: () => ({ connect: vi.fn() }) }));
vi.mock("../stores/ui", () => ({ useUiStore: () => ({ load: vi.fn() }) }));
vi.mock("../stores/onboarding", () => ({
  useOnboardingStore: () => ({ load: vi.fn(), showWizard: false, closeForSession: vi.fn() }),
}));
vi.mock("../stores/security", () => ({
  useSecurityStore: () => ({ load: vi.fn() }),
}));

const { respondApproval } = vi.hoisted(() => ({ respondApproval: vi.fn() }));
vi.mock("../services/api", () => ({
  api: { respondApproval },
  ApiError: class ApiError extends Error {},
}));

// 启动恢复/历史必须是确定的空快照：不能连本机真实后端（别的验证进程可能正在跑）。
vi.mock("../services/approvalLifecycle", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../services/approvalLifecycle")>();
  return {
    ...actual,
    getPendingApprovals: vi.fn(async () => []),
    getApprovalHistory: vi.fn(async () => []),
  };
});

function makeRouter() {
  const Empty = { template: "<div />" };
  return createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: "/", name: "conversation", component: Empty },
      { path: "/interactive", name: "interactive", component: Empty },
      { path: "/settings", name: "settings", component: Empty },
    ],
  });
}

async function mountApp(path = "/settings") {
  const pinia = createPinia();
  setActivePinia(pinia);
  const router = makeRouter();
  await router.push(path);
  await router.isReady();
  const w = mount(App, {
    global: {
      plugins: [pinia, router],
      stubs: { RouterView: true, DevTaskEntry: true },
    },
  });
  await flushPromises();
  return { w, approvals: useApprovalsStore() };
}

beforeEach(() => {
  respondApproval.mockReset();
  boot.waitForBackend.mockResolvedValue({ base: "http://127.0.0.1:1", token: "tk" });
});

describe("App 层：旧审批展示层已让位给统一体系", () => {
  it("非聊天路由：不再渲染旧 ApprovalModal/ApprovalEntry，改为统一入口 + 统一弹窗", async () => {
    const { w } = await mountApp("/settings");
    const markers = legacyApprovalMarkers(w);
    expect(markers.oldModal).toBe(false);
    expect(markers.oldEntry).toBe(false);
    expect(w.findComponent(NonChatApprovalEntry).exists()).toBe(true);
    expect(w.findComponent(UnifiedApprovalLayer).exists()).toBe(true);
    w.unmount();
  });

  it("有待办时只有一个入口；点开后只有一个统一弹窗（不是两套按钮）", async () => {
    const { w, approvals } = await mountApp("/settings");
    approvals.enqueue("ap_1", "computer", { description: "写入文件" }, { autoOpen: false });
    await flushPromises();
    expect(w.findAllComponents(NonChatApprovalEntry)).toHaveLength(1);
    expect(w.find('[data-test="nonchat-approval-entry"]').exists()).toBe(true);

    await w.find('[data-test="nonchat-approval-entry"]').trigger("click");
    await flushPromises();
    // 只有一个对话框（不是两套按钮）；旧展示层的遮罩不存在
    expect(w.findAll('[role="dialog"][aria-modal="true"]')).toHaveLength(1);
    expect(w.find('[data-unified-approval-layer]').exists()).toBe(true);
    expect(legacyApprovalMarkers(w).oldModal).toBe(false);
    w.unmount();
  });

  it("聊天路由：App 不挂非聊天入口（入口由该页自己的统一体系提供）", async () => {
    const { w } = await mountApp("/");
    expect(w.findComponent(NonChatApprovalEntry).exists()).toBe(false);
    const markers = legacyApprovalMarkers(w);
    expect(markers.oldModal).toBe(false);
    expect(markers.oldEntry).toBe(false);
    w.unmount();
  });
});
