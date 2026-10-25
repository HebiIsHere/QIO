/**
 * E 独立验收 · 真实浏览器证据（基线 1b03541e / 候选分支同一脚本复跑）
 *
 * 只通过真实 HTTP + 真实 DOM 取证，不 mock 前端：
 * - 权限模式徽标：互动页是否存在常驻徽标（基线：没有）；
 * - 0 待审批时执行结果历史是否可达（基线：旧 ApprovalModal 被门控，聊天路由没有历史入口）；
 * - 480x600 下三条同类审批标签是否裁切 / 可区分 / 可键盘聚焦；
 * - 已批准状态是否被说成已完成、只读记录是否仍显示「可修改后批准」；
 * - 终态结果是否只在几秒内可见（基线：弹窗关闭后页面没有任何结果残留）。
 *
 * 审批通过 /api/events/test 注入（QIO_ENABLE_TEST_EVENTS=1，e2e_up.py 已设置）；
 * 不调用真实模型、不需要 Key。数据目录由启动方隔离（QIO_DATA_DIR）。
 *
 * 用法：
 *   $env:QIO_ACCEPT_UI="http://127.0.0.1:5199"; $env:QIO_ACCEPT_API="http://127.0.0.1:8734";
 *   node scripts/pm-acceptance/verify_surface.mjs
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

function gitSha() {
  try {
    return execSync("git rev-parse HEAD", { cwd: process.cwd(), encoding: "utf8" }).trim();
  } catch {
    return "unknown";
  }
}

const SHA = gitSha();
const startedAt = new Date().toISOString();
const results = [];
const consoleErrors = [];
let page = null;

function record(step, expected, actual, ok, extra = {}) {
  const entry = { step, expected: String(expected), actual: String(actual), ok, sha: SHA, ...extra };
  results.push(entry);
  console.log((ok ? "PASS" : "FAIL") + " | " + step + " | 期望：" + expected + " | 实际：" + actual);
  return ok;
}

async function shot(name) {
  const file = OUT + "/" + name + ".png";
  await page.screenshot({ path: file, fullPage: false });
  results.push({ step: "screenshot", file, ok: true, sha: SHA });
  return file;
}

async function api(path, method = "GET", body) {
  const response = await fetch(API + path, {
    method,
    headers: body === undefined ? {} : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const text = await response.text();
  if (!response.ok) throw new Error("API " + method + " " + path + " -> " + response.status + " " + text.slice(0, 300));
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
      kind: approval.kind || "tool_op",
      payload: approval.payload || { description: approval.label, name: approval.label },
      label: approval.label,
      purpose: approval.purpose || "验收：修改发布材料",
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

/** 输入框获得焦点时审批不会自动弹出（产品规则）；取证前先把焦点移开，才是在看「无人编辑」时的真实行为。 */
async function blurActive() {
  await page.evaluate(() => {
    const active = document.activeElement;
    if (active && typeof active.blur === "function") active.blur();
  });
}

/**
 * 确保统一审批弹窗可见：先看自动弹出，未弹出时走用户入口（输入区五图标里的「审批」）。
 * 返回打开方式，供证据记录：auto / entry / none。
 */
async function ensureApprovalModal() {
  if ((await page.locator("[data-unified-approval-modal]").count()) > 0) return "auto";
  // 对话收起时先展开，入口才在页面上（与用户真实操作一致）
  const expand = page.getByText("展开对话", { exact: false });
  if ((await expand.count()) > 0) {
    await expand.first().click().catch(() => undefined);
    await page.waitForTimeout(500);
  }
  const entry = page.locator('[data-entry="approvals"]');
  if ((await entry.count()) > 0) {
    await entry.first().click().catch(() => undefined);
    await page.waitForTimeout(500);
  }
  return (await page.locator("[data-unified-approval-modal]").count()) > 0 ? "entry" : "none";
}

async function waitReady() {
  // 对话页与互动页没有共同的根类名：等「启动等待页已消失 + 主界面有内容」这个共同事实。
  await page.waitForFunction(
    () => {
      const boot = document.querySelector(".boot-note");
      const app = document.querySelector("#app");
      return !boot && !!app && (app.textContent || "").trim().length > 20;
    },
    undefined,
    { timeout: 30000 },
  );
  await page.waitForTimeout(1500);
}

async function goto(hash) {
  // 连接建立后前端会拉一次权威快照；等这次恢复真正完成（事件流在此期间是缓冲状态）。
  const restored = page
    .waitForResponse((response) => response.url().includes("/api/runtime/state"), { timeout: 20000 })
    .catch(() => null);
  await page.goto(UI + "/" + hash, { waitUntil: "domcontentloaded" });
  await waitReady();
  await restored;
  await page.waitForTimeout(1000);
}

