// e2e：悬浮球 → 星球页 → 话题列表 → 详情 → 从这里开始
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";
import { mkdirSync } from "node:fs";

const require = createRequire(import.meta.url);
const PW = "C:\\Users\\zxy\\.cache\\codex-runtimes\\codex-primary-runtime\\dependencies\\node\\node_modules\\playwright";
const { chromium } = require(PW);
const outDir = fileURLToPath(new URL("../e2e-shots/", import.meta.url));
mkdirSync(outDir, { recursive: true });

const browser = await chromium.launch({ channel: "msedge", headless: true });
try {
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  await page.goto("http://127.0.0.1:5199/", { waitUntil: "networkidle", timeout: 30000 });
  await page.waitForTimeout(2000);

  const dock = page.locator(".dock");
  await dock.waitFor({ state: "visible", timeout: 15000 });
  const rect = await dock.boundingBox();
  console.log("dock rect:", JSON.stringify(rect));
  await dock.click({ timeout: 15000 });
  await page.waitForTimeout(3500); // 场景初始化 + 数据加载

  const hud = await page.textContent(".hud");
  console.log("hud:", hud?.replace(/\s+/g, " ").trim());
  const topicCount = await page.locator(".topic-list li").count();
  console.log("topic list count:", topicCount);

  if (topicCount > 0) {
    await page.click(".topic-list li:nth-child(1)");
    await page.waitForTimeout(2000);
    const detailName = await page.textContent(".detail h3");
    const fragmentCount = await page.locator(".fragment-item").count();
    console.log("detail:", detailName, "| fragments:", fragmentCount);
    const startBtn = await page.textContent(".start-btn");
    console.log("start button:", startBtn);
    await page.screenshot({ path: outDir + "planet-full.png" });
    await page.click(".start-btn");
    await page.waitForTimeout(1500);
    const planetClosed = (await page.locator(".planet-view").count()) === 0;
    console.log("planet closed after start-here:", planetClosed);
  }
} finally {
  await browser.close();
}