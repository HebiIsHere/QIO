/**
 * F 独立验收 · 真实界面证据（480x600，隔离后端 8842 / vite 5292，数据目录 .tmp-fu2-f-ui-data）
 *
 * 覆盖（COMMON §4.3）：
 * 1) 0 待办但有历史：⑤ 入口可见，点开「审批结果与历史」能看到真实历史（持久记录）；
 * 2) 刷新/重连（新 SSE connect）后同一历史仍可回看（补读 history 的可见结果）；
 * 3) 已批准（等待执行）≠ 已执行：只读生命周期记录不显示成已完成、无预算编辑控件/「可修改后批准」；
 * 4) 决定失败时真实原因可见（外部已消费后点击允许 → 后端 404，界面显示原因而不是假装成功）；
 * 5) 480 窄窗无横向溢出。
 *
 * 证据：D:/qio-dev/_fixpm2/evidence/F/ui/*.png + verify_ui_480.json
 */
import { chromium } from "file:///C:/Users/zxy/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright/index.mjs";
import fs from "node:fs";
import path from "node:path";

const UI = "http://127.0.0.1:5292";
const API = "http://127.0.0.1:8842";
const OUT = "D:/qio-dev/_fixpm2/evidence/F/ui";
fs.mkdirSync(OUT, { recursive: true });
const results = [];
const rec = (id, pass, detail) => {
  results.push({ id, pass, detail });
  console.log((pass ? "PASS" : "FAIL") + " | " + id + " | " + JSON.stringify(detail));
};

