import { describe, expect, it, beforeEach, vi } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";
import { nextTick } from "vue";
import { createPinia, setActivePinia, type Pinia } from "pinia";
import SettingsView from "../SettingsView.vue";
import { getTheme, getThemePreference, setTheme } from "../../utils/theme";
import { floatingState, resetFloatPositions } from "../../composables/floatingState";
import { useUiStore } from "../../stores/ui";

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
      keyless_fallback: body.keyless_fallback === undefined ? true : Boolean(body.keyless_fallback),
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

const NAV_LABELS = ["外观", "对话与记忆", "模型与联网", "工具与权限", "数据与维护", "凭据", "高级"];

function makePinia(): Pinia {
  const pinia = createPinia();
  setActivePinia(pinia);
  return pinia;
}

async function mountSettings() {
  const w = mount(SettingsView, {
    attachTo: document.body,
    global: { plugins: [makePinia()], stubs: { RouterLink: true } },
  });
  await flushPromises();
  return w;
}

async function openTab(w: Awaited<ReturnType<typeof mountSettings>>, label: string) {
  const tab = w.findAll(".tab").find((b) => b.text().includes(label));
  expect(tab, `找不到分区「${label}」`).toBeTruthy();
  await tab!.trigger("click");
  await nextTick();
}

/** 当前可见面板（v-show 隐藏的面板不参与断言） */
function visiblePanel(w: Awaited<ReturnType<typeof mountSettings>>) {
  return w.findAll(".panel").find((p) => (p.element as HTMLElement).style.display !== "none")!;
}

beforeEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
  resetFloatPositions();
  document.documentElement.removeAttribute("data-theme");
  document.body.innerHTML = "";
});

describe("SettingsView 信息架构", () => {
  it("按用户心智分为七个一级分区，默认停在「外观」", async () => {
    const w = await mountSettings();
    expect(w.findAll(".tab").map((b) => b.text())).toEqual(NAV_LABELS);
    expect(w.find(".tab.active").text()).toBe("外观");
    expect(visiblePanel(w).text()).toContain("主题");
    w.unmount();
  });

  it("凭据分区保留「新建凭据」入口，服务端 tuning 收进「高级」", async () => {
    const w = await mountSettings();
    expect(w.find(".new-cred").exists()).toBe(true);
    await openTab(w, "高级");
    const panel = visiblePanel(w);
    expect(panel.text()).toContain("开发者模式");
    expect(panel.text()).toContain("SearXNG");
    w.unmount();
  });

  it("主题：System / Dark / Light 三选项，选择 system 存偏好而非解析结果", async () => {
    setTheme("dark");
    const w = await mountSettings();
    const opts = w.findAll(".theme-opt");
    expect(opts.map((o) => o.text())).toEqual(["系统", "暗紫晶", "净白"]);
    await opts[0].trigger("click");
    expect(getThemePreference()).toBe("system");
    expect(localStorage.getItem("qio-theme")).toBe("system");
    await opts[2].trigger("click");
    expect(getThemePreference()).toBe("light");
    expect(document.documentElement.dataset.theme).toBe("light");
    await opts[1].trigger("click");
    expect(getTheme()).toBe("dark");
    w.unmount();
  });

  it("窗口行为并入「外观」：两个贴靠隐藏开关 + 还原布局", async () => {
    const w = await mountSettings();
    const switches = visiblePanel(w).findAll(".qio-switch");
    expect(switches.length).toBe(2);
    expect(visiblePanel(w).text()).toContain("还原默认布局");
    w.unmount();
  });
});

