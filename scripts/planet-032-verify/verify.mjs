/**
 * 星球 032 组验收（真实浏览器 + 隔离后端数据）。
 *
 * 覆盖用户验收口径：搜索分源失败/旧响应、Esc/焦点、知识草稿四种离开选择、
 * 真实整批撤回/历史/重复请求、实体属性显式 0、待处理分源、六处文案、窄窗。
 *
 * 用法（在仓库根目录）：
 *   node scripts/planet-032-verify/verify.mjs --only search,esc
 *   node scripts/planet-032-verify/verify.mjs            # 全部
 * 环境：QIO_VERIFY_APP（默认 http://127.0.0.1:5112）、QIO_VERIFY_API（默认 8762）、
 *       QIO_VERIFY_LABEL（默认 lead032，读 scripts/mp-verify/.state/<label>.json）。
 */
import { execFileSync } from "node:child_process";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { join, resolve } from "node:path";
import { ProbeBrowser, sleep } from "./lib/probe.mjs";
import { openPlanetFromDock, waitPlanetReady } from "../mp-verify/lib/page.mjs";

const APP = process.env.QIO_VERIFY_APP || "http://127.0.0.1:5112";
const API = process.env.QIO_VERIFY_API || "http://127.0.0.1:8762";
const LABEL = process.env.QIO_VERIFY_LABEL || "lead032";
const OUT = resolve(process.env.QIO_VERIFY_OUT || "_lead-logs/planet032/acceptance");
mkdirSync(OUT, { recursive: true });

const stateFile = resolve("scripts/mp-verify/.state/" + LABEL + ".json");
const state = JSON.parse(readFileSync(stateFile, "utf8"));
const DATA_DIR = state.dataDir;
const PYTHON = state.pythonCmd[0];

const results = [];
let shotIndex = 0;

function check(name, ok, detail) {
  const entry = { name, ok: Boolean(ok), detail: detail == null ? "" : String(detail) };
  results.push(entry);
  console.log((entry.ok ? "PASS" : "FAIL") + "  " + name + (entry.detail ? "  | " + entry.detail : ""));
}
function assert(cond, message) {
  if (!cond) throw new Error(message);
}
let currentBrowser = null;
async function scenario(name, fn) {
  console.log("\n===== " + name + " =====");
  try {
    await fn();
  } catch (error) {
    check(name + "：场景异常", false, error && error.message ? error.message : error);
    if (currentBrowser) {
      try {
        const state = await currentBrowser.evalJs("(() => ({ tab: document.querySelector('.management')?.getAttribute('data-tab'), view: document.querySelector('.management')?.getAttribute('data-view'), mgWidth: document.querySelector('.management')?.getBoundingClientRect().width, detail: !!document.querySelector('[data-k-detail]'), detailRect: (() => { const d = document.querySelector('[data-k-detail]'); if (!d) return null; const r = d.getBoundingClientRect(); return [r.width, r.height]; })() }))()");
        console.log("STATE:", JSON.stringify(state));
        await shot(currentBrowser, "FAIL-" + name.replace(/[^a-zA-Z0-9]/g, "_").slice(0, 30));
      } catch { /* 忽略 */ }
    }
  }
}
function flag(name) {
  const args = process.argv.slice(2);
  const i = args.indexOf("--" + name);
  return i < 0 ? null : args[i + 1];
}
function only() {
  const raw = flag("only");
  return raw ? new Set(raw.split(",").map((s) => s.trim()).filter(Boolean)) : null;
}
function want(key) {
  const set = only();
  return !set || set.has(key);
}

async function getJson(path) {
  const resp = await fetch(API + path);
  assert(resp.ok, "GET " + path + " -> " + resp.status);
  return resp.json();
}
async function postJson(path, body) {
  const resp = await fetch(API + path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body ?? {}),
  });
  let data = null;
  try {
    data = await resp.json();
  } catch {
    data = null;
  }
  return { status: resp.status, data };
}

// ---------------------------------------------------------------- 页面基础操作
async function boot(viewport = { width: 1440, height: 970 }, cdpPort = 9831) {
  const browser = new ProbeBrowser({ port: cdpPort, width: viewport.width, height: viewport.height, outDir: OUT });
  await browser.launch();
  await browser.enableFetch();
  return browser;
}

