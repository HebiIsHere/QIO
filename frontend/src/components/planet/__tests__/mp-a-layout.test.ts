/**
 * 页面结构与设计语言的源码级守卫（A 交付 §1 / §6）。
 *
 * 这些是「会慢慢退回去」的约束：全局栏被覆盖、管理区挤压画布（球体变小/被切）、
 * 又冒出一套 `--r-*` 圆角、或者页面里长出原生控件外观。
 * 读源码比截图快，且能在 CI 里稳定拦住回归。
 */
import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

const SRC = resolve(process.cwd(), "src");
function read(rel: string): string {
  // 注释里会引用被删掉的旧写法，断言前先剥掉块注释
  return readFileSync(resolve(SRC, rel), "utf8")
    .split("/*")
    .map((part) => {
      const end = part.indexOf("*/");
      return end >= 0 ? part.slice(end + 2) : part;
    })
    .join("");
}
/**
 * 取某条选择器的规则体：**必须从行首开始匹配**。
 * 否则 `.planet-stage` 会先命中 `.planet-view.ball .planet-stage`（球态规则），
 * 断言就会看到另一条规则的声明。
 */
function ruleBody(source: string, selector: string): string {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const m = new RegExp("(?:^|\\n)\\s*" + escaped + "\\s*\\{([^}]*)\\}").exec(source);
  return m ? m[1] : "";
}

const PLANET = read("views/PlanetView.vue");

describe("星球页：内容区域在全局栏下方（契约 §3.1）", () => {
  it("新增 .planet-content 一层：绝对定位在标题条之下", () => {
    const body = ruleBody(PLANET, ".planet-content");
    expect(body).toContain("position: absolute");
    expect(body).toContain("inset: var(--titlebar-height) 0 0 0");
  });

  it("整层仍是 fixed + overflow: clip（不建立可被程序性滚动的容器）", () => {
    const body = ruleBody(PLANET, ".planet-view");
    expect(body).toContain("position: fixed");
    expect(body).toContain("overflow: clip");
    expect(body).not.toContain("overflow: hidden");
  });

  it("星球层铺满内容区且尺寸不随管理区变化（画布不 resize、直径不变）", () => {
    const stage = ruleBody(PLANET, ".planet-stage");
    expect(stage).toContain("position: absolute");
    expect(stage).toContain("inset: 0");
    // 管理区不用 flex 挤压画布：它是绝对定位的右侧面板
    const panel = ruleBody(PLANET, ".panel");
    expect(panel).toContain("position: absolute");
    expect(panel).toContain("right: 0");
  });
});

describe("星球页：管理展开只靠布局位移（契约 §3.3 / §4.1）", () => {
  it("画布位移与控制变量都来自 --mp，不自己另造进度", () => {
    const canvas = ruleBody(PLANET, ".planet-canvas");
    expect(canvas).toContain("translate3d(calc(var(--mp, 0) * -50%), 0, 0)");
    const panel = ruleBody(PLANET, ".panel");
    expect(panel).toContain("width: calc(var(--mp, 0) * var(--mp-w))");
    expect(ruleBody(PLANET, ".planet-view")).toContain("--mp-w: clamp(380px, 46vw, 640px)");
  });

  it("裁切只在管理展开时生效（默认态美术与基线一致）", () => {
    expect(PLANET).toContain(".planet-view.managing .planet-stage");
    expect(ruleBody(PLANET, ".planet-view.managing .planet-content")).toContain("overflow: clip");
    const content = ruleBody(PLANET, ".planet-content");
    expect(content).toContain("overflow: visible");
  });

  it("窄窗口改上下排布，且内容区只有一层纵向滚动、横向裁切", () => {
    const body = ruleBody(PLANET, ".planet-view.managing.stacked .planet-content");
    expect(body).toContain("flex-direction: column");
    expect(body).toContain("overflow-y: auto");
    expect(body).toContain("overflow-x: clip");
    // 上下排布时不产生左右位移
    expect(PLANET).toContain(".planet-view.managing.stacked .planet-canvas { transform: none; }");
  });

  it("星球页不复制、不覆盖全局栏", () => {
    expect(PLANET).not.toContain("WindowChrome");
    expect(PLANET).not.toContain("window-controls");
    expect(PLANET).not.toContain("data-tauri-drag-region");
    // 管理区只能展开在全局栏下面：内容层读同一个标题条令牌
    expect(PLANET).toContain("var(--titlebar-height)");
  });

  it("管理展开不做任何相机/重对焦补间（旋转由 C 的 setManageYaw 负责）", () => {
    expect(PLANET).not.toContain("scheduleRecenter");
    expect(PLANET).not.toContain("onPanelWidthChange");
    // 画布尺寸变化时唯一的星球侧反应是 resize
    expect(PLANET).toContain("planet.resize()");
  });
});

