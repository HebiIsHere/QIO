/**
 * 设置页外壳：五类导航、宽窄切换（同一份内容 DOM）、状态保留、离开即丢弃草稿。
 *
 * 只测结构与状态，不测具体控件语义（那些在 settings-sections / save-model 里）。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises } from "@vue/test-utils";
import { nextTick } from "vue";
import { SETTINGS_CATEGORIES } from "../types";
import { resetFloatPositions } from "../../../composables/floatingState";
import {
  makePinia,
  mountSettings,
  openCategory,
  sectionById,
  settingKeys,
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

describe("设置页五类导航", () => {
  it("导航只有五个分类，默认停在「常规」，且没有设置页自己的标题栏", async () => {
    const w = await mountSettings();
    expect(w.findAll(".set-nav__tab").map((tab) => tab.text())).toEqual(
      SETTINGS_CATEGORIES.map((category) => category.label),
    );
    expect(w.findAll(".set-nav__tab").map((tab) => tab.text())).toEqual([
      "常规",
      "模型与服务",
      "记忆与整理",
      "工具与权限",
      "关于与更新",
    ]);
    expect(w.find(".set-nav__tab.active").text()).toBe("常规");
    expect(w.find('[data-testid="settings-page"]').attributes("data-settings-page")).toBe("regular");
    // 不画自己的标题栏：没有 h1、没有旧的页头 / 「返回对话」链接行
    expect(w.find("h1").exists()).toBe(false);
    expect(w.find("header > h1").exists()).toBe(false);
    // 但仍有一个紧凑的返回入口（图标按钮，不是标题栏）
    const back = w.find('[data-testid="settings-back"]');
    expect(back.exists()).toBe(true);
    expect(back.attributes("aria-label")).toBe("返回对话");
    expect((visibleSection(w) as HTMLElement).dataset.settingsSection).toBe("regular");
    w.unmount();
  });

  it("切换分类只换可见面板，五个分类的分区都保持挂载", async () => {
    const w = await mountSettings();
    await openCategory(w, "tools");
    expect(w.find('[data-testid="settings-page"]').attributes("data-settings-page")).toBe("tools");
    expect((visibleSection(w) as HTMLElement).dataset.settingsSection).toBe("tools");
    // 五个分区都在 DOM 里（v-show），切分类不重建表单
    expect(w.findAll("[data-settings-section]").length).toBe(5);
    w.unmount();
  });
});

describe("宽窄切换：只换导航布局，内容 DOM 等价", () => {
  it("宽窗左右布局 / 窄窗顶部标签页，且 data-settings-key 集合与顺序完全一致", async () => {
    const w = await mountSettings();
    const page = w.find('[data-testid="settings-page"]');
    expect(page.attributes("data-nav-layout")).toBe("side");
    const panels = w.find('[data-testid="settings-panels"]').element as HTMLElement;
    const before = settingKeys(panels);

    setViewportWidth(720);
    await nextTick();
    await flushPromises();
    expect(page.attributes("data-nav-layout")).toBe("top");
    const after = settingKeys(panels);
    expect(after).toEqual(before);
    expect(after.length).toBeGreaterThan(30);

    setViewportWidth(1280);
    await nextTick();
    await flushPromises();
    expect(page.attributes("data-nav-layout")).toBe("side");
    expect(settingKeys(panels)).toEqual(before);
    w.unmount();
  });

  it("宽窄切换保留当前分类、展开状态与未保存草稿", async () => {
    const w = await mountSettings();
    await openCategory(w, "memory");
    const memory = sectionById(w, "memory");
    // 展开高级选项并改一个尚未提交的草稿（记忆分段是按组保存）
    await memory.find('[data-settings-fold-toggle="memory.advanced"]').trigger("click");
    await nextTick();
    const tokens = memory.find('input[aria-label="单段长度目标"]');
    await tokens.setValue("8000");
    await nextTick();

    setViewportWidth(700);
    await nextTick();
    await flushPromises();

    expect(w.find('[data-testid="settings-page"]').attributes("data-settings-page")).toBe("memory");
    expect(
      sectionById(w, "memory")
        .find('[data-settings-fold-toggle="memory.advanced"]')
        .attributes("aria-expanded"),
    ).toBe("true");
    expect(
      (sectionById(w, "memory").find('input[aria-label="单段长度目标"]').element as HTMLInputElement)
        .value,
    ).toBe("8000");
    w.unmount();
  });
});

describe("离开设置页", () => {
  it("未保存草稿直接丢弃（不自动保存、不弹询问、返回后不恢复）", async () => {
    const w1 = await mountSettings();
    await openCategory(w1, "memory");
    const memory = sectionById(w1, "memory");
    await memory.find('input[aria-label="单段长度目标"]').setValue("8000");
    await nextTick();
    // 离开设置页（整页路由卸载）
    w1.unmount();
    document.body.innerHTML = "";

    const w2 = await mountSettings();
    await openCategory(w2, "memory");
    expect(
      (sectionById(w2, "memory").find('input[aria-label="单段长度目标"]').element as HTMLInputElement)
        .value,
    ).toBe("4096");
    w2.unmount();
  });

  it("离开时不为草稿发额外写请求", async () => {
    const { api } = await import("../../../services/api");
    const updates = [
      api.updateMemorySettings,
      api.updateMaintenanceSettings,
      api.updateSearchSettings,
      api.updateComputerSettings,
      api.updateToolHistorySettings,
      api.updateLoopSettings,
      api.updateUISettings,
      api.updateCloseBehavior,
    ] as unknown as ReturnType<typeof vi.fn>[];

    const w = await mountSettings();
    await openCategory(w, "memory");
    const memory = sectionById(w, "memory");
    await memory.find('input[aria-label="单段长度目标"]').setValue("9000");
    await nextTick();
    expect(updates.every((fn) => fn.mock.calls.length === 0)).toBe(true);

    w.unmount();
    await flushPromises();
    expect(updates.every((fn) => fn.mock.calls.length === 0)).toBe(true);
    document.body.innerHTML = "";
  });

  it("记住上次所在分类（同一次使用内）", async () => {
    const pinia = makePinia();
    const w1 = await mountSettings(pinia);
    await openCategory(w1, "about");
    w1.unmount();
    document.body.innerHTML = "";

    const w2 = await mountSettings(pinia);
    expect(w2.find('[data-testid="settings-page"]').attributes("data-settings-page")).toBe("about");
    w2.unmount();
  });
});
