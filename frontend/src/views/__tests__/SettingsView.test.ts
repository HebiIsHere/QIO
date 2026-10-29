/**
 * 设置页（改版后）：五类信息架构、分区反馈不串台、自绘控件与关键保存语义。
 * 更细的按组保存/迟到响应在 views/settings/__tests__ 下；本文件保留原
 * SettingsView.test.ts 里仍然有效的用户可见行为。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { nextTick } from "vue";
import { resetFloatPositions } from "../../composables/floatingState";
import {
  chooseSelectLabel,
  flushFrames,
  makePinia,
  mountSettings,
  openCategory,
  sectionById,
  setViewportWidth,
  visibleSection,
} from "../settings/__tests__/settingsTestUtils";

vi.mock("../../services/api", async () => {
  const { settingsApiModule } = await import("../settings/__tests__/settingsApiMock");
  return settingsApiModule();
});

// 「模型与服务」是真实面板（不是占位）：数据源换成确定性替身。
vi.mock("../../services/modelCatalog", async () => {
  const { modelCatalogModule } = await import("../settings/__tests__/settingsApiMock");
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

describe("SettingsView 信息架构（五类）", () => {
  it("五个一级分区，默认停在「常规」，不重复渲染设置页标题栏", async () => {
    const w = await mountSettings();
    expect(w.findAll(".set-nav__tab").map((tab) => tab.text())).toEqual([
      "常规",
      "模型与服务",
      "记忆与整理",
      "工具与权限",
      "关于与更新",
    ]);
    expect(w.find(".set-nav__tab.active").text()).toBe("常规");
    expect(w.find("h1").exists()).toBe(false);
    expect(w.find('[data-testid="settings-back"]').exists()).toBe(true);
    expect(visibleSection(w).dataset.settingsSection).toBe("regular");
    w.unmount();
  });

  it("切到「模型与服务」：先是 B 的模型面板容器，随后是联网搜索配置", async () => {
    const w = await mountSettings();
    await openCategory(w, "models");
    const models = sectionById(w, "models");
    expect(models.find('[data-testid="models-panel"]').exists()).toBe(true);
    expect(models.text()).toContain("联网搜索");
    expect(models.find('[data-settings-group="models.search"]').exists()).toBe(true);
    w.unmount();
  });
});

describe("分区反馈不串台", () => {
  it("记忆保存失败：错误只出现在记忆区，切到别的分区看不到", async () => {
    const { api } = await import("../../services/api");
    (api.updateMemorySettings as ReturnType<typeof vi.fn>).mockRejectedValueOnce(
      new Error("PUT /api/settings/memory -> 500: boom"),
    );
    const w = await mountSettings();
    await openCategory(w, "memory");
    const memory = sectionById(w, "memory");
    await memory.find('input[aria-label="单段长度目标"]').setValue("8000");
    await nextTick();
    await memory
      .find('[data-settings-group="memory.fragment"] [data-settings-action="save"]')
      .trigger("click");
    await flushFrames();

    const notice = memory.find(".set-notice.err");
    expect(notice.exists()).toBe(true);
    expect(notice.text()).toContain("记忆");
    expect(notice.text()).toContain("失败");
    expect(notice.classes()).not.toContain("ok");

    await openCategory(w, "models");
    expect(visibleSection(w).textContent).not.toContain("保存记忆分段失败");
    w.unmount();
  });

  it("输出速度保存失败：控件回滚到真实值并显示错误", async () => {
    const { api } = await import("../../services/api");
    const { useUiStore } = await import("../../stores/ui");
    (api.updateUISettings as ReturnType<typeof vi.fn>).mockRejectedValueOnce(
      new Error("网络不可用"),
    );
    const w = await mountSettings();
    const ui = useUiStore();
    expect(ui.typewriterCps).toBe(50);
    const regular = sectionById(w, "regular");
    await regular.find(".qio-select").trigger("click");
    await nextTick();
    const option = regular.findAll(".qio-select-menu .opt").find((item) => item.text().includes("慢"));
    await option!.trigger("click");
    await flushFrames();

    expect(ui.typewriterCps).toBe(50);
    expect(regular.find(".set-notice.err").text()).toContain("保存输出速度失败");
    w.unmount();
  });
});

describe("联网搜索：免密钥通道不是总开关", () => {
  it("切换免密钥开关只提交 keyless_fallback，不连带博查 Key / SearXNG", async () => {
    const { api } = await import("../../services/api");
    const w = await mountSettings();
    await openCategory(w, "models");
    const models = sectionById(w, "models");
    const sw = models.find('button[aria-label="免密钥联网搜索"]');
    expect(sw.attributes("aria-checked")).toBe("true");

    await sw.trigger("click");
    await flushFrames();

    const calls = (api.updateSearchSettings as ReturnType<typeof vi.fn>).mock.calls;
    expect(calls[calls.length - 1]?.[0]).toEqual({ keyless_fallback: false });
    expect(models.find('button[aria-label="免密钥联网搜索"]').attributes("aria-checked")).toBe(
      "false",
    );
    expect(models.text()).toContain("博查 Key 与 SearXNG 地址都保留");
    w.unmount();
  });

  it("没有任何通道时，状态说明缺什么（不把控件摆成禁用就算）", async () => {
    const { api } = await import("../../services/api");
    (api.getSearchSettings as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      searxng_url: "",
      bocha_has_key: false,
      keyless_fallback: false,
      top_k_default: 5,
      max_fetch_chars: 15000,
    });
    const w = await mountSettings();
    await openCategory(w, "models");
    const models = sectionById(w, "models");
    expect(models.find('[data-search-channel="keyless"]').text()).toContain("已关闭");
    expect(models.find('[data-search-channel="bocha"]').text()).toContain("未配置");
    expect(models.find('[data-search-channel="searxng"]').text()).toContain("未配置");
    expect(models.text()).toContain("「已配置」不代表「检查通过」");
    w.unmount();
  });

  it("博查「清除」是独立危险动作：走 QIO 确认层并明确发送空值", async () => {
    const { api } = await import("../../services/api");
    (api.getSearchSettings as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      searxng_url: "",
      bocha_has_key: true,
      keyless_fallback: true,
      top_k_default: 5,
      max_fetch_chars: 15000,
    });
    const w = await mountSettings();
    await openCategory(w, "models");
    const models = sectionById(w, "models");
    const clear = models.find('[data-testid="settings-search-bocha-clear"]');
    expect(clear.exists()).toBe(true);
    await clear.trigger("click");
    await nextTick();
    const dialog = w.find('[role="dialog"]');
    expect(dialog.exists()).toBe(true);
    expect(dialog.text()).toContain("清除已保存的博查 API Key？");
    expect(api.updateSearchSettings).not.toHaveBeenCalled();
    await dialog.find('[data-qio-confirm-action="confirm"]').trigger("click");
    await flushFrames();
    const body = (api.updateSearchSettings as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(body).toEqual({ bocha_api_key: "" });
    w.unmount();
  });
});

describe("工具与权限 / 关于", () => {
  it("工作区根目录：失焦保存，只提交 root_dir", async () => {
    const { api } = await import("../../services/api");
    const w = await mountSettings();
    await openCategory(w, "tools");
    const tools = sectionById(w, "tools");
    const input = tools.find('input[aria-label="工作区根目录"]');
    await input.setValue("C:/work");
    await input.trigger("change");
    await flushFrames();
    expect(api.updateComputerSettings).toHaveBeenCalledWith({ root_dir: "C:/work" });
    w.unmount();
  });

  it("工具记录开关：只提交 record_outputs", async () => {
    const { api } = await import("../../services/api");
    const w = await mountSettings();
    await openCategory(w, "tools");
    const tools = sectionById(w, "tools");
    await tools.find('button[aria-label="保存工具输出全文开关"]').trigger("click");
    await flushFrames();
    expect(api.updateToolHistorySettings).toHaveBeenCalledWith({ record_outputs: false });
    w.unmount();
  });

  it("关于：许可证与第三方声明是随包内联原文（不联网、不看后端）", async () => {
    const w = await mountSettings();
    await openCategory(w, "about");
    const about = sectionById(w, "about");
    expect(about.get('[data-testid="qio-license"]').text()).toContain("Permission is hereby granted");
    expect(about.get('[data-testid="third-party-notices"]').text()).toContain(
      "QIO 第三方开源组件声明",
    );
    w.unmount();
  });
});

describe("状态保留（同一次使用内）", () => {
  it("再次打开设置时恢复上次所在分类", async () => {
    const pinia = makePinia();
    const w1 = await mountSettings(pinia);
    await openCategory(w1, "tools");
    w1.unmount();
    document.body.innerHTML = "";

    const w2 = await mountSettings(pinia);
    expect(w2.find('[data-testid="settings-page"]').attributes("data-settings-page")).toBe("tools");
    w2.unmount();
  });

  it("动画偏好：选择「减少动画」立即落到 html[data-motion]", async () => {
    const w = await mountSettings();
    await chooseSelectLabel(sectionById(w, "regular"), "减少动画");
    expect(document.documentElement.getAttribute("data-motion")).toBe("reduced");
    w.unmount();
  });
});