async function resetPage(b) {
  b.clearRules();
  await b.navigate(APP + "/?verify=" + Date.now() + "#/", 3200);
  await b.waitFor("!!document.querySelector('[data-planet-entry], .dock')", { timeout: 60000 });
  // 冷启动 WebGL 初始化慢：入口还不可点时再点一次（最多 3 次）
  for (let i = 0; i < 3; i += 1) {
    try {
      await openPlanetFromDock(b, { timeout: 25000 });
      break;
    } catch {
      await sleep(2500);
    }
  }
  // 等星球**真正进入展开态**（.planet-view.entered）：只有存在不等于已打开，球态也会挂着 DOM
  for (let i = 0; i < 2; i += 1) {
    try {
      await waitPlanetReady(b, { timeout: 45000 });
      break;
    } catch {
      try { await openPlanetFromDock(b, { timeout: 20000 }); } catch { /* 再试一次 */ }
    }
  }
  await b.waitFor("!!document.querySelector('[data-mp-manage-toggle]')", { timeout: 60000 });
  // 冷启动时第一次点可能落在管理区还没挂载的那一帧：按真实宽度重试，但两次点击至少隔 2.2s
  //（管理按钮是开/关切换，点太快会把刚开始的展开又收回去）
  let lastToggleAt = 0;
  for (let i = 0; i < 12; i += 1) {
    const width = await b.evalJs("(() => { const m = document.querySelector('.management'); return m ? m.getBoundingClientRect().width : 0; })()");
    if (width > 100) break;
    if (Date.now() - lastToggleAt > 2200) {
      await clickEl(b, "[data-mp-manage-toggle]");
      lastToggleAt = Date.now();
    }
    await sleep(420);
  }
  await b.waitFor("document.querySelector('.management') && document.querySelector('.management').getBoundingClientRect().width > 100", { timeout: 30000 });
  await b.settleFrames(3);
}
function mgAttr(b, name) {
  return b.evalJs("(() => { const m = document.querySelector('.management'); return m ? m.getAttribute('" + name + "') : null; })()");
}
async function openTab(b, tab) {
  for (let i = 0; i < 6; i += 1) {
    if ((await mgAttr(b, "data-tab")) === tab) break;
    try {
      await clickEl(b, '[data-mp-tab="' + tab + '"]');
    } catch {
      await sleep(500);
      continue;
    }
    await sleep(400);
  }
  await b.waitFor("document.querySelector('.management') && document.querySelector('.management').getAttribute('data-tab') === '" + tab + "'", { timeout: 15000 });
  await sleep(300);
}
async function openAux(b, aux) {
  for (let i = 0; i < 6; i += 1) {
    if ((await mgAttr(b, "data-view")) === aux) break;
    try {
      await clickEl(b, '[data-mp-aux="' + aux + '"]');
    } catch {
      await sleep(500);
      continue;
    }
    await sleep(400);
  }
  await b.waitFor("document.querySelector('.management') && document.querySelector('.management').getAttribute('data-view') === '" + aux + "'", { timeout: 15000 });
  await sleep(400);
}
async function searchInputSelector(b) {
  return b.evalJs("document.querySelector('[data-mp-search-input] input') ? '[data-mp-search-input] input' : (document.querySelector('input[data-mp-search-input]') ? 'input[data-mp-search-input]' : null)");
}
async function openSearch(b) {
  // 展开动画还会挪动入口按钮：点一次没进入搜索视图就再点（最多 4 次），不靠睡固定时长
  for (let i = 0; i < 5; i += 1) {
    if ((await mgAttr(b, "data-view")) === "search") break;
    try {
      await clickEl(b, "[data-mp-search-open]");
    } catch {
      await sleep(500);
      continue;
    }
    await sleep(450);
  }
  await b.waitFor("document.querySelector('.management') && document.querySelector('.management').getAttribute('data-view') === 'search'", { timeout: 15000 });
  await b.waitFor("!!document.querySelector('[data-mp-search-input] input, input[data-mp-search-input]')", { timeout: 10000 });
  await sleep(260);
}
async function setQuery(b, keyword) {
  const selector = await searchInputSelector(b);
  assert(selector, "找不到搜索输入框");
  await b.setInput(selector, keyword);
  await sleep(420);
}
async function guardButtonTexts(b) {
  return b.evalJs("[...document.querySelectorAll('[data-unsaved-guard] button')].map((x) => x.textContent.replace(/\\s+/g, '').trim())");
}
async function clickIn(b, selector, text) {
  const ok = await b.evalJs(
    "(function(){ var root=document.querySelector(" + JSON.stringify(selector) + "); if(!root) return false;" +
      " var el=[...root.querySelectorAll('button')].find(function(x){return x.textContent.replace(/\\s+/g,'').indexOf(" + JSON.stringify(text) + ")>=0;});" +
      " if(!el) return false; el.click(); return true; })()",
  );
  await sleep(200);
  return ok;
}
async function clickSelectorInView(b, selector) {
  await b.evalJs("(function(){var el=document.querySelector(" + JSON.stringify(selector) + "); if(el && el.scrollIntoView) el.scrollIntoView({block:'center'}); return !!el;})()");
  await sleep(160);
  return b.clickSelector(selector);
}
async function closeRestoreDialog(b) {
  if (!(await b.exists(".restore-dialog"))) return;
  await b.clickSelector(".restore-dialog__cancel");
  await b.waitFor("!document.querySelector('.restore-dialog')", { timeout: 12000 });
  // 弹窗卸载后仲裁器还要一帧才释放；等它真的可以让下一个弹窗打开（否则会看到「另一个中央弹窗正在处理」）
  await sleep(700);
}
async function clickEl(b, selector) {
  const ok = await b.evalJs("(function(){ var el = document.querySelector(" + JSON.stringify(selector) + "); if (!el) return false; el.scrollIntoView({ block: 'center' }); el.click(); return true; })()");
  await sleep(220);
  if (!ok) throw new Error("找不到控件：" + selector);
}
async function clickItem(b, selector) {
  // 列表条目本身可能是 role=button 的 li，内部还嵌了别的按钮：按条目身份点击（真实鼠标点中间可能命中嵌套链接）
  const ok = await b.evalJs("(function(){ var el = document.querySelector(" + JSON.stringify(selector) + "); if (!el) return false; el.scrollIntoView({ block: 'center' }); el.click(); return true; })()");
  await sleep(180);
  if (!ok) throw new Error("找不到条目：" + selector);
}
async function shot(b, name) {
  shotIndex += 1;
  const file = join(OUT, "shot-" + String(shotIndex).padStart(2, "0") + "-" + name + ".png");
  await b.shotFile(file);
  return file;
}
async function waitClickable(b, selector, timeout = 20000) {
  await b.waitFor("(() => { const el = document.querySelector(" + JSON.stringify(selector) + "); if (!el) return false; const r = el.getBoundingClientRect(); const cs = getComputedStyle(el); return r.width > 0 && r.height > 0 && cs.visibility !== 'hidden' && cs.display !== 'none'; })()", { timeout });
}
async function waitVisible(b, selector, timeout = 15000) {
  await b.waitFor("(() => { const el = document.querySelector(" + JSON.stringify(selector) + "); if (!el) return false; const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0 && getComputedStyle(el).visibility !== 'hidden'; })()", { timeout });
}

