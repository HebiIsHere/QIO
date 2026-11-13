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

const SETTINGS_SURFACE_CSS = read("styles/settings-surface.css");
const SETTINGS_SURFACE_NO_COMMENTS = stripComments(SETTINGS_SURFACE_CSS);

describe("圆角：唯一档位，但只给真正的承载面", () => {
  // 视觉修复（2026-10-28）：短控件不再画闭合外轮廓，所以它们本来就没有圆角。
  // 这里不逐条比对实现，只钉住两条会真正伤害一致性的规则：
  // ① 除 0 / 圆形 / 胶囊几何外，圆角只能来自 --qio-ui-radius（14px）；
  // ② 仍然有形状的东西（承载面、浮层菜单）必须用这个令牌。
  const allowed = new Set(["0", "50%", "var(--qio-ui-radius)", "var(--r-pill)"]);

  it("除 0 / 圆形 / 胶囊外，圆角只来自 14px 令牌", () => {
    for (const [label, source] of [
      ["styles/settings.css", SETTINGS_CSS_NO_COMMENTS],
      ["styles/settings-surface.css", SETTINGS_SURFACE_NO_COMMENTS],
    ] as const) {
      const values = [...source.matchAll(/border-radius:\s*([^;]+);/g)].map((m) => m[1].trim());
      expect(values.length, label).toBeGreaterThan(5);
      for (const value of values) {
        expect(allowed.has(value), label + " -> " + value).toBe(true);
      }
    }
    expect(SETTINGS_CSS).not.toMatch(/border-radius:\s*var\(--r-(xs|sm|md|lg|xl)\)/);
  });

  it("承载面与浮层菜单仍然用 14px 令牌", () => {
    expect(SETTINGS_SURFACE_CSS).toMatch(/\.set-main \{[^}]*border-radius:\s*var\(--qio-ui-radius\)/);
    expect(SETTINGS_SURFACE_CSS).toMatch(/\.qio-select-menu\.qio-select-menu \{[^}]*border-radius:\s*var\(--qio-ui-radius\)/);
  });
});

