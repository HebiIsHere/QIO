// 用系统 Edge 无头渲染星球原型，输出三张截图：
// overview（远景）/ focus（话题聚焦）/ planet（全景）
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";
import { mkdirSync } from "node:fs";

const require = createRequire(import.meta.url);
const PW = "C:\\Users\\zxy\\.cache\\codex-runtimes\\codex-primary-runtime\\dependencies\\node\\node_modules\\playwright";
const { chromium } = require(PW);

const outDir = fileURLToPath(new URL("../prototypes/planet/shots/", import.meta.url));
mkdirSync(outDir, { recursive: true });
const BASE = "http://127.0.0.1:5199/prototypes/planet/";

const browser = await chromium.launch({ channel: "msedge", headless: true });
try {
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  await page.goto(BASE, { waitUntil: "networkidle", timeout: 30000 });
  await page.waitForTimeout(2500);
  const status = await page.textContent("#status");
  console.log("status:", status);

  await page.screenshot({ path: outDir + "1-overview.png" });
  console.log("shot: overview");

  // 点击列表第一项 → 自动旋转聚焦
  await page.click("#topic-list li:nth-child(3)");
  await page.waitForTimeout(1200);
  await page.screenshot({ path: outDir + "2-focus.png" });
  console.log("shot: focus (", await page.textContent("#d-title"), ")");

  // 星球全景
  await page.click("#btn-planet");
  await page.waitForTimeout(1000);
  await page.screenshot({ path: outDir + "3-planet.png" });
  console.log("shot: planet");

  // 远景（悬浮球视角）
  await page.click("#btn-overview");
  await page.waitForTimeout(1000);
  await page.screenshot({ path: outDir + "4-overview2.png" });
  console.log("shot: overview2");
} finally {
  await browser.close();
}