import { describe, expect, it, beforeEach, vi } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";
import { nextTick } from "vue";
import SettingsView from "../SettingsView.vue";
import { getTheme, setTheme } from "../../utils/theme";
import { floatingState, resetFloatPositions } from "../../composables/floatingState";

vi.mock("../../services/api", () => ({
  api: {
    listCredentials: vi.fn(async () => ({ credentials: [] })),
    createCredential: vi.fn(async () => ({ ok: true, key_id: "k1", version: 2 })),
    revokeCredential: vi.fn(async () => ({ ok: true })),
    testCredential: vi.fn(async () => ({ key_id: "k1", probe: { mode: "native", detail: "连接正常" } })),
    getMemorySettings: vi.fn(async () => ({ fragment_max_messages: 10 })),
    updateMemorySettings: vi.fn(async () => ({ fragment_max_messages: 10 })),
    getMaintenanceSettings: vi.fn(async () => ({ enabled: false, interval_hours: 24 })),
    updateMaintenanceSettings: vi.fn(async () => ({ enabled: false, interval_hours: 24 })),
    runMaintenance: vi.fn(async () => ({ started: false })),
    openai: {},
  },
}));

const CRED = {
  key_id: "k1",
  version: 2,
  tags: ["chat"],
  endpoint: "https://api.openai.com/v1",
  default_model: "gpt-4o-mini",
  scope: ["default"],
  budget: 40,
  budget_used: 12,
  status: "active",
  note: "测试",
};

beforeEach(() => {
  localStorage.clear();
  resetFloatPositions();
  document.documentElement.removeAttribute("data-theme");
});

describe("SettingsView", () => {
  it("渲染凭据/偏好/窗口三个 tab 与表单控件（QInput/QSelect）", async () => {
    const w = mount(SettingsView, { global: { stubs: { RouterLink: true } } });
    await nextTick();
    expect(w.findAll(".tab").length).toBe(3);
    expect(w.findAll(".tab").map((b) => b.text())).toEqual(["凭据", "偏好", "窗口"]);
    expect(w.find(".tab.active").text()).toBe("凭据");
    expect(w.find(".qio-input").exists()).toBe(true);
    expect(w.find(".qio-select").exists()).toBe(true);
    expect(w.find(".create").text()).toBe("创建凭据");
    w.unmount();
  });
  it("点击偏好 tab 显示偏好分区", async () => {
    const w = mount(SettingsView, { global: { stubs: { RouterLink: true } } });
    await w.findAll(".tab")[1].trigger("click");
    expect(w.find(".tab.active").text()).toBe("偏好");
    expect(w.find(".qio-switch").exists()).toBe(true);
    w.unmount();
  });
  it("空密钥创建被拦截并提示", async () => {
    const w = mount(SettingsView, { global: { stubs: { RouterLink: true } } });
    await nextTick();
    await w.find(".create").trigger("click");
    await nextTick();
    expect(w.find(".msg.err").text()).toContain("请先粘贴 API Key");
    w.unmount();
  });

  it("测试凭据：结果以一闪而过的 toast 气泡展示（成功）", async () => {
    const { api } = await import("../../services/api");
    (api.listCredentials as ReturnType<typeof vi.fn>).mockResolvedValueOnce({ credentials: [CRED] });
    const w = mount(SettingsView, { global: { stubs: { RouterLink: true } } });
    await flushPromises();
    const testBtn = w.findAll(".cred-card .qio-btn")[0];
    await testBtn.trigger("click");
    await flushPromises();
    const toast = w.find(".toast");
    expect(toast.exists()).toBe(true);
    expect(toast.classes()).toContain("ok");
    expect(toast.text()).toContain("native");
    w.unmount();
  });
  it("偏好页主题开关切换主题并持久化 data-theme/qio-theme", async () => {
    // 模拟 main.ts 启动时应用持久化主题（组件不再兜底写 data-theme）
    setTheme(getTheme());
    const w = mount(SettingsView, { global: { stubs: { RouterLink: true } } });
    await w.findAll(".tab")[1].trigger("click");
    const sw = w.find(".theme-switch");
    expect(sw.exists()).toBe(true);
    // 默认暗色
    expect(document.documentElement.dataset.theme).toBe("dark");
    await sw.trigger("click");
    expect(document.documentElement.dataset.theme).toBe("light");
    expect(localStorage.getItem("qio-theme")).toBe("light");
    await sw.trigger("click");
    expect(document.documentElement.dataset.theme).toBe("dark");
    expect(localStorage.getItem("qio-theme")).toBe("dark");
    w.unmount();
  });
});

describe("SettingsView 窗口 Tab", () => {
  it("窗口 tab 展示两个贴靠隐藏开关（星球/设置）与还原布局按钮", async () => {
    const w = mount(SettingsView, { global: { stubs: { RouterLink: true } } });
    await w.findAll(".tab")[2].trigger("click");
    await nextTick();
    expect(w.find(".tab.active").text()).toBe("窗口");
    const switches = w.findAll(".panel:not([style*='display: none']) .qio-switch");
    expect(switches.length).toBe(2);
    expect(w.find(".panel:not([style*='display: none']) .qio-btn").text()).toContain("还原默认布局");
    w.unmount();
  });

  it("切换「话题星球入口」贴靠隐藏：写入 shared state 并持久化 localStorage", async () => {
    const w = mount(SettingsView, { global: { stubs: { RouterLink: true } } });
    await w.findAll(".tab")[2].trigger("click");
    await nextTick();
    expect(floatingState["planet-dock"].hideEnabled).toBe(false);
    // 第一个开关 = 话题星球入口（WINDOW_ITEMS 顺序：planet-dock / settings-float）
    const sw = w.findAll(".panel:not([style*='display: none']) .qio-switch")[0];
    await sw.trigger("click");
    await nextTick();
    expect(floatingState["planet-dock"].hideEnabled).toBe(true);
    expect(sw.attributes("aria-checked")).toBe("true");
    const saved = JSON.parse(localStorage.getItem("qio-float-hide") || "{}");
    expect(saved["planet-dock"]).toBe(true);
    w.unmount();
  });

  it("还原默认布局：关闭全部贴靠隐藏并清除持久化", async () => {
    const w = mount(SettingsView, { global: { stubs: { RouterLink: true } } });
    await w.findAll(".tab")[2].trigger("click");
    await nextTick();
    const switches = w.findAll(".panel:not([style*='display: none']) .qio-switch");
    for (const s of switches) await s.trigger("click");
    await nextTick();
    expect(floatingState["planet-dock"].hideEnabled).toBe(true);
    expect(localStorage.getItem("qio-float-hide")).toBeTruthy();
    await w.find(".panel:not([style*='display: none']) .qio-btn").trigger("click");
    await nextTick();
    expect(floatingState.composer.hideEnabled).toBe(false);
    expect(floatingState["settings-float"].hideEnabled).toBe(false);
    expect(localStorage.getItem("qio-float-positions")).toBeNull();
    expect(localStorage.getItem("qio-float-hide")).toBeNull();
    expect(w.find(".msg.ok").text()).toContain("已还原默认布局");
    w.unmount();
  });
});
