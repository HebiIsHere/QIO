// 耗时面板的视觉检查（Playwright + 系统 Edge，无新增依赖）。
// 用法：先起演示服务，再 node visual/probe.mjs
//   npx vite preview --config vite.visual.config.ts
// 证据：<worktree>\.visual-out\shots\*.png + 控制台错误清单。
import { createRequire } from "node:module";
import { mkdirSync, writeFileSync } from "node:fs";
import { resolve } from "node:path";

const require = createRequire(import.meta.url);
const PW = "C:\\Users\\zxy\\.cache\\codex-runtimes\\codex-primary-runtime\\dependencies\\node\\node_modules\\playwright";
const { chromium } = require(PW);

const BASE = process.env.VISUAL_BASE ?? "http://127.0.0.1:5299/timing-demo.html";
const OUT = resolve(process.env.VISUAL_OUT ?? "./.visual-out/shots");
mkdirSync(OUT, { recursive: true });

const browser = await chromium.launch({ channel: "msedge", headless: true });
const results = [];
const consoleErrors = [];

async function shoot(name, width, height) {
  const context = await browser.newContext({ viewport: { width, height } });
  const page = await context.newPage();
  page.on("console", (m) => {
    if (m.type() === "error") consoleErrors.push(`${name}: ${m.text().slice(0, 200)}`);
  });
  page.on("pageerror", (e) => consoleErrors.push(`${name}: pageerror ${String(e.message).slice(0, 200)}`));
  page.on("response", (r) => {
    if (r.status() >= 400) consoleErrors.push(`${name}: HTTP ${r.status()} ${r.url()}`);
  });
  await page.goto(BASE, { waitUntil: "domcontentloaded" });
  await page.waitForSelector("[data-test='turn-timing']", { timeout: 15000 });

  // 逐个展开：走真实交互路径（click → toggle → 拉取（假数据）→ 渲染）
  const summaries = await page.$$("[data-test='turn-timing'] summary");
  for (const s of summaries) {
    await s.click();
    await page.waitForTimeout(120);
  }
  await page.waitForTimeout(300);

  const rows = await page.$$eval(".tt-row", (els) =>
    els.map((el) => ({
      label: el.querySelector(".tt-label")?.textContent?.trim(),
      value: el.querySelector(".tt-value")?.textContent?.trim(),
      width: el.querySelector(".tt-fill")?.getAttribute("style"),
    })),
  );
  const notes = await page.$$eval(".tt-note", (els) => els.map((el) => el.textContent.trim()));
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);

  const file = resolve(OUT, `${name}.png`);
  writeFileSync(file, await page.screenshot({ fullPage: true }));
  results.push({ name, width, rows: rows.length, notes, overflowX: overflow, file });
  await context.close();
}

await shoot("timing-1280", 1280, 900);
await shoot("timing-900", 900, 700);

await browser.close();
console.log(JSON.stringify({ results, consoleErrors }, null, 1));
