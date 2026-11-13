/**
 * QIO 0.3.2 返修轮 · 真实浏览器验收（round2）
 *
 * 覆盖本轮四项：
 *   p1 代码预览：对话生成代码 → 打开预览 → 关标签 → 进设置 → 返回 → 重开同一代码块（正文/搜索/滚动）
 *   p2 上传中 → 设置 → 返回：成功状态与文件卡数量（#46 体验保持）
 *   c1 联网搜索通道行真实状态（不再有常驻判据说明）
 *   c2 附件关联成功文案只有一句
 *   c3 更新失败：主行 + 下一步 + 按需展开诊断（浏览器里没有 Tauri 更新器 → 真实失败路径）
 *
 * 只读驱动：不改产品代码；模型调用只打本机 fake provider。
 */
import { appendFileSync, mkdirSync, writeFileSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const HERE = dirname(fileURLToPath(import.meta.url));
const WT = process.env.QIO_UI_WT || 'D:/qio-dev/pm-qio032-integration';
const BASE = process.env.QIO_E2E_BASE || 'http://127.0.0.1:5199';
const API = process.env.QIO_E2E_API || 'http://127.0.0.1:8734';
const PROVIDER = process.env.QIO_E2E_PROVIDER || 'http://127.0.0.1:8807';
const OUT = resolve(process.env.QIO_UI_OUT || HERE);
const SHOTS = join(OUT, 'shots');
const MATERIALS = join(OUT, 'materials');
const CDP_PORT = Number(process.env.QIO_UI_CDP_PORT || '9333');
const ONLY = String(process.env.QIO_UI_ONLY || '').split(',').map((s) => s.trim()).filter(Boolean);
const RUN_ID = new Date().toISOString().replace(/[-:T.]/g, '').slice(0, 14);
const LOG_FILE = join(OUT, 'run.log');

mkdirSync(SHOTS, { recursive: true });
mkdirSync(MATERIALS, { recursive: true });
writeFileSync(LOG_FILE, '', 'utf8');

const cdp = await import(pathToFileURL(join(WT, 'scripts', 'verify-pm-qio032', 'cdp.mjs')).href);
const { CdpBrowser, Page, sleep } = cdp;

let browser = null;
let page = null;
const results = [];
const simulated = [];
const unverifiedNotes = [];
let stepNo = 0;

function log(line) {
  const text = String(line);
  console.log(text);
  try { appendFileSync(LOG_FILE, text + '\n', 'utf8'); } catch { /* ignore */ }
}
function record(name, ok, detail, opts) {
  const unverified = Boolean(opts && opts.unverified);
  const entry = { name, ok: Boolean(ok) && !unverified, unverified, detail: detail === undefined ? null : detail };
  results.push(entry);
  const tag = unverified ? 'UNVERIFIED' : (entry.ok ? 'PASS' : 'FAIL');
  let body = '';
  if (detail !== undefined) { try { body = ' :: ' + JSON.stringify(detail).slice(0, 1200); } catch { body = ''; } }
  log('[' + tag + '] ' + name + body);
  return entry.ok;
}
function markUnverified(name, reason, detail) {
  unverifiedNotes.push({ name, reason });
  record(name, false, Object.assign({ note: reason }, detail || {}), { unverified: true });
}
function must(cond, message) { if (!cond) throw new Error(message); }
async function shot(name) {
  stepNo += 1;
  const file = join(SHOTS, String(stepNo).padStart(2, '0') + '-' + name + '.png');
  await page.screenshot(file);
  return file;
}
async function shotSafe(name) { try { return await shot(name); } catch { return null; } }
async function waitExpr(expression, timeout, label) {
  const deadline = Date.now() + (timeout || 20000);
  let last = null;
  while (Date.now() < deadline) {
    try { last = await page.eval('(function(){ try { return (' + expression + '); } catch (e) { return null; } })()'); }
    catch (e) { last = 'ERR'; }
    if (last) return last;
    await sleep(200);
  }
  throw new Error('等待超时（' + (label || expression).slice(0, 70) + '），最后值=' + JSON.stringify(last));
}
const exists = (sel) => page.exists(sel);
const count = (sel) => page.count(sel);
const text = (sel, i) => page.text(sel, i || 0);
const click = (sel, i) => page.click(sel, i || 0, { timeout: 20000 });
const typeInto = (sel, v) => page.type(sel, v);
const pressKey = (k) => page.pressKey(k);
const inputValue = (sel) => page.inputValue(sel);

async function providerReset() {
  await fetch(PROVIDER + '/__reset', { method: 'POST', headers: { 'content-type': 'application/json' }, body: '{}' });
}
async function providerScript(steps, fallback) {
  const body = { steps: steps };
  if (fallback) body.default = fallback;
  const r = await fetch(PROVIDER + '/__script', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body) });
  return r.status;
}