describe("表面：不再有常驻灰底与药丸描边", () => {
  it("设置样式里没有把 --bg-inset / --bg-surface 当控件填充", () => {
    expect(SETTINGS_CSS).not.toMatch(/background:\s*var\(--bg-inset\)/);
    expect(SETTINGS_CSS).not.toMatch(/background:\s*var\(--bg-surface\)/);
  });

  it("没有药丸外壳的三件套：底边内阴影、单侧竖条", () => {
    expect(SETTINGS_CSS).not.toMatch(/inset 0 -2px/);
    expect(SETTINGS_CSS).not.toMatch(/border-left:\s*[23]px/);
    expect(SETTINGS_SURFACE_CSS).not.toMatch(/inset 0 -2px/);
  });

  it("分段控件没有轨道：不画底色与外框", () => {
    const rule = /\.set-seg \{([^}]*)\}/.exec(SETTINGS_CSS);
    expect(rule, ".set-seg").not.toBeNull();
    const body = rule === null ? "" : rule[1];
    expect(body).toContain("background: none");
    expect(body).toMatch(/border:\s*0/);
  });

  it("作用域：共享原语的覆盖必须限定在 .qio-settings 内，且不用 !important", () => {
    expect(SETTINGS_SURFACE_NO_COMMENTS).not.toContain("!important");
    const body = SETTINGS_SURFACE_NO_COMMENTS;
    const withoutAtBlocks = body.replace(/@media[^{]+\{/g, "").replace(/@keyframes[^{]+\{/g, "");
    for (const match of withoutAtBlocks.matchAll(/([^{}]+)\{/g)) {
      for (const selector of match[1].split(",")) {
        const sel = selector.trim();
        if (!sel || sel.startsWith("from") || sel.startsWith("to") || /^\d+%$/.test(sel)) continue;
        expect(sel.startsWith(".qio-settings"), "未加作用域的选择器：" + sel).toBe(true);
      }
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


describe("#90 五个分区共用外边界：外框宽度只由容器决定", () => {
  it("窄窗切成纵向布局时回 stretch，并让主容器占满整行", () => {
    const top = /\.qio-settings\[data-nav-layout="top"\] \{([^}]*)\}/.exec(SETTINGS_CSS);
    expect(top, "窄窗布局规则").not.toBeNull();
    expect(top === null ? "" : top[1]).toContain("align-items: stretch");
    expect(SETTINGS_CSS).toMatch(/\.qio-settings\[data-nav-layout="top"\] \.set-main \{[^}]*width:\s*100%/);
  });

  it("面板与分区都声明 100% 宽度，不会按分区内容收缩", () => {
    expect(SETTINGS_CSS).toMatch(/\.set-panels \{[^}]*width:\s*100%/);
    expect(SETTINGS_CSS).toMatch(/\.set-section \{[^}]*width:\s*100%/);
  });

  it("不存在按分区 id/内容写死宽度的规则", () => {
    expect(SETTINGS_CSS).not.toMatch(/\[data-settings-section[^\]]*\][^{]*\{[^}]*width:/);
    expect(SETTINGS_SURFACE_CSS).not.toMatch(/\[data-settings-section[^\]]*\][^{]*\{[^}]*width:/);
  });
});

describe("#70 设置行：标签与控件落在同一可读区域", () => {
  it("行用两列网格，控件紧跟标题列；不再用弹性空档把控件推到面板最右", () => {
    const row = /\.set-row \{([^}]*)\}/.exec(SETTINGS_CSS);
    expect(row, ".set-row").not.toBeNull();
    const body = row === null ? "" : row[1];
    expect(body).toContain("display: grid");
    expect(body).toMatch(/grid-template-areas:[\s\S]*"title ctl"/);
    expect(body).toContain("column-gap: 24px");
    // 旧写法：标签侧 flex 增长 + 控件被剩余空间推远。两种都不允许回潮。
    expect(SETTINGS_CSS).not.toMatch(/\.set-row__txt \{[^}]*flex:/);
    expect(SETTINGS_CSS).not.toMatch(/\.set-row__ctl \{[^}]*margin-left:\s*auto/);
    // 只允许 min-width: 0（允许收缩）；禁止用固定 min-width 撑开距离
    expect(SETTINGS_CSS).not.toMatch(/\.set-row__ctl \{[^}]*min-width:\s*[1-9]\d*px/);
  });

  it("说明文字跨两列，不参与撑宽标题列；窄窗按 标题 → 说明 → 控件 上下排列", () => {
    expect(SETTINGS_CSS).toMatch(/\.set-row__desc \{[^}]*grid-area:\s*desc/);
    expect(SETTINGS_CSS).toMatch(/grid-template-areas:[\s\S]*"title"[\s\S]*"desc"[\s\S]*"ctl"/);
    expect(SETTINGS_CSS).toMatch(/@media \(max-width: 900px\) \{[\s\S]*grid-template-columns:\s*minmax\(0,\s*1fr\)/);
  });

  it("SettingRow 结构：标题单独一列，说明与控件是兄弟节点", () => {
    const source = read("views/settings/shell/SettingRow.vue");
    expect(source).toMatch(/<div class="set-row__txt">\s*<div class="set-row__title">/);
    expect(source).toMatch(/<p v-if="desc" class="set-row__desc">/);
    expect(source).toMatch(/<div class="set-row__ctl">/);
    // 说明不再放在标题容器里：否则说明宽度会把标题列撑宽、控件被推远
    const txtBlock = /<div class="set-row__txt">([\s\S]*?)<\/div>/.exec(source);
    expect(txtBlock, "set-row__txt").not.toBeNull();
    expect(txtBlock === null ? "" : txtBlock[1]).not.toContain("set-row__desc");
  });
});

describe("#87 危险按钮：设置页普通按钮规则不得覆盖危险态", () => {
  it("danger 与 danger-solid 在普通按钮规则之后恢复危险外观", () => {
    const generic = SETTINGS_SURFACE_CSS.indexOf(".qio-settings .qio-btn.qio-btn {");
    const danger = SETTINGS_SURFACE_CSS.indexOf(".qio-settings .qio-btn.qio-btn.danger {");
    const solid = SETTINGS_SURFACE_CSS.indexOf(".qio-settings .qio-btn.qio-btn.danger-solid {");
    expect(generic).toBeGreaterThanOrEqual(0);
    expect(danger).toBeGreaterThan(generic);
    expect(solid).toBeGreaterThan(generic);
    expect(SETTINGS_SURFACE_NO_COMMENTS).toMatch(
      /\.qio-settings \.qio-btn\.qio-btn\.danger-solid \{[^}]*background:\s*var\(--danger\)/,
    );
    expect(SETTINGS_SURFACE_NO_COMMENTS).toMatch(
      /\.qio-settings \.qio-btn\.qio-btn\.danger \{[^}]*color:\s*var\(--danger\)/,
    );
  });

  it("设置页不新增第二套危险按钮类名（沿用基线的 .danger / .danger-solid）", () => {
    const custom = [...SETTINGS_SURFACE_NO_COMMENTS.matchAll(/\.qio-btn\.qio-btn\.([a-z-]+)/g)]
      .map((match) => match[1])
      .filter((name) => name === "danger" || name === "danger-solid");
    expect(custom.length).toBeGreaterThanOrEqual(2);
    expect(SETTINGS_SURFACE_NO_COMMENTS).not.toMatch(/\.qio-danger|qio-btn--danger|btn-danger/);
  });
});