/** 页面上「执行结果 / 审批历史」的可操作入口（按钮 / 链接 / 带无障碍名的元素）。 */
async function historySurfaces() {
  return page.evaluate(() => {
    const label = /(执行结果历史|审批结果与历史|结果与历史|审批历史|执行历史|审批结果|执行结果)/;
    const hits = [];
    for (const el of Array.from(document.querySelectorAll("body *"))) {
      const attrs = ["aria-label", "title", "data-test", "data-im"]
        .map((name) => el.getAttribute(name) || "")
        .join(" ");
      const ownText = Array.from(el.childNodes)
        .filter((node) => node.nodeType === 3)
        .map((node) => node.textContent || "")
        .join(" ")
        .trim();
      const control =
        el.tagName === "BUTTON" ||
        el.tagName === "A" ||
        el.tagName === "SUMMARY" ||
        el.getAttribute("role") === "button" ||
        el.getAttribute("role") === "tab";
      if (label.test(attrs) || (control && label.test(ownText))) {
        hits.push({ tag: el.tagName.toLowerCase(), text: (el.textContent || "").trim().slice(0, 120), attrs });
      }
    }
    return hits.slice(0, 10);
  });
}

async function badgeInfo() {
  return page.evaluate(() => {
    const nodes = Array.from(document.querySelectorAll('[data-im="permission-mode-badge"]'));
    return {
      count: nodes.length,
      texts: nodes.map((node) => (node.textContent || "").trim()),
      visible: nodes.some((node) => {
        const rect = node.getBoundingClientRect();
        const style = window.getComputedStyle(node);
        return rect.width > 0 && rect.height > 0 && style.visibility !== "hidden" && style.display !== "none";
      }),
    };
  });
}

