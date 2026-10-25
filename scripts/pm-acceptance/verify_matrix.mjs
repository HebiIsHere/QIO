/**
 * E 独立验收 · 验收矩阵补充项（真实界面，不 mock 前端）
 *
 * - 矩阵 1：设置切换权限模式后，对话页与互动页显示一致、刷新后仍一致；
 * - 矩阵 2：两个聊天页 0 待审批仍能进入执行历史；
 * - 矩阵 5：pending 读取失败（用 Playwright 拦包返回 500）时可见失败与重试；
 * - 矩阵 6：内部数据三次确认进度来自后端（confirmed_stage=1/requires=3）。
 *
 * 用法：
 *   $env:QIO_ACCEPT_UI="http://127.0.0.1:5199"; $env:QIO_ACCEPT_API="http://127.0.0.1:8734";
 *   node scripts/pm-acceptance/verify_matrix.mjs
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
const consoleErrors = [];
const SH = sha();

function record(step, expected, actual, ok, extra = {}) {
  results.push({ step, expected: String(expected), actual: String(actual), ok, sha: SH, ...extra });
  console.log((ok ? "PASS" : "FAIL") + " | " + step + " | 期望：" + expected + " | 实际：" + actual);
}

async function rest(path, method = "GET", body) {
  const response = await fetch(API + path, {
    method,
    headers: body === undefined ? {} : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const text = await response.text();
  if (!response.ok) throw new Error("API " + method + " " + path + " -> " + response.status + " " + text.slice(0, 300));
  return text ? JSON.parse(text) : null;
}

async function waitReady(p) {
  await p.waitForFunction(
    () => !document.querySelector(".boot-note") && (document.querySelector("#app")?.textContent || "").trim().length > 20,
    undefined,
    { timeout: 30000 },
  );
  await p.waitForTimeout(1500);
}

async function goto(p, hash, waitRestore = true) {
  const restored = waitRestore
    ? p.waitForResponse((r) => r.url().includes("/api/runtime/state"), { timeout: 20000 }).catch(() => null)
    : Promise.resolve(null);
  await p.goto(UI + "/" + hash, { waitUntil: "domcontentloaded" });
  await waitReady(p);
  await restored;
  await p.waitForTimeout(1000);
}

const HISTORY_LABEL = /(执行结果历史|审批结果与历史|结果与历史|审批历史|执行历史|审批结果|执行结果)/;
async function historySurfaces(p) {
  return p.evaluate((source) => {
    const label = new RegExp(source);
    const hits = [];
    for (const el of Array.from(document.querySelectorAll("body *"))) {
      const attrs = ["aria-label", "title", "data-test", "data-im"].map((name) => el.getAttribute(name) || "").join(" ");
      const ownText = Array.from(el.childNodes)
        .filter((node) => node.nodeType === 3)
        .map((node) => node.textContent || "")
        .join(" ")
        .trim();
      const control =
        el.tagName === "BUTTON" || el.tagName === "A" || el.tagName === "SUMMARY" ||
        el.getAttribute("role") === "button" || el.getAttribute("role") === "tab";
      if (control && (label.test(attrs) || label.test(ownText))) {
        hits.push({ tag: el.tagName.toLowerCase(), text: (el.textContent || "").trim().slice(0, 120), attrs });
      }
    }
    return hits.slice(0, 10);
  }, HISTORY_LABEL.source);
}

async function badgeInfo(p) {
  return p.evaluate(() => {
    const nodes = Array.from(document.querySelectorAll('[data-im="permission-mode-badge"]'));
    return {
      count: nodes.length,
      texts: nodes.map((node) => (node.textContent || "").trim()),
    };
  });
}

const STAMP = new Date().toISOString();
function approvalPayload(id, label, extra = {}) {
  return {
    approval: {
      approval_id: id,
      kind: extra.kind || "tool_execution",
      payload: extra.payload || { description: label, name: label },
      label,
      purpose: extra.purpose || "验收：修改发布材料",
      source: extra.source || "tool",
      state: extra.state || "waiting",
      requires_confirmations: extra.requires_confirmations ?? 1,
      confirmed_stage: extra.confirmed_stage ?? 0,
      created_at: STAMP,
      expires_at: STAMP,
      turn_id: null,
      session_id: null,
      request_digest: null,
    },
  };
}
async function inject(type, data) {
  return rest("/api/events/test?event_type=" + encodeURIComponent(type), "POST", data);
}

const { chromium } = await import(pathToFileURL(PLAYWRIGHT).href);
await rest("/api/onboarding/complete", "POST", {});
const browser = await chromium.launch({ headless: HEADLESS, executablePath: CHROME, args: ["--no-first-run", "--disable-gpu"] });
const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
function attach(p) {
  p.on("console", (message) => {
    if (message.type() === "error") consoleErrors.push(message.text().slice(0, 300));
  });
  p.on("pageerror", (error) => consoleErrors.push("pageerror: " + String(error).slice(0, 300)));
}

// ---- 矩阵 2：0 待审批时两个聊天页的历史入口 ------------------------------
let page = await context.newPage();
attach(page);
await goto(page, "#/");
const conversationHistory = await historySurfaces(page);
const seedSummary = process.env.QIO_SEED_HISTORY_SUMMARY || "已执行：E-ACCEPT 种子历史（写入 2 个文件）";
if (conversationHistory.length > 0) {
  // 打开入口，确认不是「入口存在但内容是空的」
  await page.locator('[data-entry="approvals"]').first().click().catch(() => undefined);
  await page.waitForTimeout(1200);
}
const openedHistory = await page.evaluate(() => document.body.innerText || "");
await page.screenshot({ path: OUT + "/12-conversation-no-pending.png" });
record(
  "矩阵2｜对话页 0 待审批、有历史时仍能进入历史并读到那条真实记录",
  "存在历史入口，打开后能看到种子历史记录",
  JSON.stringify({ entries: conversationHistory, seenSeed: openedHistory.includes(seedSummary) }),
  conversationHistory.length > 0 && openedHistory.includes(seedSummary),
);

// ---- 矩阵 1：权限模式切换后在两页一致、刷新后仍一致 ----------------------
const original = await rest("/api/security/mode");
await rest("/api/security/mode", "PUT", { mode: "read_only", user_confirmed: true });
// 同一 URL 的 page.goto 不保证重新加载：模式是否被页面读到，必须用真正的刷新验证
await page.reload({ waitUntil: "domcontentloaded" });
await waitReady(page);
await page.waitForTimeout(1000);
const conversationMode = await badgeInfo(page);
record(
  "矩阵1｜对话页读到切换后的真实模式",
  "徽标文案包含「只读」",
  JSON.stringify(conversationMode),
  conversationMode.count > 0 && conversationMode.texts.some((text) => text.includes("只读")),
);
await goto(page, "#/interactive");
const interactiveMode = await badgeInfo(page);
await page.screenshot({ path: OUT + "/13-interactive-mode-readonly.png" });
record(
  "矩阵1｜互动页常驻显示同一模式（设置切换、刷新后）",
  "互动页徽标文案包含「只读」",
  JSON.stringify(interactiveMode),
  interactiveMode.count > 0 && interactiveMode.texts.some((text) => text.includes("只读")),
);
await page.reload({ waitUntil: "domcontentloaded" });
await waitReady(page);
const interactiveModeAfterRefresh = await badgeInfo(page);
record(
  "矩阵1｜刷新后互动页仍显示只读（不丢、不回到默认）",
  "刷新后徽标仍包含「只读」",
  JSON.stringify(interactiveModeAfterRefresh),
  interactiveModeAfterRefresh.count > 0 && interactiveModeAfterRefresh.texts.some((text) => text.includes("只读")),
);
await rest("/api/security/mode", "PUT", { mode: original.mode, user_confirmed: true });

// ---- 矩阵 5：pending 读取失败时可见错误 + 重试 --------------------------
const failurePage = await context.newPage();
attach(failurePage);
await failurePage.route("**/api/approvals/pending*", (route) =>
  route.fulfill({ status: 500, contentType: "application/json", body: JSON.stringify({ detail: "SERVER_BOOM" }) }),
);
await goto(failurePage, "#/interactive");
await failurePage.waitForTimeout(2000);
// 恢复失败的可发现入口：⑤ 审批入口（A 的实现把原因+重试放在打开的「结果与历史」面板里）
const failureEntry = failurePage.locator('[data-entry="approvals"]');
const entryLabel = (await failureEntry.count()) > 0 ? await failureEntry.first().getAttribute("aria-label") : null;
if ((await failureEntry.count()) > 0) {
  await failureEntry.first().click().catch(() => undefined);
  await failurePage.waitForTimeout(1200);
}
const failureState = await failurePage.evaluate(() => {
  const alerts = Array.from(document.querySelectorAll('[role="alert"]')).map((el) => (el.textContent || "").trim());
  const retry = Array.from(document.querySelectorAll("button")).filter((el) => /重试/.test(el.textContent || ""));
  return {
    alerts,
    retryCount: retry.length,
    bodyMentions: /恢复待审批项失败|读取.*失败|重试/.test(document.body.innerText || ""),
  };
});
await failurePage.screenshot({ path: OUT + "/14-pending-read-failure.png" });
record(
  "矩阵5｜pending 读取失败可见（错误 + 重试，不伪装为空）",
  "⑤ 入口可发现失败；打开后有恢复失败原因与重试入口",
  JSON.stringify({ entryLabel, ...failureState }),
  Boolean(entryLabel && entryLabel.includes("失败")) &&
    failureState.alerts.some((text) => text.includes("恢复待审批项失败")) &&
    failureState.retryCount > 0,
);
await failurePage.close();