const RESOLUTION_JSON = JSON.stringify({ relation: 'new', target_requirement_id: null, target_version: null, corrections: [], limits: [], removed_limits: [], reason: '界面验收测试消息：无限制' });
function codeReplyText() {
  const py = [];
  for (let i = 1; i <= 70; i += 1) py.push('line_' + String(i).padStart(3, '0') + ' = "alpha payload ' + i + '"');
  const fence = String.fromCharCode(96, 96, 96);
  return '下面是示例（' + RUN_ID + '）：\n\n' + fence + 'python\n' + py.join('\n') + '\n' + fence + '\n';
}

/** 首次引导（欢迎页）可能在本版本首次打开时自动展开：先关掉它，否则所有点击都被挡住。 */
async function dismissOnboarding() {
  const visible = await page.eval("(function(){ const el=document.querySelector('.onboarding'); if(!el) return false; const st=getComputedStyle(el); return st.display !== 'none' && st.visibility !== 'hidden'; })()");
  if (!visible) return { wasOpen: false };
  await click('.onboarding-close').catch(() => null);
  await sleep(600);
  // 关闭询问（填过内容才出现）：确认丢弃
  const ask = await page.eval("(function(){ const el=document.querySelector('.onboarding-close-ask'); return Boolean(el && el.offsetParent !== null); })()");
  if (ask) {
    await page.eval("(function(){ const btns=[...document.querySelectorAll('.onboarding-close-ask button')]; const b=btns.find(function(x){ return (x.innerText||'').indexOf('关闭并丢弃')>=0; }); if(b){ b.click(); return true; } return false; })()");
    await sleep(700);
  }
  const stillOpen = await page.eval("(function(){ const el=document.querySelector('.onboarding'); if(!el) return false; const st=getComputedStyle(el); return st.display !== 'none'; })()");
  return { wasOpen: true, stillOpen };
}

async function enterConversationMode() {
  if (await exists('[data-im="board"]')) {
    await click('#composer-mode-switch .mode-btn');
    await waitExpr("!document.querySelector('[data-im=\"board\"]')", 20000, '回到对话页');
    await sleep(800);
  }
  await waitExpr('document.querySelector(".stream") && document.querySelector("#composer-mode-switch .mode-btn")', 60000, '对话页就绪');
  await sleep(400);
}
async function enterInteractiveMode() {
  await enterConversationMode();
  if (await exists('[data-im="board"]')) return { already: true };
  await click('#composer-mode-switch .mode-btn');
  await waitExpr("document.querySelector('[data-im=\"board\"]')", 30000, '互动板面');
  await sleep(1500);
  return { already: false };
}
async function openSettingsPage() {
  if (await exists('[data-testid="settings-page"]')) return;
  const ok = await page.eval('(function(){ const b=[...document.querySelectorAll("button")].find(function(x){ const l=x.getAttribute("aria-label")||""; return l.indexOf("设置")===0; }); if(!b) return false; b.click(); return true; })()');
  must(ok, '找不到设置入口');
  await waitExpr("document.querySelector('[data-testid=\"settings-page\"]')", 20000, '设置页');
  await sleep(700);
}
async function backFromSettingsPage() {
  must(await exists('[data-testid="settings-back"]'), '设置页没有返回按钮');
  await click('[data-testid="settings-back"]');
  await waitExpr("!document.querySelector('[data-testid=\"settings-page\"]')", 20000, '离开设置页');
  await sleep(900);
  await waitExpr("document.querySelector('.stream') || document.querySelector('[data-im=\"board\"]')", 30000, '返回后应用就绪');
}
async function selectSettingsCategory(id) {
  await click('[data-settings-nav-tab=' + JSON.stringify(id) + ']');
  await sleep(600);
}

