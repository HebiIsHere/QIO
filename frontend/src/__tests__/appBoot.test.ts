/**
 * 启动界面：后端没应答时不能是一片空白。
 *
 * 真实事故（2026-09-24）：外壳启动被本机系统程序拖住 1 分 42 秒，窗口一直是白的，
 * 用户只能判断"它没启动"。现在这段等待必须是有内容的，等不到还要能重试。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import App from "../App.vue";

const boot = vi.hoisted(() => ({ waitForBackend: vi.fn() }));
vi.mock("../services/boot", () => ({ waitForBackend: boot.waitForBackend }));

const connect = vi.fn();
vi.mock("../stores/events", () => ({ useEventStore: () => ({ connect }) }));
vi.mock("../stores/ui", () => ({ useUiStore: () => ({ load: vi.fn() }) }));
vi.mock("../stores/onboarding", () => ({
  useOnboardingStore: () => ({ load: vi.fn(), showWizard: false, closeForSession: vi.fn() }),
}));

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

beforeEach(() => {
  vi.clearAllMocks();
});

describe("App 启动状态", () => {
  it("后端还没应答：显示正在启动的说明，而不是空白", async () => {
    boot.waitForBackend.mockReturnValue(new Promise(() => {})); // 一直 pending

    const w = await mountApp();

    expect(w.find(".boot-note").text()).toContain("正在启动");
    expect(w.find(".boot-note").text()).toContain("秒");
    expect(w.findComponent({ name: "RouterView" }).exists()).toBe(false);
    expect(connect).not.toHaveBeenCalled();
    w.unmount();
  });

  it("后端应答后渲染主界面，并建立事件流", async () => {
    boot.waitForBackend.mockResolvedValue({ base: "http://127.0.0.1:1", token: "tk" });

    const w = await mountApp();

    expect(w.find(".boot-note").exists()).toBe(false);
    expect(w.findComponent({ name: "RouterView" }).exists()).toBe(true);
    expect(connect).toHaveBeenCalled();
    w.unmount();
  });

  it("等不到后端：给出原因、日志位置与重试；重试成功后进入主界面", async () => {
    boot.waitForBackend.mockRejectedValueOnce(new Error("后端没有应答（最后一条错误：Failed to fetch）"));

    const w = await mountApp();

    expect(w.find(".boot-note.err").text()).toContain("后端没有应答");
    expect(w.find(".boot-note.err").text()).toContain("Failed to fetch");
    expect(w.find(".boot-note.err").text()).toContain("QIO.log");
    expect(w.find(".boot-note.err").text()).toContain("重试");
    expect(w.findComponent({ name: "RouterView" }).exists()).toBe(false);

    boot.waitForBackend.mockResolvedValueOnce({ base: "http://127.0.0.1:1", token: "tk" });
    await w.find(".boot-note.err button").trigger("click");
    await flushPromises();

    expect(w.find(".boot-note").exists()).toBe(false);
    expect(w.findComponent({ name: "RouterView" }).exists()).toBe(true);
    w.unmount();
  });
});
