/**
 * pm032-options：设置页「设置逻辑」子分支（options）的请求级验收。
 *
 * 覆盖：
 * - #63/#64/#65：非法文本 / 清空 / 显式 0 / 503 回滚 / 重试 / 迟到响应隔离
 *   —— 全部读真实隔离后端的 GET 结果对账，并记录浏览器实际发出的 PUT（方法/URL/状态/字段）。
 * - #22：可对话但未设默认模型（fixture 拦截模型目录）→ 面板首屏显示实际在用模型，
 *   默认模型提示只出现在「默认模型」一项；空模型清单不出现「0 / 0 已验证」。
 * - #23：改搜索配置后回到「已配置，尚未检查」；检查只由用户点按触发；
 *   检查失败保留配置并可重试；只有真实成功才显示「检查通过」。
 *
 * 用法（环境先由 serve.mjs up --label opt --app-port 5107 --backend-port 8757 起好）：
 *   node scripts/settings-verify/pm032-options.mjs --url http://127.0.0.1:5107 --api http://127.0.0.1:8757 --cdp-port 9817 --out D:\qio-dev\_lead-logs\pm032-settings\options
 *
 * 只连 127.0.0.1；不使用真实 Key、不联网；写请求打真实隔离后端，失败/迟到用 CDP Fetch 注入。
 */
import { mkdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { VerifyBrowser } from "./lib/browser.mjs";
import { clickCategory, installHelpers, openSettings } from "./lib/probe.mjs";
import { SETTINGS_CATEGORIES } from "./lib/env.mjs";

const args = process.argv.slice(2);
const flag = (name, fallback) => {
  const index = args.indexOf("--" + name);
  if (index < 0) return fallback;
  const value = args[index + 1];
  return value === undefined || value.startsWith("--") ? true : value;
};
const sleep = (ms) => new Promise((done) => setTimeout(done, ms));

const appUrl = String(flag("url", "http://127.0.0.1:5107")).replace(/\/$/, "");
const apiBase = String(flag("api", "http://127.0.0.1:8757")).replace(/\/$/, "");
const cdpPort = Number(flag("cdp-port", 9817));
const outDir = String(flag("out", join(process.env.TEMP || ".", "pm032-options")));
mkdirSync(outDir, { recursive: true });

const report = {
  label: "pm032-options",
  startedAt: new Date().toISOString(),
  appUrl,
  apiBase,
  checks: [],
  requests: [],
  screenshots: [],
  notes: [],
};

function record(name, ok, detail) {
  report.checks.push({ name, ok: Boolean(ok), detail: detail ?? null });
  console.log((ok ? "  [PASS] " : "  [FAIL] ") + name + (detail === undefined ? "" : " :: " + JSON.stringify(detail).slice(0, 400)));
  return Boolean(ok);
}
function note(text) {
  report.notes.push(text);
  console.log("  [NOTE] " + text);
}

async function apiJson(path, init) {
  const resp = await fetch(apiBase + path, init);
  const text = await resp.text();
  let body = null;
  try { body = text ? JSON.parse(text) : null; } catch { body = text; }
  return { status: resp.status, body };
}
const getToolHistory = async () => (await apiJson("/api/settings/tools")).body;
const putToolHistoryToBackend = async (body) =>
  apiJson("/api/settings/tools", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });

/** 浏览器日志 → 结构化请求记录（只保留设置/model 请求） */
function collectRequests(browser, sinceIndex = 0) {
  const entries = browser.log.slice(sinceIndex);
  const byId = new Map();
  const out = [];
  for (const entry of entries) {
    if (entry.kind === "request") {
      byId.set(entry.requestId, entry);
      out.push({ kind: "request", method: entry.method, url: entry.url, postData: entry.postData ?? null, status: null });
    } else if (entry.kind === "response") {
      const request = byId.get(entry.requestId);
      if (!request) continue;
      const row = out.find((item) => item.kind === "request" && item.url === entry.url && item.status === null);
      if (row) row.status = entry.status;
    } else if (entry.kind === "stub") {
      out.push({ kind: "stub", method: entry.method, url: entry.url, status: entry.status, postData: null });
    }
  }
  return out.filter((item) => /\/api\/(settings|models|credentials)/.test(item.url));
}

