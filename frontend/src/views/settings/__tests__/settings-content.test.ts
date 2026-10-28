/**
 * 四类内容（常规 / 记忆与整理 / 工具与权限 / 关于与更新）的覆盖与关键交互：
 * 迁移清单里的每一项都必须有位置；只读区只显示已保存的值；危险操作走 QConfirm。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { nextTick } from "vue";
import { resetFloatPositions } from "../../../composables/floatingState";
import {
  chooseSelectLabel,
  flushFrames,
  mountSettings,
  openCategory,
  sectionById,
  setViewportWidth,
  visibleSection,
} from "./settingsTestUtils";

vi.mock("../../../services/api", async () => {
  const { settingsApiModule } = await import("./settingsApiMock");
  return settingsApiModule();
});

// 「模型与服务」是真实面板（不是占位）：把它的数据源换成确定性替身，
// 否则每个设置页用例都要跟随网络失败的时序。
vi.mock("../../../services/modelCatalog", async () => {
  const { modelCatalogModule } = await import("./settingsApiMock");
  return modelCatalogModule();
});

beforeEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
  resetFloatPositions();
  document.body.innerHTML = "";
  document.documentElement.removeAttribute("data-theme");
  document.documentElement.removeAttribute("data-motion");
  document.documentElement.removeAttribute("data-scrollbar");
  setViewportWidth(1280);
});

const A_OWNED_KEYS = [
  "regular.theme",
  "regular.motion",
  "regular.scrollbar",
  "regular.speed",
  "regular.onboarding",
  "regular.window.planet-hide",
  "regular.window.settings-hide",
  "regular.window.reset-layout",
  "regular.window.close-behavior",
  "regular.advanced.diagnostics",
  "models.search.status",
  "models.search.keyless",
  "models.search.bocha",
  "models.search.advanced.searxng",
  "models.search.advanced.top-k",
  "models.search.advanced.max-fetch-chars",
  "memory.fragment.tier",
  "memory.fragment.max-tokens",
  "memory.maintenance.enabled",
  "memory.maintenance.interval",
  "memory.maintenance.run",
  "memory.advanced.max-iterations",
  "memory.advanced.output-budget",
  "tools.permission",
  "tools.workspace",
  "tools.records.record-outputs",
  "tools.records.output-retention",
  "tools.records.record-retention",
  "tools.effective",
  "about.version",
  "about.update",
  "about.license",
  "about.third-party",
];

describe("迁移清单覆盖：每一项都有稳定 key 与位置", () => {
  it("A 拥有的所有设置项都渲染出 data-settings-key", async () => {
    const w = await mountSettings();
    const missing = A_OWNED_KEYS.filter(
      (key) => w.findAll(`[data-settings-key="${key}"]`).length === 0,
    );
    expect(missing).toEqual([]);
    w.unmount();
  });

  it("常规：主题/动画/滚动指示条/输出速度/引导/两个窗口开关/还原布局/关闭行为/诊断", async () => {
    const w = await mountSettings();
    const regular = sectionById(w, "regular");
    expect(regular.findAll(".set-seg__opt").map((option) => option.text())).toEqual(
      expect.arrayContaining(["系统", "暗紫晶", "净白", "跟随系统", "标准", "减少动画"]),
    );
    expect(regular.findAll('button[role="switch"]').length).toBeGreaterThanOrEqual(3);
    expect(regular.text()).toMatch(/继续设置|重新运行设置助手/);
    expect(regular.text()).toContain("还原默认布局");
    for (const label of ["每次询问", "最小化到托盘", "直接退出"]) {
      expect(regular.text()).toContain(label);
    }
    // 诊断开关在「高级选项」里，默认收起
    expect(regular.find('[data-settings-fold-toggle="regular.advanced"]').attributes("aria-expanded")).toBe(
      "false",
    );
    expect(
      regular.find('[data-settings-key="regular.advanced.diagnostics"]').exists(),
    ).toBe(true);
    w.unmount();
  });

  it("记忆与整理：记忆分段按组保存、后台整理即时、立即整理走 QConfirm", async () => {
    const w = await mountSettings();
    await openCategory(w, "memory");
    const memory = sectionById(w, "memory");
    expect(memory.find('[data-settings-group="memory.fragment"]').exists()).toBe(true);
    expect(memory.text()).toContain("根据讨论的进展分段");
    expect(memory.text()).toContain("到点会分段");
    expect(memory.text()).toContain("不等于上一段的任务已经完成");

    // 立即整理是危险/高影响操作：先确认
    const run = memory.findAll(".qio-btn").find((button) => button.text() === "立即整理");
    expect(run).toBeTruthy();
    await run!.trigger("click");
    await nextTick();
    const dialog = w.find('[role="dialog"]');
    expect(dialog.exists()).toBe(true);
    expect(dialog.text()).toContain("立即整理记忆与知识？");
    const { api } = await import("../../../services/api");
    expect(api.runMaintenance).not.toHaveBeenCalled();
    await dialog.find('[data-qio-confirm-action="confirm"]').trigger("click");
    await flushFrames();
    expect(api.runMaintenance).toHaveBeenCalledTimes(1);
    w.unmount();
  });

  it("工具与权限：权限模式复用既有组件、工作区、两个保留天数、只读生效参数", async () => {
    const w = await mountSettings();
    await openCategory(w, "tools");
    const tools = sectionById(w, "tools");
    expect(tools.find('[data-im="permission-mode-settings"]').exists()).toBe(true);
    expect(tools.text()).toContain("工作区根目录");
    expect(tools.text()).toContain("输出保留天数");
    expect(tools.text()).toContain("记录保留天数");
    // 只读区：真实生效值（来自其它分区的「已保存」值）
    const raw = tools.find('[data-settings-key="tools.effective"]');
    expect(raw.text()).toContain("4096");
    expect(raw.text()).toContain("128");
    expect(raw.text()).toContain("51200");
    expect(raw.text()).toContain("尚未读取");
    w.unmount();
  });

  it("只读生效参数不显示草稿：改了记忆分段但没保存，工具分区仍是旧值", async () => {
    const w = await mountSettings();
    await openCategory(w, "memory");
    const memory = sectionById(w, "memory");
    await memory.find('input[aria-label="单段长度目标"]').setValue("8000");
    await nextTick();

    await openCategory(w, "tools");
    const raw = sectionById(w, "tools").find('[data-settings-key="tools.effective"]');
    expect(raw.text()).toContain("4096");
    expect(raw.text()).not.toContain("8000");
    w.unmount();
  });

  it("关于与更新：版本、更新卡片、许可全文与第三方声明（离线内联）", async () => {
    const w = await mountSettings();
    await openCategory(w, "about");
    const about = sectionById(w, "about");
    expect(about.text()).toContain("QIO");
    expect(about.text()).toContain("BYOK");
    expect(about.find('[data-settings-key="about.update"]').exists()).toBe(true);
    // 许可与第三方声明是仓库根目录原文，随构建内联（不联网、不看后端）
    const license = about.get('[data-testid="qio-license"]');
    expect(license.text()).toContain("Permission is hereby granted");
    const notices = about.get('[data-testid="third-party-notices"]').text();
    expect(notices).toContain("QIO 第三方开源组件声明");
    expect(notices.length).toBeGreaterThan(10000);
    w.unmount();
  });
});

describe("常规分区的即时保存语义", () => {
  it("主题：三档，选择 system 存偏好而不是解析结果", async () => {
    const { getTheme, getThemePreference, setTheme } = await import("../../../utils/theme");
    // 先固定成暗色：否则默认偏好本来就是 system，点「系统」不会发生切换、也就不会写偏好
    setTheme("dark");
    const w = await mountSettings();
    const regular = sectionById(w, "regular");
    await chooseSelectLabel(regular, "系统");
    expect(getThemePreference()).toBe("system");
    expect(localStorage.getItem("qio-theme")).toBe("system");
    await chooseSelectLabel(regular, "净白");
    expect(getThemePreference()).toBe("light");
    expect(document.documentElement.dataset.theme).toBe("light");
    await chooseSelectLabel(regular, "暗紫晶");
    expect(getTheme()).toBe("dark");
    w.unmount();
  });

  it("动画：选择「减少动画」立即落到 html[data-motion]；滚动指示条落到 html[data-scrollbar]", async () => {
    const w = await mountSettings();
    const regular = sectionById(w, "regular");
    await chooseSelectLabel(regular, "减少动画");
    expect(document.documentElement.getAttribute("data-motion")).toBe("reduced");
    await chooseSelectLabel(regular, "始终显示");
    expect(document.documentElement.getAttribute("data-scrollbar")).toBe("always");
    expect(localStorage.getItem("qio-scrollbar")).toBe("always");
    expect(regular.text()).toContain("不会让板面出现滚动条");
    expect(regular.text()).toContain("不开放对话横向滚动");
    w.unmount();
  });

  it("窗口行为：两个贴靠隐藏开关 + 还原默认布局", async () => {
    const { floatingState, resetFloatPositions: reset } = await import(
      "../../../composables/floatingState"
    );
    const w = await mountSettings();
    const regular = sectionById(w, "regular");
    const planet = regular.find('button[aria-label="星球入口贴靠后自动隐藏"]');
    await planet.trigger("click");
    expect(floatingState["planet-dock"].hideEnabled).toBe(true);
    await regular.find('button[aria-label="设置入口贴靠后自动隐藏"]').trigger("click");
    expect(floatingState["settings-float"].hideEnabled).toBe(true);

    const btn = regular.findAll(".qio-btn").find((item) => item.text() === "还原默认布局");
    await btn!.trigger("click");
    expect(floatingState["planet-dock"].hideEnabled).toBe(false);
    expect(floatingState["settings-float"].hideEnabled).toBe(false);
    reset();
    w.unmount();
  });

  it("关闭主窗口行为：三档与契约一致，保存到后端设置", async () => {
    const { api } = await import("../../../services/api");
    const w = await mountSettings();
    const regular = sectionById(w, "regular");
    const text = regular.text();
    for (const label of ["每次询问", "最小化到托盘", "直接退出"]) expect(text).toContain(label);
    expect(text).toContain("继续运行");
    expect(text).toContain("有界收尾");
    await chooseSelectLabel(regular, "最小化到托盘");
    await flushFrames();
    expect(api.updateCloseBehavior).toHaveBeenCalledWith("tray");
    expect(regular.find(".set-notice").text()).toContain("已保存");
    w.unmount();
  });

  it("诊断（开发者模式）在常规的高级选项里，开启后持久化", async () => {
    const { useUiStore } = await import("../../../stores/ui");
    const w = await mountSettings();
    const ui = useUiStore();
    expect(ui.developerMode).toBe(false);
    const regular = sectionById(w, "regular");
    await regular.find('[data-settings-fold-toggle="regular.advanced"]').trigger("click");
    await nextTick();
    const sw = regular
      .find('[data-settings-key="regular.advanced.diagnostics"]')
      .find('button[role="switch"]');
    await sw.trigger("click");
    await nextTick();
    expect(ui.developerMode).toBe(true);
    expect(localStorage.getItem("qio-developer-mode")).toBe("1");
    w.unmount();
  });
});

describe("模型与服务容器", () => {
  it("主体是 B 的模型面板，搜索配置在第二块", async () => {
    const w = await mountSettings();
    await openCategory(w, "models");
    const models = sectionById(w, "models");
    // 集成后主体是 B 的真实面板（占位文件已被整体替换）：按稳定锚点断言，
    // 不按标题文案猜。
    expect(models.find('[data-testid="models-panel"]').exists()).toBe(true);
    expect(models.find('[data-settings-key="models.search"]').exists()).toBe(true);
    expect(visibleSection(w).dataset.settingsSection).toBe("models");
    w.unmount();
  });

  it("联网搜索状态分「未配置 / 已配置（未检查）」，不把已配置说成检查通过", async () => {
    const { api } = await import("../../../services/api");
    (api.getSearchSettings as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      searxng_url: "https://searx.example",
      bocha_has_key: true,
      keyless_fallback: true,
      top_k_default: 5,
      max_fetch_chars: 15000,
    });
    const w = await mountSettings();
    await openCategory(w, "models");
    const models = sectionById(w, "models");
    expect(models.find('[data-search-channel="bocha"]').text()).toContain("已配置（未检查）");
    expect(models.find('[data-search-channel="searxng"]').text()).toContain("已填写地址（未检查）");
    expect(models.find('[data-search-channel="keyless"]').text()).toContain("开启（未检查）");
    expect(models.text()).toContain("「已配置」不代表「检查通过」");
    w.unmount();
  });

  it("连接检查失败只说明检查失败，不冒充通道不可用", async () => {
    const { api } = await import("../../../services/api");
    (api.checkSearch as ReturnType<typeof vi.fn>).mockRejectedValueOnce(new Error("network down"));
    const w = await mountSettings();
    await openCategory(w, "models");
    const models = sectionById(w, "models");
    await models.find('[data-testid="settings-search-check"]').trigger("click");
    await flushFrames();
    expect(models.find(".set-notice.err").text()).toContain("不代表通道不可用");
    w.unmount();
  });
});