// ---------- p1：代码预览往返 ----------
async function readPreviewFacts() {
  return page.eval(`(function(){
    const reader = document.querySelector('.pv-reader');
    const tab = document.querySelector('.pv-tab.is-active');
    const loading = document.querySelector('[data-pv-loading]');
    const problem = document.querySelector('[data-pv-problem]');
    const scroll = document.querySelector('[data-pv-scroll]');
    const lines = document.querySelectorAll('[data-line]');
    return {
      tabId: tab ? tab.getAttribute('data-tab-id') : null,
      tabCount: document.querySelectorAll('.pv-tab').length,
      readerKind: reader ? reader.getAttribute('data-pv-kind') : null,
      loading: Boolean(loading),
      loadingText: loading ? (loading.innerText || '').trim().slice(0, 120) : null,
      problem: problem ? (problem.innerText || '').trim().slice(0, 200) : null,
      search: Boolean(document.querySelector('.pv-search-input')),
      scroll: Boolean(scroll),
      lineCount: lines.length,
      bodyHead: (document.querySelector('.pv-content') || {}).innerText ? document.querySelector('.pv-content').innerText.slice(0, 120) : null,
    };
  })()`);
}

async function p1PreviewRoundTrip() {
  await enterConversationMode();
  await page.setViewport(1440, 900);
  await sleep(800);
  // 清掉历史预览标签
  for (let pass = 0; pass < 2; pass += 1) {
    if ((await exists('.preview-reopen')) && !(await exists('.pv-tab'))) { await click('.preview-reopen').catch(() => null); await sleep(600); }
    let guard = 0;
    while ((await count('.pv-tab-close')) > 0 && guard < 12) { await click('.pv-tab-close', 0); await sleep(300); guard += 1; }
    await sleep(500);
  }
  await providerReset();
  await providerScript([{ chunks: [RESOLUTION_JSON], chunk_delay_ms: 0 }, { chunks: [codeReplyText()], chunk_delay_ms: 0 }], { chunks: [codeReplyText()], chunk_delay_ms: 0 });
  const marker = '下面是示例（' + RUN_ID + '）';
  await typeInto('#composer-input', 'UI-ROUND2 ' + RUN_ID + '：请回复一段示例代码');
  await waitExpr('document.querySelector(".send-btn") && document.querySelector(".send-btn").getAttribute("data-ready") === "true"', 15000, '可发送');
  await click('.send-btn');
  const deadline = Date.now() + 120000;
  let st = null;
  while (Date.now() < deadline) {
    st = await page.eval('(function(){ return { hasMarker: document.body.innerText.indexOf(' + JSON.stringify(marker) + ') >= 0, buttons: document.querySelectorAll(".code-preview-btn").length }; })()');
    if (st.hasMarker && st.buttons >= 1) break;
    await sleep(500);
  }
  must(st && st.hasMarker && st.buttons >= 1, '助手回复代码块未出现：' + JSON.stringify(st));
  await sleep(2500);
  await shot('p1-reply-codeblock');

  const btnIndex = (await count('.code-preview-btn')) - 1;
  await click('.code-preview-btn', btnIndex);
  await waitExpr('document.querySelector(".pv-reader")', 25000, '预览阅读器');
  await sleep(2500);
  const first = await readPreviewFacts();
  record('p1-1 首次打开回复代码：正文可读（不是停在读取中）', first.loading === false && first.lineCount > 0 && !first.problem, first);
  must(first.search, '首次打开没有搜索框：' + JSON.stringify(first));
  await typeInto('.pv-search-input', 'alpha payload 7');
  await sleep(800);
  const searchCount = await text('[data-pv-search-count]');
  const scrolled = await page.eval('(function(){ const el=document.querySelector("[data-pv-scroll]"); if(!el) return null; el.scrollTop = 300; el.dispatchEvent(new Event("scroll",{bubbles:true})); return el.scrollTop; })()');
  await sleep(700);
  record('p1-2 首次打开：搜索框可用（有命中计数）且正文可滚动', Boolean(searchCount) && Number(scrolled) > 0, { searchCount, scrollTop: scrolled });
  await shot('p1-first-open');

  // 关闭该标签
  await click('.pv-tab-close', 0);
  await sleep(900);
  const afterClose = await count('.pv-tab');
  record('p1-3 关闭预览标签', afterClose === 0, { tabCount: afterClose });

  // 进设置 → 返回
  await openSettingsPage();
  await sleep(900);
  await shot('p1-settings');
  await backFromSettingsPage();
  await sleep(1200);
  await shot('p1-back-to-conversation');

  // 重新打开同一段代码
  const btnIndex2 = (await count('.code-preview-btn')) - 1;
  await click('.code-preview-btn', btnIndex2);
  await waitExpr('document.querySelector(".pv-reader")', 25000, '返回后预览阅读器');
  await sleep(3000);
  const second = await readPreviewFacts();
  record('p1-4 设置返回后重开同一代码块：直接可读（不再停在「正在读取这个文件…」）',
    second.loading === false && second.lineCount > 0 && !second.problem,
    second);
  record('p1-5 设置返回后重开：搜索框与滚动容器都在', second.search === true && second.scroll === true, second);
  if (second.search) {
    await typeInto('.pv-search-input', 'alpha payload 9');
    await sleep(800);
    const c2 = await text('[data-pv-search-count]');
    const sc2 = await page.eval('(function(){ const el=document.querySelector("[data-pv-scroll]"); if(!el) return null; el.scrollTop = 240; el.dispatchEvent(new Event("scroll",{bubbles:true})); return el.scrollTop; })()');
    await sleep(600);
    record('p1-6 设置返回后重开：搜索与滚动真实可用', Boolean(c2) && Number(sc2) > 0, { searchCount: c2, scrollTop: sc2 });
  } else {
    record('p1-6 设置返回后重开：搜索与滚动真实可用', false, second);
  }
  await shot('p1-reopen-after-settings');

  // 同一代码块不重复开标签
  const tabsNow = await count('.pv-tab');
  record('p1-7 同一代码块重开不重复开标签', tabsNow === 1, { tabCount: tabsNow });
}

