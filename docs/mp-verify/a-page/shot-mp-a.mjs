/**
 * A 组（页面结构与导航）的视觉/几何验收脚本。
 *
 * 为什么自己写一份而不是等工具：Lead 给的判定口径是**几何 + 像素**（球心、半径、横向滚动），
 * 这些只能在实际渲染里量。这里只读页面状态，不写任何业务数据。
 *
 * 用法（前端 dev 服务需在跑）：
 *   node docs/mp-verify/a-page/shot-mp-a.mjs [baseUrl]
 * 输出：同目录下 01..06.png 与 report.json
 *
 * 量到的关键口径（Lead 验收用）：
 * - 默认态：球心 = (内容宽/2, 标题条 + 内容高/2)
 * - 管理态：canvas.getBoundingClientRect() 中心 x ≈ 0（±2px）
 * - 管理前后球体半径一致（±1px）
 * - 任何窗口下 scrollWidth <= clientWidth + 1（无横向滚动）
 */
import { createRequire } from "node:module";
import { existsSync, mkdirSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const require = createRequire(import.meta.url);

/**
 * playwright-core 的来源按顺序找：
 * 1) QIO_PLAYWRIGHT_CORE 环境变量（指向包目录）；
 * 2) 团队约定位置 %TEMP%\qio-pw\node_modules\playwright-core；
 * 3) 直接 require（NODE_PATH 或本目录 node_modules 里装过）。
 * 找不到就明确报错，不静默降级成「没有截图也能过」。
 */
function loadPlaywright() {
  const candidates = [
    process.env.QIO_PLAYWRIGHT_CORE,
    `${process.env.TEMP}\\qio-pw\\node_modules\\playwright-core`,
    "playwright-core",
  ].filter(Boolean);
  for (const candidate of candidates) {
    try {
      return require(candidate);
    } catch {
      /* 换下一个来源 */
    }
  }
  throw new Error(
    "找不到 playwright-core：设置 QIO_PLAYWRIGHT_CORE，或 npm i playwright-core 并把 node_modules 放进 NODE_PATH",
  );
}
const { chromium } = loadPlaywright();

const HERE = dirname(fileURLToPath(import.meta.url));
const BASE = process.argv[2] ?? "http://localhost:5104";
const wait = (ms) => new Promise((r) => setTimeout(r, ms));

const CHROME = [
  "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
  "C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe",
].find((p) => existsSync(p));

const browser = await chromium.launch({
  ...(CHROME ? { executablePath: CHROME } : { channel: "msedge" }),
  headless: true,
  args: ["--use-gl=angle", "--use-angle=swiftshader", "--enable-unsafe-swiftshader"],
});

const report = { base: BASE, steps: [] };

async function measure(page, label) {
  const data = await page.evaluate(() => {
    const q = (sel) => document.querySelector(sel);
    const canvas = q(".planet-canvas");
    const content = q(".planet-content");
    const panel = q("aside.panel");
    const stage = window.__qioPlanetStage?.() ?? {};
    const rect = canvas?.getBoundingClientRect();
    const contentRect = content?.getBoundingClientRect();
    const titlebar = getComputedStyle(document.documentElement).getPropertyValue("--titlebar-height").trim();
    return {
      titlebar,
      innerWidth: window.innerWidth,
      innerHeight: window.innerHeight,
      contentRect: contentRect && { left: contentRect.left, top: contentRect.top, width: contentRect.width, height: contentRect.height },
      canvasRect: rect && { left: rect.left, top: rect.top, width: rect.width, height: rect.height, cx: rect.left + rect.width / 2, cy: rect.top + rect.height / 2 },
      panelRect: panel && (() => { const r = panel.getBoundingClientRect(); return { left: r.left, width: r.width, right: r.right }; })(),
      sphere: stage.sphere ?? null,
      selected: stage.selected ?? null,
      selectedLabel: stage.selectedLabel ?? null,
      mp: document.querySelector(".planet-view")?.getAttribute("data-mp") ?? null,
      managing: document.querySelector(".planet-view")?.classList.contains("managing") ?? null,
      stacked: document.querySelector(".planet-view")?.classList.contains("stacked") ?? null,
      dataView: document.querySelector(".planet-view")?.getAttribute("data-view") ?? null,
      dataTab: document.querySelector(".planet-view")?.getAttribute("data-tab") ?? null,
      docScrollWidth: document.documentElement.scrollWidth,
      docClientWidth: document.documentElement.clientWidth,
      contentScrollWidth: content?.scrollWidth ?? null,
      contentClientWidth: content?.clientWidth ?? null,
      contentOverflowY: content ? getComputedStyle(content).overflowY : null,
      pendingCount: document.querySelector("[data-mp-aux=pending]")?.getAttribute("data-mp-pending-count") ?? null,
    };
  });
  report.steps.push({ label, ...data });
  return data;
}

const page = await browser.newPage({ viewport: { width: 1440, height: 970 } });
page.on("pageerror", (e) => console.log("[pageerror]", String(e.message).slice(0, 300)));
await page.goto(`${BASE}/?fresh=${Date.now()}#/`, { waitUntil: "domcontentloaded" });
await page.waitForSelector("#composer-input", { timeout: 20000 });
// 等真实星球场景接管入口小球（WebGL 软件渲染较慢）
await page.waitForFunction(() => (window.__qioPlanetStage?.() ?? {}).ball === true, { timeout: 30000 }).catch(() => console.log("[warn] 等球态超时"));
await wait(800);
await page.$eval(".dock", (el) => el.click());
await page.waitForFunction(() => (window.__qioPlanetStage?.() ?? {}).phase === "ready", { timeout: 30000 }).catch(() => console.log("[warn] 等 ready 超时"));

/** 等球体屏幕半径稳定（= 聚焦补间结束）：半径变化 < 0.5px 连续两次就认为稳了 */
async function waitStableSphere(maxMs = 4000) {
  let last = null;
  const started = Date.now();
  while (Date.now() - started < maxMs) {
    const radius = await page.evaluate(() => (window.__qioPlanetStage?.() ?? {}).sphere?.radius ?? null);
    if (typeof radius === "number" && last !== null && Math.abs(radius - last) < 0.5) return radius;
    last = radius;
    await wait(150);
  }
  return last;
}
report.stableRadius = await waitStableSphere();
await wait(300);

const normal = await measure(page, "default-1440x970");
await page.screenshot({ path: resolve(HERE, "01-default-1440x970.png") });

// 展开管理区：采样 --mp 证明「有过程」（不是瞬跳），并记录中间值
/** 展开/收起过程中采样 data-mp（每 40ms 一次，最多 durationMs） */
async function sampleMp(durationMs) {
  const samples = [];
  const started = Date.now();
  while (Date.now() - started < durationMs) {
    const v = await page.evaluate(() => document.querySelector(".planet-view")?.getAttribute("data-mp") ?? null);
    samples.push(v === null ? null : Number(v));
    await wait(40);
  }
  return samples;
}

const expandSamples = await (async () => {
  const sampling = sampleMp(900);
  await page.click("[data-mp-manage-toggle]");
  return sampling;
})();
const intermediate = expandSamples.filter((v) => typeof v === "number" && v > 0 && v < 1);
report.mpExpandSamples = expandSamples;
report.mpExpandIntermediateCount = intermediate.length;
await wait(1200);
const managing = await measure(page, "managing-1440x970");
await page.screenshot({ path: resolve(HERE, "02-managing-1440x970.png") });

// 知识标签（B 的面板，只确认能切换过去且不触发换景）
await page.click('[data-mp-tab="knowledge"]');
await wait(600);
await measure(page, "knowledge-tab");
await page.screenshot({ path: resolve(HERE, "03-knowledge-tab.png") });

// 记忆 → 话题概览
await page.click('[data-mp-tab="memory"]');
await wait(500);
const firstTopic = await page.$("[data-topic-id]");
if (firstTopic) {
  await firstTopic.click();
  await wait(700);
  await measure(page, "memory-overview");
  await page.screenshot({ path: resolve(HERE, "04-memory-overview.png") });
}

// 辅助入口：待处理 / 整理记录（B 的面板，确认同一块内容区域能承载并返回）
await page.click('[data-mp-aux="pending"]');
await wait(800);
await measure(page, "pending-view");
await page.screenshot({ path: resolve(HERE, "05a-pending-view.png") });
await page.click(".ma-back");
await wait(400);

// 全局搜索（用真实数据里存在的话题名做关键词，保证能看到「有结果」的分组态）
await page.click("[data-mp-search-open]");
await wait(400);
await page.fill("[data-mp-search-input]", "默认");
await wait(900);
const search = await measure(page, "global-search");
await page.screenshot({ path: resolve(HERE, "05b-global-search-results.png") });
await page.fill("[data-mp-search-input]", "zzz-没有这个关键词");
await wait(900);
await measure(page, "global-search-empty");
await page.screenshot({ path: resolve(HERE, "05c-global-search-empty.png") });

// 收起管理区（回到默认浏览态）：同样采样收起过程
const collapseSamples = await (async () => {
  const sampling = sampleMp(900);
  await page.click("[data-mp-manage-toggle]");
  return sampling;
})();
report.mpCollapseIntermediateCount = collapseSamples.filter((v) => typeof v === "number" && v > 0 && v < 1).length;
await wait(1200);
const collapsed = await measure(page, "collapsed-1440x970");
await page.screenshot({ path: resolve(HERE, "06-collapsed-1440x970.png") });

// 窄窗口：上下排布（管理内容排到星球下方，内容区整体一层纵向滚动）
await page.setViewportSize({ width: 900, height: 820 });
await wait(600);
await page.click("[data-mp-manage-toggle]");
await wait(1400);
const narrow = await measure(page, "narrow-900x820-stacked");
await page.screenshot({ path: resolve(HERE, "07-narrow-900x820.png"), fullPage: false });

// 窄窗滚动：内容区能纵向滚、横向仍不滚
report.narrowScroll = await page.evaluate(() => {
  const content = document.querySelector(".planet-content");
  if (!content) return null;
  content.scrollTop = 240;
  return {
    scrollTop: content.scrollTop,
    scrollHeight: content.scrollHeight,
    clientHeight: content.clientHeight,
    scrollWidth: content.scrollWidth,
    clientWidth: content.clientWidth,
  };
});
await page.screenshot({ path: resolve(HERE, "08-narrow-scrolled.png"), fullPage: false });

writeFileSync(resolve(HERE, "report.json"), JSON.stringify(report, null, 2), "utf8");

// 结论摘要（人眼可读）
const line = (label, v) => console.log(label.padEnd(34), JSON.stringify(v));
console.log("\n== 关键读数 ==");
line("默认 球心(内容区坐标)", normal.sphere && { cx: normal.sphere.cx, cy: normal.sphere.cy, r: Math.round(normal.sphere.radius) });
line("默认 选中话题标签位置", normal.selectedLabel && { x: Math.round(normal.selectedLabel.x), y: Math.round(normal.selectedLabel.y) });
line("管理 选中话题标签位置", managing.selectedLabel && { x: Math.round(managing.selectedLabel.x), y: Math.round(managing.selectedLabel.y) });
line("默认 内容区", normal.contentRect);
line("管理 canvas 中心 x", managing.canvasRect?.cx?.toFixed(1));
line("管理 球体半径/默认半径", [Math.round(managing.sphere?.radius ?? 0), Math.round(normal.sphere?.radius ?? 0)]);
line("管理 管理区宽度/左边界", [Math.round(managing.panelRect?.width ?? 0), managing.panelRect?.left]);
line("管理 横向溢出(doc)", managing.docScrollWidth - managing.docClientWidth);
line("窄窗 stacked", narrow.stacked);
line("窄窗 横向溢出(content)", (narrow.contentScrollWidth ?? 0) - (narrow.contentClientWidth ?? 0));
line("窄窗 overflowY", narrow.contentOverflowY);
line("收起后 球心", collapsed.sphere && { cx: collapsed.sphere.cx, cy: collapsed.sphere.cy });
line("搜索视图 data-view", search.dataView);
line("展开过程中间值个数", report.mpExpandIntermediateCount);
line("收起过程中间值个数", report.mpCollapseIntermediateCount);
line("窄窗滚动(纵向可滚/横向溢出)", report.narrowScroll && [report.narrowScroll.scrollTop, report.narrowScroll.scrollWidth - report.narrowScroll.clientWidth]);

await browser.close();