// ---------------------------------------------------------------- 场景：搜索
async function scenarioSearch(b) {
  await resetPage(b);
  await openSearch(b);

  // 一个来源 503，其他来源仍可用 + 单源重试
  b.rule({ urlPart: "/api/knowledge", method: "GET", action: "status", status: 503, remaining: 20 });
  await setQuery(b, "基线");
  await waitVisible(b, '[data-search-source-error="knowledge"]');
  const retryExists = await b.exists('[data-search-source-retry="knowledge"]');
  const topicHits = await b.count('[data-search-kind="topic"]');
  const noEmptyClaim = !(await b.evalJs("document.querySelector('[data-mp-search]').textContent.indexOf('没有匹配的话题、知识或实体') >= 0"));
  check("搜索：知识来源 503 时话题结果仍可用", topicHits > 0, "topic hits=" + topicHits);
  check("搜索：失败来源就地提供单源重试", retryExists === true);
  check("搜索：部分失败不显示总空态", noEmptyClaim === true);
  await shot(b, "search-source-503");

  b.clearRules();
  await clickSelectorInView(b, '[data-search-source-retry="knowledge"]');
  await b.waitFor("!!document.querySelector('[data-search-source=\"knowledge\"][data-source-status=\"ok\"]')", { timeout: 15000 });
  const topicAfter = await b.count('[data-search-kind="topic"]');
  check("搜索：单源重试成功后其他结果仍在", topicAfter > 0, "topic hits=" + topicAfter);

  // 旧请求迟到：话题来源延迟一次，快速换词后旧结果不得覆盖
  b.rule({ urlPart: "/api/graph/topics", method: "GET", action: "delay", delayMs: 1400 });
  await setQuery(b, "相机");
  await sleep(320);
  await setQuery(b, "窗口");
  await sleep(2600);
  const summary = await b.textOf(".gs-summary");
  const cameraTitleVisible = await b.evalJs("document.querySelector('[data-mp-search-results]').textContent.indexOf('星球相机聚焦距离与时长') >= 0");
  const windowTitleVisible = await b.evalJs("document.querySelector('[data-mp-search-results]').textContent.indexOf('窗口变窄时侧栏的覆盖布局') >= 0");
  check("搜索：旧关键词迟到不覆盖当前词", Boolean(summary && summary.indexOf("窗口") >= 0 && summary.indexOf("相机") < 0), summary);
  check("搜索：当前词命中可见、旧词命中不残留", windowTitleVisible && !cameraTitleVisible, "window=" + windowTitleVisible + " camera=" + cameraTitleVisible);
  await shot(b, "search-late-response");

  // 全部来源失败：不冒充没有结果
  b.clearRules();
  b.rule({ urlPart: "/api/graph/topics", method: "GET", action: "status", status: 503, remaining: 20 });
  b.rule({ urlPart: "/api/knowledge", method: "GET", action: "status", status: 503, remaining: 20 });
  b.rule({ urlPart: "/api/entities", method: "GET", action: "status", status: 503, remaining: 20 });
  await setQuery(b, "验收不存在的关键词xyz");
  await b.waitFor("document.querySelectorAll('[data-search-source-error]').length === 3", { timeout: 15000 });
  const noResultClaim = await b.evalJs("document.querySelector('[data-mp-search]').textContent.indexOf('没有匹配的话题、知识或实体') >= 0");
  check("搜索：三源全失败时不显示总空态", noResultClaim === false);
  b.clearRules();
}

// ---------------------------------------------------------------- 场景：Esc
async function scenarioEsc(b) {
  await resetPage(b);
  await openSearch(b);
  await setQuery(b, "基线");
  await b.waitFor("!!document.querySelector('[data-search-kind]')", { timeout: 15000 });
  const beforeCount = await b.count('[data-search-kind]');

  const inputSel = await searchInputSelector(b);
  await b.focus(inputSel);
  await b.pressKey("Escape");
  await b.waitFor("document.querySelector('.management') && document.querySelector('.management').getAttribute('data-view') !== 'search'", { timeout: 10000 });
  const active = await b.activeElement();
  check("Esc：输入框里退出搜索", true);
  check("Esc：焦点回到真实搜索入口", Boolean(active && active.data === true), JSON.stringify(active));

  await openSearch(b);
  const value = await b.evalJs("(document.querySelector('[data-mp-search-input] input') || document.querySelector('input[data-mp-search-input]')).value");
  const afterCount = await b.count('[data-search-kind]');
  check("Esc：再次打开保留关键词", value === "基线", value);
  check("Esc：再次打开保留结果", afterCount === beforeCount, beforeCount + " -> " + afterCount);

  // 结果列表里 Esc
  await b.evalJs("(document.querySelector('[data-search-kind]') || {}).focus && document.querySelector('[data-search-kind]').focus()");
  await b.pressKey("Escape");
  await b.waitFor("document.querySelector('.management') && document.querySelector('.management').getAttribute('data-view') !== 'search'", { timeout: 10000 });
  check("Esc：结果列表里退出搜索", true);

  // 重新搜索按钮里 Esc
  await openSearch(b);
  b.rule({ urlPart: "/api/knowledge", method: "GET", action: "status", status: 503, remaining: 20 });
  await b.clickSelector('[data-mp-search] button[aria-label="重新搜索"]');
  await waitVisible(b, '[data-search-source-retry="knowledge"]');
  await b.evalJs("document.querySelector('[data-search-source-retry=\"knowledge\"]').focus()");
  await b.pressKey("Escape");
  await b.waitFor("document.querySelector('.management') && document.querySelector('.management').getAttribute('data-view') !== 'search'", { timeout: 10000 });
  check("Esc：重新搜索按钮里退出搜索", true);
  b.clearRules();

  // 中文输入法组字优先
  await openSearch(b);
  const inputSel2 = await searchInputSelector(b);
  await b.focus(inputSel2);
  const composing = await b.escapeWhileComposing();
  await sleep(200);
  const stillSearch = (await mgAttr(b, "data-view")) === "search";
  check("Esc：组字中不拦截（先给输入法）", composing.composing === true && stillSearch, JSON.stringify(composing));

  // Tab 可达重新搜索按钮
  await b.pressKey("Tab");
  const tabTarget = await b.activeElement();
  check("Tab：搜索输入框后可到重新搜索按钮", Boolean(tabTarget && tabTarget.label === "重新搜索"), JSON.stringify(tabTarget));
}