// ---------- p2：上传中 → 设置 → 返回 ----------
async function p2UploadAcrossSettings() {
  await enterInteractiveMode();
  await sleep(800);
  const before = await count('[data-im="card"]');
  const name = 'round2-' + RUN_ID + '.txt';
  const path = join(MATERIALS, name);
  writeFileSync(path, 'round2 upload fixture ' + RUN_ID + '\n' + '正文行\n'.repeat(10), 'utf8');

  await page.delayRequests(function (url, method) { return method === 'POST' && String(url).indexOf('/api/attachments/upload') >= 0; }, 4000);
  simulated.push('p2 上传延迟 4s（CDP Fetch delayRequests，真实端点 POST /api/attachments/upload）');
  await click('[data-im="add-menu"]');
  await waitExpr('document.querySelector("[data-im=\"add-menu-list\"]")', 10000, '添加菜单');
  const chooser = page.setFilesOnChooser([path], { timeout: 20000 });
  await click('[data-im="add-file"]');
  await chooser;
  await waitExpr('document.querySelectorAll("[data-im=\"card\"]").length > ' + before, 20000, '上传中文件卡出现');
  await sleep(600);
  await shot('p2-uploading-interactive');

  await openSettingsPage();
  await sleep(1200);
  await shot('p2-uploading-settings');
  await backFromSettingsPage();
  // 成功提示是 3 秒的临时提示：返回后立刻轮询抓它（抓不到就是「没显示」或「已过期」）。
  const noticeDeadline = Date.now() + 12000;
  let notice = null;
  while (Date.now() < noticeDeadline) {
    notice = await page.eval(`(function(){
      const host = document.querySelector('[data-im="add-material-status"]');
      if (!host) return null;
      const t = host.querySelector('[data-im="add-material-status-text"]') || host;
      return { text: (t.innerText || '').trim(), retry: Boolean(host.querySelector('[data-im="add-material-retry"]')) };
    })()`);
    if (notice && notice.text) break;
    await sleep(250);
  }
  await shot('p2-returned-board');
  await sleep(1200);

  const after = await count('[data-im="card"]');
  const cardText = await page.eval("(function(){ const c=document.querySelectorAll('[data-im=\"card\"]')[" + before + "]; return c ? (c.innerText||'').trim().slice(0,200) : null; })()");
  record('p2-1 上传中进设置、返回后文件卡只多一张（无重复卡）', after === before + 1, { before, after, cardText });
  record('p2-2 返回后成功提示只有一句「已关联附件『文件名』」',
    Boolean(notice) && notice.text === '已关联附件「' + name + '」',
    { notice, expected: '已关联附件「' + name + '」' });
  record('p2-3 返回后没有要求重新选文件 / 重新上传', !(notice && notice.retry), { notice });
  await sleep(1500);
  const stable = await count('[data-im="card"]');
  record('p2-4 卡片数量稳定（不重复结算）', stable === after, { after, stable });
}