function sanitizeRequests(rows) {
  return rows.map((row) => ({
    kind: row.kind,
    method: row.method,
    path: (() => { try { return new URL(row.url).pathname; } catch { return row.url; } })(),
    status: row.status,
    fields: row.postData ? Object.keys(JSON.parse(row.postData)) : [],
  }));
}

/** 在页面里「输入 + 失焦」：真实浏览器里输入只有 input 事件，提交发生在 blur。 */
async function typeAndBlur(browser, selector, value) {
  await browser.evalJs(
    "(() => { const el = document.querySelector(" + JSON.stringify(selector) + "); if (!el) return { ok: false, reason: 'not-found' }; " +
      "el.focus(); const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set; " +
      "setter.call(el, " + JSON.stringify(String(value)) + "); el.dispatchEvent(new Event('input', { bubbles: true })); " +
      "el.blur(); return { ok: true }; })()",
  );
}
async function inputValue(browser, selector) {
  return browser.evalJs("(() => { const el = document.querySelector(" + JSON.stringify(selector) + "); return el ? el.value : null; })()");
}

async function openTools(browser) {
  await openSettings(browser, appUrl);
  await clickCategory(browser, SETTINGS_CATEGORIES[3]);
  await browser.waitFor("!!document.querySelector('[data-settings-section=tools]')", { timeout: 15000 });
  await sleep(900);
}

const TOOL_INPUT = 'input[aria-label="输出保留（天）"]';
const TOOL_NOTICE_ERR = '[data-settings-section="tools"] .set-notice.err';