// ---------------------------------------------------------------- 场景：文案
async function scenarioCopy(b) {
  await resetPage(b);
  // 搜索入口悬停：全局搜索
  const rect = await b.rectOf("[data-mp-search-open]");
  await b.send("Input.dispatchMouseEvent", { type: "mouseMoved", x: rect.x + rect.width / 2, y: rect.y + rect.height / 2, button: "none" });
  await sleep(760);
  const tip1 = await b.evalJs("(() => { const t = document.querySelector('[role=\"tooltip\"]'); if (!t) return null; const cs = getComputedStyle(t); return cs.visibility === 'hidden' ? null : t.textContent.trim(); })()");
  check("文案：搜索入口悬停只写「全局搜索」", tip1 === "全局搜索", String(tip1));
  await b.send("Input.dispatchMouseEvent", { type: "mouseMoved", x: 6, y: 6, button: "none" });
  await sleep(200);

  await openSearch(b);
  const emptyHint = await b.evalJs("document.querySelector('[data-mp-search]').textContent.indexOf('一次搜索同时覆盖') >= 0");
  check("文案：空态不再重复解释搜索范围", emptyHint === false);
  const refreshRect = await b.rectOf('[data-mp-search] button[aria-label="重新搜索"]');
  await b.send("Input.dispatchMouseEvent", { type: "mouseMoved", x: refreshRect.x + refreshRect.width / 2, y: refreshRect.y + refreshRect.height / 2, button: "none" });
  await sleep(760);
  const tip2 = await b.evalJs("(() => { const t = document.querySelector('[role=\"tooltip\"]'); if (!t) return null; const cs = getComputedStyle(t); return cs.visibility === 'hidden' ? null : t.textContent.trim(); })()");
  check("文案：搜索刷新悬停只写「重新搜索」", tip2 === "重新搜索", String(tip2));
  await b.send("Input.dispatchMouseEvent", { type: "mouseMoved", x: 6, y: 6, button: "none" });
  await sleep(150);

  // 讨论记录：标题只出现一次 + 动作「从这里继续」
  await b.pressKey("Escape");
  await b.waitFor("document.querySelector('.management') && document.querySelector('.management').getAttribute('data-view') !== 'search'", { timeout: 10000 });
  await openTab(b, "memory");
  await b.waitFor("!!document.querySelector('[data-memory-topics] [data-topic-id]')", { timeout: 15000 });
  await clickItem(b, "[data-memory-topics] [data-topic-id]");
  await b.waitFor("!!document.querySelector('.mt-open-record')", { timeout: 15000 });
  await b.clickSelector(".mt-open-record");
  await b.waitFor("!!document.querySelector('[data-memory-record] h2')", { timeout: 15000 });
  const titles = await b.evalJs("[...document.querySelectorAll('[data-memory-record] h2')].map((x) => x.textContent.trim())");
  const continueText = await b.evalJs("document.querySelector('[data-memory-record] [data-memory-continue]') ? document.querySelector('[data-memory-record] [data-memory-continue]').textContent.trim() : null");
  const fullName = await b.evalJs("document.body.textContent.indexOf('完整讨论记录') >= 0");
  check("文案：讨论记录标题只出现「讨论记录」", JSON.stringify(titles) === JSON.stringify(["讨论记录"]), JSON.stringify(titles));
  check("文案：讨论记录动作「从这里继续」", Boolean(continueText && continueText.indexOf("从这里继续") >= 0), String(continueText));
  check("文案：不再出现「完整讨论记录」", fullName === false);
  await shot(b, "copy-memory-record");
}