// ---------- c1：联网搜索通道状态 ----------
async function c1SearchChannelState() {
  await openSettingsPage();
  await selectSettingsCategory('models');
  await sleep(900);
  const facts = await page.eval(`(function(){
    const rows = [...document.querySelectorAll('[data-search-channel]')].map(function(row){
      const cfg = row.querySelector('[data-channel-config]');
      const chk = row.querySelector('[data-channel-check]');
      return {
        id: row.getAttribute('data-search-channel'),
        state: row.getAttribute('data-channel-state'),
        config: cfg ? (cfg.innerText || '').trim() : null,
        check: chk ? (chk.innerText || '').trim() : null,
        checkState: chk ? chk.getAttribute('data-channel-check-state') : null,
      };
    });
    const hint = document.querySelector('[data-settings-key="models.search.status"] .set-hint');
    return { rows: rows, hint: hint ? (hint.innerText || '').trim() : null, bodyHasOldSentence: document.body.innerText.indexOf('不代表「检查通过」') >= 0 };
  })()`);
  record('c1-1 联网搜索不再有常驻「已配置不代表检查通过」判据说明',
    facts.bodyHasOldSentence === false && !(facts.hint && facts.hint.indexOf('不代表') >= 0),
    { hint: facts.hint, bodyHasOldSentence: facts.bodyHasOldSentence });
  const withState = facts.rows.filter((r) => r.checkState && r.checkState !== 'off');
  record('c1-2 通道行显示真实检查状态（data-channel-check-state）',
    facts.rows.length > 0 && withState.length > 0 && withState.every((r) => ['unchecked', 'ok', 'failed'].indexOf(r.checkState) >= 0),
    { rows: facts.rows });
  await shot('c1-search-channels');

  // 请求失败 → 「这次检查失败」+ 重试
  await page.delayRequests(function (url, method) { return method === 'POST' && String(url).indexOf('/api/settings/search/check') >= 0; }, 30 * 60 * 1000);
  let rules = [];
  page.browser.on(function (msg) {
    if (!page || msg.sessionId !== page.sessionId || msg.method !== 'Fetch.requestPaused') return;
    const p = msg.params || {};
    const req = p.request || {};
    if (String(req.url || '').indexOf('/api/settings/search/check') < 0) return;
    page.browser.send('Fetch.fulfillRequest', {
      requestId: p.requestId, responseCode: 500,
      responseHeaders: [{ name: 'Content-Type', value: 'application/json' }, { name: 'Access-Control-Allow-Origin', value: '*' }],
      body: Buffer.from('{"ok":false,"error":"simulated check failure"}', 'utf8').toString('base64'),
    }, page.sessionId).catch(function () { return null; });
  });
  simulated.push('c1 连接检查请求 500（CDP Fetch fulfillRequest）');
  await click('[data-testid="settings-search-check"]');
  await sleep(2500);
  const notice = await page.eval(`(function(){
    const el = document.querySelector('.set-notice, [role="status"], [role="alert"]');
    const all = [...document.querySelectorAll('.set-notice, [role="status"], [role="alert"]')].map(function(n){ return (n.innerText||'').trim(); }).filter(Boolean);
    return { first: el ? (el.innerText||'').trim() : null, all: all.slice(0, 6) };
  })()`);
  const hasFailText = notice.all.some((t) => t.indexOf('这次检查失败') >= 0);
  record('c1-3 检查请求失败时用「这次检查失败」并保留重试', hasFailText, notice);
  await shot('c1-check-failed');
  await page.clearFailRequests().catch(() => null);
}

