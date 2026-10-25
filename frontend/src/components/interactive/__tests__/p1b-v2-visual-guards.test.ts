/**
 * Phase 1-B 视觉约束守卫（子智能体 p1-layout 负责）。
 *
 * 只扫源码文本，跑得快：
 * - 颜色只能来自 var(--*) 令牌，不许硬编码色值（契约 §2.3 / AGENTS.md）；
 * - 不允许把 HTML 演示控件（体验状态 / 重新体验 / 模拟失败）带进生产组件；
 * - 布局外壳不许再画第二条标题栏。
 */
import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

const read = (relative: string) =>
  readFileSync(resolve(process.cwd(), "src", "components", "interactive", "v2", relative), "utf8");

const workspace = read("InteractiveWorkspace.vue");
const chromeTools = read("InteractiveChromeTools.vue");
const layoutTs = read("layout.ts");
const placementTs = read("chromePlacement.ts");
const saveStatusTs = read("saveStatus.ts");
const chromeTargetTs = read("chromeTarget.ts");

const sources: Array<[string, string]> = [
  ["InteractiveWorkspace.vue", workspace],
  ["InteractiveChromeTools.vue", chromeTools],
  ["layout.ts", layoutTs],
  ["chromePlacement.ts", placementTs],
  ["saveStatus.ts", saveStatusTs],
  ["chromeTarget.ts", chromeTargetTs],
];

describe("互动页 v2 视觉约束", () => {
  it("颜色只来自 var(--*) 令牌，没有硬编码色值", () => {
    for (const [name, source] of sources) {
      expect(source, name + " 出现十六进制色值").not.toMatch(/#[0-9a-fA-F]{3,8}\b/);
      expect(source, name + " 出现 rgb()/rgba()").not.toMatch(/rgba?\(/);
      expect(source, name + " 出现 hsl()/hsla()").not.toMatch(/hsla?\(/);
    }
    // 确实用到了语义令牌（不是碰巧没有颜色声明）
    expect(workspace).toContain("var(--bg-surface)");
    expect(workspace).toContain("var(--border-subtle)");
    expect(chromeTools).toContain("var(--focus-ring)");
    expect(chromeTools).toContain("var(--danger)");
  });

  it("不把 HTML 演示控件带进生产组件", () => {
    for (const [name, source] of sources) {
      for (const demo of ["体验状态", "重新体验", "模拟失败", "demo-intent"]) {
        expect(source, name + " 混入了演示元素 " + demo).not.toContain(demo);
      }
    }
  });

  it("布局外壳不再画第二条标题栏", () => {
    expect(workspace).not.toContain("<header");
    expect(workspace).not.toContain("titlebar");
    expect(workspace).not.toContain("<h1");
    // 区域标题只给可访问名称，不渲染可见标题栏
    expect(workspace).toContain('aria-label="板面"');
    expect(workspace).toContain('aria-label="书签栏"');
    expect(workspace).toContain('aria-label="对话区"');
  });
});
