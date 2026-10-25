/**
 * E 独立验收 · 480x600 下三条同类审批的可读性 / 可区分 / 可键盘操作。
 *
 * 独立于 surface 脚本（同一标签页连续导航后，旧事件订阅会让注入事件悬空；
 * 本脚本用干净上下文 + 全新页面，只做这一组取证）。
 *
 * 用法（服务由 scripts/e2e_up.py 起好）：
 *   $env:QIO_ACCEPT_UI="http://127.0.0.1:5199"; $env:QIO_ACCEPT_API="http://127.0.0.1:8734";
 *   node scripts/pm-acceptance/approvals_480.mjs
 */
import { mkdirSync, writeFileSync } from "node:fs";
import { pathToFileURL } from "node:url";
import { execSync } from "node:child_process";

const PLAYWRIGHT = "C:/Users/zxy/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright/index.mjs";
const CHROME = "C:/Program Files/Google/Chrome/Application/chrome.exe";
const UI = (process.env.QIO_ACCEPT_UI || "http://127.0.0.1:5199").replace(/\/$/, "");
const API = (process.env.QIO_ACCEPT_API || "http://127.0.0.1:8734").replace(/\/$/, "");
const OUT = process.env.QIO_ACCEPT_OUT || "D:/qio-dev/_fixpm/evidence/E/shots";
const HEADLESS = (process.env.QIO_ACCEPT_HEADLESS || "1") !== "0";
mkdirSync(OUT, { recursive: true });

function sha() {
  try {
    return execSync("git rev-parse HEAD", { cwd: process.cwd(), encoding: "utf8" }).trim();
  } catch {
    return "unknown";
  }
}

const results = [];
const SH = sha();

function record(step, expected, actual, ok, extra = {}) {
  results.push({ step, expected: String(expected), actual: String(actual), ok, sha: SH, ...extra });
  console.log((ok ? "PASS" : "FAIL") + " | " + step + " | 期望：" + expected + " | 实际：" + actual);
}

async function post(path, body) {
  const response = await fetch(API + path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body ?? {}) });
  const text = await response.text();
  if (!response.ok) throw new Error("POST " + path + " -> " + response.status + " " + text.slice(0, 300));
  return text ? JSON.parse(text) : null;
}

const STAMP = new Date().toISOString();
const similar = [
  { id: "chips_1", label: "修改发布材料：第 1 章 摘要（D:\\work\\发布材料-终稿-第一版.docx）" },
  { id: "chips_2", label: "修改发布材料：第 1 章 摘要（D:\\work\\发布材料-终稿-第二版.docx）" },
  { id: "chips_3", label: "修改发布材料：第 1 章 摘要（D:\\work\\发布材料-终稿-第三版.docx）" },
];

const { chromium } = await import(pathToFileURL(PLAYWRIGHT).href);
await post("/api/onboarding/complete", {});
const browser = await chromium.launch({ headless: HEADLESS, executablePath: CHROME, args: ["--no-first-run", "--disable-gpu"] });
const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
const page = await context.newPage();
const consoleErrors = [];
page.on("console", (message) => {
  if (message.type() === "error") consoleErrors.push(message.text().slice(0, 300));
});
page.on("pageerror", (error) => consoleErrors.push("pageerror: " + String(error).slice(0, 300)));

await page.goto(UI + "/#/interactive", { waitUntil: "domcontentloaded" });
await page.waitForFunction(() => !document.querySelector(".boot-note") && (document.querySelector("#app")?.textContent || "").trim().length > 20, undefined, { timeout: 30000 });
await page.waitForTimeout(2500);
await page.evaluate(() => document.activeElement?.blur?.());
const injected = [];
for (const item of similar) {
  injected.push(
    await post("/api/events/test?event_type=APPROVAL_REQUIRED", {
      approval: {
        approval_id: item.id,
        kind: "tool_execution",
        payload: { description: item.label, name: item.label },
        label: item.label,
        purpose: "验收：修改发布材料",
        source: "tool",
        state: "waiting",
        requires_confirmations: 1,
        confirmed_stage: 0,
        created_at: STAMP,
        expires_at: STAMP,
        turn_id: null,
        session_id: null,
        request_digest: null,
      },
    }),
  );
}
await page.waitForTimeout(2000);
if ((await page.locator("[data-unified-approval-modal]").count()) === 0) {
  const expand = page.getByText("展开对话", { exact: false });
  if ((await expand.count()) > 0) await expand.first().click().catch(() => undefined);
  await page.waitForTimeout(600);
  const entry = page.locator('[data-entry="approvals"]');
  if ((await entry.count()) > 0) await entry.first().click().catch(() => undefined);
  await page.waitForTimeout(600);
}
const modalCount = await page.locator("[data-unified-approval-modal]").count();
await page.screenshot({ path: OUT + "/09-approvals-modal-1440.png" });
record("三条审批在 1440x900 打开统一弹窗", "modal 存在", "modal 数=" + modalCount + " 注入=" + injected.length, modalCount > 0);