// ---------- c2：附件关联成功文案（已在 p2 覆盖，这里补卡片侧不重复） ----------
async function c2AttachCopy() {
  // 上一场景可能停在设置页：先退出来再进互动页（enterConversationMode 只处理互动↔对话）。
  if (await exists('[data-testid="settings-page"]')) await backFromSettingsPage();
  await enterInteractiveMode();
  await sleep(700);
  const facts = await page.eval(`(function(){
    const host = document.querySelector('[data-im="add-material-status"]');
    const t = host ? (host.querySelector('[data-im="add-material-status-text"]') || host) : null;
    const text = t ? (t.innerText || '').trim() : '';
    return { text: text, hasSourceNote: text.indexOf('工作区材料源文件') >= 0 || text.indexOf('浏览器上传') >= 0, hasLocalNote: text.indexOf('板面关联是本地改动') >= 0 };
  })()`);
  record('c2-1 附件关联成功提示不含来源说明与「本地改动」复述',
    facts.hasSourceNote === false && facts.hasLocalNote === false,
    facts);
}

// ---------- c3：更新失败诊断 ----------
async function c3UpdateFailure() {
  await openSettingsPage();
  await selectSettingsCategory('about');
  await sleep(1200);
  await waitExpr("document.querySelector('[data-qio-fit]') || document.querySelector('[data-action=\"check\"]')", 20000, '更新卡片');
  await click('[data-action="check"]');
  await sleep(4000);
  const facts = await page.eval(`(function(){
    const card = document.querySelector('.update-card');
    if (!card) return { missing: true };
    const failure = card.querySelector('[data-update-failure]');
    const next = card.querySelector('[data-update-next]');
    const diag = card.querySelector('[data-update-diagnostics]');
    return {
      phaseText: (card.innerText || '').trim().slice(0, 400),
      failure: failure ? (failure.innerText || '').trim() : null,
      next: next ? (next.innerText || '').trim() : null,
      hasDiagnostics: Boolean(diag),
      diagnosticsOpen: diag ? diag.hasAttribute('open') : null,
      diagnosticsText: diag ? (diag.innerText || '').trim().slice(0, 300) : null,
      mainHasProxy: (failure ? (failure.innerText || '') : '').indexOf('updater-proxy') >= 0,
      mainHasLog: (failure ? (failure.innerText || '') : '').indexOf('qio.log') >= 0,
    };
  })()`);
  record('c3-1 更新失败主行只给可理解的失败状态（不含代理/日志路径）',
    Boolean(facts.failure) && facts.mainHasProxy === false && facts.mainHasLog === false,
    facts);
  record('c3-2 更新失败给出实际可用的下一步', Boolean(facts.next), { next: facts.next });
  record('c3-3 原始错误/代理/日志在按需展开的诊断里（默认折叠）',
    facts.hasDiagnostics === true && facts.diagnosticsOpen === false && Boolean(facts.diagnosticsText),
    { hasDiagnostics: facts.hasDiagnostics, open: facts.diagnosticsOpen, text: facts.diagnosticsText });
  await shot('c3-update-failed');
  if (facts.hasDiagnostics) {
    await page.eval('(function(){ const d=document.querySelector("[data-update-diagnostics]"); if(d) d.open = true; return true; })()');
    await sleep(500);
    const opened = await page.eval('(function(){ const d=document.querySelector("[data-update-diagnostics]"); return d ? (d.innerText||"").trim().slice(0,300) : null; })()');
    record('c3-4 展开诊断后能看到原始错误与代理/日志路径',
      Boolean(opened) && opened.indexOf('updater-proxy') >= 0 && opened.indexOf('qio.log') >= 0,
      { opened });
    await shot('c3-update-diagnostics-open');
  }
}