// ---------------------------------------------------------------- 场景：知识离开检查
async function scenarioKnowledge(b) {
  await resetPage(b);
  const all = (await getJson("/api/knowledge")).knowledge;
  // 避开维护批次的恢复目标：编辑它们会让「整批撤回」从可恢复变成 blocked，影响后面的真实撤回验收
  const batchList = (await getJson("/api/memory/maintenance/batches?limit=50")).batches || [];
  const maintIds = new Set();
  for (const b of batchList) {
    try {
      const detail = await getJson("/api/memory/maintenance/batches/" + b.id);
      for (const op of detail.operations || []) {
        for (const key of ["before", "after"]) {
          for (const snap of op[key] || []) {
            if (snap && snap.id) maintIds.add(String(snap.id));
          }
        }
      }
    } catch {
      /* 单批次读不到不影响选条目 */
    }
  }
  const active = all.filter(
    (k) => k.state === "active" && !k.ended && !maintIds.has(k.id) && k.content.indexOf("（验收草稿）") < 0,
  );
  assert(active.length >= 3, "可编辑知识不足 3 条（排除维护目标后）");
  const A = active[0];
  const B = active[1];
  const C = active[2];

  await openTab(b, "knowledge");
  await b.waitFor("!!document.querySelector('[data-k-id=\"" + A.id + "\"]')", { timeout: 15000 });

  async function openItem(id) {
    for (let attempt = 0; attempt < 3; attempt += 1) {
      await clickItem(b, '[data-k-id="' + id + '"]');
      await sleep(350);
      const opened = await b.evalJs("(() => { const d = document.querySelector('[data-k-detail]'); if (!d) return false; const r = d.getBoundingClientRect(); return r.width > 0 && !!document.querySelector('[data-k-edit]'); })()");
      if (opened) {
        await waitClickable(b, "[data-k-edit]", 8000);
        await sleep(120);
        return;
      }
    }
    throw new Error("知识详情没有进入可编辑态：" + id);
  }
  async function startEdit() {
    await waitClickable(b, "[data-k-edit]", 15000);
    await clickEl(b, "[data-k-edit]");
    await waitVisible(b, "[data-k-draft] textarea");
  }
  async function setDraft(text) {
    await b.setInput("[data-k-draft] textarea", text);
    await sleep(120);
  }
  async function detailText() {
    return b.textOf("[data-k-detail]");
  }
  async function triggerOpenViaSearch(id, keyword) {
    await openSearch(b);
    await setQuery(b, keyword);
    await b.waitFor("!!document.querySelector('[data-search-kind=\"knowledge\"][data-search-id=\"" + id + "\"]')", { timeout: 15000 });
    await clickSelectorInView(b, '[data-search-kind="knowledge"][data-search-id="' + id + '"]');
    await waitVisible(b, "[data-unsaved-guard]");
  }

  const draftText = A.content + "（验收草稿）";
  await openItem(A.id);
  await startEdit();
  await setDraft(draftText);

  // 1) 继续编辑
  await triggerOpenViaSearch(B.id, B.content.slice(0, 6));
  const buttons = await guardButtonTexts(b);
  check("知识：离开确认三个动作齐全", JSON.stringify(buttons) === JSON.stringify(["继续编辑", "保存并打开", "放弃并打开"]), JSON.stringify(buttons));
  const synParagraph = await b.evalJs("document.querySelector('[data-unsaved-guard]').textContent.indexOf('返回列表会丢弃') >= 0");
  check("文案：离开确认删除同义段落", synParagraph === false);
  await clickEl(b, "[data-unsaved-keep]");
  await b.waitFor("!document.querySelector('[data-unsaved-guard]')", { timeout: 10000 });
  const keptDraft = await b.evalJs("(document.querySelector('[data-k-draft] textarea') || {}).value || ''");
  check("知识：继续编辑保留草稿与原条目", keptDraft === draftText, keptDraft.slice(-8));

  // 2) 保存失败：留在原条目、保留内容
  b.rule({ urlPart: "/api/knowledge/" + A.id + "/revise", method: "POST", action: "status", status: 503, remaining: 20 });
  await triggerOpenViaSearch(B.id, B.content.slice(0, 6));
  await clickEl(b, "[data-unsaved-save-open]");
  // 保存失败：离开确认关闭，留在原条目的编辑态，真实原因可见（不是停在「保存中」）
  await b.waitFor("!!document.querySelector('.k-feedback.err')", { timeout: 20000 });
  const failDraft = await b.evalJs("(document.querySelector('[data-k-draft] textarea') || {}).value || ''");
  const failError = await b.textOf(".k-feedback.err");
  const sameItem = await b.evalJs("!!document.querySelector('[data-k-detail] [data-k-draft] textarea')");
  check("知识：保存失败留在原条目并保留内容", sameItem === true && failDraft === draftText && Boolean(failError), "draft kept=" + (failDraft === draftText) + " err=" + String(failError).slice(0, 30));
  await shot(b, "knowledge-save-failed");
  b.clearRules();

  // 3) 保存并打开：保存成功才跳转
  await triggerOpenViaSearch(B.id, B.content.slice(0, 6));
  await clickEl(b, "[data-unsaved-save-open]");
  await b.waitFor("!document.querySelector('[data-unsaved-guard]') && document.querySelector('[data-k-detail]').textContent.indexOf(" + JSON.stringify(B.content) + ") >= 0", { timeout: 20000 });
  const saved = (await getJson("/api/knowledge")).knowledge.filter((k) => k.content.indexOf("（验收草稿）") >= 0);
  check("知识：保存成功后才打开另一条", saved.length === 1, "edited versions=" + saved.length);
  check("知识：打开的目标是 B", (await detailText()).indexOf(B.content) >= 0);

  // 4) 放弃并打开：B 的修改被丢弃
  const A2 = saved[0];
  await startEdit();
  const bDraft = B.content + "（应被放弃）";
  await setDraft(bDraft);
  await openSearch(b);
  await setQuery(b, A2.content.slice(2, 8));
  await b.waitFor("!!document.querySelector('[data-search-kind=\"knowledge\"][data-search-id=\"" + A2.id + "\"]')", { timeout: 15000 });
  await clickSelectorInView(b, '[data-search-kind="knowledge"][data-search-id="' + A2.id + '"]');
  await waitVisible(b, "[data-unsaved-guard]");
  await clickEl(b, "[data-unsaved-discard]");
  await b.waitFor("!document.querySelector('[data-unsaved-guard]') && document.querySelector('[data-k-detail]').textContent.indexOf(" + JSON.stringify(A2.content.slice(0, 12)) + ") >= 0", { timeout: 20000 });
  const bNow = (await getJson("/api/knowledge")).knowledge.find((k) => k.id === B.id);
  check("知识：放弃并打开后原条目内容未变", bNow && bNow.content === B.content);

  // 5) 页面内返回也走同一检查
  await clickEl(b, "[data-k-back]");
  await b.waitFor("(() => { const l = document.querySelector('[data-k-list]'); return !!l && l.getBoundingClientRect().width > 0; })()", { timeout: 12000 });
  await clickItem(b, '[data-k-id="' + B.id + '"]');
  await b.waitFor("document.querySelector('[data-k-detail]').textContent.indexOf(" + JSON.stringify(B.content) + ") >= 0", { timeout: 15000 });
  await startEdit();
  await setDraft(B.content + "（返回草稿）");
  await clickEl(b, "[data-k-back]");
  await waitVisible(b, "[data-unsaved-guard]");
  const backButtons = await guardButtonTexts(b);
  check("知识：页面内返回走同一离开检查", JSON.stringify(backButtons) === JSON.stringify(["继续编辑", "保存并返回", "放弃并返回"]), JSON.stringify(backButtons));
  await clickEl(b, "[data-unsaved-keep]");
  await b.waitFor("!document.querySelector('[data-unsaved-guard]')", { timeout: 10000 });

  // 6) 确认层 Esc：退确认层而不退出管理区
  await clickEl(b, "[data-k-back]");
  await waitVisible(b, "[data-unsaved-guard]");
  await b.pressKey("Escape");
  await b.waitFor("!document.querySelector('[data-unsaved-guard]')", { timeout: 10000 });
  const stillEditing = await b.visible("[data-k-draft] textarea");
  const mgView = await mgAttr(b, "data-view");
  check("知识：确认层 Esc 关闭确认且仍在编辑", stillEditing === true && mgView === "tabs", mgView);

  // 7) 确认期间再次点另一目标：最终只打开最后一次明确选择
  await clickEl(b, "[data-k-back]");
  await waitVisible(b, "[data-unsaved-guard]");
  await b.evalJs("document.querySelector('[data-k-id=\"" + A2.id + "\"]').click()");
  await sleep(150);
  await b.evalJs("document.querySelector('[data-k-id=\"" + C.id + "\"]').click()");
  await sleep(150);
  await clickEl(b, "[data-unsaved-discard]");
  await b.waitFor("!document.querySelector('[data-unsaved-guard]') && document.querySelector('[data-k-detail]').textContent.indexOf(" + JSON.stringify(C.content) + ") >= 0", { timeout: 20000 });
  check("知识：确认期间换目标以最后一次选择为准", (await detailText()).indexOf(C.content) >= 0);
  await shot(b, "knowledge-guard-final");
}

// ---------------------------------------------------------------- 场景：待处理
async function scenarioPending(b) {
  await resetPage(b);
  await openAux(b, "pending");
  await b.waitFor("!!document.querySelector('[data-pending-list]')", { timeout: 15000 });
  await waitVisible(b, '[data-pending-list] [data-pending-id]');

  b.rule({ urlPart: "/api/entities/candidates", method: "GET", action: "status", status: 503, remaining: 20 });
  await b.clickSelector(".p-refresh");
  await waitVisible(b, "[data-pending-source-status]");
  const statusText = await b.textOf("[data-pending-source-status]");
  const rowsAfterPartial = await b.count("[data-pending-id]");
  const totalEmpty = await b.evalJs("document.body.textContent.indexOf('暂无待处理事项') >= 0");
  check("待处理：单源失败保留成功部分", rowsAfterPartial > 0, "rows=" + rowsAfterPartial);
  check("待处理：就地写「暂时读不到待处理事项」并说明范围", statusText.indexOf("暂时读不到待处理事项") >= 0 && statusText.length > 10, statusText.slice(0, 80));
  check("待处理：局部失败不显示总空态", totalEmpty === false);
  await shot(b, "pending-partial-failure");

  b.clearRules();
  await clickSelectorInView(b, ".p-retry");
  await b.waitFor("!document.querySelector('[data-pending-source-status]')", { timeout: 15000 });
  await sleep(600);
  const candidateRows = await b.count('[data-pending-kind="entity_candidate"]');
  check("待处理：单源重试后候选来源恢复", candidateRows > 0, "candidate rows=" + candidateRows);

  b.rule({ urlPart: "/api/memory/pending", method: "GET", action: "status", status: 503, remaining: 20 });
  b.rule({ urlPart: "/api/entities/candidates", method: "GET", action: "status", status: 503, remaining: 20 });
  await b.clickSelector(".p-refresh");
  await waitVisible(b, "[data-pending-source-status]");
  const allFailText = await b.textOf("[data-pending-source-status]");
  const allFailEmpty = await b.evalJs("document.body.textContent.indexOf('暂无待处理事项') >= 0");
  check("待处理：全部失败时两个来源都说明且不冒充空", allFailText.indexOf("暂时读不到待处理事项") >= 0 && allFailEmpty === false, allFailText.slice(0, 100));
  b.clearRules();
}

