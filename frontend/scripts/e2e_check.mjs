// e2e：对话页加载 + 连接状态 + 发送消息（无凭据警告）+ 设置页表单
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
  await page.waitForTimeout(2500);

  const status = await page.textContent(".status-bar");
  console.log("status-bar:", status.replace(/\s+/g, " ").trim());

  // 发送消息（无凭据 → 后端 WARNING）
  await page.fill("textarea", "你好，介绍一下自己");
  await page.click(".actions button");
  await page.waitForTimeout(3000);
  const msgs = await page.locator(".message").count();
  console.log("messages after send:", msgs);
  const body = await page.textContent(".conversation");
  console.log("has 你好:", body.includes("你好"));
  await page.screenshot({ path: outDir + "conversation.png" });

  // 设置页
  await page.click(".settings-link");
  await page.waitForTimeout(1200);
  const h1 = await page.textContent("header h1");
  console.log("settings title:", h1);
  await page.fill('input[placeholder="key_id（如 main-key）"]', "main-key");
  await page.fill('input[placeholder="API Key"]', "sk-e2e-test");
  await page.click(".form button");
  await page.waitForTimeout(1500);
  const creds = await page.locator(".cred-list li").count();
  console.log("credentials after create:", creds);
  await page.screenshot({ path: outDir + "settings.png" });
} finally {
  await browser.close();
}