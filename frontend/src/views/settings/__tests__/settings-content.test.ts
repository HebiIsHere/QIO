/**
 * 四类内容（常规 / 记忆与整理 / 工具与权限 / 关于与更新）的覆盖与关键交互：
 * 迁移清单里的每一项都必须有位置；只读区只显示已保存的值；危险操作走 QConfirm。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { nextTick } from "vue";
import { resetFloatPositions } from "../../../composables/floatingState";
import {
  chooseSelectLabel,
  chooseSelectOption,
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

  it("常规：主题/动画/滚动指示条/输出速度/引导/还原布局/关闭行为/诊断（失效入口设置已删）", async () => {
    const w = await mountSettings();
    const regular = sectionById(w, "regular");
    expect(regular.findAll(".set-seg__opt").map((option) => option.text())).toEqual(
      expect.arrayContaining(["系统", "暗紫晶", "净白", "跟随系统", "标准", "减少动画"]),
    );
    // 旧行为被本项否定（#39）：两个贴靠隐藏开关已删，常规里只剩「开发者模式」一个开关
    expect(regular.findAll('button[role="switch"]').length).toBe(1);
    expect(regular.text()).toMatch(/继续设置|重新运行设置助手/);
    // #47：还原默认布局恢复（还原的是对话页布局偏好，不是已移除的浮动入口位置）
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
    // #16：没有改动时不摆常驻保存条；首屏先给后台整理三项（#24）
    expect(memory.find('[data-settings-group="memory.fragment"]').exists()).toBe(false);
    expect(memory.text()).toContain("启用后台整理");
    expect(memory.text()).toContain("整理间隔");
    expect(memory.text()).toContain("立即整理");
    // 分段设置与模型用量在「高级选项」里（默认收起，控件仍在 DOM）
    expect(
      memory.find('[data-settings-fold-toggle="memory.advanced"]').attributes("aria-expanded"),
    ).toBe("false");
    expect(memory.find('[data-settings-key="memory.fragment.tier"]').exists()).toBe(true);
    expect(memory.find('[data-settings-key="memory.fragment.max-tokens"]').exists()).toBe(true);
    await chooseSelectOption(memory, "自定义");
    expect(memory.find('[data-settings-key="memory.fragment.custom-turns"]').exists()).toBe(true);
    expect(
      memory.find('[data-settings-group="memory.fragment"] [data-settings-dirty]').text(),
    ).toContain("有未保存的修改");

    // 立即整理是危险/高影响操作：先确认
    const run = memory.findAll(".qio-btn").find((button) => button.text() === "立即整理");
    expect(run).toBeTruthy();
    await run!.trigger("click");
    await nextTick();
    // layer 档确认框是窗口层（Teleport 到 body），按文档查而不是按组件子树查。
    const dialog = document.querySelector('[role="dialog"]');
    expect(dialog).not.toBeNull();
    expect(dialog!.textContent).toContain("立即整理记忆与知识？");
    const { api } = await import("../../../services/api");
    expect(api.runMaintenance).not.toHaveBeenCalled();
    (dialog!.querySelector('[data-qio-confirm-action="confirm"]') as HTMLElement).click();
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

  it("文案 1 / 9：当前生效参数里嵌入写「嵌入模型 · 使用当前配置」，不生效用途不重复同一句话", async () => {
    const { setModelFacts, resetModelFacts } = await import("../modelFacts");
    const w = await mountSettings();
    // SettingsView 挂载时会重建只读事实表：必须挂载后再注入，否则会被 reset 掉。
    setModelFacts({
      defaultModel: null,
      usages: [
        {
          usageKey: "main-chat",
          label: "主对话",
          followsDefault: true,
          resolved: { credentialId: "c1", modelId: "m1" },
          connectionName: "主力连接",
          status: "follow",
        },
        { usageKey: "video", label: "视频", followsDefault: false, status: "existing", note: "沿用现有处理方式" },
        { usageKey: "research", label: "研究", followsDefault: false, status: "unsupported", note: "未接通独立调用路径" },
        {
          usageKey: "embedding",
          label: "向量与嵌入",
          followsDefault: false,
          status: "existing",
          note: "沿用现有处理方式",
        },
      ],
    });
    await nextTick();
    await openCategory(w, "tools");
    await nextTick();
    const usages = sectionById(w, "tools").find('[data-settings-key="tools.effective.usages"]');
    expect(usages.exists()).toBe(true);
    const text = usages.text();
    // 嵌入：显示名与结论都按「使用当前配置」，不写成内部用途名或无配置态
    expect(text).toContain("嵌入模型");
    expect(text).toContain("使用当前配置");
    expect(text).not.toContain("向量与嵌入");
    // 同一行不再把同一句话写两遍（旧实现是「沿用现有处理方式 （沿用现有处理方式）」）
    expect(text).not.toContain("（沿用现有处理方式）");
    expect(text).not.toContain("（未接通）");
    expect(text.split("沿用现有处理方式").length - 1).toBe(1);
    resetModelFacts();
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
    // 本轮清理解释性文案：改为断言选中态与再次切换后的持久化行为，不再断言说明文字。
    expect(
      regular.find('[data-settings-key="regular.scrollbar"] .set-seg__opt.on').text(),
    ).toBe("始终显示");
    await chooseSelectLabel(regular, "自动显示");
    expect(document.documentElement.getAttribute("data-scrollbar")).toBe("auto");
    expect(localStorage.getItem("qio-scrollbar")).toBe("auto");
    w.unmount();
  });

  it("窗口行为：贴靠隐藏开关仍移除（#39）；还原默认布局恢复并说明保留标签/位置（#47）", async () => {
    const w = await mountSettings();
    const regular = sectionById(w, "regular");
    // 旧行为被本项否定：入口固定在输入区/全局顶栏后，这两项设置保存的是一个不存在的效果
    expect(regular.find('button[aria-label="星球入口贴靠后自动隐藏"]').exists()).toBe(false);
    expect(regular.find('button[aria-label="设置入口贴靠后自动隐藏"]').exists()).toBe(false);
    for (const key of ["regular.window.planet-hide", "regular.window.settings-hide"]) {
      expect(regular.find(`[data-settings-key="${key}"]`).exists(), key).toBe(false);
    }
    // #47：还原默认布局重新可用，且明确它保留预览标签与阅读位置
    const row = regular.find('[data-settings-key="regular.window.reset-layout"]');
    expect(row.exists()).toBe(true);
    expect(row.find("button").text()).toBe("还原默认布局");
    expect(row.text()).toContain("预览标签");
    expect(row.text()).toContain("阅读位置");
    // 关闭主窗口行为仍在这里（不受 #39 / #47 影响）
    expect(regular.find('[data-settings-key="regular.window.close-behavior"]').exists()).toBe(true);
    w.unmount();
  });

  it("关闭主窗口行为：三档与契约一致，保存到后端设置", async () => {
    const { api } = await import("../../../services/api");
    const w = await mountSettings();
    const regular = sectionById(w, "regular");
    const text = regular.text();
    for (const label of ["每次询问", "最小化到托盘", "直接退出"]) expect(text).toContain(label);
    // 文案 #16：只交代托盘时继续运行、退出后有效待审批事项的去向
    expect(text).toContain("继续运行");
    expect(text).toContain("待审批");
    expect(text).toContain("保留到下次启动");
    expect(text).not.toContain("有界收尾");
    expect(text).not.toContain("可从系统托盘恢复");
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

  it("联网搜索通道行显示真实状态：已配置 + 尚未检查（按状态断言，不按长句断言）", async () => {
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
    // 配置状态与检查状态是两段，各自有稳定的状态标记
    const bocha = models.find('[data-search-channel="bocha"]');
    expect(bocha.find("[data-channel-config]").text()).toBe("已配置");
    expect(bocha.find("[data-channel-check]").attributes("data-channel-check-state")).toBe("unchecked");
    expect(bocha.text()).toContain("已配置，尚未检查");
    const searxng = models.find('[data-search-channel="searxng"]');
    expect(searxng.find("[data-channel-config]").text()).toBe("已填写地址");
    expect(searxng.find("[data-channel-check]").attributes("data-channel-check-state")).toBe("unchecked");
    // #7：免密钥通道的状态只写「已开启 / 已关闭」，检查状态单独一段
    const keyless = models.find('[data-search-channel="keyless"]');
    expect(keyless.find("[data-channel-config]").text()).toBe("已开启");
    expect(keyless.find("[data-channel-check]").attributes("data-channel-check-state")).toBe("unchecked");
    // 常驻的判据解释已删除：状态行自己说清「已配置 / 尚未检查」
    expect(models.text()).not.toContain("「已配置」不代表「检查通过」");
    expect(models.text()).not.toContain("检查只在你主动点击");
    w.unmount();
  });

  it("#23 检查只由用户点按触发；改配置后回到「已配置，尚未检查」，旧结论不沿用", async () => {
    const { api } = await import("../../../services/api");
    (api.getSearchSettings as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      searxng_url: "https://old.example",
      bocha_has_key: true,
      keyless_fallback: true,
      top_k_default: 5,
      max_fetch_chars: 15000,
    });
    (api.getSearchStatus as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      channels: [
        { channel: "searxng", configured: true, checked: true, ok: true, reason: "" },
      ],
      checked_at: "2026-11-12T00:00:00Z",
    });
    const w = await mountSettings();
    await openCategory(w, "models");
    const models = sectionById(w, "models");

    // 挂载不自动做连接检查
    expect(api.checkSearch).not.toHaveBeenCalled();
    expect(models.find('[data-search-channel="searxng"] [data-channel-check]').text()).toContain(
      "检查通过",
    );

    // 改动 SearXNG 地址并保存：旧检查结论属于旧地址，必须回到「尚未检查」
    await models.find('[data-settings-fold-toggle="models.search.advanced"]').trigger("click");
    await nextTick();
    await models.find('input[aria-label="自建 SearXNG 地址"]').setValue("https://new.example");
    await nextTick();
    await models
      .find('[data-settings-group="models.search"] [data-settings-action="save"]')
      .trigger("click");
    await flushFrames();
    const searxngRow = models.find('[data-search-channel="searxng"]');
    expect(searxngRow.text()).toContain("已填写地址，尚未检查");
    expect(searxngRow.text()).not.toContain("检查通过");
    w.unmount();
  });

  it("#23 用户点检查失败：保留配置、显示这次失败并可重试；成功后才显示「检查通过」", async () => {
    const { api } = await import("../../../services/api");
    (api.getSearchSettings as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      searxng_url: "https://searx.example",
      bocha_has_key: false,
      keyless_fallback: true,
      top_k_default: 5,
      max_fetch_chars: 15000,
    });
    (api.checkSearch as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      channels: [
        { channel: "searxng", configured: true, checked: true, ok: false, reason: "连接被拒绝" },
      ],
    });
    const w = await mountSettings();
    await openCategory(w, "models");
    const models = sectionById(w, "models");

    await models.find('[data-testid="settings-search-check"]').trigger("click");
    await flushFrames();
    // 只描述「这一次检查」失败；已填配置保留
    const failedRow = models.find('[data-search-channel="searxng"]');
    expect(failedRow.find("[data-channel-check]").attributes("data-channel-check-state")).toBe("failed");
    expect(failedRow.text()).toContain("检查失败：连接被拒绝");
    expect(
      (models.find('input[aria-label="自建 SearXNG 地址"]').element as HTMLInputElement).value,
    ).toBe("https://searx.example");
    expect(models.find('[data-testid="settings-search-check"]').text()).toContain("重新检查");
    expect(api.updateSearchSettings).not.toHaveBeenCalled();

    // 用户再点一次检查：真实成功之后才写「检查通过」
    (api.checkSearch as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      channels: [
        { channel: "searxng", configured: true, checked: true, ok: true, reason: "" },
      ],
    });
    await models.find('[data-testid="settings-search-check"]').trigger("click");
    await flushFrames();
    const okRow = models.find('[data-search-channel="searxng"]');
    expect(okRow.find("[data-channel-check]").attributes("data-channel-check-state")).toBe("ok");
    expect(okRow.text()).toContain("检查通过");
    expect(okRow.text()).not.toContain("检查失败");
    w.unmount();
  });

  it("#7 免密钥通道状态只写「已开启 / 已关闭」，开关保留", async () => {
    const w = await mountSettings();
    await openCategory(w, "models");
    const models = sectionById(w, "models");
    expect(models.find('[data-search-channel="keyless"] [data-channel-config]').text()).toBe("已开启");
    const toggle = models.find('button[aria-label="免密钥联网搜索"]');
    expect(toggle.exists()).toBe(true);
    await toggle.trigger("click");
    await flushFrames();
    expect(models.find('[data-search-channel="keyless"] [data-channel-config]').text()).toBe("已关闭");
    w.unmount();
  });

  it("#8 博查通道常态保留「状态 / 替换 / 清除」，「留空保留原密钥」只在替换输入旁说明", async () => {
    const { api } = await import("../../../services/api");
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
    await flushFrames();
    const row = models.find('[data-settings-key="models.search.bocha"]');
    expect(row.text()).toContain("已配置");
    expect(row.text()).toContain("替换");
    expect(row.text()).toContain("清除");
    // 常态不显示长句说明
    expect(row.text()).not.toContain("留空保留");
    const replace = row.findAll("button").find((button) => button.text() === "替换");
    await replace!.trigger("click");
    await nextTick();
    const editingRow = models.find('[data-settings-key="models.search.bocha"]');
    expect(editingRow.find('[data-testid="settings-search-bocha-hint"]').text()).toContain(
      "留空保存不会改动已保存的 Key",
    );
    w.unmount();
  });

  it("检查请求失败：主界面说「这次检查失败」并保留重试；配置原样保留", async () => {
    const { api } = await import("../../../services/api");
    (api.getSearchSettings as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      searxng_url: "https://searx.example",
      bocha_has_key: true,
      keyless_fallback: true,
      top_k_default: 5,
      max_fetch_chars: 15000,
    });
    (api.checkSearch as ReturnType<typeof vi.fn>).mockRejectedValueOnce(new Error("network down"));
    const w = await mountSettings();
    await openCategory(w, "models");
    const models = sectionById(w, "models");
    await models.find('[data-testid="settings-search-check"]').trigger("click");
    await flushFrames();

    const notice = models.find(".set-notice.err");
    expect(notice.text()).toContain("这次检查失败");
    // 失败后配置仍然保留（检查失败 ≠ 配置丢失）
    expect(
      (models.find('input[aria-label="自建 SearXNG 地址"]').element as HTMLInputElement).value,
    ).toBe("https://searx.example");
    // 重试是真实动作：再点一次会重新发起检查
    const retry = models.find('[data-settings-action="retry"]');
    expect(retry.exists()).toBe(true);
    const callsBefore = (api.checkSearch as ReturnType<typeof vi.fn>).mock.calls.length;
    (api.checkSearch as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      channels: [{ channel: "searxng", configured: true, checked: true, ok: true, reason: "" }],
    });
    await retry.trigger("click");
    await flushFrames();
    expect((api.checkSearch as ReturnType<typeof vi.fn>).mock.calls.length).toBe(callsBefore + 1);
    expect(
      models.find('[data-search-channel="searxng"] [data-channel-check]').attributes(
        "data-channel-check-state",
      ),
    ).toBe("ok");
    w.unmount();
  });

  it("旧后端没有状态接口：只显示配置状态，并如实说明为什么没有检查状态", async () => {
    const { api } = await import("../../../services/api");
    (api.getSearchSettings as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      searxng_url: "https://searx.example",
      bocha_has_key: true,
      keyless_fallback: true,
      top_k_default: 5,
      max_fetch_chars: 15000,
    });
    // 旧后端：状态端点 404
    (api.getSearchStatus as ReturnType<typeof vi.fn>).mockRejectedValueOnce(
      new Error("404 Not Found"),
    );
    const w = await mountSettings();
    await openCategory(w, "models");
    const models = sectionById(w, "models");

    // 降级：配置状态照常显示；检查状态只能是「尚未检查」（不把「已配置」说成「检查通过」）
    expect(models.find('[data-search-channel="bocha"] [data-channel-config]').text()).toBe("已配置");
    expect(
      models.find('[data-search-channel="bocha"] [data-channel-check]').attributes(
        "data-channel-check-state",
      ),
    ).toBe("unchecked");
    expect(models.find('[data-search-channel="bocha"]').text()).not.toContain("检查通过");
    // 只有这种降级场景才解释一次（常态不再常驻判据说明）
    const unavailable = models.find('[data-testid="settings-search-status-unavailable"]');
    expect(unavailable.exists()).toBe(true);
    expect(unavailable.text()).toContain("配置状态");
    expect(models.text()).not.toContain("「已配置」不代表「检查通过」");
    w.unmount();
  });

  it("检查接口 404：如实说当前版本不提供检查，不冒充检查失败", async () => {
    const { api } = await import("../../../services/api");
    (api.checkSearch as ReturnType<typeof vi.fn>).mockRejectedValueOnce(
      new Error("404 Not Found"),
    );
    const w = await mountSettings();
    await openCategory(w, "models");
    const models = sectionById(w, "models");
    await models.find('[data-testid="settings-search-check"]').trigger("click");
    await flushFrames();

    const notice = models.find(".set-notice");
    expect(notice.text()).toContain("当前版本暂不提供连接检查");
    // 不是「这次检查失败」：没有检查能力 ≠ 检查没通过
    expect(notice.text()).not.toContain("这次检查失败");
    expect(models.find('[data-testid="settings-search-status-unavailable"]').exists()).toBe(true);
    w.unmount();
  });
});

describe("关于与更新的文案边界（#13 / 第三方声明入口）", () => {
  it("许可证保留 MIT 摘要与全文入口，不再指引「见仓库根目录」", async () => {
    const w = await mountSettings();
    await openCategory(w, "about");
    const about = sectionById(w, "about");
    const text = about.text();
    expect(text).toContain("QIO 以 MIT 许可证开源");
    expect(text).toContain("查看 QIO 许可全文");
    expect(text).not.toContain("仓库根目录");
    expect(text).not.toContain("本页展示的就是它");
    // 全文仍然可读（构建期内联，不是链接）
    expect(about.get('[data-testid="qio-license"]').text()).toContain("Permission is hereby granted");
    w.unmount();
  });

  it("第三方声明入口写「查看第三方开源声明」，不再写生成物/仓库校验口径", async () => {
    const w = await mountSettings();
    await openCategory(w, "about");
    const about = sectionById(w, "about");
    const text = about.text();
    expect(text).toContain("查看第三方开源声明");
    expect(text).not.toContain("查看第三方开源组件声明");
    expect(text).not.toContain("生成物");
    expect(text).not.toContain("覆盖率由仓库里的校验脚本保证");
    // 声明全文仍然与包一起分发、可离线展开
    expect(about.get('[data-testid="third-party-notices"]').text()).toContain(
      "QIO 第三方开源组件声明",
    );
    w.unmount();
  });
});