await page.setViewportSize({ width: 480, height: 600 });
await page.waitForTimeout(800);
const layout = await page.evaluate(() => {
  const chips = Array.from(document.querySelectorAll(".ua-chip")).map((node) => {
    const rect = node.getBoundingClientRect();
    const style = window.getComputedStyle(node);
    node.focus();
    return {
      text: (node.textContent || "").trim(),
      title: node.getAttribute("title"),
      width: Math.round(rect.width),
      height: Math.round(rect.height),
      scrollWidth: node.scrollWidth,
      clientWidth: node.clientWidth,
      overflowX: style.overflowX,
      textOverflow: style.textOverflow,
      whiteSpace: style.whiteSpace,
      focusable: document.activeElement === node,
    };
  });
  const modal = document.querySelector(".ua-modal");
  const rect = modal ? modal.getBoundingClientRect() : null;
  return {
    chips,
    modal: rect ? { left: Math.round(rect.left), top: Math.round(rect.top), width: Math.round(rect.width), height: Math.round(rect.height) } : null,
    docScrollWidth: document.documentElement.scrollWidth,
    docClientWidth: document.documentElement.clientWidth,
    bodyScrollWidth: document.body.scrollWidth,
  };
});
await page.screenshot({ path: OUT + "/10-approvals-480x600.png" });
record(
  "480x600：三条同类审批标签不裁切内容",
  "chip.scrollWidth <= chip.clientWidth（或 title 提供完整文本）",
  JSON.stringify(layout.chips.map((chip) => [chip.width + "x" + chip.height, chip.scrollWidth + "/" + chip.clientWidth, chip.text.slice(0, 30)])),
  layout.chips.length > 0 && layout.chips.every((chip) => chip.scrollWidth <= chip.clientWidth + 1 || chip.title),
  { viewport: "480x600" },
);
record(
  "480x600：同类型审批可区分",
  "三个标签文字互不相同",
  JSON.stringify(layout.chips.map((chip) => chip.text)),
  new Set(layout.chips.map((chip) => chip.text)).size === layout.chips.length && layout.chips.length > 0,
  { viewport: "480x600" },
);
record(
  "480x600：标签可键盘聚焦",
  "每个 chip 都能获得焦点（原生 button）",
  JSON.stringify(layout.chips.map((chip) => chip.focusable)),
  layout.chips.length > 0 && layout.chips.every((chip) => chip.focusable),
  { viewport: "480x600" },
);
record(
  "480x600：无横向溢出",
  "document.scrollWidth <= clientWidth",
  layout.docScrollWidth + "/" + layout.docClientWidth,
  layout.docScrollWidth <= layout.docClientWidth + 1,
  { viewport: "480x600" },
);

if ((await page.locator(".ua-chip").count()) > 1) {
  await page.locator(".ua-chip").nth(1).click();
  await page.waitForTimeout(500);
  const selected = await page.evaluate(() =>
    Array.from(document.querySelectorAll(".ua-chip")).map((node) => node.getAttribute("aria-selected")),
  );
  await page.screenshot({ path: OUT + "/11-approvals-480-switched.png" });
  record(
    "480x600：可切换审批项",
    "点击第二项后 aria-selected 跟随",
    JSON.stringify(selected),
    selected.filter((value) => value === "true").length === 1 && selected[1] === "true",
    { viewport: "480x600" },
  );
}

writeFileSync(OUT + "/approvals_480.json", JSON.stringify({ sha: SH, at: STAMP, injected, layout, consoleErrors, results }, null, 2), "utf8");
const failed = results.filter((item) => item.ok === false);
console.log("\n结果：" + results.length + " 项，失败 " + failed.length);
await context.close();
await browser.close();
process.exit(failed.length ? 1 : 0);