describe("验收钩子：只读 data-* 属性", () => {
  it("根元素暴露 data-tab / data-view", () => {
    expect(PLANET).toContain(':data-tab="manageTab"');
    expect(PLANET).toContain(':data-view="manageView"');
  });

  it("页面工具行：搜索与管理入口", () => {
    expect(read("components/planet/PageTools.vue")).toContain("data-mp-search-open");
    expect(read("components/planet/PageTools.vue")).toContain("data-mp-manage-toggle");
  });

  it("管理区：标签 / 辅助入口 / 待处理数量", () => {
    const src = read("components/planet/ManagementArea.vue");
    for (const hook of ['data-mp-tab="memory"', 'data-mp-tab="knowledge"', 'data-mp-tab="entity"', 'data-mp-aux="pending"', 'data-mp-aux="records"', ":data-mp-pending-count="]) {
      expect(src).toContain(hook);
    }
  });

  it("记忆页：列表 / 概览 / 记录 / 继续按钮 / 关联条目", () => {
    const src = read("components/planet/MemoryTab.vue");
    for (const hook of ["data-memory-topics", ":data-topic-id=", "data-memory-overview", "data-memory-record", "data-memory-continue", ":data-rel-knowledge-id=", ":data-rel-entity-id="]) {
      expect(src).toContain(hook);
    }
  });

  it("搜索：面板 / 输入框 / 结果容器 / 结果项", () => {
    const src = read("components/planet/GlobalSearchPanel.vue");
    for (const hook of ["data-mp-search", "data-mp-search-input", "data-mp-search-results", ":data-search-kind=", ":data-search-id="]) {
      expect(src).toContain(hook);
    }
  });

  it("关键滚动容器带 data-scroll（脚本量 scrollTop 恢复用）", () => {
    expect(read("components/planet/MemoryTab.vue")).toContain('data-scroll="memory-topics"');
    expect(read("components/planet/MemoryTab.vue")).toContain('data-scroll="memory-overview"');
    expect(read("components/planet/MemoryTab.vue")).toContain('data-scroll="memory-record"');
    expect(read("components/planet/GlobalSearchPanel.vue")).toContain('data-scroll="search-results"');
    expect(PLANET).toContain('data-scroll="planet-content"');
  });
});

describe("本页圆角统一 var(--qio-ui-radius)（契约 §2）", () => {
  const files = [
    "views/PlanetView.vue",
    "components/planet/PageTools.vue",
    "components/planet/ManagementArea.vue",
    "components/planet/MemoryTab.vue",
    "components/planet/GlobalSearchPanel.vue",
  ];

  it("不允许再出现 --r-xs/--r-sm/--r-md/--r-lg/--r-xl/--r-pill", () => {
    const hits = files.filter((rel) => /--r-(xs|sm|md|lg|xl|pill)\b/.test(read(rel)));
    expect(hits).toEqual([]);
  });

  it("所有 border-radius 声明都用唯一令牌（图形几何除外）", () => {
    const bad: string[] = [];
    for (const rel of files) {
      const src = read(rel);
      for (const m of src.matchAll(/border-radius:\s*([^;]+);/g)) {
        const value = m[1].trim();
        // 允许：唯一令牌、复合的四角写法、50%（圆形几何）
        if (value === "var(--qio-ui-radius)") continue;
        if (value.includes("var(--qio-ui-radius)")) continue;
        if (value === "50%") continue;
        bad.push(`${rel}: border-radius: ${value}`);
      }
    }
    expect(bad).toEqual([]);
  });
});

describe("不使用原生控件外观 / 原生确认框", () => {
  const files = [
    "views/PlanetView.vue",
    "components/planet/PageTools.vue",
    "components/planet/ManagementArea.vue",
    "components/planet/MemoryTab.vue",
    "components/planet/GlobalSearchPanel.vue",
  ];

  it("没有 window.confirm / alert / prompt", () => {
    const hits = files.filter((rel) => /window\.(confirm|alert|prompt)\s*\(/.test(read(rel)));
    expect(hits).toEqual([]);
  });

  it("没有原生下拉 / 日期 / 文件控件", () => {
    const hits = files.filter((rel) => /<select\b|<datalist\b|type="(checkbox|radio|file|date|datetime-local|month|week|time|color|range)"/.test(read(rel)));
    expect(hits).toEqual([]);
  });
});
