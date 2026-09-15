// 星球「展开 / 收起」逐帧取证：用于对比改动前后的过渡是否连贯。
// 用法（服务需在跑）：
//   node scripts/baseline/planet-frames.mjs before
//   node scripts/baseline/planet-frames.mjs after
// 输出：%TEMP%\qio-baseline\planet\<tag>-{open,close}-<n>.png 与 timeline-<tag>.json
import { createRequire } from "node:module";
import { mkdirSync, writeFileSync } from "node:fs";

const require = createRequire(import.meta.url);
const PW = "C:\\Users\\zxy\\.cache\\codex-runtimes\\codex-primary-runtime\\dependencies\\node\\node_modules\\playwright";
const { chromium } = require(PW);

const BASE = "http://127.0.0.1:5199";
const OUT = `${process.env.TEMP}\\qio-baseline\\planet`;
mkdirSync(OUT, { recursive: true });
const tag = process.argv[2] ?? "run";
/** --slow：用 ?planetdemo=slow 打开（时间线 ×3），并按放慢后的时间点取样 */
const slow = process.argv.includes("--slow");
const wait = (ms) => new Promise((r) => setTimeout(r, ms));

const browser = await chromium.launch({
  channel: "msedge",
  headless: true,
  args: ["--use-gl=angle", "--use-angle=swiftshader", "--enable-unsafe-swiftshader"],
});
const context = await browser.newContext({ viewport: { width: 1280, height: 800 } });
const page = await context.newPage();
const timeline = { tag, open: [], close: [], openTotalMs: null, closeTotalMs: null };

async function shot(kind, i, t) {
  const file = `${OUT}\\${tag}-${kind}-${i}.png`;
  await page.screenshot({ path: file });
  return { file, atMs: t };
}

async function run(kind, fire) {
  const samples = [];
  const t0 = Date.now();
  await fire();
  // --slow：配合 ?planetdemo=slow（时间线放慢 3 倍），取 6 个时间点看阶段顺序
  const marks = slow ? [60, 260, 460, 660, 860, 1060] : [40, 170, 320, 480];
  for (const target of marks) {
    const now = Date.now() - t0;
    if (target > now) await wait(target - now);
    samples.push(await shot(kind, samples.length, Date.now() - t0));
  }
  return samples;
}

// 打开：点悬浮球 → 覆盖层出现
await page.goto(
  `${BASE}/?fresh=${Math.floor(Date.now() / 1000)}${slow ? "&planetdemo=slow" : ""}#/`,
  { waitUntil: "domcontentloaded" },
);
await page.waitForSelector(".dock", { timeout: 15000 });
await page.waitForTimeout(1200);
timeline.open = await run("open", async () => {
  await page.locator(".dock").click({ noWaitAfter: true });
});
timeline.openTotalMs = await page.evaluate(() => Math.round(performance.now()));
await page.waitForTimeout(1500);

// 收起：点「收起星球」→ 覆盖层从 DOM 移除
const closeTotal = await page.evaluate(async () => {
  const t0 = performance.now();
  document.querySelector(".close-btn")?.click();
  return await new Promise((res) => {
    const tick = () => {
      if (!document.querySelector(".planet-view")) res(Math.round(performance.now() - t0));
      else requestAnimationFrame(tick);
    };
    tick();
  });
});
timeline.closeTotalMs = closeTotal;

// 关闭过程单独再采一次（上面那次按真实结束计时，这里补画面）
await page.waitForTimeout(800);
await page.locator(".dock").click();
await page.waitForTimeout(1600);
timeline.close = await run("close", async () => {
  await page.locator(".close-btn").click({ noWaitAfter: true });
});
await page.waitForTimeout(800);

writeFileSync(`${OUT}\\timeline-${tag}.json`, JSON.stringify(timeline, null, 2), "utf8");
console.log(JSON.stringify(timeline, null, 2));
await browser.close();
