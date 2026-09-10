import { describe, expect, it, beforeEach, vi } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";
import { nextTick } from "vue";
import { createPinia, setActivePinia, type Pinia } from "pinia";
import SettingsView from "../SettingsView.vue";
import { getTheme, setTheme } from "../../utils/theme";
import { floatingState, resetFloatPositions } from "../../composables/floatingState";

vi.mock("../../services/api", () => ({
  api: {
    listCredentials: vi.fn(async () => ({ credentials: [] })),
    createCredential: vi.fn(async () => ({ ok: true, key_id: "k1", version: 2 })),
    revokeCredential: vi.fn(async () => ({ ok: true })),
    deleteCredential: vi.fn(async () => ({ ok: true, key_id: "k1" })),
    testCredential: vi.fn(async () => ({ key_id: "k1", probe: { mode: "native", detail: "连接正常" } })),
    updateCredentialMeta: vi.fn(async () => ({ ok: true, credential: {} })),
    setCredentialEnabled: vi.fn(async () => ({ ok: true, credential: {} })),
    getCredentialAudit: vi.fn(async () => ({ ok: true, audit: [] })),
    getMemorySettings: vi.fn(async () => ({ fragment_max_messages: 10 })),
    updateMemorySettings: vi.fn(async () => ({ fragment_max_messages: 10 })),
    getMaintenanceSettings: vi.fn(async () => ({ enabled: false, interval_hours: 24 })),
    updateMaintenanceSettings: vi.fn(async () => ({ enabled: false, interval_hours: 24 })),
    runMaintenance: vi.fn(async () => ({ started: false })),
    getSearchSettings: vi.fn(async () => ({
      searxng_url: "",
      bocha_has_key: false,
      top_k_default: 5,
      max_fetch_chars: 15000,
    })),
    updateSearchSettings: vi.fn(async (body: Record<string, unknown>) => ({
      searxng_url: String(body.searxng_url ?? ""),
      bocha_has_key: Boolean(body.bocha_api_key),
      top_k_default: Number(body.top_k_default ?? 5),
      max_fetch_chars: Number(body.max_fetch_chars ?? 15000),
    })),
    getUISettings: vi.fn(async () => ({ typewriter_cps: 50 })),
    updateUISettings: vi.fn(async (cps: number) => ({ ok: true, typewriter_cps: cps })),
    getComputerSettings: vi.fn(async () => ({ root_dir: "", permission_mode: "default" })),
    updateComputerSettings: vi.fn(async (body: Record<string, unknown>) => ({
      root_dir: String(body.root_dir ?? ""),
      permission_mode: String(body.permission_mode ?? "default"),
    })),
    getLoopSettings: vi.fn(async () => ({ max_iterations: 128, output_token_budget: 51200 })),
    updateLoopSettings: vi.fn(async (body: Record<string, unknown>) => ({
      max_iterations: Number(body.max_iterations ?? 128),
      output_token_budget: Number(body.output_token_budget ?? 51200),
    })),
    openai: {},
  },
}));

const CRED = {
  key_id: "k1",
  version: 2,
  tags: ["chat"],
  endpoint: "https://api.openai.com/v1",
  default_model: "gpt-4o-mini",
  budget: 40,
  budget_used: 12,
  status: "active",
  enabled: true,
  note: "测试",
};

function makePinia(): Pinia {
  const pinia = createPinia();
  setActivePinia(pinia);
  return pinia;
}

beforeEach(() => {
  localStorage.clear();
  resetFloatPositions();
  document.documentElement.removeAttribute("data-theme");
});

