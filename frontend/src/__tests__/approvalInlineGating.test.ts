/**
 * 同一时刻只允许一套按钮（Lead 裁决 3）：App 层的门控本身要有断言，
 * 不能只靠调用点自觉 —— 过程区已内联显示这条审批时，
 * 全局 ApprovalModal 必须从 DOM 里消失（按 approval_id 门控）。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import App from "../App.vue";
import ApprovalModal from "../components/ApprovalModal.vue";
import { useApprovalsStore } from "../stores/approvals";

const boot = vi.hoisted(() => ({ waitForBackend: vi.fn() }));
vi.mock("../services/boot", () => ({ waitForBackend: boot.waitForBackend }));

vi.mock("../stores/events", () => ({ useEventStore: () => ({ connect: vi.fn() }) }));
vi.mock("../stores/ui", () => ({ useUiStore: () => ({ load: vi.fn() }) }));
vi.mock("../stores/onboarding", () => ({
  useOnboardingStore: () => ({ load: vi.fn(), showWizard: false, closeForSession: vi.fn() }),
}));

const { respondApproval } = vi.hoisted(() => ({ respondApproval: vi.fn() }));
vi.mock("../services/api", () => ({
  api: { respondApproval },
  ApiError: class ApiError extends Error {},
}));

async function mountApp() {
  const pinia = createPinia();
  setActivePinia(pinia);
  const w = mount(App, {
    global: {
      plugins: [pinia],
      stubs: { RouterView: true, ApprovalEntry: true, DevTaskEntry: true },
    },
  });
  await flushPromises();
  return { w, approvals: useApprovalsStore() };
}

beforeEach(() => {
  respondApproval.mockReset();
  boot.waitForBackend.mockResolvedValue({ base: "http://127.0.0.1:1", token: "tk" });
});

describe("App 层审批门控", () => {
  it("过程区内联显示这条审批时，全局弹窗不渲染；释放后恢复", async () => {
    const { w, approvals } = await mountApp();
    approvals.enqueue("ap_1", "tool_execution", { description: "删除临时目录" }, { autoOpen: true });
    await flushPromises();
    // 正向对照：没有内联声明时，全局弹窗照常挂载
    expect(w.findComponent(ApprovalModal).exists()).toBe(true);

    approvals.claimInline("ap_1");
    await flushPromises();
    expect(w.findComponent(ApprovalModal).exists()).toBe(false);

    approvals.releaseInline("ap_1");
    await flushPromises();
    expect(w.findComponent(ApprovalModal).exists()).toBe(true);
    w.unmount();
  });

  it("内联声明的是别的审批：全局弹窗照常渲染（门控按 approval_id，不是「有审批就藏」）", async () => {
    const { w, approvals } = await mountApp();
    approvals.enqueue("ap_1", "tool_execution", { description: "删除临时目录" }, { autoOpen: true });
    approvals.claimInline("ap_other");
    await flushPromises();
    expect(w.findComponent(ApprovalModal).exists()).toBe(true);
    w.unmount();
  });
});