// ---------------------------------------------------------------- 场景：实体（#49/#34）
async function scenarioEntity(b) {
  await resetPage(b);
  const cards = (await getJson("/api/entities")).entities;
  assert(cards.length >= 1, "没有实体卡");
  const card = cards[0];
  const originalKind = card.kind;
  // 用真实接口把 kind 改成内部 slug person，验证可读名映射（验收后还原）
  const revise = await postJson("/api/entities/" + card.id + "/revise", { kind: "person", expected_revision: card.revision ?? 0 });
  assert(revise.status === 200 && revise.data && revise.data.ok, "改 kind 失败 " + JSON.stringify(revise));

  await resetPage(b);
  await openTab(b, "entity");
  await b.waitFor("!!document.querySelector('[data-e-id=\"" + card.id + "\"]')", { timeout: 15000 });
  await clickItem(b, '[data-e-id="' + card.id + '"]');
  await b.waitFor("!!document.querySelector('[data-e-detail]')", { timeout: 15000 });
  await sleep(250);
  const kindText = await b.textOf("[data-e-detail] .e-read-kind");
  check("实体：内部 kind slug 显示为可读名称", kindText === "人物", String(kindText));
  const moreOpen = await b.evalJs("document.querySelector('[data-e-more]') ? document.querySelector('[data-e-more]').open : null");
  const nodeVisibleClosed = await b.evalJs("(() => { const el = document.querySelector('[data-e-more] .e-node-id'); if (!el) return false; return el.checkVisibility ? el.checkVisibility() : el.getBoundingClientRect().height > 0; })()");
  check("实体：节点编号默认收在按需展开区", moreOpen === false && nodeVisibleClosed === false, "open=" + moreOpen);
  await b.evalJs("document.querySelector('[data-e-more] summary').click()");
  await sleep(200);
  const nodeVisibleOpen = await b.evalJs("(() => { const el = document.querySelector('[data-e-more] .e-node-id'); if (!el) return false; return el.checkVisibility ? el.checkVisibility() : el.getBoundingClientRect().height > 0; })()");
  check("实体：展开后能看到真实节点编号", nodeVisibleOpen === true);
  await shot(b, "entity-detail-hierarchy");

  // #49：显式输入置信度 0
  await clickSelectorInView(b, "[data-e-edit]");
  await waitVisible(b, "[data-e-draft]");
  await b.setInput(".attr-row.add .e-attr-key", "验收数值");
  await b.setInput(".attr-row.add .e-attr-value", "0");
  await b.setInput(".attr-row.add .e-attr-conf", "0");
  await clickSelectorInView(b, ".attr-row.add .e-attr-add");
  await sleep(200);
  const addedConf = await b.evalJs("(document.querySelector('input[aria-label=\"置信度：验收数值\"]') || {}).value");
  const addedValue = await b.evalJs("(document.querySelector('input[aria-label=\"属性值：验收数值\"]') || {}).value");
  check("实体：添加属性时显式 0 不被默认值覆盖", addedConf === "0" && addedValue === "0", "conf=" + addedConf + " value=" + addedValue);

  b.rule({ urlPart: "/api/entities/" + card.id + "/revise", method: "POST", action: "capture" });
  await clickSelectorInView(b, "[data-e-save]");
  await sleep(900);
  const captured = b.captured[b.captured.length - 1];
  let body = null;
  try {
    body = captured && captured.postData ? JSON.parse(captured.postData) : null;
  } catch {
    body = null;
  }
  const attr = body && Array.isArray(body.attributes) ? body.attributes.find((a) => a.key === "验收数值") : null;
  check("实体：请求 payload 里置信度就是 0", Boolean(attr && attr.value === "0" && attr.confidence === 0), JSON.stringify(attr));
  b.clearRules();
  await b.waitFor("!document.querySelector('[data-e-draft]')", { timeout: 20000 });

  // 刷新回显：重新进入编辑，输入框仍是 0
  await b.reload(3500);
  await b.waitFor("!!document.querySelector('[data-planet-entry], .dock')", { timeout: 45000 });
  await openPlanetFromDock(b, { timeout: 45000 });
  await b.waitFor("!!document.querySelector('[data-mp-manage-toggle]')", { timeout: 30000 });
  await waitClickable(b, "[data-mp-manage-toggle]", 45000);
  const w2 = await b.evalJs("(() => { const m = document.querySelector('.management'); return m ? m.getBoundingClientRect().width : 0; })()");
  if (!(w2 > 100)) await clickEl(b, "[data-mp-manage-toggle]");
  await b.waitFor("document.querySelector('.management') && document.querySelector('.management').getBoundingClientRect().width > 100", { timeout: 25000 });
  await openTab(b, "entity");
  await b.waitFor("!!document.querySelector('[data-e-id=\"" + card.id + "\"]')", { timeout: 15000 });
  await clickItem(b, '[data-e-id="' + card.id + '"]');
  await b.waitFor("!!document.querySelector('[data-e-detail]')", { timeout: 15000 });
  await b.clickSelector("[data-e-edit]");
  await waitVisible(b, "[data-e-draft]");
  const echoConf = await b.evalJs("(document.querySelector('input[aria-label=\"置信度：验收数值\"]') || {}).value");
  check("实体：刷新后回显仍为 0", echoConf === "0", String(echoConf));
  await shot(b, "entity-zero-after-reload");

  // 还原 kind（不动用户数据；验收库是隔离目录）
  const now = (await getJson("/api/entities")).entities.find((c) => c.id === card.id);
  if (now && originalKind !== undefined) {
    await postJson("/api/entities/" + card.id + "/revise", { kind: originalKind, expected_revision: now.revision ?? 0 });
  }
}