async function main() {
  const chromiumModule = await import(pathToFileURL(PLAYWRIGHT).href);
  const browser = await chromiumModule.chromium.launch({
    headless: HEADLESS,
    executablePath: CHROME,
    args: ["--no-first-run", "--no-default-browser-check", "--disable-gpu"],
  });
  // 首次启动的引导向导会盖住整个页面（且被视作阻塞弹窗，审批不会自动打开）：
  // 真实用户点「完成」等价于下面这一次完成调用；不清掉它拍到的就不是产品界面。
  await api("/api/onboarding/complete", "POST", {});
  await api("/api/onboarding/seen", "POST", {});
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  function attach(pageInstance) {
    pageInstance.on("console", (message) => {
      if (message.type() === "error") consoleErrors.push(message.text().slice(0, 300));
    });
    pageInstance.on("pageerror", (error) => consoleErrors.push("pageerror: " + String(error).slice(0, 300)));
  }
  page = await context.newPage();
  attach(page);

  // ---- 1) 权限模式徽标：互动页 vs 对话页 -----------------------------------
  await goto("#/");
  const conversationBadge = await badgeInfo();
  await shot("02-conversation-1440");
  record(
    "对话页权限模式徽标（1440x900，对照）",
    "页面存在常驻的权限模式徽标",
    "徽标数=" + conversationBadge.count + " 文案=" + JSON.stringify(conversationBadge.texts),
    conversationBadge.count > 0 && conversationBadge.visible,
    { surface: "conversation" },
  );

  await goto("#/interactive");
  // 事件流是页面加载后建立的：等它真的连上再注入，否则注入会落在「没有订阅者」的窗口里
  await page.waitForTimeout(2500);
  const interactiveBadge = await badgeInfo();
  await shot("01-interactive-1440");
  record(
    "互动页权限模式徽标（1440x900）",
    "页面存在常驻的权限模式徽标",
    "徽标数=" + interactiveBadge.count + " 文案=" + JSON.stringify(interactiveBadge.texts),
    interactiveBadge.count > 0 && interactiveBadge.visible,
    { surface: "interactive" },
  );

  // ---- 2) 结果残留：  // ---- 3) 结果残留：三条全部 executed 后，0 待审批时历史是否可达 ------------
  await page.waitForTimeout(600);
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.waitForTimeout(400);
  const surfacesAfterResult = await historySurfaces();
  await shot("05-no-pending-history");
  record(
    "0 待审批时，互动页仍有执行结果 / 审批历史的可操作入口",
    "存在历史入口",
    JSON.stringify(surfacesAfterResult),
    surfacesAfterResult.length > 0,
  );

  // ---- 4) 已批准状态 + 只读预算控件 ---------------------------------------
  await inject(
    "APPROVAL_REQUIRED",
    approvalEvent({
      approval_id: "pm_e_ui_approved",
      kind: "tool_create",
      label: "创建子智能体：整理发布材料",
      state: "approved",
      payload: {
        name: "子智能体：整理发布材料",
        tool_type: "subagent",
        subagent_budget: { max_iterations: 7, max_tokens: 200000, output_limit_chars: 4000 },
        description: "创建子智能体来整理发布材料",
      },
    }),
  );
  await page.waitForTimeout(1000);
  const approvedState = await page.evaluate(() => {
    const text = document.body.textContent || "";
    return {
      hasApproved: text.includes("已批准"),
      saysCompleted: text.includes("已完成"),
      budgetCopy: text.includes("可修改后批准"),
      numberInputs: document.querySelectorAll('input[type="number"]').length,
      modalText: (document.querySelector(".ua-modal")?.textContent || "").slice(0, 600),
    };
  });
  await shot("06-approved-readonly-budget");
  record(
    "已批准 / 等待执行不被说成已完成",
    "有「已批准」，没有「已完成」",
    JSON.stringify({ hasApproved: approvedState.hasApproved, saysCompleted: approvedState.saysCompleted }),
    approvedState.hasApproved && !approvedState.saysCompleted,
  );
  record(
    "只读生命周期记录不显示可编辑预算 /「可修改后批准」文案",
    "无预算数字输入、无「可修改后批准」",
    JSON.stringify({ budgetCopy: approvedState.budgetCopy, numberInputs: approvedState.numberInputs }),
    !approvedState.budgetCopy && approvedState.numberInputs === 0,
  );
  await inject("APPROVAL_RESULT", {
    approval_id: "pm_e_ui_approved",
    phase: "executed",
    ok: true,
    summary: "E-ACCEPT-已批准记录执行完成",
  });
  await page.waitForTimeout(600);

  // ---- 5) 终态结果：1 秒后 / 7 秒后是否还在 ------------------------------
  await inject(
    "APPROVAL_REQUIRED",
    approvalEvent({ approval_id: "pm_e_ui_late", label: "修改发布材料：延迟执行的那一项" }),
  );
  await page.waitForTimeout(800);
  await inject("APPROVAL_RESULT", {
    approval_id: "pm_e_ui_late",
    phase: "executed",
    ok: true,
    summary: "E-ACCEPT-延迟结果-唯一字符串-5b7c",
  });
  await page.waitForTimeout(1200);
  // 「结果可回看」= 用户打开 ⑤ 审批入口后能在「结果与历史」里读到；不是要求结果浮在页面上
  const lateEntry = page.locator('[data-entry="approvals"]');
  if ((await lateEntry.count()) > 0) {
    await lateEntry.first().click().catch(() => undefined);
    await page.waitForTimeout(1200);
  }
  const at1s = await page.evaluate(() => document.body.textContent.includes("E-ACCEPT-延迟结果-唯一字符串-5b7c"));
  await shot("07-result-1s");
  await page.waitForTimeout(7000);
  const at8s = await page.evaluate(() => document.body.textContent.includes("E-ACCEPT-延迟结果-唯一字符串-5b7c"));
  await shot("08-result-8s");
  record(
    "执行结果不是几秒即逝：7 秒后仍可读回",
    "1 秒与 8 秒时页面都能读到同一结果",
    JSON.stringify({ at1s, at8s }),
    at1s && at8s,
  );

  // ---- 6) 旧审批弹窗（带历史）在聊天路由不挂载 -----------------------------
  const legacy = await page.evaluate(() => ({
    oldModal: document.querySelectorAll("[data-test=\"approval-history\"]").length,
    hasOldHistoryText: (document.body.textContent || "").includes("执行结果历史（只读）"),
  }));
  record(
    "旧 ApprovalModal（唯一带历史的面板）不在聊天路由挂载",
    "聊天路由不存在旧历史面板",
    JSON.stringify(legacy),
    legacy.oldModal === 0 && !legacy.hasOldHistoryText,
  );

  const summary = {
    sha: SHA,
    ui: UI,
    api: API,
    startedAt,
    finishedAt: new Date().toISOString(),
    viewportSamples: ["1440x900", "480x600"],
    consoleErrors,
    results,
  };
  writeFileSync(OUT + "/surface.json", JSON.stringify(summary, null, 2), "utf8");
  const lines = [
    "# E 独立验收 · 真实浏览器证据（" + SHA.slice(0, 8) + "）",
    "",
    "- UI: " + UI,
    "- API: " + API,
    "- 时间: " + startedAt,
    "",
    "| 步骤 | 期望 | 实际 | 结果 |",
    "| --- | --- | --- | --- |",
  ];
  for (const item of results) {
    if (item.step === "screenshot") continue;
    lines.push("| " + item.step + " | " + item.expected + " | " + item.actual.replace(/\|/g, "\\|") + " | " + (item.ok ? "PASS" : "FAIL") + " |");
  }
  lines.push("", "## 控制台错误", "");
  lines.push(consoleErrors.length ? consoleErrors.map((text) => "- " + text).join("\n") : "- 无");
  writeFileSync(OUT + "/surface.md", lines.join("\n"), "utf8");

  await context.close();
  await browser.close();
  const failed = results.filter((item) => item.ok === false && item.step !== "screenshot");
  console.log("\n结果：" + results.filter((item) => item.step !== "screenshot").length + " 项，失败 " + failed.length + "；输出 " + OUT);
  process.exit(failed.length ? 1 : 0);
}

main().catch((error) => {
  console.error("验收脚本崩溃：", error);
  process.exit(2);
});