describe("SettingsView 分区反馈不串台（问题5/6）", () => {
  it("记忆保存失败：错误只出现在记忆区，且用错误语义而非成功色", async () => {
    const { api } = await import("../../services/api");
    (api.updateMemorySettings as ReturnType<typeof vi.fn>).mockRejectedValueOnce(
      new Error("PUT /api/settings/memory -> 500: boom"),
    );
    const w = await mountSettings();
    await openTab(w, "对话与记忆");

    // 通过自定义轮数触发保存（失焦 commit）
    const tierSelect = visiblePanel(w).find(".qio-select");
    await tierSelect.trigger("click");
    await nextTick();
    const custom = visiblePanel(w).findAll(".qio-select-menu .opt").find((o) => o.text().includes("自定义"));
    await custom!.trigger("click");
    await nextTick();
    const input = visiblePanel(w).find(".q-number-input");
    await input.setValue("12");
    await input.trigger("blur");
    await flushPromises();

    const memoryMsg = visiblePanel(w).find(".msg");
    expect(memoryMsg.exists()).toBe(true);
    expect(memoryMsg.classes()).toContain("err");
    expect(memoryMsg.classes()).not.toContain("ok");
    expect(memoryMsg.text()).toContain("记忆");
    expect(memoryMsg.text()).toContain("失败");

    // 切到联网分区：不应看到记忆分区的错误
    await openTab(w, "模型与联网");
    expect(visiblePanel(w).text()).not.toContain("记忆");
    w.unmount();
  });

  it("维护保存成功：只在维护分区显示成功提示（带来源说明）", async () => {
    const { api } = await import("../../services/api");
    (api.runMaintenance as ReturnType<typeof vi.fn>).mockResolvedValueOnce({ ok: true, started: true });
    const w = await mountSettings();
    await openTab(w, "数据与维护");
    const btn = visiblePanel(w).findAll(".qio-btn").find((b) => b.text().includes("立即运行"));
    await btn!.trigger("click");
    await flushPromises();
    const msg = visiblePanel(w).find(".msg");
    expect(msg.classes()).toContain("ok");
    expect(msg.text()).toContain("维护");
    w.unmount();
  });
});

describe("SettingsView 凭据分区", () => {
  it("点击卡片「停用」调用 setCredentialEnabled(false)", async () => {
    const { api } = await import("../../services/api");
    (api.listCredentials as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      credentials: [{ ...CRED, enabled: true }],
    });
    const w = await mountSettings();
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
    const w = await mountSettings();
    expect(w.findAll(".cred-card").length).toBe(1);
    const revokedBtn = w.findAll(".filter").find((b) => b.text() === "已撤销");
    await revokedBtn!.trigger("click");
    await nextTick();
    expect(w.findAll(".cred-card").length).toBe(1);
    expect(w.find(".name").text()).toBe("k_old");
    w.unmount();
  });

  it("点击卡片「删除」确认后调用 deleteCredential 并移出列表", async () => {
    const { api } = await import("../../services/api");
    (api.listCredentials as ReturnType<typeof vi.fn>).mockResolvedValueOnce({ credentials: [CRED] });
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(true);
    const w = await mountSettings();
    await w.find(".btn-danger").trigger("click");
    await flushPromises();
    expect(api.deleteCredential).toHaveBeenCalledWith("k1");
    expect(w.findAll(".cred-card").length).toBe(0);
    confirmSpy.mockRestore();
    w.unmount();
  });

  it("新建凭据打开模态框", async () => {
    const w = await mountSettings();
    expect(w.find(".modal-mask").exists()).toBe(false);
    await w.find(".new-cred").trigger("click");
    await nextTick();
    expect(w.find(".modal-mask").exists()).toBe(true);
    w.unmount();
  });

  it("测试凭据：结果以一闪而过的 toast 气泡展示（成功）", async () => {
    const { api } = await import("../../services/api");
    (api.listCredentials as ReturnType<typeof vi.fn>).mockResolvedValueOnce({ credentials: [CRED] });
    const w = await mountSettings();
    await w.findAll(".cred-card .qio-btn")[0].trigger("click");
    await flushPromises();
    const toast = w.find(".toast");
    expect(toast.exists()).toBe(true);
    expect(toast.classes()).toContain("ok");
    expect(toast.text()).toContain("native");
    w.unmount();
  });
});