// ---------------------------------------------------------------- 场景：整理记录
async function scenarioRecords(b) {
  // 先造两个可预测的维护批次：partial = 1 恢复 + 1 拒绝；full = 可完整撤回
  const created = JSON.parse(
    execFileSync(PYTHON, [resolve("scripts/planet-032-verify/make_rollback_batch.py"), "--data-dir", DATA_DIR, "--worktree", resolve(".")], { encoding: "utf8" }),
  );
  assert(created.ok, "造验收批次失败：" + JSON.stringify(created));
  await resetPage(b);
  const list = await getJson("/api/memory/maintenance/batches?limit=50");
  await openAux(b, "records");
  await b.waitFor("!!document.querySelector('[data-records-list] [data-record-id]')", { timeout: 15000 });

  async function openBatch(id) {
    for (let attempt = 0; attempt < 3; attempt += 1) {
      await clickItem(b, '[data-record-id="' + id + '"]');
      await sleep(400);
      const opened = await b.evalJs("(() => { const d = document.querySelector('[data-record-detail]'); if (!d) return false; const r = d.getBoundingClientRect(); return r.width > 0 && r.height > 0; })()");
      if (opened) { await sleep(250); return; }
    }
    throw new Error("整理详情没有打开：" + id);
  }
  async function clickRestore() {
    await waitClickable(b, "[data-record-detail] .mr-preview-batch", 20000);
    // 仲裁器上一次关闭可能晚一帧：点一次没开就等一会再点（最多 4 次），不用固定长睡
    for (let i = 0; i < 4; i += 1) {
      await b.clickSelector("[data-record-detail] .mr-preview-batch");
      await sleep(650);
      if (await b.exists(".restore-dialog")) break;
    }
    await waitVisible(b, ".restore-dialog");
    await sleep(450);
  }

  // 1) 部分恢复：merge 被拒、normalize 恢复成功
  const preview = (await getJson("/api/memory/maintenance/restore/preview?batch_id=" + created.partial_batch_id)).preview;
  const previewNames = preview.operations
    .flatMap((op) => op.targets || [])
    .map((t) => (t.before && t.before.content) || "")
    .filter(Boolean);
  await openBatch(created.partial_batch_id);
  await clickRestore();
  const dialogPreview = await b.textOf(".restore-dialog");
  check("撤回：预览展示可辨认的知识名称", previewNames.some((name) => dialogPreview.indexOf(name.slice(0, 10)) >= 0), "names=" + previewNames.length);
  await b.waitFor("(() => { const el = document.querySelector('.restore-dialog__confirm'); return el && !el.disabled; })()", { timeout: 25000 });
  await b.clickSelector(".restore-dialog__confirm");
  await b.waitFor("!!document.querySelector('[data-restore-result-label]')", { timeout: 40000 });
  await sleep(300);
  const resultText = (await b.textOf("[data-restore-result]")) || "";
  const counts = (await b.textOf("[data-restore-result-counts]")) || "";
  const label = (await b.textOf("[data-restore-result-label]")) || "";
  check("撤回：部分完成按真实结果显示", label.indexOf("部分") >= 0 && counts.indexOf("已恢复 1") >= 0 && counts.indexOf("未恢复 1") >= 0, label + " / " + counts);
  check("撤回：结果含用户可辨认的知识名称", previewNames.some((name) => resultText.indexOf(name.slice(0, 10)) >= 0));
  await shot(b, "records-partial-result");
  const detailAfter = await getJson("/api/memory/maintenance/batches/" + created.partial_batch_id);
  check("撤回：批次状态如实为部分恢复", detailAfter.batch.status === "rollback_partial", detailAfter.batch.status);

  // 2) 完整撤回：清单状态变已撤回，重开只看历史
  await b.clickSelector("[data-records-back]");
  await b.waitFor("!!document.querySelector('[data-records-list]')", { timeout: 15000 });
  await b.clickSelector(".mr-refresh");
  await b.waitFor("!!document.querySelector('[data-record-id=\"" + created.full_batch_id + "\"]')", { timeout: 15000 });
  await openBatch(created.full_batch_id);
  await clickRestore();
  const fullConfirm = await b.exists(".restore-dialog__confirm");
  check("撤回：可完整撤回批次有确认入口", fullConfirm === true);
  await b.waitFor("(() => { const el = document.querySelector('.restore-dialog__confirm'); return el && !el.disabled; })()", { timeout: 25000 });
  await b.clickSelector(".restore-dialog__confirm");
  await b.waitFor("!!document.querySelector('[data-restore-result-label]')", { timeout: 40000 });
  await sleep(300);
  const rolledLabel = (await b.textOf("[data-restore-result-label]")) || "";
  const rolledCounts = (await b.textOf("[data-restore-result-counts]")) || "";
  check("撤回：完整撤回显示已撤回与真实数量", rolledLabel.indexOf("已撤回") >= 0 && rolledCounts.indexOf("已恢复 1") >= 0, rolledLabel + " / " + rolledCounts);
  await shot(b, "records-rolled-back");
  const rolledBatch = await getJson("/api/memory/maintenance/batches/" + created.full_batch_id);
  check("撤回：后端批次状态为 rolled_back", rolledBatch.batch.status === "rolled_back", rolledBatch.batch.status);

  // 3) 重复请求：后端幂等，区分「已撤回」
  const again = await postJson("/api/memory/maintenance/restore", { batch_id: created.full_batch_id, confirm: true });
  check("撤回：重复请求幂等且返回已撤回", again.data && again.data.status === "already_rolled_back", JSON.stringify(again.data && again.data.status));

  // 4) 再打开同一批次：只显示历史，没有可再次执行的确认入口
  await b.clickSelector("[data-records-back]");
  await b.waitFor("!!document.querySelector('[data-records-list]')", { timeout: 15000 });
  await b.clickSelector(".mr-refresh");
  await b.waitFor("!!document.querySelector('[data-record-id=\"" + created.full_batch_id + "\"]')", { timeout: 15000 });
  await openBatch(created.full_batch_id);
  const entryText = await b.evalJs("document.querySelector('[data-record-detail]').textContent");
  check("撤回：已撤回批次入口改为查看撤回情况", entryText.indexOf("查看撤回情况") >= 0);
  await waitClickable(b, "[data-record-detail] .mr-preview-batch", 15000);
  await b.clickSelector("[data-record-detail] .mr-preview-batch");
  await waitVisible(b, ".restore-dialog");
  await sleep(400);
  const readonly = await b.exists("[data-restore-readonly]");
  const confirmBtn = await b.exists(".restore-dialog__confirm");
  const note = await b.textOf("[data-restore-readonly-note]");
  check("撤回：已撤回且无可恢复项时无确认入口", readonly === true && confirmBtn === false, "readonly=" + readonly + " confirm=" + confirmBtn);
  check("撤回：只读历史态如实说明不写入", Boolean(note && note.length > 5), note && note.slice(0, 60));
  await shot(b, "records-history-only");
  await closeRestoreDialog(b);

  // 5) 确认确实为空：没有事项的批次
  const emptyBatch = list.batches.find((x) => x.status === "running" && x.id.indexOf("verify_case") < 0);
  if (emptyBatch) {
    await b.clickSelector("[data-records-back]");
    await b.waitFor("!!document.querySelector('[data-records-list]')", { timeout: 15000 });
    await openBatch(emptyBatch.id);
    await clickRestore();
    const emptyText = await b.textOf(".restore-dialog");
    check("文案：确认确实为空才写「没有可撤回的内容」", emptyText.indexOf("没有可撤回的内容") >= 0, emptyText.slice(0, 80));
    await closeRestoreDialog(b);
  }

  // 6) 预览读取失败：暂时读不到
  await closeRestoreDialog(b);
  b.rule({ urlPart: "/api/memory/maintenance/restore/preview", method: "GET", action: "status", status: 503, remaining: 20 });
  await b.clickSelector("[data-records-back]");
  await b.waitFor("!!document.querySelector('[data-records-list]')", { timeout: 15000 });
  await openBatch(created.partial_batch_id);
  await clickRestore();
  await b.waitFor("!!document.querySelector('.restore-dialog__error')", { timeout: 20000 });
  const errText = await b.textOf(".restore-dialog");
  check("文案：预览读取失败写「暂时读不到可撤回的内容」", errText.indexOf("暂时读不到可撤回的内容") >= 0 && errText.indexOf("没有可撤回的内容") < 0, errText.slice(0, 90));
  await shot(b, "records-preview-failed");
  b.clearRules();
}

