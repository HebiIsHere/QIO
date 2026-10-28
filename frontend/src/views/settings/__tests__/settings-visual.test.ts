/**
 * 设置页视觉/实现约束（静态扫描，与已有的 design-guards / 滚动策略守卫同口径）：
 * 14px 圆角令牌、颜色只走令牌、无原生控件、无浏览器确认框、
 * 减少动画沿用全局降级而不是「所有时长归零」、单一滚动视口。
 */
import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

const SRC = resolve(process.cwd(), "src");

function read(rel: string): string {
  return readFileSync(resolve(SRC, rel), "utf8");
}

/** 注释里会引用旧的/错误的写法（说明为什么不用），断言前先剥掉块注释 */
function stripComments(source: string): string {
  return source
    .split("/*")
    .map((part) => {
      const end = part.indexOf("*/");
      return end >= 0 ? part.slice(end + 2) : part;
    })
    .join("");
}

const SETTINGS_CSS = read("styles/settings.css");
const SETTINGS_CSS_NO_COMMENTS = stripComments(SETTINGS_CSS);
const SHELL_AND_SECTIONS = [
  "views/SettingsView.vue",
  "views/settings/shell/SettingRow.vue",
  "views/settings/shell/SegmentedControl.vue",
  "views/settings/shell/AdvGroup.vue",
  "views/settings/shell/GroupSaveBar.vue",
  "views/settings/shell/SettingsNoticeLine.vue",
  "views/settings/shell/SettingsNav.vue",
  "views/settings/shell/SettingsSearchPanel.vue",
  "views/settings/sections/RegularSection.vue",
  "views/settings/sections/ModelServicesSection.vue",
  "views/settings/sections/SearchConfigGroup.vue",
  "views/settings/sections/MemorySection.vue",
  "views/settings/sections/ToolsSection.vue",
  "views/settings/sections/AboutSection.vue",
];

describe("14px 圆角令牌（§4）", () => {
  it("设置页内的圆角一律使用 var(--qio-ui-radius)", () => {
    const declarations = [...SETTINGS_CSS.matchAll(/border-radius:\s*([^;]+);/g)].map((m) => m[1].trim());
    expect(declarations.length).toBeGreaterThan(10);
    for (const value of declarations) {
      expect(value, value).toContain("var(--qio-ui-radius)");
    }
    // 不复用旧 --r-* 近似值（旧令牌只留给未纳入本轮的组件）
    expect(SETTINGS_CSS).not.toMatch(/border-radius:\s*var\(--r-(xs|sm|md|lg|xl)\)/);
  });

  it("分区/搜索/折叠组都有明确圆角（不是只给按钮加）", () => {
    for (const selector of [".set-seg", ".set-adv", ".set-groupbar", ".set-channels", ".set-search__hit", ".set-license"]) {
      const rule = new RegExp(selector.replace(".", "\\.") + "\\s*\\{([^}]*)\\}").exec(SETTINGS_CSS);
      expect(rule, selector).not.toBeNull();
      expect(rule![1], selector).toContain("var(--qio-ui-radius)");
    }
  });
});

describe("颜色与动效只走令牌（§1 / §14.2）", () => {
  it("设置样式里没有硬编码颜色", () => {
    expect(SETTINGS_CSS).not.toMatch(/#[0-9a-fA-F]{3,8}\b/);
    expect(SETTINGS_CSS).not.toMatch(/\brgba?\(/);
    expect(SETTINGS_CSS).not.toMatch(/\bhsla?\(/);
  });

  it("没有新增「所有时长归零」的旧做法，减少动画交给 html[data-motion] 全局降级", () => {
    expect(SETTINGS_CSS_NO_COMMENTS).not.toMatch(/animation-duration:\s*0/);
    expect(SETTINGS_CSS_NO_COMMENTS).not.toMatch(/transition-duration:\s*0/);
    expect(SETTINGS_CSS_NO_COMMENTS).not.toContain("[data-motion");
    // 动效时长写令牌而不是裸数字
    expect(SETTINGS_CSS_NO_COMMENTS).not.toMatch(/transition:[^;]*\b\d{2,4}ms/);
    expect(SETTINGS_CSS_NO_COMMENTS).not.toMatch(/animation:[^;]*\b\d{2,4}ms/);
  });

  it("焦点可见：自绘控件都有 :focus-visible 轮廓（不抹掉原生焦点）", () => {
    const count = [...SETTINGS_CSS.matchAll(/:focus-visible/g)].length;
    expect(count).toBeGreaterThanOrEqual(5);
    expect(SETTINGS_CSS).not.toMatch(/outline:\s*none/);
  });
});

describe("自绘控件：不用原生外观（§4）", () => {
  it("设置页组件里没有原生 select / 数字步进 / 系统复选框 / 浏览器 confirm", () => {
    for (const rel of SHELL_AND_SECTIONS) {
      const source = read(rel);
      expect(source, rel).not.toMatch(/<select\b/);
      expect(source, rel).not.toMatch(/type="number"/);
      expect(source, rel).not.toMatch(/type="checkbox"/);
      expect(source, rel).not.toMatch(/window\.(confirm|alert|prompt)/);
      // 原生 title 不受控（延迟/外观/键盘可达性），图标按钮的说明走 QIconButton 的 v-qio-tip
      expect(source, rel).not.toMatch(/<(button|a|span|div|input)[^>]*\stitle="/);
    }
  });

  it("图标按钮使用统一的 QIconButton（内置 aria-label 与自绘提示）", () => {
    expect(read("views/settings/shell/SettingsNav.vue")).toContain("QIconButton");
    expect(read("views/settings/shell/SettingsSearchPanel.vue")).toContain("QIconButton");
  });

  it("每一行设置都有稳定的 data-settings-key", () => {
    const total = SHELL_AND_SECTIONS.reduce(
      (sum, rel) => sum + [...read(rel).matchAll(/settings-key="/g)].length,
      0,
    );
    expect(total).toBeGreaterThan(28);
    // SettingRow 的每次使用都必须带 settings-key（漏一个搜索就定位不到）
    for (const rel of SHELL_AND_SECTIONS) {
      const source = read(rel);
      for (const match of source.matchAll(/<SettingRow[\s\S]*?>/g)) {
        expect(match[0], rel).toContain("settings-key=");
      }
    }
  });
});

describe("滚动：只有一个主要纵向视口（§15）", () => {
  it("只有 SettingsView 自己声明纵向滚动，并且接入共享指示条", () => {
    const rest = SHELL_AND_SECTIONS.filter((rel) => rel !== "views/SettingsView.vue");
    for (const rel of rest) {
      expect(read(rel), rel).not.toContain("overflow-y: auto");
      expect(read(rel), rel).not.toContain("overflow-x: auto");
      expect(read(rel), rel).not.toContain("overflow-x: scroll");
    }
    const view = read("views/SettingsView.vue");
    expect(view).toContain("overflow-y: auto");
    expect(view).toContain("v-qio-scrollbar");
    expect(view).toContain("data-qio-fit");
    expect(view).toContain("overflow-x: clip");
    // 不出现原生/自绘滚动条
    expect(view).not.toMatch(/::-webkit-scrollbar|scrollbar-width\s*:/);
  });
});
