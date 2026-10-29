/**
 * 逐帧验收：管理过渡的卡顿/跳变只能按帧看（Lead 的判据同样按帧）。
 *
 * 判定口径：
 * - 从**点击那一刻**开始算（不是从采样开始），expand / collapse 各自：
 *   · 曲线前 300ms 内至少出现 2 个中间值（0 < mp < 1）；
 *   · maxStep 越小越好（受环境合成吞吐限制：本机 headless swiftshader 在画布 transform
 *     期间只有 ~10fps，单帧就吃掉 0.1–0.6 的进度，所以这里同时报告帧间隔）；
 * - 几何不变式：canvas 中心 x 720 → 0、半径不变、mp 0 → 1。
 *
 * 用法（前端 dev 服务在跑）：
 *   node docs/mp-verify/a-page/frames-mp-a.mjs [baseUrl]
 * 输出：同目录 frames-report.json
 */
import { createRequire } from "node:module";
import { existsSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const require = createRequire(import.meta.url);
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
  throw new Error("找不到 playwright-core（设置 QIO_PLAYWRIGHT_CORE 或 NODE_PATH）");
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
const page = await browser.newPage({ viewport: { width: 1440, height: 970 } });
page.on("pageerror", (e) => console.log("[pageerror]", String(e.message).slice(0, 300)));
await page.goto(`${BASE}/?fresh=${Date.now()}#/`, { waitUntil: "domcontentloaded" });
await page.waitForSelector("#composer-input", { timeout: 20000 });
await page.waitForFunction(() => (window.__qioPlanetStage?.() ?? {}).ball === true, { timeout: 30000 }).catch(() => {});
await wait(700);
await page.$eval(".dock", (el) => el.click());
await page.waitForFunction(() => (window.__qioPlanetStage?.() ?? {}).phase === "ready", { timeout: 30000 }).catch(() => {});
// 等聚焦补间结束（半径稳定）+ 管理区预挂载完成
let lastRadius = null;
for (let i = 0; i < 40; i++) {
  const radius = await page.evaluate(() => (window.__qioPlanetStage?.() ?? {}).sphere?.radius ?? null);
  if (typeof radius === "number" && lastRadius !== null && Math.abs(radius - lastRadius) < 0.5) break;
  lastRadius = radius;
  await wait(120);
}
await wait(700);

function summarize(samples, clickedAt) {
  const pts = samples
    .filter((s) => s.t >= clickedAt - 4)
    .map((s) => ({ dt: +(s.t - clickedAt).toFixed(1), mp: s.mp, cx: s.cx }));
  let maxStep = 0;
  let maxStepAt = null;
  for (let i = 1; i < pts.length; i++) {
    const step = Math.abs(pts[i].mp - pts[i - 1].mp);
    if (step > maxStep) {
      maxStep = step;
      maxStepAt = pts[i].dt;
    }
  }
  const gaps = pts.slice(1).map((p, i) => p.dt - pts[i].dt).filter((g) => g > 0);
  const intermediate = pts.filter((p) => p.mp > 0.001 && p.mp < 0.999);
  const first = pts.find((p) => p.mp > 0.001);
  const settled = pts.find((p) => p.mp >= 0.999 || p.mp <= 0.001);
  return {
    frames: pts.length,
    medianFrameGapMs: gaps.length ? +gaps.sort((a, b) => a - b)[Math.floor(gaps.length / 2)].toFixed(1) : null,
    maxStep: +maxStep.toFixed(3),
    maxStepAtMs: maxStepAt,
    intermediateCount: intermediate.length,
    intermediateInFirst300ms: intermediate.filter((p) => p.dt <= 300).length,
    firstProgressAtMs: first ? first.dt : null,
    settledAtMs: settled ? settled.dt : null,
    startMp: pts[0]?.mp ?? null,
    startCenterX: pts[0]?.cx ?? null,
    endMp: pts.at(-1)?.mp ?? null,
    endCenterX: pts.at(-1)?.cx ?? null,
    first12: pts.slice(0, 12),
  };
}

const report_extra = {};

/** 把管理区归位到指定状态（0 = 收起，1 = 展开），返回后测量才有意义 */
async function ensureState(target) {
  for (let i = 0; i < 20; i++) {
    const mp = await page.evaluate(() =>
      Number(document.querySelector(".planet-view")?.getAttribute("data-mp") ?? 0),
    );
    const ok = target === 1 ? mp > 0.999 : mp < 0.001;
    if (ok) return;
    await page.evaluate(() => document.querySelector("[data-mp-manage-toggle]")?.click());
    await wait(1300);
  }
  throw new Error("管理区状态归位失败：" + target);
}

async function measureTransition() {
  await page.evaluate(() => {
    window.__mpSamples = [];
    window.__mpOn = true;
    const loop = () => {
      const root = document.querySelector(".planet-view");
      window.__mpSamples.push({
        t: performance.now(),
        mp: Number(root?.getAttribute("data-mp") ?? 0),
        cx: document.querySelector(".planet-canvas")?.getBoundingClientRect().left ?? null,
      });
      if (window.__mpOn) requestAnimationFrame(loop);
    };
    requestAnimationFrame(loop);
  });
  await wait(150); // 先跑几帧基线
  const clickedAt = await page.evaluate(() => {
    const t0 = performance.now();
    document.querySelector("[data-mp-manage-toggle]").click();
    return t0;
  });
  await wait(1500);
  const samples = await page.evaluate(() => {
    window.__mpOn = false;
    return window.__mpSamples;
  });
  return summarize(samples, clickedAt);
}

const radius = await page.evaluate(() => (window.__qioPlanetStage?.() ?? {}).sphere?.radius ?? null);

// 预热：新页面头几次过渡会碰到合成器/图层预热；同时这台机器是共享的（其他任务在跑），
// 帧间隔会出现「7ms/帧」与「~100ms/帧」两个模态。先把热身帧跑掉，再取 3 次的中位。
for (let i = 0; i < 2; i++) {
  await ensureState(0);
  await measureTransition();
}
const expandRuns = [];
for (let i = 0; i < 3; i++) {
  await ensureState(0);
  expandRuns.push(await measureTransition());
}
expandRuns.sort((a, b) => a.maxStep - b.maxStep);
const expand = expandRuns[Math.floor(expandRuns.length / 2)];
report_extra.expandRuns = expandRuns.map((r) => ({ maxStep: r.maxStep, medianFrameGapMs: r.medianFrameGapMs, intermediateInFirst300ms: r.intermediateInFirst300ms }));
await ensureState(1);
const expanded = await page.evaluate(() => ({
  mp: Number(document.querySelector(".planet-view")?.getAttribute("data-mp") ?? 0),
  centerX: document.querySelector(".planet-canvas")?.getBoundingClientRect().left ?? null,
  radius: (window.__qioPlanetStage?.() ?? {}).sphere?.radius ?? null,
  panelRect: (() => { const r = document.querySelector("aside.panel")?.getBoundingClientRect(); return r ? { left: r.left, width: r.width } : null; })(),
}));
const collapseRuns = [];
for (let i = 0; i < 3; i++) {
  await ensureState(1);
  collapseRuns.push(await measureTransition());
}
collapseRuns.sort((a, b) => a.maxStep - b.maxStep);
const collapse = collapseRuns[Math.floor(collapseRuns.length / 2)];
report_extra.collapseRuns = collapseRuns.map((r) => ({ maxStep: r.maxStep, medianFrameGapMs: r.medianFrameGapMs, intermediateInFirst300ms: r.intermediateInFirst300ms }));
await ensureState(0);
const collapsedState = await page.evaluate(() => ({
  mp: Number(document.querySelector(".planet-view")?.getAttribute("data-mp") ?? 0),
  centerX: document.querySelector(".planet-canvas")?.getBoundingClientRect().left ?? null,
  radius: (window.__qioPlanetStage?.() ?? {}).sphere?.radius ?? null,
  docOverflowX: document.documentElement.scrollWidth - document.documentElement.clientWidth,
}));

// 单次 dispatchEvent 是否只切换一次（Lead 反馈怀疑双监听）
const dispatchOnce = await page.evaluate(async () => {
  const btn = document.querySelector("[data-mp-manage-toggle]");
  const before = btn?.getAttribute("aria-expanded");
  btn?.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true }));
  await new Promise((r) => setTimeout(r, 30));
  return { before, after: btn?.getAttribute("aria-expanded") };
});
await wait(1200);
const afterDispatch = await page.evaluate(() => ({
  mp: Number(document.querySelector(".planet-view")?.getAttribute("data-mp") ?? 0),
  expanded: document.querySelector("[data-mp-manage-toggle]")?.getAttribute("aria-expanded"),
}));