async function runNumberSemantics(browser) {
  console.log("\n== #63/#64/#65 数字输入语义（真实隔离后端） ==");
  const baseline = await getToolHistory();
  note("后端基线 output_retention_days=" + baseline.output_retention_days);

  await openTools(browser);
  const logStart = browser.log.length;

  for (const bad of ["abc", "1e", "-1"]) {
    const before = await getToolHistory();
    await typeAndBlur(browser, TOOL_INPUT, bad);
    await sleep(800);
    const shown = await inputValue(browser, TOOL_INPUT);
    const after = await getToolHistory();
    const requests = collectRequests(browser, logStart).filter((row) => row.url.includes("/api/settings/tools") && row.method === "PUT");
    const text = (await browser.text('[data-settings-section="tools"]')) || "";
    record("#63 非法文本 " + JSON.stringify(bad) + "：不发保存请求、后端值不变、界面回显最近值",
      requests.length === 0 && after.output_retention_days === before.output_retention_days && shown === String(before.output_retention_days) && /已恢复为当前值|必填/.test(text),
      { requests: sanitizeRequests(requests), shown, backend: after.output_retention_days });
  }

  {
    const before = await getToolHistory();
    await browser.evalJs("(() => { const el = document.querySelector(" + JSON.stringify(TOOL_INPUT) + "); el.focus(); const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set; setter.call(el, ''); el.dispatchEvent(new Event('input',{bubbles:true})); return true; })()");
    await sleep(300);
    const editingValue = await browser.evalJs("document.querySelector(" + JSON.stringify(TOOL_INPUT) + ").value");
    const noRequestWhileEditing = collectRequests(browser, logStart).filter((row) => row.url.includes("/api/settings/tools") && row.method === "PUT").length === 0;
    await browser.evalJs("document.querySelector(" + JSON.stringify(TOOL_INPUT) + ").blur()");
    await sleep(700);
    const after = await getToolHistory();
    const finalValue = await browser.evalJs("document.querySelector(" + JSON.stringify(TOOL_INPUT) + ").value");
    const text = (await browser.text('[data-settings-section="tools"]')) || "";
    record("#64 清空：编辑中不保存、离开提示必填并回显，后端仍是原值（不显示 0）",
      noRequestWhileEditing && after.output_retention_days === before.output_retention_days && finalValue === String(before.output_retention_days) && /这项必填/.test(text) && editingValue === "",
      { editingValue, finalValue, backend: after.output_retention_days });
  }

  {
    const log0 = browser.log.length;
    await typeAndBlur(browser, TOOL_INPUT, "0");
    await sleep(1200);
    const after = await getToolHistory();
    const puts = collectRequests(browser, log0).filter((row) => row.url.includes("/api/settings/tools") && row.method === "PUT");
    record("#63 显式 0：发出保存请求并落库 0", after.output_retention_days === 0 && puts.length === 1 && JSON.stringify(JSON.parse(puts[0].postData || "{}")) === JSON.stringify({ output_retention_days: 0 }),
      { puts: sanitizeRequests(puts), backend: after.output_retention_days });
    await browser.reload(2600);
    await installHelpers(browser);
    await clickCategory(browser, SETTINGS_CATEGORIES[3]);
    await sleep(1100);
    const reloaded = await browser.evalJs("(() => { const el = document.querySelector(" + JSON.stringify(TOOL_INPUT) + "); return el ? el.value : null; })()");
    record("#64 刷新后与后端一致（0 不是空）", reloaded === "0" && (await getToolHistory()).output_retention_days === 0, { reloaded });
    await typeAndBlur(browser, TOOL_INPUT, "90");
    await sleep(900);
  }

  {
    const off = await browser.route({ method: "PUT", path: /\/api\/settings\/tool-history/ }, () => ({
      status: 503,
      body: { detail: "upstream overloaded" },
    }));
    const log1 = browser.log.length;
    await typeAndBlur(browser, TOOL_INPUT, "30");
    await sleep(1300);
    const failedBackend = await getToolHistory();
    const afterFailure = await browser.evalJs("document.querySelector(" + JSON.stringify(TOOL_INPUT) + ").value");
    const errText = (await browser.text(TOOL_NOTICE_ERR)) || "";
    const detailsText = (await browser.evalJs("(() => { const el = document.querySelector('[data-settings-section=tools] details'); return el ? (el.innerText || el.textContent || '') : ''; })()")) || "";
    record("#65 503：界面回到生效值 90、错误简短、技术详情可展开",
      failedBackend.output_retention_days === 90 && afterFailure === "90" && errText.includes("保存输出保留期失败") && !errText.includes("503") && detailsText.includes("503"),
      { afterFailure, backend: failedBackend.output_retention_days, error: errText.slice(0, 120), details: detailsText.slice(0, 200), requests: sanitizeRequests(collectRequests(browser, log1)) });

    await browser.evalJs("(() => { const b = document.querySelector('[data-settings-section=tools] button[aria-label=\"关闭这项提示\"]'); if (b) b.click(); return !!b; })()");
    await sleep(300);
    const errAfterClose = await browser.evalJs("!!document.querySelector(" + JSON.stringify(TOOL_NOTICE_ERR) + ")");
    record("#65 错误可关闭（关闭后不残留）", errAfterClose === false, { errAfterClose });

    off();
    const log2 = browser.log.length;
    await browser.evalJs("(() => { const b = document.querySelector('[data-settings-section=tools] [data-settings-action=retry]'); if (b) b.click(); return !!b; })()");
    await sleep(1400);
    const retried = await getToolHistory();
    const afterRetry = await browser.evalJs("document.querySelector(" + JSON.stringify(TOOL_INPUT) + ").value");
    const puts2 = collectRequests(browser, log2).filter((row) => row.url.includes("/api/settings/tools") && row.method === "PUT");
    record("#65 重试重放原操作（30）并真正成功", retried.output_retention_days === 30 && afterRetry === "30" && puts2.length === 1 && JSON.stringify(JSON.parse(puts2[0].postData || "{}")) === JSON.stringify({ output_retention_days: 30 }),
      { backend: retried.output_retention_days, afterRetry, puts: sanitizeRequests(puts2) });
    await typeAndBlur(browser, TOOL_INPUT, "90");
    await sleep(700);
  }

  {
    let call = 0;
    const off = await browser.route({ method: "PUT", path: /\/api\/settings\/tool-history/ }, async (request) => {
      call += 1;
      if (call === 1) {
        await sleep(1800);
        return { status: 503, body: { detail: "late failure" } };
      }
      const parsed = JSON.parse(request.postData || "{}");
      const resp = await putToolHistoryToBackend(parsed);
      return { status: resp.status, body: resp.body };
    });
    await typeAndBlur(browser, TOOL_INPUT, "30");
    await sleep(120);
    await typeAndBlur(browser, TOOL_INPUT, "40");
    await sleep(3400);
    off();
    const backend = await getToolHistory();
    const value = await browser.evalJs("document.querySelector(" + JSON.stringify(TOOL_INPUT) + ").value");
    const errAfter = await browser.evalJs("!!document.querySelector(" + JSON.stringify(TOOL_NOTICE_ERR) + ")");
    record("#65 迟到失败回执被隔离：最终显示第二次的 40 且无旧错误", backend.output_retention_days === 40 && value === "40" && errAfter === false,
      { backend: backend.output_retention_days, value, errAfter });
    await typeAndBlur(browser, TOOL_INPUT, "90");
    await sleep(700);
  }

  const final = await getToolHistory();
  record("清理：保留期恢复为基线 90", final.output_retention_days === 90, { backend: final.output_retention_days });
  report.requests.push(...sanitizeRequests(collectRequests(browser, logStart)));
}

