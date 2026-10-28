/**
 * 设置页交互取证：草稿保留 / 离开丢弃 / 分组保存成功与失败 / 迟到响应 / 搜索定位。
 *
 * 真实性边界（报告里必须照抄）：
 * - 读取类 fixture（凭据卡 / 模型目录 / 搜索通道状态）是浏览器侧固定响应，不是真实后端数据；
 * - 写请求除明确标为「注入失败 / 注入延迟」的用例之外，都打到**真实隔离后端**
 *   （%TEMP% 下独立数据目录），不是 mock 成功；
 * - 「离开设置页」用应用内 hash 路由切换（同一页面实例），
 *   整页刷新会连 Pinia 内存一起清掉，测的就不是「离开设置页」了；
 * - 不联网、不用真实 Key。
 *
 * 用法：
 *   node scripts/settings-verify/interaction.mjs --url http://127.0.0.1:5106 --label run-xxx
 *   可选：--cdp-port 9806 --allow-fail
 */
import { mkdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { VerifyBrowser } from "./lib/browser.mjs";
import { openSettings, installHelpers, clickCategory, clickNavByText, categoryReport } from "./lib/probe.mjs";
import { installFixtures } from "./lib/fixtures.mjs";
import { EVIDENCE_DIR, SETTINGS_CATEGORIES, stamp } from "./lib/env.mjs";

const args = process.argv.slice(2);
const flag = (name, fallback) => {
  const index = args.indexOf("--" + name);
  if (index < 0) return fallback;
  const value = args[index + 1];
  return value === undefined || value.startsWith("--") ? true : value;
};
const sleep = (ms) => new Promise((done) => setTimeout(done, ms));
const appUrl = String(flag("url", "http://127.0.0.1:5106")).replace(/\/$/, "");
const cdpPort = Number(flag("cdp-port", 9806));
const apiBase = String(flag("api", process.env.QIO_SETTINGS_API || "http://127.0.0.1:8756")).replace(/\/$/, "");
const label = String(flag("label", "run-" + stamp()));
const allowFail = !!flag("allow-fail", false);
mkdirSync(EVIDENCE_DIR, { recursive: true });

const run = { label, startedAt: new Date().toISOString(), appUrl, checks: [], notes: [] };
function record(scenario, name, ok, detail) {
  run.checks.push({ scenario, name, ok: !!ok, detail: detail === undefined ? null : detail });
  console.log("  " + (ok ? "[PASS]" : "[FAIL]") + " " + scenario + " / " + name + (ok ? "" : " :: " + JSON.stringify(detail).slice(0, 360)));
  return ok;
}

const NAV_ALIASES = [
  "常规", "外观",
  "模型与服务", "模型与联网",
  "记忆与整理", "对话与记忆",
  "工具与权限", "工具",
  "关于与更新", "关于",
  "高级", "凭据",
];

/** 依次点击候选导航文字，返回命中的那个。 */
async function gotoAnyNav(browser, aliases) {
  for (const text of aliases) {
    const hit = await clickNavByText(browser, text);
    if (hit?.ok) {
      await sleep(240);
      return text;
    }
  }
  return null;
}

async function openSettingsStable(browser) {
  for (let attempt = 0; attempt < 2; attempt += 1) {
    try {
      await openSettings(browser, appUrl);
      return true;
    } catch (error) {
      if (attempt === 1) throw error;
      await sleep(800);
    }
  }
  return false;
}

/** 保证探针存在（整页导航会清掉 window.__stg）。 */
async function ensureProbe(browser) {
  const ok = await browser.evalJs("!!window.__stg");
  if (!ok) await installHelpers(browser);
}

/**
 * 找「SearXNG 地址」这类按组保存的文本字段。
 * 两个时代的落点不同：基线在「高级」tab 的折叠区；新方案在「模型与服务 → 高级选项」。
 * 逐个分类找，找到就返回所在导航文字，供后续切回同一处读值。
 */
async function findSearxngField(browser) {
  for (const navText of NAV_ALIASES) {
    const hit = await clickNavByText(browser, navText);
    if (!hit?.ok) continue;
    await sleep(220);
    await browser.evalJs("window.__stg.expandAdvanced()");
    // 折叠区（details/summary）需要展开后输入才可见
    await browser.evalJs("window.__stg.clickLabel('SearXNG')");
    await sleep(240);
    const field = await browser.evalJs("window.__stg.findControl('SearXNG')");
    if (field?.found && ["input", "textarea"].includes(field.kind)) return { category: navText, field };
  }
  return null;
}

async function gotoFieldCategory(browser, fieldInfo) {
  if (!fieldInfo) return false;
  const hit = await clickNavByText(browser, fieldInfo.category);
  await sleep(240);
  await browser.evalJs("window.__stg.expandAdvanced()");
  await browser.evalJs("window.__stg.clickLabel('SearXNG')");
  await sleep(240);
  return !!hit?.ok;
}

async function setTextByLabel(browser, fieldLabel, value) {
  return browser.evalJs(
    "(() => { const info = window.__stg.findControl(" + JSON.stringify(String(fieldLabel)) + "); if (!info.found) return { ok: false, reason: info.reason }; " +
      "const el = window.__stg._last; if (!el.matches('input, textarea')) return { ok: false, reason: 'not-text', kind: info.kind }; " +
      "el.focus(); window.__stg.fireInput(el, " + JSON.stringify(String(value)) + "); return { ok: true, kind: info.kind }; })()",
  );
}

async function readByLabel(browser, fieldLabel) {
  const info = await browser.evalJs("window.__stg.findControl(" + JSON.stringify(String(fieldLabel)) + ")");
  return info?.found ? info.value : null;
}

async function openModelsAdvanced(browser) {
  const clicked = await clickCategory(browser, SETTINGS_CATEGORIES[1]);
  if (clicked?.ok) {
    await sleep(300);
  } else {
    await gotoAnyNav(browser, ["模型与服务", "模型与联网"]);
  }
  await browser.evalJs("window.__stg.expandAdvanced()");
  await sleep(260);
  return true;
}

/** 找按组保存的数字字段（两个时代的共同中文名都含「返回条数」）。 */
async function findNumberField(browser) {
  for (const text of ["返回条数", "默认返回条数", "读正文字符预算", "单段长度目标"]) {
    const info = await browser.evalJs("window.__stg.findControl(" + JSON.stringify(text) + ")");
    if (info?.found && ["input", "native-number"].includes(info.kind)) return { label: text, info };
  }
  return { label: null, info: null };
}

async function setNumberField(browser, fieldLabel, value) {
  return browser.evalJs(
    "(() => { const info = window.__stg.findControl(" + JSON.stringify(String(fieldLabel)) + "); if (!info.found) return { ok: false, reason: info.reason }; " +
      "const el = window.__stg._last; if (!el.matches('input')) return { ok: false, reason: 'not-input', kind: info.kind }; " +
      "el.focus(); window.__stg.fireInput(el, " + JSON.stringify(String(value)) + "); " +
      "el.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true })); el.blur(); return { ok: true }; })()",
  );
}

async function saveButton(browser) {
  // A 的按组保存条有稳定钩子：优先点可见且未禁用的 data-settings-action=save
  const direct = await browser.evalJs("(() => { const root = window.__stg.root(); const buttons = [...root.querySelectorAll('[data-settings-action=save]')].filter((el) => window.__stg.visible(el) && !el.disabled); if (!buttons.length) return { ok: false }; buttons[0].click(); return { ok: true, text: window.__stg.norm(buttons[0].innerText || '') }; })()");
  if (direct?.ok) return { ok: true, label: direct.text, direct: true };
  for (const text of ["保存搜索配置", "保存修改", "保存设置", "保存本组", "应用修改"]) {
    const hit = await browser.evalJs("window.__stg.clickLabel(" + JSON.stringify(text) + ")");
    if (hit?.ok) return { ok: true, label: text, hit };
  }
  const exact = await browser.evalJs("window.__stg.clickLabel('保存', { exact: true })");
  if (exact?.ok) return { ok: true, label: "保存", hit: exact };
  // 兜底：找当前可见分区里文字最短的「保存」按钮，避免误点到「保存输出全文」开关
  const fallback = await browser.evalJs("(() => { const root = window.__stg.root(); const buttons = [...root.querySelectorAll('button, [role=button], .qio-btn')].filter((el) => window.__stg.visible(el) && /^保存/.test(window.__stg.norm(el.innerText || el.textContent || ''))); if (!buttons.length) return { ok: false }; const hit = buttons.sort((a, b) => (a.innerText || '').length - (b.innerText || '').length)[0]; hit.click(); return { ok: true, text: window.__stg.norm(hit.innerText || '') }; })()");
  return fallback?.ok ? { ok: true, label: fallback.text, fallback: true } : { ok: false };
}

function writesSince(browser, sinceIndex, filter) {
  return browser.log.slice(sinceIndex).filter((entry) => {
    if (entry.kind !== "request" || entry.method === "GET") return false;
    if (!filter) return true;
    return filter(entry);
  });
}

const SETTINGS_WRITE = (entry) => /\/api\/(settings|credentials|models|security)/.test(entry.url);

async function noticeTexts(browser) {
  return browser.evalJs("(() => [...window.__stg.root().querySelectorAll('[role=status], [role=alert], .msg, .notice, .qio-notice, [data-settings-notice]')].filter((el) => window.__stg.visible(el)).map((el) => window.__stg.norm(el.innerText || el.textContent || '')).filter(Boolean))()");
}

async function scenarioDraftRetention(browser) {
  const scenario = "draft-retention";
  await openSettingsStable(browser);
  run.categories = await categoryReport(browser);
  const fieldInfo = await findSearxngField(browser);
  const draftValue = "https://draft-retention.invalid/searxng";
  let set = null;
  if (fieldInfo) set = await setTextByLabel(browser, "SearXNG", draftValue);
  record(scenario, "draft-field-found", !!(fieldInfo && set?.ok), { category: fieldInfo?.category ?? null, set });
  if (!(fieldInfo && set?.ok)) return;
  // 切到另一个分类，再切回来：v-show 保留面板实例，草稿应还在
  await clickCategory(browser, SETTINGS_CATEGORIES[0]);
  await sleep(300);
  await gotoFieldCategory(browser, fieldInfo);
  const retained = await readByLabel(browser, "SearXNG");
  record(scenario, "draft-kept-after-category-switch", retained === draftValue, { expected: draftValue, actual: retained });

  // 宽窄切换也要保留草稿
  await browser.setViewport(480, 600);
  await sleep(900);
  await browser.setViewport(1440, 900);
  await sleep(900);
  await gotoFieldCategory(browser, fieldInfo);
  const afterResize = await readByLabel(browser, "SearXNG");
  record(scenario, "draft-kept-after-resize", afterResize === draftValue, { expected: draftValue, actual: afterResize });
}

async function scenarioDiscardOnLeave(browser) {
  const scenario = "draft-discard-on-leave";
  await openSettingsStable(browser);
  const fieldInfo = await findSearxngField(browser);
  const draftValue = "https://discard-on-leave.invalid/searxng";
  let set = null;
  if (fieldInfo) set = await setTextByLabel(browser, "SearXNG", draftValue);
  record(scenario, "draft-field-found", !!(fieldInfo && set?.ok), { category: fieldInfo?.category ?? null, set });
  if (!(fieldInfo && set?.ok)) return;
  const mark = browser.log.length;
  // 应用内路由离开设置页（不整页刷新）
  await browser.evalJs("location.hash = '#/'; true");
  await sleep(1500);
  const leaveWrites = writesSince(browser, mark, SETTINGS_WRITE);
  await browser.evalJs("location.hash = '#/settings'; true");
  await sleep(1600);
  await ensureProbe(browser);
  await gotoFieldCategory(browser, fieldInfo);
  const afterReturn = await readByLabel(browser, "SearXNG");
  record(scenario, "leave-discards-draft", afterReturn !== draftValue, { expectedNot: draftValue, valueAfterReturn: afterReturn });
  record(scenario, "leave-sends-no-extra-write", leaveWrites.length === 0, leaveWrites.map((entry) => entry.method + " " + entry.url));
}

async function scenarioSaveSuccess(browser) {
  const scenario = "group-save-success";
  await openSettingsStable(browser);
  await openModelsAdvanced(browser);
  const found = await findNumberField(browser);
  const debug = await browser.evalJs("(() => ({ url: location.href, hasProbe: !!window.__stg, active: window.__stg ? window.__stg.activeCategory() : null, cats: window.__stg ? window.__stg.categories().map((c) => c.label) : null, probe: window.__stg ? window.__stg.findControl('返回条数') : null }))()");
  record(scenario, "group-field-found", !!found.label, { info: found.info, debug });
  if (!found.label) return;
  // 目标值必须与「当前服务端值」不同，否则控件不会变脏、保存按钮禁用 —— 这是测试隔离问题，不是产品缺陷
  const currentRaw = String((await readByLabel(browser, found.label)) ?? "").trim();
  const current = Number(currentRaw);
  const target = Number.isFinite(current) && current === 7 ? 8 : 7;
  await setNumberField(browser, found.label, target);
  await sleep(200);
  const dirtyState = await browser.evalJs("(() => { const el = window.__stg.root().querySelector('[data-settings-dirty]'); return el && window.__stg.visible(el) ? window.__stg.norm(el.innerText || '') : null; })()");
  record(scenario, "dirty-marker-shown", !!dirtyState && /未保存/.test(dirtyState), { dirtyState });
  const mark = browser.log.length;
  const saved = await saveButton(browser);
  await sleep(1200);
  const writes = writesSince(browser, mark, SETTINGS_WRITE);
  const notices = await noticeTexts(browser);
  record(scenario, "save-button-clicked", !!saved.ok, saved);
  record(scenario, "save-request-sent", writes.length > 0, writes.map((entry) => entry.method + " " + entry.url));
  const okNotice = notices.some((text) => /已保存|保存成功|已生效/.test(text));
  const errNotice = notices.some((text) => /失败|错误|无法/.test(text));
  record(scenario, "success-feedback-shown", okNotice && !errNotice, { notices });
  // 重新加载后仍是新值 = 真写进了后端
  await openSettingsStable(browser);
  await openModelsAdvanced(browser);
  const persisted = await readByLabel(browser, found.label);
  record(scenario, "value-persisted-after-reload", String(persisted) === String(target), { expected: target, actual: persisted });
}

async function scenarioSaveFailure(browser) {
  const scenario = "group-save-failure";
  await openSettingsStable(browser);
  await openModelsAdvanced(browser);
  const found = await findNumberField(browser);
  record(scenario, "group-field-found", !!found.label, found.info);
  if (!found.label) return;
  const fail = () => ({ status: 500, body: { detail: "验收注入：保存失败（fake，不发真实请求）" } });
  const offs = [
    await browser.route({ method: "POST", path: /\/api\// }, fail),
    await browser.route({ method: "PUT", path: /\/api\// }, fail),
    await browser.route({ method: "PATCH", path: /\/api\// }, fail),
  ];
  const currentRaw = String((await readByLabel(browser, found.label)) ?? "").trim();
  const target = Number(currentRaw) === 9 ? 10 : 9;
  await setNumberField(browser, found.label, target);
  const mark = browser.log.length;
  await saveButton(browser);
  await sleep(1100);
  const notices = await noticeTexts(browser);
  const valueNow = await readByLabel(browser, found.label);
  const errNotice = notices.some((text) => /失败|错误|无法|未保存/.test(text));
  record(scenario, "failure-feedback-shown", errNotice, { notices });
  record(scenario, "draft-kept-after-failure", String(valueNow) === String(target), { expected: target, actual: valueNow });
  record(scenario, "fake-write-injected", browser.log.some((entry) => entry.kind === "stub" && SETTINGS_WRITE(entry)), { writes: writesSince(browser, mark).map((entry) => entry.method + " " + entry.url) });
  for (const off of offs) off();
  await browser.clearRoutes();
  await sleep(200);
}

async function scenarioLateResponse(browser) {
  const scenario = "late-response";
  await openSettingsStable(browser);
  await openModelsAdvanced(browser);
  const found = await findNumberField(browser);
  record(scenario, "group-field-found", !!found.label, found.info);
  if (!found.label) return;
  let call = 0;
  const echo = async (request) => {
    call += 1;
    let body = {};
    try { body = request.postData ? JSON.parse(request.postData) : {}; } catch { body = {}; }
    return {
      body: {
        top_k_default: body.top_k_default ?? null,
        max_fetch_chars: body.max_fetch_chars ?? null,
        searxng_url: body.searxng_url ?? "",
        bocha_has_key: false,
        keyless_fallback: true,
      },
      delayMs: 1800,
    };
  };
  const offs = [
    await browser.route({ method: "POST", path: /\/api\// }, echo),
    await browser.route({ method: "PUT", path: /\/api\// }, echo),
    await browser.route({ method: "PATCH", path: /\/api\// }, echo),
  ];
  await setNumberField(browser, found.label, 11);
  await saveButton(browser);
  // 保存中继续改：真实用户会这么干；迟到的旧响应不得把用户刚输入的新值覆盖回去
  await sleep(220);
  const duringSave = await setNumberField(browser, found.label, 13);
  const savingState = await browser.evalJs("(() => { const el = window.__stg.root().querySelector('[data-settings-dirty]'); return el ? window.__stg.norm(el.innerText || '') : null; })()");
  await sleep(2900);
  const valueNow = await readByLabel(browser, found.label);
  const dirtyAfter = await browser.evalJs("(() => { const el = window.__stg.root().querySelector('[data-settings-dirty]'); return el && window.__stg.visible(el) ? window.__stg.norm(el.innerText || '') : null; })()");
  record(scenario, "save-in-flight-edited", !!duringSave?.ok, { duringSave, savingState });
  record(scenario, "late-old-response-not-applied", String(valueNow) === "13", { expected: 13, actual: valueNow, writes: call });
  record(scenario, "newer-draft-still-marked-dirty", !!dirtyAfter && /未保存/.test(dirtyAfter), { dirtyAfter });
  record(scenario, "one-write-made", call >= 1, { writes: call });
  for (const off of offs) off();
  await browser.clearRoutes();
  await sleep(200);
}

async function scenarioSearch(browser) {
  const scenario = "search-locate";
  await openSettingsStable(browser);
  const opened = await browser.evalJs("window.__stg.openSearch()");
  record(scenario, "search-entry", !!opened?.found, opened);
  if (!opened?.found) return;
  const queries = [
    { name: "普通项", query: "主题", expectAny: ["常规", "外观"] },
    { name: "高级参数", query: "返回条数", expectAny: ["模型与服务", "模型与联网", "模型"] },
    { name: "折叠用途", query: "记忆整理", expectAny: ["模型与服务", "模型与联网", "模型"] },
  ];
  for (const item of queries) {
    await browser.evalJs("window.__stg.openSearch()");
    await sleep(200);
    await browser.evalJs("window.__stg.search(" + JSON.stringify(item.query) + ")");
    await sleep(520);
    const results = await browser.evalJs("window.__stg.searchResults()");
    const before = await browser.evalJs("window.__stg.activeCategory()");
    let clicked = null;
    let after = before;
    let highlighted = [];
    if (Array.isArray(results) && results.length) {
      clicked = await browser.evalJs("window.__stg.clickSearchResult(0)");
      await sleep(800);
      after = await browser.evalJs("window.__stg.activeCategory()");
      highlighted = await browser.evalJs("window.__stg.highlighted()");
    }
    const categoryOk = item.expectAny.some((alias) => String(after).includes(alias));
    record(scenario, item.name + "-result-found", Array.isArray(results) && results.length > 0, { query: item.query, count: results?.length ?? 0 });
    record(scenario, item.name + "-located", categoryOk && (highlighted.length > 0 || !!(clicked && clicked.ok)), { query: item.query, before, after, highlighted: highlighted?.length ?? 0 });
    await browser.pressKey("Escape");
    await sleep(220);
  }
}

async function main() {
  const browser = new VerifyBrowser({ port: cdpPort, width: 1440, height: 900, apiBase });
  await browser.launch();
  await browser.installDialogGuards();
  await browser.setViewport(1440, 900);
  await browser.navigate(appUrl + "/", 2200);
  const fixtures = await installFixtures(browser);
  run.fixtures = fixtures.hits;

  const scenarios = [
    ["draft-retention", scenarioDraftRetention],
    ["draft-discard-on-leave", scenarioDiscardOnLeave],
    ["group-save-success", scenarioSaveSuccess],
    ["group-save-failure", scenarioSaveFailure],
    ["late-response", scenarioLateResponse],
    ["search-locate", scenarioSearch],
  ];
  const only = String(flag("only", ""));
  for (const [name, fn] of scenarios) {
    if (only && !only.split(",").includes(name)) continue;
    // 每个场景前后都检查一次原生对话框调用（交互里最容易误用 window.confirm 的地方）
    try {
      const dialogs = await browser.evalJs("window.__qioDialogCalls || null");
      if (dialogs) run.dialogCalls = dialogs;
    } catch { /* 页面还没就绪时忽略 */ }
    try {
      await fn(browser);
    } catch (error) {
      record(name, "scenario-error", false, String(error?.message ?? error));
      try { await browser.clearRoutes(); } catch { /* 忽略 */ }
      try { await openSettingsStable(browser); } catch { /* 忽略 */ }
    }
  }
  try {
    const dialogs = await browser.evalJs("window.__qioDialogCalls || null");
    run.dialogCalls = dialogs;
    const total = dialogs ? dialogs.confirm + dialogs.alert + dialogs.prompt : 0;
    record("native-dialogs", "no-confirm-alert-prompt", total === 0, dialogs);
  } catch (error) {
    record("native-dialogs", "no-confirm-alert-prompt", false, String(error?.message ?? error));
  }
  await browser.close();
}

try {
  await main();
} catch (error) {
  run.error = String(error && error.stack ? error.stack : error);
  console.error("交互取证中断：" + run.error);
} finally {
  run.finishedAt = new Date().toISOString();
  run.summary = { total: run.checks.length, passed: run.checks.filter((item) => item.ok).length, failed: run.checks.filter((item) => !item.ok).length, fixtures: run.fixtures };
  const file = join(EVIDENCE_DIR, "interaction-" + label + ".json");
  writeFileSync(file, JSON.stringify(run, null, 2), "utf8");
  console.log("\n交互检查 " + run.summary.total + " 条：通过 " + run.summary.passed + " / 失败 " + run.summary.failed);
  console.log("证据：" + file);
  if ((run.summary.failed > 0 || run.error) && !allowFail) process.exitCode = 1;
}