describe("SettingsView 联网搜索凭据语义（问题7）", () => {
  it("已配置时留空保存：不发送 bocha_api_key，凭据不被清除", async () => {
    const { api } = await import("../../services/api");
    (api.getSearchSettings as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      searxng_url: "http://127.0.0.1:8080",
      bocha_has_key: true,
      top_k_default: 8,
      max_fetch_chars: 20000,
    });
    const w = await mountSettings();
    await openTab(w, "模型与联网");
    const panel = visiblePanel(w);
    expect(panel.text()).toContain("已配置");

    const saveBtn = panel.findAll(".qio-btn").find((b) => b.text().includes("保存搜索配置"));
    await saveBtn!.trigger("click");
    await flushPromises();

    const body = (api.updateSearchSettings as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(body).not.toHaveProperty("bocha_api_key");
    expect(visiblePanel(w).text()).toContain("已保存搜索配置");
    w.unmount();
  });

  it("「替换」输入新 Key 后保存才发送 bocha_api_key", async () => {
    const { api } = await import("../../services/api");
    (api.getSearchSettings as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      searxng_url: "",
      bocha_has_key: true,
      top_k_default: 5,
      max_fetch_chars: 15000,
    });
    const w = await mountSettings();
    await openTab(w, "模型与联网");
    const panel = () => visiblePanel(w);
    const replaceBtn = panel().findAll(".qio-btn").find((b) => b.text().includes("替换"));
    await replaceBtn!.trigger("click");
    await nextTick();
    await panel().find(".bocha-key-input").setValue("sk-new-key");
    const saveBtn = panel().findAll(".qio-btn").find((b) => b.text().includes("保存搜索配置"));
    await saveBtn!.trigger("click");
    await flushPromises();

    const body = (api.updateSearchSettings as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(body.bocha_api_key).toBe("sk-new-key");
    w.unmount();
  });

  it("「清除」是独立的危险动作，需要确认，且明确发送空值", async () => {
    const { api } = await import("../../services/api");
    (api.getSearchSettings as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      searxng_url: "",
      bocha_has_key: true,
      top_k_default: 5,
      max_fetch_chars: 15000,
    });
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(true);
    const w = await mountSettings();
    await openTab(w, "模型与联网");
    const clearBtn = visiblePanel(w).findAll(".qio-btn").find((b) => b.text().includes("清除"));
    expect(clearBtn).toBeTruthy();
    await clearBtn!.trigger("click");
    await flushPromises();
    expect(confirmSpy).toHaveBeenCalled();
    const body = (api.updateSearchSettings as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(body).toEqual({ bocha_api_key: "" });
    confirmSpy.mockRestore();
    w.unmount();
  });

  it("免密钥联网搜索开关：默认开启，点击后立即保存到后端", async () => {
    const { api } = await import("../../services/api");
    (api.getSearchSettings as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      searxng_url: "",
      bocha_has_key: false,
      keyless_fallback: true,
      top_k_default: 5,
      max_fetch_chars: 15000,
    });
    const w = await mountSettings();
    await openTab(w, "模型与联网");
    const sw = visiblePanel(w).find(".keyless-switch");
    expect(sw.exists()).toBe(true);
    expect(sw.attributes("aria-checked")).toBe("true");

    await sw.trigger("click");
    await flushPromises();

    const calls = (api.updateSearchSettings as ReturnType<typeof vi.fn>).mock.calls;
    const body = calls[calls.length - 1]?.[0];
    expect(body).toEqual({ keyless_fallback: false });
    expect(visiblePanel(w).find(".keyless-switch").attributes("aria-checked")).toBe("false");
    expect(visiblePanel(w).text()).toContain("免密钥");
    w.unmount();
  });

  it("保存失败：错误用错误语义显示", async () => {
    const { api } = await import("../../services/api");
    (api.updateSearchSettings as ReturnType<typeof vi.fn>).mockRejectedValueOnce(
      new Error("PUT /api/settings/search -> 500: boom"),
    );
    const w = await mountSettings();
    await openTab(w, "模型与联网");
    const saveBtn = visiblePanel(w).findAll(".qio-btn").find((b) => b.text().includes("保存搜索配置"));
    await saveBtn!.trigger("click");
    await flushPromises();
    const msg = visiblePanel(w).find(".msg");
    expect(msg.classes()).toContain("err");
    expect(msg.text()).toContain("保存搜索配置失败");
    w.unmount();
  });
});

describe("SettingsView 工具与高级", () => {
  it("工具与权限：渲染电脑操控配置", async () => {
    const { api } = await import("../../services/api");
    (api.getComputerSettings as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      root_dir: "C:/work",
      permission_mode: "accept-edits",
    });
    const w = await mountSettings();
    await openTab(w, "工具与权限");
    const panel = visiblePanel(w);
    expect(panel.text()).toContain("电脑操控");
    expect(panel.text()).toContain("工作区根目录");
    w.unmount();
  });

  it("开发者模式默认关闭，可在高级分区打开并持久化", async () => {
    const w = await mountSettings();
    const ui = useUiStore();
    expect(ui.developerMode).toBe(false);
    await openTab(w, "高级");
    const sw = visiblePanel(w).find(".qio-switch");
    await sw.trigger("click");
    await nextTick();
    expect(ui.developerMode).toBe(true);
    expect(localStorage.getItem("qio-developer-mode")).toBe("1");
    w.unmount();
  });

  it("对话深度在「对话与记忆」分区并拉取当前配置", async () => {
    const { api } = await import("../../services/api");
    (api.getLoopSettings as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      max_iterations: 200,
      output_token_budget: 90000,
    });
    const w = await mountSettings();
    await openTab(w, "对话与记忆");
    const panel = visiblePanel(w);
    expect(panel.text()).toContain("对话深度");
    expect(panel.text()).toContain("单轮迭代上限");
    w.unmount();
  });
});