const MODEL_CATALOG_BASE = {
  connections: [
    {
      credential_id: "cred_primary",
      name: "主力连接",
      endpoint: "https://api.example-primary.invalid/v1",
      kind: "openai",
      provider_id: "example-primary",
      provider_name: "示例服务商甲",
      enabled: true,
      status: "active",
      verify_state: "verified",
      budget: 2000000,
      budget_used: 420000,
      budget_left: 1580000,
      models: [
        { model_id: "example-chat-2", display_name: "示例对话 2", source: "discovered", capabilities: ["chat"], verify_state: "verified", verified_at: "2026-10-28T03:00:00.000Z", verify_error: null, usable: true, unavailable_reason: "" },
      ],
    },
  ],
  default: { credential_id: "cred_primary", model_id: "example-chat-2" },
  usages: [
    { usage_key: "main-chat", label: "主对话", kind: "chat", group: "primary", model_id: null, credential_id: null, follows_default: true, inherits_ok: true, resolved: { credential_id: "cred_primary", model_id: "example-chat-2", source: "default", usable: true, unavailable_reason: "", credential_name: "主力连接" }, note: "", delivery: "model" },
    { usage_key: "general-chat", label: "通用对话", kind: "chat", group: "primary", model_id: null, credential_id: null, follows_default: true, inherits_ok: true, resolved: { credential_id: "cred_primary", model_id: "example-chat-2", source: "default", usable: true, unavailable_reason: "", credential_name: "主力连接" }, note: "", delivery: "model" },
    { usage_key: "embedding", label: "向量 / 嵌入", kind: "embedding", group: "special", model_id: null, credential_id: null, follows_default: false, inherits_ok: false, resolved: null, note: "沿用现有处理方式", delivery: "existing" },
  ],
  discovery: {},
};

async function mountModelsWith(browser, catalog) {
  await browser.clearRoutes();
  await browser.route({ method: "GET", path: /\/api\/models\/catalog/ }, () => ({ body: catalog, headers: { "Cache-Control": "no-store" } }));
  await browser.route({ method: "GET", path: /\/api\/credentials(\?|$)/ }, () => ({ body: { credentials: [], default_key_id: null } }));
  await browser.navigate(appUrl + "/?verify=" + Date.now() + "#/settings", 2600);
  await browser.waitFor("!!document.querySelector('[data-settings-page]')", { timeout: 20000 });
  await installHelpers(browser);
  await clickCategory(browser, SETTINGS_CATEGORIES[1]);
  await browser.waitFor("!!document.querySelector('[data-testid=models-panel]')", { timeout: 20000 });
  await sleep(1300);
}

async function shot(browser, name) {
  const file = join(outDir, name + ".png");
  await browser.shotFile(file, null, true);
  report.screenshots.push(file);
  return file;
}

