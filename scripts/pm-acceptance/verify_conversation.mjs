/**
 * E 独立验收 · 对话页用户路径（矩阵 11/12 的真实页面部分）
 *
 * 矩阵 12：草稿恢复、发送失败后的草稿保留（真实页面 + 真实后端；不配置模型，故意走失败分支）。
 * 矩阵 11：互动页空闲时不出现阻塞的影响确认层（真实页面）；材料冲突/剩余撤回决定的完整流程
 *          需要一次真实的材料变更确认（或模型执行），本脚本记为 LIMITED 并注明原因。
 *
 * 用法：
 *   $env:QIO_ACCEPT_UI="http://127.0.0.1:5199"; $env:QIO_ACCEPT_API="http://127.0.0.1:8734";
 *   node scripts/pm-acceptance/verify_conversation.mjs
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
const SH = sha();
const results = [];
const consoleErrors = [];

function record(step, expected, actual, ok, extra = {}) {
  results.push({ step, expected: String(expected), actual: String(actual), ok, sha: SH, ...extra });
  console.log((ok ? "PASS" : "FAIL") + " | " + step + " | 期望：" + expected + " | 实际：" + actual);
}
function recordLimited(step, expected, reason) {
  results.push({ step, expected: String(expected), actual: "LIMITED: " + reason, ok: null, sha: SH });
  console.log("LIMITED | " + step + " | " + reason);
}

async function rest(path, method = "GET", body) {
  const response = await fetch(API + path, {
    method,
    headers: body === undefined ? {} : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!response.ok) throw new Error("API " + method + " " + path + " -> " + response.status);
  const text = await response.text();
  return text ? JSON.parse(text) : null;
}

async function waitReady(p) {
  await p.waitForFunction(
    () => !document.querySelector(".boot-note") && (document.querySelector("#app")?.textContent || "").trim().length > 20,
    undefined,
    { timeout: 30000 },
  );
  await p.waitForTimeout(1800);
}

const DRAFT_TEXT = "E-ACCEPT 草稿恢复-唯一文本-7d2a";

const { chromium } = await import(pathToFileURL(PLAYWRIGHT).href);
await rest("/api/onboarding/complete", "POST", {});
await rest("/api/onboarding/seen", "POST", {});
const browser = await chromium.launch({ headless: HEADLESS, executablePath: CHROME, args: ["--no-first-run", "--disable-gpu"] });
const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
const page = await context.newPage();
page.on("console", (message) => {
  if (message.type() === "error") consoleErrors.push(message.text().slice(0, 300));
});
page.on("pageerror", (error) => consoleErrors.push("pageerror: " + String(error).slice(0, 300)));

// ---- 矩阵 12：草稿输入 → 刷新 → 草稿恢复 --------------------------------
await page.goto(UI + "/#/", { waitUntil: "domcontentloaded" });
await waitReady(page);
const input = page.locator("#composer-input");
await input.waitFor({ state: "visible", timeout: 15000 });
await input.click();
await input.fill(DRAFT_TEXT);
await page.waitForTimeout(1800); // 草稿是防抖保存的
await page.reload({ waitUntil: "domcontentloaded" });
await waitReady(page);
const restored = await page.locator("#composer-input").inputValue().catch(() => "");
await page.screenshot({ path: OUT + "/19-draft-restored.png" });
record(
  "矩阵12｜草稿恢复：刷新后输入框恢复未发送内容",
  "刷新后输入框内容包含刚输入的草稿",
  JSON.stringify({ restored: restored.slice(0, 60) }),
  restored.includes(DRAFT_TEXT),
);

// ---- 矩阵 12a：发送请求失败（传输层）必须可见、内容可找回 ------------
// 用 Playwright 拦掉 POST /api/turns（等价于断网/后端拒收）：这是「没发出去」的真实路径。
await page.route("**/api/turns", (route) => route.abort("failed"));
const sendButton = page.locator(".send-btn");
const sendEnabled = (await sendButton.getAttribute("data-ready")) === "true" && !(await sendButton.isDisabled());
if (!sendEnabled) {
  recordLimited("矩阵12｜发送失败可见且内容可找回", "发送按钮可用", "输入已填写但发送按钮仍不可用（缺少附件/会话前置条件）");
} else {
  await sendButton.click();
  await page.waitForTimeout(2500);
  const issuesEntry = page.locator('[data-entry="sendIssues"]');
  const hasIssuesEntry = (await issuesEntry.count()) > 0;
  let issuesText = "";
  const bodyText = await page.evaluate(() => document.body.innerText || "");
  if (hasIssuesEntry) {
    await issuesEntry.first().click().catch(() => undefined);
    await page.waitForTimeout(900);
    issuesText = await page.evaluate(() => document.body.innerText || "");
  }
  await page.screenshot({ path: OUT + "/20-send-failure.png" });
  const failureVisible = hasIssuesEntry || /失败|没有发出去|未发送|无法发送/.test(bodyText);
  const contentRecoverable = issuesText.includes(DRAFT_TEXT) || /重发|重新发送|继续发送|编辑|重试/.test(issuesText);
  record(
    "矩阵12｜发送失败有可见反馈，且内容能从新版入口找回（不丢）",
    "出现发送事项入口/失败说明，打开后能找回原文或重试动作",
    JSON.stringify({ failureVisible, hasIssuesEntry, contentRecoverable, snippet: issuesText.slice(0, 200) }),
    failureVisible && contentRecoverable,
  );
  await page.unroute("**/api/turns");
}