const report = { radiusBefore: radius, expand, expanded, collapse, collapsedState, dispatchOnce, afterDispatch, runs: report_extra }; 
writeFileSync(resolve(HERE, "frames-report.json"), JSON.stringify(report, null, 2), "utf8");

const line = (k, v) => console.log(k.padEnd(32), JSON.stringify(v));
console.log("\n== 逐帧验收（从点击起算） ==");
line("expand.maxStep / 出现时刻", [expand.maxStep, expand.maxStepAtMs]);
line("expand 中间值(总数/前300ms)", [expand.intermediateCount, expand.intermediateInFirst300ms]);
line("expand 首帧进度 / 首帧间隔", [expand.first12[0]?.mp, expand.medianFrameGapMs]);
line("expand 起止", [expand.startMp, expand.endMp]);
line("expand centerX", [expand.startCenterX, expand.endCenterX]);
line("collapse.maxStep / 出现时刻", [collapse.maxStep, collapse.maxStepAtMs]);
line("collapse 中间值(总数/前300ms)", [collapse.intermediateCount, collapse.intermediateInFirst300ms]);
line("collapse 起止", [collapse.startMp, collapse.endMp]);
line("collapse centerX", [collapse.startCenterX, collapse.endCenterX]);
line("展开态 球半径/面板", [Math.round(expanded.radius ?? 0), expanded.panelRect]);
line("收起态 半径/横向溢出", [Math.round(collapsedState.radius ?? 0), collapsedState.docOverflowX]);
line("单次 dispatchEvent 切换", dispatchOnce);
line("dispatch 之后状态", afterDispatch);

await browser.close();
