/**
 * 设置搜索：索引安全、分类归属、普通项 / 高级参数 / 折叠用途的定位、
 * 返回结果时保留关键词与结果、空结果自绘状态。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { nextTick } from "vue";
import { resetFloatPositions } from "../../../composables/floatingState";
import { SETTINGS_SEARCH_INDEX, searchSettings } from "../searchIndex";
import {
  flushFrames,
  mountSettings,
  openCategory,
  sectionById,
  setViewportWidth,
  settingKeys,
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
  setViewportWidth(1280);
});

async function openSearch(/* wrapper 由各用例传入 */ w: Awaited<ReturnType<typeof mountSettings>>) {
  await w.find('[data-testid="settings-search-entry"]').trigger("click");
  await nextTick();
}

async function typeQuery(
  w: Awaited<ReturnType<typeof mountSettings>>,
  query: string,
): Promise<void> {
  await w.find('[data-testid="settings-search-input"]').setValue(query);
  await nextTick();
}

describe("设置搜索索引", () => {
  it("索引只含设置名称/说明/用途名，不含任何 Key 明文或密钥字段值", () => {
    const text = JSON.stringify(SETTINGS_SEARCH_INDEX);
    expect(text).not.toMatch(/sk-[A-Za-z0-9]/);
    expect(text).not.toMatch(/Bearer\s+/);
    expect(text).not.toMatch(/bocha_api_key/);
    expect(text).not.toMatch(/api[_-]?key\s*[:=]/i);
    // 结果必须给出「名称 + 分类 + 说明」三件信息
    for (const entry of SETTINGS_SEARCH_INDEX) {
      expect(entry.title.length).toBeGreaterThan(0);
      expect(entry.description.length).toBeGreaterThan(0);
      expect(["regular", "models", "memory", "tools", "about"]).toContain(entry.category);
    }
  });

  it("用途名称在索引里（13 类，含三个后台用途）", () => {
    for (const usage of [
      "main-chat",
      "general-chat",
      "code",
      "subagent",
      "vision",
      "video",
      "audio",
      "research",
      "embedding",
      "memory-organize",
      "knowledge-distill",
      "entity-extract",
    ]) {
      const entry = SETTINGS_SEARCH_INDEX.find((item) => item.key === `models.usage.${usage}`);
      expect(entry, usage).toBeTruthy();
    }
    const background = SETTINGS_SEARCH_INDEX.find(
      (item) => item.key === "models.usage.memory-organize",
    );
    expect(background?.fold).toBe("models.usage.background");
  });

  it("搜索按标题优先并支持用途名", () => {
    expect(searchSettings("主题")[0]?.key).toBe("regular.theme");
    expect(searchSettings("记忆整理").some((entry) => entry.key === "models.usage.memory-organize")).toBe(
      true,
    );
    expect(searchSettings("返回条数")[0]?.key).toBe("models.search.advanced.top-k");
    expect(searchSettings("zzzz-没有")).toEqual([]);
  });
});

