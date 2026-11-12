/**
 * #36 / #75 · 星球页浮层与窗口层（标题条）的命中边界（源码级守卫，无需浏览器）。
 *
 * 背景：无边框窗口的顶部拖动区是 WindowChrome（fixed，z-index 240，HTCAPTION 命中区）。
 * 它的**实际底边** = 透明留白(--window-frame-inset) + 标题条高度(--titlebar-height)
 * = --chrome-top；星球页是 fixed inset:0 的整层，内容必须从 --chrome-top 开始，
 * 否则「收起星球 / 搜索 / 管理」按钮和控制条的上半截会落进拖动区（点上去只拖动窗口），
 * 左边还会落进 28px 透明留白带（视觉上被吃掉）。
 *
 * jsdom 不做真实排版（offsetTop 全 0），所以这里按仓库既有约定读源码钉住几何事实。
 */
import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

const SRC = resolve(process.cwd(), "src");
function read(rel: string): string {
  return readFileSync(resolve(SRC, rel), "utf8");
}
/** 剥掉块注释：注释里会提到被删掉的旧写法，断言不能命中注释。 */
function stripComments(source: string): string {
  return source
    .split("/*")
    .map((part) => {
      const end = part.indexOf("*/");
      return end >= 0 ? part.slice(end + 2) : part;
    })
    .join("");
}
/** 取某条选择器的规则体（必须从行首开始匹配，避免命中组合选择器里的同名词）。 */
function ruleBody(source: string, selector: string): string {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const m = new RegExp("(?:^|\n)\\s*" + escaped + "\\s*\\{([^}]*)\\}").exec(source);
  return m ? m[1] : "";
}

const tokens = read("styles/tokens.css");
const PLANET = stripComments(read("views/PlanetView.vue"));
const CHROME = stripComments(read("components/WindowChrome.vue"));

describe("标题条实际底边 = 留白 + 标题条（唯一组合令牌）", () => {
  it("tokens 定义 --chrome-top 且 --titlebar-height 从客户区顶算的问题只在组合令牌里解决", () => {
    expect(tokens).toMatch(/--chrome-top:\s*calc\(var\(--window-frame-inset\) \+ var\(--titlebar-height\)\)/);
  });

  it("WindowChrome 的窗口层从留白开始、高一个标题条 → 拖动区底边就是 --chrome-top", () => {
    const chrome = ruleBody(CHROME, ".window-chrome");
    expect(chrome).toContain("top: var(--window-frame-inset)");
    expect(chrome).toContain("height: var(--titlebar-height)");
    // 拖动面铺满窗口层：它的底边 = 窗口层底边 = --chrome-top
    expect(ruleBody(CHROME, ".drag-surface")).toContain("inset: 0");
  });
});

describe("星球页内容从标题条实际底边开始（#36 / #75）", () => {
  it("内容层顶边用 --chrome-top，四边都在可见内容区内", () => {
    const body = ruleBody(PLANET, ".planet-content");
    expect(body).toContain(
      "inset: var(--chrome-top) var(--window-frame-inset) var(--window-frame-inset) var(--window-frame-inset)",
    );
    // 旧行为被本项否定：曾经只让出 --titlebar-height，内容上沿被拖动区吃掉 28px
    expect(body).not.toContain("var(--titlebar-height)");
  });

  it("页面工具行（搜索 / 管理）在内容层内，因此落在标题条下方", () => {
    const contentStart = PLANET.indexOf('ref="contentRef"');
    const contentEnd = PLANET.indexOf("<!-- 指向话题点");
    expect(contentStart).toBeGreaterThan(-1);
    expect(contentEnd).toBeGreaterThan(contentStart);
    const contentBlock = PLANET.slice(contentStart, contentEnd);
    expect(contentBlock).toContain("<PageTools");
    // top 是相对内容层的偏移，不是相对客户区
    const tools = ruleBody(PLANET, ".page-tools-layer");
    expect(tools).toContain("top: 10px");
    expect(tools).not.toContain("--titlebar-height");
  });

  it("收起星球按钮让开拖动区与透明留白：top 用 --chrome-top，left 加回留白", () => {
    const btn = ruleBody(PLANET, ".close-btn");
    expect(btn).toContain("top: calc(var(--chrome-top) + 14px)");
    expect(btn).toContain("left: calc(var(--window-frame-inset) + 14px)");
    // 旧行为被本项否定：left: 14px 落在 28px 透明留白里、top 只让出标题条不含留白
    expect(btn).not.toMatch(/left:\s*14px/);
    expect(btn).not.toContain("top: calc(var(--titlebar-height)");
  });

  it("顶部提示（加载失败 / 加载中）与开发者 HUD 同样按可见内容区定位", () => {
    expect(ruleBody(PLANET, ".load-error")).toContain("top: calc(var(--chrome-top) + 14px)");
    // 注意：.planet-loading 也出现在合并圆角规则（.topic-hint, .close-btn, .hud, .planet-loading）里，
    // 所以按「独立规则 + 属性」匹配，避免取到合并规则的空壳。
    expect(PLANET).toMatch(
      /\.planet-loading\s*\{\s*position: absolute; top: calc\(var\(--chrome-top\) \+ 14px\)/,
    );
    const hud = ruleBody(PLANET, ".hud");
    expect(hud).toContain("left: calc(var(--window-frame-inset) + 14px)");
    expect(hud).toContain("bottom: calc(var(--window-frame-inset) + 14px)");
    // 星球页里不再有任何「从客户区顶算标题条」的绝对定位
    expect(PLANET).not.toMatch(/top:\s*calc\(var\(--titlebar-height\)/);
  });
});

describe("管理区导航整体避开标题条：宽窗右侧 / 小窗上下两种布局（#75）", () => {
  it("宽窗：管理区是内容层里的绝对定位右侧面板，顶到内容层顶边（= 标题条下方）", () => {
    const panel = ruleBody(PLANET, ".panel");
    expect(panel).toContain("position: absolute");
    expect(panel).toContain("top: 0");
    expect(panel).toContain("right: 0");
  });

  it("小窗：管理区回到文档流（相对定位），仍从内容层开始，不会伸到标题条下", () => {
    const stackedPanel = ruleBody(PLANET, ".planet-view.managing.stacked .panel");
    expect(stackedPanel).toContain("position: relative");
    expect(stackedPanel).toContain("inset: auto");
    expect(stackedPanel).not.toContain("--titlebar-height");
    // 上下排布时内容层自己接管纵向滚动，管理区跟着内容层走
    const stackedContent = ruleBody(PLANET, ".planet-view.managing.stacked .planet-content");
    expect(stackedContent).toContain("overflow-y: auto");
  });
});