async function runModels(browser) {
  console.log("\n== #22 模型面板首屏 ==");
  const canChatNoDefault = JSON.parse(JSON.stringify(MODEL_CATALOG_BASE));
  canChatNoDefault.default = null;
  canChatNoDefault.usages[0].follows_default = false;
  canChatNoDefault.usages[1].follows_default = false;
  await mountModelsWith(browser, canChatNoDefault);

  const actual = (await browser.text('[data-testid="models-actual-chat"]')) || "";
  const defaultSection = (await browser.text('[aria-labelledby="default-model-title"]')) || "";
  record("#22 可对话但未设默认模型：首屏显示当前对话实际在用的模型", actual.includes("当前对话实际在用") && actual.includes("主力连接") && actual.includes("example-chat-2"),
    { actual: actual.slice(0, 160) });
  record("#22 未设默认模型的提示只在「默认模型」一项里", defaultSection.includes("还没有设置默认模型") && !actual.includes("还没有设置默认模型"),
    { defaultHint: defaultSection.slice(0, 100) });
  const usageExpanded = await browser.evalJs("document.querySelector('[data-testid=models-usage-toggle]').getAttribute('aria-expanded')");
  const usageDisplay = await browser.evalJs("getComputedStyle(document.querySelector('[data-testid=models-usage-body]')).display");
  record("#22 用途分配默认收起（按需展开）", usageExpanded === "false" && usageDisplay === "none", { usageExpanded, usageDisplay });
  await shot(browser, "22-models-actual-chat-no-default");

  const emptyModels = JSON.parse(JSON.stringify(canChatNoDefault));
  emptyModels.connections[0].models = [];
  emptyModels.usages[0].resolved = null;
  emptyModels.usages[1].resolved = null;
  await mountModelsWith(browser, emptyModels);
  const panelText = (await browser.text('[data-testid="models-panel"]')) || "";
  record("#22 空模型清单不出现「0 / 0 已验证」", !panelText.includes("0 / 0") && panelText.includes("还没有模型记录"),
    { hasZeroSlashZero: panelText.includes("0 / 0"), sample: panelText.slice(0, 220) });
  await shot(browser, "22-models-empty-list");
  await browser.clearRoutes();
}

async function runSearch(browser) {
  console.log("\n== #23 联网搜索检查状态 ==");
  await openSettings(browser, appUrl);
  await clickCategory(browser, SETTINGS_CATEGORIES[1]);
  await browser.waitFor("!!document.querySelector('[data-settings-key=models.search]')", { timeout: 20000 });
  await sleep(800);

  await apiJson("/api/settings/search", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ searxng_url: "" }) });
  await browser.reload(2400);
  await installHelpers(browser);
  await clickCategory(browser, SETTINGS_CATEGORIES[1]);
  await browser.waitFor("!!document.querySelector('[data-settings-key=models.search]')", { timeout: 20000 });
  await sleep(900);

  const checkRequestsBefore = collectRequests(browser, 0).filter((row) => row.url.includes("/api/settings/search/check")).length;
  record("#23 打开设置不自动做连接检查", checkRequestsBefore === 0, { count: checkRequestsBefore });

  await browser.evalJs("(() => { const el = document.querySelector(\'[data-settings-fold-toggle=\\\"models.search.advanced\\\"]\'); if (el) el.click(); return !!el; })()");
  await sleep(300);
  const urlInput = 'input[aria-label="自建 SearXNG 地址"]';
  await browser.evalJs("(() => { const el = document.querySelector(" + JSON.stringify(urlInput) + "); el.focus(); const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set; setter.call(el, 'https://searx.pm032.example'); el.dispatchEvent(new Event('input',{bubbles:true})); return true; })()");
  await sleep(200);
  await browser.evalJs("(() => { const b = document.querySelector('[data-settings-group=\\\"models.search\\\"] [data-settings-action=save]'); if (b) b.click(); return !!b; })()");
  await sleep(1500);
  const afterSave = (await browser.text('[data-search-channel=searxng]')) || "";
  record("#23 改配置后显示「已填写地址，尚未检查」，不沿用旧结论", afterSave.includes("已填写地址，尚未检查"), { row: afterSave.slice(0, 120) });
  await shot(browser, "23-search-configured-not-checked");

  await browser.route({ method: "POST", path: /\/api\/settings\/search\/check/ }, () => ({
    body: {
      ok: false,
      channels: [
        { channel: "searxng", configured: true, checked: true, ok: false, reason: "连接被拒绝（注入）" },
        { channel: "keyless", configured: true, checked: true, ok: true },
        { channel: "bocha", configured: false, checked: false },
      ],
    },
  }));
  const checkLog = browser.log.length;
  await browser.evalJs("(() => { const b = document.querySelector('[data-testid=settings-search-check]'); if (b) b.click(); return !!b; })()");
  await sleep(1500);
  const failedRow = (await browser.text('[data-search-channel=searxng]')) || "";
  const failedInput = await browser.evalJs("document.querySelector(" + JSON.stringify(urlInput) + ").value");
  const buttonText = (await browser.text('[data-testid=settings-search-check]')) || "";
  const checkCalls = collectRequests(browser, checkLog).filter((row) => row.url.includes("/api/settings/search/check")).length;
  record("#23 检查失败：保留配置、显示这次失败原因、提供重新检查", failedRow.includes("检查失败：连接被拒绝（注入）") && failedInput === "https://searx.pm032.example" && buttonText.includes("重新检查") && checkCalls === 1,
    { row: failedRow.slice(0, 140), failedInput, buttonText, checkCalls });
  await shot(browser, "23-search-check-failed");

  await browser.route({ method: "POST", path: /\/api\/settings\/search\/check/ }, () => ({
    body: {
      ok: true,
      channels: [
        { channel: "searxng", configured: true, checked: true, ok: true, reason: "" },
        { channel: "keyless", configured: true, checked: true, ok: true },
        { channel: "bocha", configured: false, checked: false },
      ],
    },
  }));
  await browser.evalJs("(() => { const b = document.querySelector('[data-testid=settings-search-check]'); if (b) b.click(); return !!b; })()");
  await sleep(1500);
  const passedRow = (await browser.text('[data-search-channel=searxng]')) || "";
  const keylessRow = (await browser.text('[data-search-channel=keyless]')) || "";
  record("#23 只有真实成功才显示「检查通过」；免密钥状态仍是「已开启」", passedRow.includes("检查通过") && !passedRow.includes("检查失败") && keylessRow.includes("已开启"),
    { row: passedRow.slice(0, 120), keyless: keylessRow.slice(0, 80) });
  await shot(browser, "23-search-check-passed");
  await browser.clearRoutes();
}