describe("SettingsView", () => {
  it("渲染凭据/偏好/窗口三个 tab 与「新建凭据」入口按钮", async () => {
    const w = mount(SettingsView, { global: { plugins: [makePinia()], stubs: { RouterLink: true } } });
    await nextTick();
    expect(w.findAll(".tab").length).toBe(3);
    expect(w.findAll(".tab").map((b) => b.text())).toEqual(["凭据", "偏好", "窗口"]);
    expect(w.find(".tab.active").text()).toBe("凭据");
    expect(w.find(".new-cred").exists()).toBe(true);
    expect(w.find(".new-cred").text()).toContain("新建凭据");
    w.unmount();
  });
  it("点击偏好 tab 显示偏好分区", async () => {
    const w = mount(SettingsView, { global: { plugins: [makePinia()], stubs: { RouterLink: true } } });
    await w.findAll(".tab")[1].trigger("click");
    expect(w.find(".tab.active").text()).toBe("偏好");
    expect(w.find(".qio-switch").exists()).toBe(true);
    w.unmount();
  });
  it("点击「新建凭据」打开创建模态框", async () => {
    const w = mount(SettingsView, { global: { plugins: [makePinia()], stubs: { RouterLink: true } } });
    await nextTick();
    expect(w.find(".modal-mask").exists()).toBe(false);
    await w.find(".new-cred").trigger("click");
    await nextTick();
    expect(w.find(".modal-mask").exists()).toBe(true);
    w.unmount();
  });

  it("点击卡片「停用」调用 setCredentialEnabled(false)", async () => {
    const { api } = await import("../../services/api");
    (api.listCredentials as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      credentials: [{ ...CRED, enabled: true }],
    });
    const w = mount(SettingsView, { global: { plugins: [makePinia()], stubs: { RouterLink: true } } });
    await flushPromises();
    await w.find(".btn-toggle").trigger("click");
    await flushPromises();
    expect(api.setCredentialEnabled).toHaveBeenCalledWith("k1", false);
    w.unmount();
  });

  it("默认筛选只显示已启用，已撤销/已过期需手动切换", async () => {
    const { api } = await import("../../services/api");
    (api.listCredentials as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      credentials: [CRED, { ...CRED, key_id: "k_old", status: "revoked", note: null }],
    });
    const w = mount(SettingsView, { global: { plugins: [makePinia()], stubs: { RouterLink: true } } });
    await flushPromises();
    // 默认只显示 active+enabled 的一条
    expect(w.findAll(".cred-card").length).toBe(1);
    // 切到「已撤销」才出现被撤销那条
    const revokedBtn = w.findAll(".filter").find((b) => b.text() === "已撤销");
    expect(revokedBtn).toBeTruthy();
    await revokedBtn!.trigger("click");
    await nextTick();
    expect(w.findAll(".cred-card").length).toBe(1);
    expect(w.find(".name").text()).toBe("k_old");
    w.unmount();
  });

  it("点击卡片「删除」确认后调用 deleteCredential 并移出列表", async () => {
    const { api } = await import("../../services/api");
    (api.listCredentials as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      credentials: [CRED],
    });
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(true);
    const w = mount(SettingsView, { global: { plugins: [makePinia()], stubs: { RouterLink: true } } });
    await flushPromises();
    await w.find(".btn-danger").trigger("click");
    await flushPromises();
    expect(api.deleteCredential).toHaveBeenCalledWith("k1");
    expect(w.findAll(".cred-card").length).toBe(0);
    confirmSpy.mockRestore();
    w.unmount();
  });

  it("测试凭据：结果以一闪而过的 toast 气泡展示（成功）", async () => {
    const { api } = await import("../../services/api");
    (api.listCredentials as ReturnType<typeof vi.fn>).mockResolvedValueOnce({ credentials: [CRED] });
    const w = mount(SettingsView, { global: { plugins: [makePinia()], stubs: { RouterLink: true } } });
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
    const w = mount(SettingsView, { global: { plugins: [makePinia()], stubs: { RouterLink: true } } });
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

  it("偏好页渲染联网搜索卡片并拉取当前配置", async () => {
    const { api } = await import("../../services/api");
    (api.getSearchSettings as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      searxng_url: "http://127.0.0.1:8080",
      bocha_has_key: true,
      top_k_default: 8,
      max_fetch_chars: 20000,
    });
    const w = mount(SettingsView, { global: { plugins: [makePinia()], stubs: { RouterLink: true } } });
    await w.findAll(".tab")[1].trigger("click");
    await flushPromises();
    const panel = w.find(".panel:not([style*='display: none'])");
    expect(panel.text()).toContain("联网搜索");
    // 已配置状态出现在博查 API Key 旁
    expect(panel.text()).toContain("已配置");
    expect(api.getSearchSettings).toHaveBeenCalled();
    w.unmount();
  });

  it("保存搜索配置调用 updateSearchSettings 并显示成功提示", async () => {
    const { api } = await import("../../services/api");
    (api.getSearchSettings as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      searxng_url: "",
      bocha_has_key: false,
      top_k_default: 5,
      max_fetch_chars: 15000,
    });
    (api.updateSearchSettings as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      searxng_url: "",
      bocha_has_key: false,
      top_k_default: 5,
      max_fetch_chars: 15000,
    });
    const w = mount(SettingsView, { global: { plugins: [makePinia()], stubs: { RouterLink: true } } });
    await w.findAll(".tab")[1].trigger("click");
    await flushPromises();
    const buttons = w.findAll(".panel:not([style*='display: none']) .qio-btn");
    const saveBtn = buttons.find((b) => b.text().includes("保存搜索配置"));
    expect(saveBtn).toBeTruthy();
    await saveBtn!.trigger("click");
    await flushPromises();
    expect(api.updateSearchSettings).toHaveBeenCalled();
    const panel = w.find(".panel:not([style*='display: none'])");
    expect(panel.text()).toContain("已保存搜索配置");
    w.unmount();
  });

  it("偏好页渲染电脑操控卡片并拉取当前配置", async () => {
    const { api } = await import("../../services/api");
    (api.getComputerSettings as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      root_dir: "C:/work",
      permission_mode: "accept-edits",
    });
    const w = mount(SettingsView, { global: { plugins: [makePinia()], stubs: { RouterLink: true } } });
    await w.findAll(".tab")[1].trigger("click");
    await flushPromises();
    const panel = w.find(".panel:not([style*='display: none'])");
    expect(panel.text()).toContain("电脑操控");
    expect(panel.text()).toContain("工作区根目录");
    expect(api.getComputerSettings).toHaveBeenCalled();
    w.unmount();
  });

  it("偏好页渲染对话深度卡片并拉取当前配置", async () => {
    const { api } = await import("../../services/api");
    (api.getLoopSettings as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      max_iterations: 200,
      output_token_budget: 90000,
    });
    const w = mount(SettingsView, { global: { plugins: [makePinia()], stubs: { RouterLink: true } } });
    await w.findAll(".tab")[1].trigger("click");
    await flushPromises();
    const panel = w.find(".panel:not([style*='display: none'])");
    expect(panel.text()).toContain("对话深度");
    expect(panel.text()).toContain("单轮迭代上限");
    expect(api.getLoopSettings).toHaveBeenCalled();
    w.unmount();
  });
});

describe("SettingsView 窗口 Tab", () => {
  it("窗口 tab 展示两个贴靠隐藏开关（星球/设置）与还原布局按钮", async () => {
    const w = mount(SettingsView, { global: { plugins: [makePinia()], stubs: { RouterLink: true } } });
    await w.findAll(".tab")[2].trigger("click");
    await nextTick();
    expect(w.find(".tab.active").text()).toBe("窗口");
    const switches = w.findAll(".panel:not([style*='display: none']) .qio-switch");
    expect(switches.length).toBe(2);
    expect(w.find(".panel:not([style*='display: none']) .qio-btn").text()).toContain("还原默认布局");
    w.unmount();
  });

  it("切换「话题星球入口」贴靠隐藏：写入 shared state 并持久化 localStorage", async () => {
    const w = mount(SettingsView, { global: { plugins: [makePinia()], stubs: { RouterLink: true } } });
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
    const w = mount(SettingsView, { global: { plugins: [makePinia()], stubs: { RouterLink: true } } });
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