// ---------------------------------------------------------------- 场景：窄窗
async function scenarioNarrow(b) {
  await b.setViewport(980, 720);
  await resetPage(b);
  const overflow = await b.evalJs("(() => { const d = document.documentElement; return { doc: d.scrollWidth > d.clientWidth + 1, body: document.body.scrollWidth > document.body.clientWidth + 1 }; })()");
  check("窄窗：管理区展开无横向溢出", overflow.doc === false && overflow.body === false, JSON.stringify(overflow));
  await openSearch(b);
  await setQuery(b, "基线");
  await b.waitFor("!!document.querySelector('[data-search-kind]')", { timeout: 15000 });
  await shot(b, "narrow-search");
  const narrowInputSel = await searchInputSelector(b);
  const inputRect = await b.rectOf(narrowInputSel);
  check("窄窗：搜索输入框在视口内", Boolean(inputRect && inputRect.right <= 980 && inputRect.left >= 0), JSON.stringify(inputRect && { l: Math.round(inputRect.left), r: Math.round(inputRect.right) }));
  await b.pressKey("Escape");
  await b.waitFor("document.querySelector('.management').getAttribute('data-view') !== 'search'", { timeout: 10000 });
  await openTab(b, "knowledge");
  await b.waitFor("!!document.querySelector('[data-k-id]')", { timeout: 15000 });
  await waitClickable(b, "[data-k-id]", 15000);
  await clickItem(b, "[data-k-id]");
  await b.waitFor("(() => { const d = document.querySelector('[data-k-detail]'); if (!d) return false; const r = d.getBoundingClientRect(); return r.width > 0 && r.height > 0; })()", { timeout: 15000 });
  await waitClickable(b, "[data-k-edit]", 15000);
  await clickEl(b, "[data-k-edit]");
  await waitVisible(b, "[data-k-draft] textarea");
  await b.setInput("[data-k-draft] textarea", (await b.evalJs("document.querySelector('[data-k-draft] textarea').value")) + "窄窗草稿");
  await clickEl(b, "[data-k-back]");
  await waitVisible(b, "[data-unsaved-guard]");
  const guardRect = await b.rectOf("[data-unsaved-guard]");
  check("窄窗：离开确认在视口内", Boolean(guardRect && guardRect.width > 100 && guardRect.right <= 982), JSON.stringify(guardRect && { w: Math.round(guardRect.width), r: Math.round(guardRect.right) }));
  await shot(b, "narrow-guard");
}

// ---------------------------------------------------------------- 主流程
const browser = await boot();
currentBrowser = browser;
try {
  // 冷启动预热：首次 WebGL 初始化/星球迷场很慢，先完整走一遍再开始计分场景
  await resetPage(browser);
  await sleep(800);
  if (want("search")) await scenario("搜索分源与旧响应（#30）", () => scenarioSearch(browser));
  if (want("esc")) await scenario("Esc 与焦点（#32）", () => scenarioEsc(browser));
  if (want("copy")) await scenario("六处文案", () => scenarioCopy(browser));
  if (want("knowledge")) await scenario("知识离开检查（#29）", () => scenarioKnowledge(browser));
  if (want("pending")) await scenario("待处理分源（#31）", () => scenarioPending(browser));
  if (want("entity")) await scenario("实体显式 0 与详情层级（#34/#49）", () => scenarioEntity(browser));
  if (want("records")) await scenario("整理记录撤回（#33/#35/#2）", () => scenarioRecords(browser));
  if (want("narrow")) await scenario("窄窗与键盘可达", () => scenarioNarrow(browser));
} finally {
  await browser.close();
  const failed = results.filter((x) => !x.ok);
  const report = { app: APP, api: API, dataDir: DATA_DIR, at: new Date().toISOString(), total: results.length, passed: results.length - failed.length, failed: failed.length, results };
  writeFileSync(join(OUT, "report.json"), JSON.stringify(report, null, 2), "utf8");
  const md = ["# 星球 032 验收报告", "", "- app: " + APP, "- data: " + DATA_DIR, "- 结果: " + report.passed + "/" + report.total + " passed", "", "| 结果 | 检查项 | 说明 |", "| --- | --- | --- |"];
  for (const r of results) md.push("| " + (r.ok ? "PASS" : "FAIL") + " | " + r.name.replace(/\|/g, "/") + " | " + r.detail.replace(/\|/g, "/").slice(0, 160) + " |");
  writeFileSync(join(OUT, "report.md"), md.join("\n"), "utf8");
  console.log("\n===== " + report.passed + "/" + report.total + " passed, " + report.failed + " failed =====");
  console.log("evidence: " + OUT);
  if (failed.length) process.exitCode = 1;
}