async function api(pathname, method = "GET", body) {
  const res = await fetch(API + pathname, {
    method,
    headers: body === undefined ? {} : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const text = await res.text();
  if (!res.ok) throw new Error("API " + pathname + " -> " + res.status + " " + text.slice(0, 200));
  return text ? JSON.parse(text) : null;
}
async function inject(type, data) {
  return api("/api/events/test?event_type=" + encodeURIComponent(type), "POST", data);
}
const STAMP = new Date().toISOString();
function approvalEvent(approval) {
  return {
    approval: {
      approval_id: approval.approval_id,
      kind: approval.kind || "tool_execution",
      payload: approval.payload || { description: approval.label || "修改发布材料", name: approval.label || "修改发布材料" },
      label: approval.label || "修改发布材料",
      purpose: "F 验收：真实界面 480 证据",
      source: approval.source || "tool",
      state: approval.state || "waiting",
      requires_confirmations: approval.requires_confirmations ?? 1,
      confirmed_stage: approval.confirmed_stage ?? 0,
      created_at: STAMP,
      expires_at: STAMP,
      turn_id: null,
      session_id: null,
      request_digest: null,
    },
  };
}

const browser = await chromium.launch({ executablePath: "C:/Program Files/Google/Chrome/Application/chrome.exe", headless: true });
const context = await browser.newContext({ viewport: { width: 480, height: 600 } });
const page = await context.newPage();
const consoleErrors = [];
page.on("pageerror", (error) => consoleErrors.push(String(error.message || error)));

async function waitReady() {
  await page.waitForFunction(() => {
    const boot = document.querySelector(".boot-note");
    const app = document.querySelector("#app");
    return !boot && !!app && (app.textContent || "").trim().length > 20;
  }, undefined, { timeout: 30000 });
  await page.waitForTimeout(1200);
}
async function gotoChat() {
  await page.goto(UI + "/#/", { waitUntil: "domcontentloaded" });
  await waitReady();
}
async function historyEntry() {
  await page.waitForSelector('[data-entry="approvals"]', { timeout: 20000 });
  return page.locator('[data-entry="approvals"]').first();
}
async function openResults() {
  const entry = await historyEntry();
  await entry.click();
  await page.waitForTimeout(900);
  return page.evaluate(() => {
    const panel = document.querySelector('[data-test="approval-results-panel"]');
    const modal = document.querySelector("[data-unified-approval-modal]");
    const rect = panel ? panel.getBoundingClientRect() : null;
    return {
      panel: Boolean(panel),
      modal: Boolean(modal),
      text: (panel || modal)?.innerText?.slice(0, 900) || "",
      panelRect: rect ? { x: Math.round(rect.x), y: Math.round(rect.y), w: Math.round(rect.width) } : null,
      bodyOverflowX: document.body.scrollWidth > window.innerWidth + 1,
    };
  });
}
async function ensureModal() {
  if ((await page.locator("[data-unified-approval-modal]").count()) > 0) return true;
  const entry = page.locator('[data-entry="approvals"]');
  if ((await entry.count()) > 0) {
    await entry.first().click().catch(() => undefined);
    await page.waitForTimeout(800);
  }
  return (await page.locator("[data-unified-approval-modal]").count()) > 0;
}

// 1) 0 待办但有历史：⑤ 入口 + 结果面板可读真实历史（持久记录）
await gotoChat();
const first = await openResults();
await page.screenshot({ path: path.join(OUT, "01-480-approval-results.png") });
rec("480-history-entry-and-results", first.panel && first.text.includes("已执行：写入 2 个文件") && first.text.includes("执行失败：目标文件被占用") && first.text.includes("持久记录") && !first.bodyOverflowX, first);

// 2) 刷新/重连（新 SSE connect，走 connect→refreshApprovalsAndHistory 补读）后仍可回看
await page.reload({ waitUntil: "domcontentloaded" });
await waitReady();
const afterReload = await openResults();
await page.screenshot({ path: path.join(OUT, "02-480-after-reconnect.png") });
rec("480-results-after-reconnect", afterReload.panel && afterReload.text.includes("已执行：写入 2 个文件") && afterReload.text.includes("持久记录") && !afterReload.bodyOverflowX, afterReload);

// 3) 已批准（等待执行）≠ 已执行：只读、无预算编辑、无「可修改后批准」
await page.keyboard.press("Escape").catch(() => undefined);
await page.waitForTimeout(300);
await inject("APPROVAL_REQUIRED", approvalEvent({
  approval_id: "appr_f_approved",
  kind: "tool_create",
  label: "创建子智能体：整理发布材料",
  state: "approved",
  payload: {
    name: "子智能体：整理发布材料",
    tool_type: "subagent",
    subagent_budget: { max_iterations: 7, max_tokens: 200000, output_limit_chars: 4000 },
    description: "创建子智能体来整理发布材料",
  },
  confirmed_stage: 1,
}));
await page.waitForTimeout(900);
await ensureModal();
const approvedView = await page.evaluate(() => {
  const modal = document.querySelector("[data-unified-approval-modal]");
  return {
    modal: Boolean(modal),
    text: (modal?.innerText || "").slice(0, 900),
    numberInputs: document.querySelectorAll('input[type="number"]').length,
    budgetCopy: (modal?.innerText || "").includes("可修改后批准"),
    allowButton: Boolean(document.querySelector('[data-test="unified-approval-allow"]')),
  };
});
await page.screenshot({ path: path.join(OUT, "03-480-approved-readonly.png") });
rec("480-approved-not-executed-readonly", approvedView.modal && /已批准/.test(approvedView.text) && /等待执行|等待后端执行/.test(approvedView.text) && !/已完成/.test(approvedView.text) && approvedView.numberInputs === 0 && !approvedView.budgetCopy && !approvedView.allowButton, approvedView);

// 4) 决定失败的真实原因可见：外部先消费同一条审批，再在界面点「允许」
await page.keyboard.press("Escape").catch(() => undefined);
await page.waitForTimeout(300);
// 关掉上一张卡，确保下面这条 waiting 成为当前申请
await page.keyboard.press("Escape").catch(() => undefined);
await page.waitForTimeout(400);
// 这条审批只经事件流进入前端（后端审批表里没有它）：界面点「允许」时后端会拒绝，
// 正好用来验证「决定失败的真实原因可见，而不是假装成功」。
await inject("APPROVAL_REQUIRED", approvalEvent({
  approval_id: "appr_f_fail_decide",
  kind: "tool_execution",
  label: "修改发布材料（后端已不存在的审批）",
  state: "waiting",
}));
await page.waitForTimeout(900);
await ensureModal();
const allow = page.locator('[data-test="unified-approval-allow"]').first();
let decideFailure = { clicked: false, text: "", alert: "" };
if ((await allow.count()) > 0) {
  await allow.click().catch(() => undefined);
  decideFailure.clicked = true;
  await page.waitForTimeout(1000);
  decideFailure = {
    ...decideFailure,
    ...(await page.evaluate(() => ({
      text: (document.querySelector("[data-unified-approval-modal]")?.innerText || "").slice(0, 900),
      alert: (document.querySelector('[role="alert"]')?.innerText || "").slice(0, 400),
      bodyOverflowX: document.body.scrollWidth > window.innerWidth + 1,
    }))),
  };
}
await page.screenshot({ path: path.join(OUT, "04-480-decide-failure.png") });
const reasonVisible = /失败|没有|已处理|不能|无法|already|not found/i.test(
  (decideFailure.text || "") + " " + (decideFailure.alert || ""),
);
rec("480-decide-failure-reason-visible", decideFailure.clicked && reasonVisible, decideFailure);

const summary = {
  ui: UI,
  api: API,
  consoleErrors,
  results,
  passed: results.filter((item) => item.pass).length,
  total: results.length,
};
fs.writeFileSync(path.join(OUT, "verify_ui_480.json"), JSON.stringify(summary, null, 2), "utf8");
console.log("SUMMARY " + summary.passed + "/" + summary.total + " PASS; consoleErrors=" + consoleErrors.length);
await browser.close();
process.exit(summary.passed === summary.total ? 0 : 1);