// ---- 矩阵 6：三次确认进度来自后端 ---------------------------------------
page = await context.newPage();
attach(page);
await goto(page, "#/interactive");
await page.waitForTimeout(2000);
await page.evaluate(() => document.activeElement?.blur?.());
await inject(
  "APPROVAL_REQUIRED",
  approvalPayload("matrix_confirm_1", "直接维护内部数据", {
    kind: "internal.data_write",
    payload: { description: "直接维护内部数据", name: "内部运行数据" },
    requires_confirmations: 3,
    confirmed_stage: 1,
  }),
);
await page.waitForTimeout(2000);
const confirmState = await page.evaluate(() => {
  const text = document.body.innerText || "";
  return {
    progress: text.includes("1/3"),
    secondOfThird: text.includes("第 2/3 次"),
    modal: document.querySelectorAll("[data-unified-approval-modal]").length,
  };
});
await page.screenshot({ path: OUT + "/15-three-confirmations.png" });
record(
  "矩阵6｜内部数据确认进度来自后端（1/3，下一次是第 2 次）",
  "显示 1/3 与第 2/3 次",
  JSON.stringify(confirmState),
  confirmState.progress && confirmState.secondOfThird,
);

const summary = { sha: SH, at: STAMP, results, consoleErrors };
writeFileSync(OUT + "/matrix.json", JSON.stringify(summary, null, 2), "utf8");
const lines = ["# E 独立验收 · 矩阵补充项（" + SH.slice(0, 8) + "）", "", "| 步骤 | 结果 | 实际 |", "| --- | --- | --- |"];
for (const item of results) lines.push("| " + item.step + " | " + (item.ok ? "PASS" : "FAIL") + " | " + String(item.actual).replace(/\|/g, "\\|").slice(0, 300) + " |");
writeFileSync(OUT + "/matrix.md", lines.join("\n"), "utf8");
const failed = results.filter((item) => item.ok === false);
console.log("\n结果：" + results.length + " 项，失败 " + failed.length);
await context.close();
await browser.close();
process.exit(failed.length ? 1 : 0);