async function runScenario(id, label, fn) {
  log('');
  log('---- ' + id + ' ' + label + ' ----');
  try { await fn(); }
  catch (error) {
    record(id + ' 场景未跑完', false, { error: String((error && error.stack) || error).slice(0, 1500) });
    await shotSafe(id + '-failed');
  }
}

async function main() {
  log('== QIO round2 ui-verify == runId=' + RUN_ID);
  log('base=' + BASE + ' api=' + API + ' provider=' + PROVIDER + ' cdp=' + CDP_PORT);
  browser = await CdpBrowser.launch({ port: CDP_PORT });
  page = await Page.create(browser);
  await page.setViewport(1440, 900);
  await page.goto(BASE + '/?round2=' + RUN_ID);
  await waitExpr('document.querySelector(".stream") && document.querySelector("#composer-mode-switch .mode-btn")', 60000, '应用就绪');
  await sleep(900);
  const onboarding = await dismissOnboarding();
  log('onboarding=' + JSON.stringify(onboarding));
  await sleep(600);
  const run = (id) => ONLY.length === 0 || ONLY.indexOf(id) >= 0;
  if (run('p1')) await runScenario('p1', '代码预览：设置返回后恢复可用', p1PreviewRoundTrip);
  if (run('p2')) await runScenario('p2', '上传中→设置→返回', p2UploadAcrossSettings);
  if (run('c1')) await runScenario('c1', '联网搜索通道状态', c1SearchChannelState);
  if (run('c2')) await runScenario('c2', '附件关联成功文案', c2AttachCopy);
  if (run('c3')) await runScenario('c3', '更新失败诊断', c3UpdateFailure);
}

let fatal = null;
try { await main(); }
catch (error) { fatal = error; record('主流程未跑完', false, { error: String((error && error.stack) || error).slice(0, 2000) }); }
finally {
  try { if (browser) await browser.close(); } catch { /* ignore */ }
  const pass = results.filter((r) => r.ok).length;
  const unverified = results.filter((r) => r.unverified).length;
  const fail = results.filter((r) => !r.ok && !r.unverified).length;
  const summary = {
    runId: RUN_ID, base: BASE, api: API, provider: PROVIDER, cdpPort: CDP_PORT, out: OUT,
    pass, fail, unverified, total: results.length,
    results, simulated, unverifiedNotes,
    consoleErrors: page ? page.consoleErrors.slice(0, 60) : [],
    note: '真实界面驱动：系统 Edge + CDP；模型只打本机 fake provider；[模拟] 见 simulated。',
  };
  writeFileSync(join(OUT, 'summary.json'), JSON.stringify(summary, null, 2), 'utf8');
  writeFileSync(join(OUT, 'console.log'), results.map((r) => '[' + (r.unverified ? 'UNVERIFIED' : (r.ok ? 'PASS' : 'FAIL')) + '] ' + r.name + (r.detail ? ' :: ' + JSON.stringify(r.detail) : '')).join('\n') + '\n', 'utf8');
  log('');
  log('== 结果：PASS ' + pass + ' / FAIL ' + fail + ' / UNVERIFIED ' + unverified + ' / TOTAL ' + results.length + ' ==');
  if (fatal) log('FATAL=' + String((fatal && fatal.message) || fatal));
  process.exit(fail > 0 || fatal ? 1 : 0);
}
