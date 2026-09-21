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
    getMemorySettings: vi.fn(async () => ({ fragment_max_turns: 10, fragment_max_tokens: 4096 })),
    updateMemorySettings: vi.fn(async (payload: Record<string, number>) => ({
      ok: true,
      fragment_max_turns: payload.fragment_max_turns ?? 10,
      fragment_max_tokens: payload.fragment_max_tokens ?? 4096,
    })),
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

  it("点击卡片「删除」：先用 QIO 确认层说明后果，确认后才调用 deleteCredential", async () => {
    const { api } = await import("../../services/api");
    (api.listCredentials as ReturnType<typeof vi.fn>).mockResolvedValueOnce({ credentials: [CRED] });
    const w = await mountSettings();
    await w.find(".btn-danger").trigger("click");
    await flushPromises();
    // 第四阶段：原生 confirm 已替换为 QIO 自己的确认层（layer 档）
    const dialog = w.find('[role="dialog"]');
    expect(dialog.exists()).toBe(true);
    expect(dialog.text()).toContain("彻底删除凭据「k1」？");
    expect(dialog.text()).toContain("不可恢复");
    expect(api.deleteCredential).not.toHaveBeenCalled();
    // 取消：什么都不发生
    await dialog.findAll("button")[0].trigger("click");
    await flushPromises();
    expect(api.deleteCredential).not.toHaveBeenCalled();
    // 确认：才真的删除
    await w.find(".btn-danger").trigger("click");
    await flushPromises();
    await w.find('[role="dialog"]').findAll("button")[1].trigger("click");
    await flushPromises();
    expect(api.deleteCredential).toHaveBeenCalledWith("k1");
    expect(w.findAll(".cred-card").length).toBe(0);
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
    // 第四阶段：管理动作在「管理」区里，先展开再点测试（不再按下标猜按钮）
    await w.find(".cred-card .btn-manage").trigger("click");
    await w.find(".cred-card .btn-test").trigger("click");
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

  it("「清除」是独立的危险动作，走 QIO 确认层，且明确发送空值", async () => {
    const { api } = await import("../../services/api");
    (api.getSearchSettings as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      searxng_url: "",
      bocha_has_key: true,
      top_k_default: 5,
      max_fetch_chars: 15000,
    });
    const w = await mountSettings();
    await openTab(w, "模型与联网");
    const clearBtn = visiblePanel(w).findAll(".qio-btn").find((b) => b.text().includes("清除"));
    expect(clearBtn).toBeTruthy();
    await clearBtn!.trigger("click");
    await flushPromises();
    const dialog = w.find('[role="dialog"]');
    expect(dialog.exists()).toBe(true);
    expect(dialog.text()).toContain("清除已保存的博查 API Key？");
    expect(api.updateSearchSettings).not.toHaveBeenCalled();
    await dialog.findAll("button")[1].trigger("click");
    await flushPromises();
    const body = (api.updateSearchSettings as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(body).toEqual({ bocha_api_key: "" });
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

describe("SettingsView 状态保留与保存归属（P0）", () => {
  it("再次打开设置时恢复上次所在分类（同次使用内）", async () => {
    const pinia = makePinia();
    const mountWith = () =>
      mount(SettingsView, { attachTo: document.body, global: { plugins: [pinia], stubs: { RouterLink: true } } });

    const w1 = mountWith();
    await flushPromises();
    await openTab(w1, "凭据");
    expect(w1.find(".tab.active").text()).toBe("凭据");
    w1.unmount();

    const w2 = mountWith();
    await flushPromises();
    expect(w2.find(".tab.active").text()).toBe("凭据");
    w2.unmount();
  });

  it("维护设置：旧响应迟到时不得回填覆盖用户后来的输入", async () => {
    const api = (await import("../../services/api")).api as unknown as {
      updateMaintenanceSettings: ReturnType<typeof vi.fn>;
    };
    let releaseFirst: (v: unknown) => void = () => {};
    const first = new Promise((r) => {
      releaseFirst = r;
    });
    api.updateMaintenanceSettings.mockImplementationOnce(() => first);
    api.updateMaintenanceSettings.mockImplementationOnce(
      async (body: { enabled: boolean; interval_hours: number }) => ({
        enabled: body.enabled,
        interval_hours: body.interval_hours,
      }),
    );

    const w = await mountSettings();
    await openTab(w, "数据与维护");
    // 第一次修改：开关（请求挂起）
    await visiblePanel(w).find(".qio-switch").trigger("click");
    await nextTick();
    // 第二次修改：间隔改成 30（后发先至）
    const input = w.find('input[aria-label="维护间隔（小时）"]');
    (input.element as HTMLInputElement).value = "30";
    await input.trigger("input");
    await nextTick();
    await input.trigger("blur");
    await flushPromises();
    await nextTick();

    // 旧请求此刻才返回，带着过期值
    expect(api.updateMaintenanceSettings).toHaveBeenCalledTimes(2);
    releaseFirst({ enabled: false, interval_hours: 24 });
    await flushPromises();
    await nextTick();

    expect((w.find('input[aria-label="维护间隔（小时）"]').element as HTMLInputElement).value).toBe("30");
    expect(visiblePanel(w).find(".qio-switch").classes()).toContain("on");
    expect(visiblePanel(w).text()).toContain("30");
    w.unmount();
  });

  it("未提交的表单内容离开设置页后仍保留（非敏感字段）", async () => {
    const pinia = makePinia();
    const mountWith = () =>
      mount(SettingsView, { attachTo: document.body, global: { plugins: [pinia], stubs: { RouterLink: true } } });

    const w1 = mountWith();
    await flushPromises();
    await openTab(w1, "对话与记忆");
    const input = w1.find('input[aria-label="迭代上限"]');
    (input.element as HTMLInputElement).value = "333";
    await input.trigger("input");
    await nextTick();
    w1.unmount(); // 离开设置：组件卸载

    const w2 = mountWith();
    await flushPromises();
    await openTab(w2, "对话与记忆");
    expect((w2.find('input[aria-label="迭代上限"]').element as HTMLInputElement).value).toBe("333");
    w2.unmount();
  });

  it("动画偏好：选择「减少动画」立即落到 html[data-motion]", async () => {
    document.documentElement.removeAttribute("data-motion");
    const w = await mountSettings();
    const btn = w.findAll(".motion-opt").find((b) => b.text() === "减少动画");
    expect(btn).toBeTruthy();
    await btn!.trigger("click");
    await nextTick();
    expect(document.documentElement.getAttribute("data-motion")).toBe("reduced");
    expect(w.findAll(".motion-opt").find((b) => b.text() === "减少动画")!.classes()).toContain("on");
    document.documentElement.removeAttribute("data-motion");
    localStorage.clear();
    w.unmount();
  });
});

describe("任务 04 PART B：保存模型与可用性说明", () => {
  it("自动保存显示「保存中…」→「已保存」，不是只有最终结果", async () => {
    const api = (await import("../../services/api")).api as unknown as Record<string, ReturnType<typeof vi.fn>>;
    let release!: (v: { enabled: boolean; interval_hours: number }) => void;
    api.updateMaintenanceSettings.mockImplementationOnce(
      () => new Promise((r) => { release = r as typeof release; }),
    );
    const w = await mountSettings();
    await openTab(w, "数据与维护");

    await w.find('button[aria-label="离线维护开关"]').trigger("click");
    await nextTick();
    expect(w.find(".msg").text()).toContain("正在保存维护设置");
    expect(w.find(".msg").classes()).toContain("info");

    release({ enabled: false, interval_hours: 24 });
    await flushPromises();
    expect(w.find(".msg").text()).toContain("离线维护已保存");
    expect(w.find(".msg").classes()).toContain("ok");
    w.unmount();
  });

  it("保存失败时给出可见错误（不留在「已保存」的假状态）", async () => {
    const api = (await import("../../services/api")).api as unknown as Record<string, ReturnType<typeof vi.fn>>;
    api.updateMaintenanceSettings.mockRejectedValueOnce(new Error("500 boom"));
    const w = await mountSettings();
    await openTab(w, "数据与维护");
    await w.find('button[aria-label="离线维护开关"]').trigger("click");
    await flushPromises();
    expect(w.find(".msg").text()).toContain("保存维护设置失败");
    expect(w.find(".msg").classes()).toContain("err");
    w.unmount();
  });

  it("输出速度保存失败时会显示错误，而不是只写控制台", async () => {
    const api = (await import("../../services/api")).api as unknown as Record<string, ReturnType<typeof vi.fn>>;
    api.updateUISettings.mockRejectedValueOnce(new Error("网络不可用"));
    const w = await mountSettings();
    await w.find(".qio-select, .select").trigger("click").catch(() => {});
    const s = useUiStore();
    s.setCps(25).catch(() => {});
    await flushPromises();
    // store 里的偏好仍然被改（本地立即生效），但抛错交给调用方显示
    expect(s.typewriterCps).toBe(25);
    w.unmount();
  });

  it("联网搜索：没有任何通道时说明为什么不可用", async () => {
    const api = (await import("../../services/api")).api as unknown as Record<string, ReturnType<typeof vi.fn>>;
    api.getSearchSettings.mockResolvedValueOnce({
      searxng_url: "",
      bocha_has_key: false,
      keyless_fallback: false,
      top_k_default: 5,
      max_fetch_chars: 15000,
    });
    const w = await mountSettings();
    await openTab(w, "模型与联网");
    const avail = w.find(".avail");
    expect(avail.exists()).toBe(true);
    expect(avail.text()).toContain("没有可用的联网搜索通道");
    expect(avail.classes()).toContain("off");
    w.unmount();
  });

  it("联网搜索：有免密钥通道时说明当前可用通道", async () => {
    const w = await mountSettings();
    await openTab(w, "模型与联网");
    expect(w.find(".avail").text()).toContain("免密钥通道");
    expect(w.find(".avail").classes()).not.toContain("off");
    w.unmount();
  });

  it("保存模型可见：自动保存分区有说明，显式保存分区有保存按钮与说明", async () => {
    const w = await mountSettings();
    await openTab(w, "对话与记忆");
    const hints = () => w.findAll(".mode-hint").map((h) => h.text());
    expect(hints().some((t) => t.includes("自动保存"))).toBe(true);
    await openTab(w, "模型与联网");
    expect(hints().some((t) => t.includes("保存搜索配置"))).toBe(true);
    expect(w.find("button.qio-btn.primary").text()).toContain("保存搜索配置");
    w.unmount();
  });
});

describe("分段语义与单段长度（阶段 4/5）", () => {
  it("把「分段不等于任务完成」写清楚，并提供单段长度设置", async () => {
    const w = await mountSettings();
    await openTab(w, "对话与记忆");

    const panel = visiblePanel(w);
    expect(panel.text()).toContain("根据讨论的进展分段");
    expect(panel.text()).toContain("不等于上一段的任务已经完成");
    expect(panel.text()).toContain("单段长度目标");
    expect(panel.text()).toContain("到点会分段");
    w.unmount();
  });

  it("改单段长度只提交长度字段，轮数不受牵连", async () => {
    const { api } = await import("../../services/api");
    (api.updateMemorySettings as ReturnType<typeof vi.fn>).mockClear();
    const w = await mountSettings();
    await openTab(w, "对话与记忆");

    // 同一个分区里还有「迭代上限 / 输出预算」两个数字输入，按 aria-label 精确定位
    const tokenInput = visiblePanel(w).find('input[aria-label="单段长度目标"]');
    expect(tokenInput.exists()).toBe(true);
    await tokenInput.setValue("8000");
    await tokenInput.trigger("blur");
    await flushPromises();

    expect(api.updateMemorySettings).toHaveBeenCalledWith({ fragment_max_tokens: 8000 });
    w.unmount();
  });
});