const browser = new VerifyBrowser({ port: cdpPort, apiBase, width: 1440, height: 900, outDir });
try {
  await browser.launch();
  await browser.setViewport(1440, 900);
  await browser.installDialogGuards();
  await runNumberSemantics(browser);
  await runModels(browser);
  await runSearch(browser);
} catch (error) {
  record("harness", false, String(error && error.stack ? error.stack : error));
} finally {
  await browser.close();
}

report.finishedAt = new Date().toISOString();
report.ok = report.checks.every((check) => check.ok);
writeFileSync(join(outDir, "pm032-options-report.json"), JSON.stringify(report, null, 2), "utf8");
const md = [
  "# pm032-options 请求级验收",
  "",
  "app: " + appUrl + " / api: " + apiBase,
  "结果：" + (report.ok ? "全部通过" : "存在失败项") + "（" + report.checks.filter((c) => c.ok).length + "/" + report.checks.length + "）",
  "",
  "| 检查 | 结果 | 证据 |",
  "| --- | --- | --- |",
  ...report.checks.map((check) => "| " + check.name + " | " + (check.ok ? "PASS" : "FAIL") + " | " + String(JSON.stringify(check.detail)).replace(/\|/g, "\\|").slice(0, 300) + " |"),
  "",
  "## 设置写请求记录（URL / 方法 / 状态 / 字段名，无密钥）",
  ...report.requests.map((row) => "- " + row.method + " " + row.path + " -> " + row.status + " fields=" + JSON.stringify(row.fields)),
  "",
  "## 截图",
  ...report.screenshots.map((file) => "- " + file),
  "",
  "## 说明",
  ...report.notes.map((text) => "- " + text),
].join("\n");
writeFileSync(join(outDir, "pm032-options-report.md"), md, "utf8");
console.log("\n" + (report.ok ? "ALL PASS" : "FAILURES PRESENT") + " -> " + join(outDir, "pm032-options-report.json"));
process.exit(report.ok ? 0 : 1);