describe("搜索面板与定位", () => {
  it("入口在设置页内：结果显示名称 + 说明，分类只在分组标题出现一次（#66）", async () => {
    const w = await mountSettings();
    await openSearch(w);
    const panel = w.find('[data-testid="settings-search-panel"]');
    expect((panel.element as HTMLElement).style.display).not.toBe("none");

    await typeQuery(w, "主题");
    const hit = w.find('[data-search-hit="regular.theme"]');
    expect(hit.exists()).toBe(true);
    expect(hit.text()).toContain("主题");
    expect(hit.text()).toContain("暗紫晶");
    // 旧行为被本项否定：结果行曾经重复显示「常规」，与分组标题重复。
    expect(hit.text()).not.toContain("常规");
    expect(hit.find(".set-search__hit-cat").exists()).toBe(false);
    // 分类用分组标题区分（同分类的多条结果共用一个标题）
    expect(w.find(".set-search__group").text()).toBe("常规");
    // 搜索入口没有替代全局栏：页面里仍然没有 h1
    expect(w.find("h1").exists()).toBe(false);
    w.unmount();
  });

  it("每个分组只有一个分类标题，跨分类结果各自成组（#66）", async () => {
    const w = await mountSettings();
    await openSearch(w);
    // 「1」出现在多个分类的说明里（速度档位 / 条数区间 / 轮数区间），能跨分类成组
    await typeQuery(w, "1");
    const groups = w.findAll(".set-search__group").map((group) => group.text());
    expect(groups.length).toBeGreaterThan(1);
    expect(groups).toContain("模型与服务");
    expect(new Set(groups).size).toBe(groups.length);
    for (const hit of w.findAll("[data-search-hit]")) {
      const labels = ["常规", "模型与服务", "记忆与整理", "工具与权限", "关于与更新"];
      expect(labels.some((label) => hit.text().includes(label))).toBe(false);
    }
    w.unmount();
  });

  it("点普通项：进入对应分类、定位并短暂高亮", async () => {
    const w = await mountSettings();
    await openSearch(w);
    await typeQuery(w, "主题");
    await w.find('[data-search-hit="regular.theme"]').trigger("click");
    await flushFrames();

    expect(w.find('[data-testid="settings-page"]').attributes("data-settings-page")).toBe("regular");
    expect((w.find('[data-testid="settings-search-panel"]').element as HTMLElement).style.display).toBe(
      "none",
    );
    const row = sectionById(w, "regular").find('[data-settings-key="regular.theme"]');
    expect(row.classes()).toContain("is-revealed");
    w.unmount();
  });

  it("点高级参数：先展开高级选项，再定位高亮", async () => {
    const w = await mountSettings();
    await openSearch(w);
    await typeQuery(w, "返回条数");
    await w.find('[data-search-hit="models.search.advanced.top-k"]').trigger("click");
    await flushFrames();

    expect(w.find('[data-testid="settings-page"]').attributes("data-settings-page")).toBe("models");
    const models = sectionById(w, "models");
    expect(
      models.find('[data-settings-fold-toggle="models.search.advanced"]').attributes("aria-expanded"),
    ).toBe("true");
    expect(models.find('[data-settings-key="models.search.advanced.top-k"]').classes()).toContain(
      "is-revealed",
    );
    w.unmount();
  });

  it("点折叠用途：进入模型与服务，并请求展开「后台用途」（目标由 B 的面板提供时不假装定位成功）", async () => {
    const w = await mountSettings();
    await openSearch(w);
    await typeQuery(w, "记忆整理");
    const hit = w.find('[data-search-hit="models.usage.memory-organize"]');
    expect(hit.exists()).toBe(true);
    await hit.trigger("click");
    await flushFrames();

    expect(w.find('[data-testid="settings-page"]').attributes("data-settings-page")).toBe("models");
    // 当前分支里模型面板是占位文件，没有该行：不允许 throw，也不允许出现错误的「已定位」状态
    expect(w.find('[data-search-hit="models.usage.memory-organize"]').exists()).toBe(true);
    w.unmount();
  });

  it("返回结果时保留关键词与结果位置", async () => {
    const w = await mountSettings();
    await openSearch(w);
    await typeQuery(w, "权限");
    await w.find('[data-search-hit="tools.permission"]').trigger("click");
    await flushFrames();

    const back = w.find('[data-testid="settings-search-return"]');
    expect(back.exists()).toBe(true);
    await back.trigger("click");
    await flushFrames();

    expect(
      (w.find('[data-testid="settings-search-panel"]').element as HTMLElement).style.display,
    ).not.toBe("none");
    expect(
      (w.find('[data-testid="settings-search-input"]').element as HTMLInputElement).value,
    ).toBe("权限");
    expect(w.find('[data-search-hit="tools.permission"]').exists()).toBe(true);
    w.unmount();
  });

  it("空结果用自绘状态；Esc 关闭搜索；关键词清空后回到提示态", async () => {
    const w = await mountSettings();
    await openSearch(w);
    await typeQuery(w, "没有这个设置");
    expect(w.find('[data-testid="settings-search-empty"]').exists()).toBe(true);
    expect((w.find('[data-testid="settings-search-empty"]').element as HTMLElement).textContent).toContain(
      "没有找到",
    );

    // Esc 关闭搜索面板（键盘可达）
    await w.find('[data-testid="settings-search-input"]').trigger("keydown", { key: "Escape" });
    await flushFrames();
    expect(
      (w.find('[data-testid="settings-search-panel"]').element as HTMLElement).style.display,
    ).toBe("none");
    w.unmount();
  });

  it("Esc 关闭搜索后把焦点交还搜索入口（#67）", async () => {
    const w = await mountSettings();
    await openSearch(w);
    await typeQuery(w, "主题");
    await w.find('[data-testid="settings-search-input"]').trigger("keydown", { key: "Escape" });
    await flushFrames();

    expect(
      (w.find('[data-testid="settings-search-panel"]').element as HTMLElement).style.display,
    ).toBe("none");
    expect(document.activeElement).toBe(w.find('[data-testid="settings-search-entry"]').element);
    w.unmount();
  });

  it("结果列表里 Esc 关闭搜索也归还焦点（#67）", async () => {
    const w = await mountSettings();
    await openSearch(w);
    await typeQuery(w, "主题");
    await w.find('[data-testid="settings-search-input"]').trigger("keydown", { key: "ArrowDown" });
    const firstHit = w.find('[data-search-hit="regular.theme"]');
    expect(document.activeElement).toBe(firstHit.element);
    await firstHit.trigger("keydown", { key: "Escape" });
    await flushFrames();

    expect(
      (w.find('[data-testid="settings-search-panel"]').element as HTMLElement).style.display,
    ).toBe("none");
    expect(document.activeElement).toBe(w.find('[data-testid="settings-search-entry"]').element);
    w.unmount();
  });

  it("中文输入法组字中按 Esc 只取消选字，不关闭搜索（#67）", async () => {
    const w = await mountSettings();
    await openSearch(w);
    await typeQuery(w, "主题");
    const input = w.find('[data-testid="settings-search-input"]');
    await input.trigger("keydown", { key: "Escape", isComposing: true });
    await flushFrames();
    // 组字中：面板仍然打开，焦点仍在输入框（选字优先）
    expect(
      (w.find('[data-testid="settings-search-panel"]').element as HTMLElement).style.display,
    ).not.toBe("none");
    expect(document.activeElement).toBe(input.element);
    // 组字结束后再按 Esc 才关闭
    await input.trigger("keydown", { key: "Escape", isComposing: false });
    await flushFrames();
    expect(
      (w.find('[data-testid="settings-search-panel"]').element as HTMLElement).style.display,
    ).toBe("none");
    w.unmount();
  });

  it("搜索定位不改变其它分区的草稿", async () => {
    const w = await mountSettings();
    await openCategory(w, "memory");
    const memory = sectionById(w, "memory");
    // 7000 是步进（1000）的整数倍：步进器失焦收口后仍是同一个值，便于证明「草稿没被搜索改掉」
    await memory.find('input[aria-label="单段长度目标"]').setValue("7000");
    await nextTick();
    const keysBefore = settingKeys(w.find('[data-testid="settings-panels"]').element as HTMLElement);

    await openSearch(w);
    await typeQuery(w, "主题");
    await w.find('[data-search-hit="regular.theme"]').trigger("click");
    await flushFrames();

    expect(
      (sectionById(w, "memory").find('input[aria-label="单段长度目标"]').element as HTMLInputElement)
        .value,
    ).toBe("7000");
    expect(settingKeys(w.find('[data-testid="settings-panels"]').element as HTMLElement)).toEqual(
      keysBefore,
    );
    w.unmount();
  });
});