// ---- 矩阵 12b：发送被受理但这一轮失败（无模型凭据）时，失败原因留在消息区 ----
const fresh = await context.newPage();
fresh.on("console", (message) => {
  if (message.type() === "error") consoleErrors.push(message.text().slice(0, 300));
});
await fresh.goto(UI + "/#/", { waitUntil: "domcontentloaded" });
await waitReady(fresh);
await fresh.locator("#composer-input").fill("E-ACCEPT 无模型发送-唯一文本-3c9f");
await fresh.waitForTimeout(400);
const freshSend = fresh.locator(".send-btn");
if ((await freshSend.getAttribute("data-ready")) !== "true" || (await freshSend.isDisabled())) {
  recordLimited("矩阵12｜被受理的一轮失败也要可见（无模型凭据）", "发送按钮可用", "输入已填写但发送按钮仍不可用");
} else {
  await freshSend.click();
  await fresh.waitForTimeout(7000);
  const roundFailure = await fresh.evaluate(() => (document.body.innerText || "").slice(0, 5000));
  await fresh.screenshot({ path: OUT + "/20b-round-failure.png" });
  record(
    "矩阵12｜发送被受理但这一轮没开始：失败原因在消息区可见（不是静默）",
    "消息区写明「还没有配置可用的模型凭据/这一轮没有开始」",
    JSON.stringify({ hasReason: /模型凭据|这一轮没有开始|未完成/.test(roundFailure) }),
    /模型凭据|这一轮没有开始|未完成/.test(roundFailure),
  );
}
await fresh.close();

// ---- 矩阵 11：互动页空闲时不出现阻塞的影响确认层 -------------------------
await page.goto(UI + "/#/interactive", { waitUntil: "domcontentloaded" });
await waitReady(page);
const impactIdle = await page.evaluate(() => ({
  layer: document.querySelectorAll('[data-im="impact-dialog"], [data-im="impact-confirm-layer"]').length,
  modalLike: document.querySelectorAll('[role="dialog"][aria-modal="true"]').length,
}));
await page.screenshot({ path: OUT + "/21-interactive-idle.png" });
record(
  "矩阵11｜互动页无待确认影响时不出现阻塞层（空闲态）",
  "没有影响确认/撤回决定阻塞层",
  JSON.stringify(impactIdle),
  impactIdle.layer === 0,
);
recordLimited(
  "矩阵11｜材料影响确认 + 剩余撤回决定（完整真实流程）",
  "真实材料冲突/撤回决定经新界面承接",
  "需要先提交一次材料影响记录或一次真实执行产生 pendingDecision；当前隔离库无模型与历史提交，无法在不造假数据的前提下触发",
);

const summary = { sha: SH, at: new Date().toISOString(), results, consoleErrors };
writeFileSync(OUT + "/conversation.json", JSON.stringify(summary, null, 2), "utf8");
const lines = ["# E 独立验收 · 对话页用户路径（" + SH.slice(0, 8) + "）", "", "| 步骤 | 结果 | 实际 |", "| --- | --- | --- |"];
for (const item of results) lines.push("| " + item.step + " | " + (item.ok === null ? "LIMITED" : item.ok ? "PASS" : "FAIL") + " | " + String(item.actual).replace(/\|/g, "\\|").slice(0, 300) + " |");
writeFileSync(OUT + "/conversation.md", lines.join("\n"), "utf8");
const failed = results.filter((item) => item.ok === false);
console.log("\n结果：" + results.length + " 项，失败 " + failed.length + "，环境受限 " + results.filter((item) => item.ok === null).length);
await context.close();
await browser.close();
process.exit(failed.length ? 1 : 0);