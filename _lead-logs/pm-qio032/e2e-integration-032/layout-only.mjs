/**
 * #47 宽窗布局定向复跑（只补这一块；不覆盖已有 summary.json）。
 *
 * 关键改动（相对 ui-verify.mjs 的 s1）：发消息拿到两个代码块后**先整页 reload**，
 * 让消息持久化（id 从 local_ 变成服务端 id），再点最后两个代码块预览按钮。
 * 依据：diag-preview 证明已持久化消息的代码预览能加载（searchInput/scroll 都为 true），
 * 而刚流式产出、local_ 临时 id 的消息会停在「正在读取这个文件…」。
 *
 * 产物：summary-layout.json + shots-layout/*.png + run-layout.log（不覆盖 summary.json）。
 */
import { appendFileSync, mkdirSync, writeFileSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { execFileSync } from 'node:child_process';

const HERE = dirname(fileURLToPath(import.meta.url));
const WT = process.env.QIO_UI_WT || 'D:/qio-dev/pm-qio032-integration';
const BASE = process.env.QIO_E2E_BASE || 'http://127.0.0.1:5199';
const API = process.env.QIO_E2E_API || 'http://127.0.0.1:8734';
const PROVIDER = process.env.QIO_E2E_PROVIDER || 'http://127.0.0.1:8807';
const OUT = resolve(process.env.QIO_UI_OUT || HERE);
const SHOTS = join(OUT, 'shots-layout');
const RUN_ID = new Date().toISOString().replace(/[-:T.]/g, '').slice(0, 14);
const LOG = join(OUT, 'run-layout.log');
mkdirSync(SHOTS, { recursive: true });
writeFileSync(LOG, '', 'utf8');

const { CdpBrowser, Page, sleep } = await import(pathToFileURL(join(WT, 'scripts/verify-pm-qio032/cdp.mjs')).href);

const results = [];
const simulated = [];
const unverified = [];
let stepNo = 0;
let page = null;
let browser = null;

function log(line) { const t = String(line); console.log(t); try { appendFileSync(LOG, t + '\n', 'utf8'); } catch {} }
function record(name, ok, detail, opts) {
  const isUnverified = Boolean(opts && opts.unverified);
  const entry = { name, ok: Boolean(ok) && !isUnverified, unverified: isUnverified, detail: detail === undefined ? null : detail };
  results.push(entry);
  const tag = isUnverified ? 'UNVERIFIED' : (entry.ok ? 'PASS' : 'FAIL');
  let body = '';
  try { body = detail === undefined ? '' : ' :: ' + JSON.stringify(detail).slice(0, 1500); } catch { body = ''; }
  log('[' + tag + '] ' + name + body);
  return entry.ok;
}
function markUnverified(name, reason, detail) {
  unverified.push({ name, reason });
  return record(name, false, Object.assign({ kind: 'unverified', reason }, detail || {}), { unverified: true });
}
function must(cond, message) { if (!cond) throw new Error('断言失败：' + message); }
async function shot(name) {
  stepNo += 1;
  const file = join(SHOTS, String(stepNo).padStart(2, '0') + '-' + name + '.png');
  await page.screenshot(file);
  return file;
}
async function shotSafe(name) { try { return await shot(name); } catch { return null; } }
async function waitExpr(expr, timeout, label) { return page.waitFor(expr, { timeout: timeout || 20000, label: label || '' }); }
async function count(sel) { return page.count(sel); }
async function exists(sel) { return page.exists(sel); }
async function click(sel, index) { return page.click(sel, index || 0, { timeout: 20000 }); }
async function readWidth(sel) { return page.eval('(function(){ const el = document.querySelector(' + JSON.stringify(sel) + '); return el ? el.getBoundingClientRect().width : null; })()'); }

async function fetchJson(url, init) {
  const res = await fetch(url, init);
  const text = await res.text();
  let json = null;
  try { json = text ? JSON.parse(text) : null; } catch {}
  if (!res.ok) throw new Error('HTTP ' + res.status + ' ' + url + ' :: ' + text.slice(0, 200));
  return json;
}
function parseLayout(raw) {
  if (raw && typeof raw === 'object') return raw;
  if (typeof raw !== 'string' || !raw.trim()) return null;
  try { return JSON.parse(raw); } catch { return null; }
}
async function layoutPrefs() {
  const settings = await fetchJson(API + '/api/settings/ui');
  const prefs = parseLayout(settings && settings.conversation_layout);
  must(prefs && typeof prefs === 'object', '服务端没有可解析的 conversation_layout');
  return prefs;
}
async function waitLayout(predicate, label, timeout) {
  const deadline = Date.now() + (timeout || 15000);
  let last = null;
  while (Date.now() < deadline) {
    try { last = await layoutPrefs(); if (predicate(last)) return last; } catch (error) { last = { error: String(error && error.message || error) }; }
    await sleep(300);
  }
  throw new Error('等待超时（' + label + '），最后值=' + JSON.stringify(last).slice(0, 800));
}

async function overviewFacts() {
  return page.eval(`(function(){
    const ws = document.querySelector('.conversation-workspace');
    const left = document.querySelector('.left-pane');
    const right = document.querySelector('.right-pane');
    return {
      leftPane: Boolean(left),
      leftWidth: left ? left.getBoundingClientRect().width : null,
      rightPane: Boolean(right),
      rightWidth: right ? right.getBoundingClientRect().width : null,
      leftAuto: ws ? ws.getAttribute('data-left-auto') : null,
      previewFull: ws ? ws.getAttribute('data-preview-full') : null,
      overlay: Boolean(document.querySelector('.preview-overlay')),
      reopen: Boolean(document.querySelector('.preview-reopen')),
      reopenLabel: (document.querySelector('.preview-reopen') || {}).ariaLabel || null,
      centerWidth: ws ? ws.getAttribute('data-center-width') : null,
      viewport: { w: innerWidth, h: innerHeight },
    };
  })()`);
}
async function readTabStrip() {
  return page.eval(`(function(){
    return [...document.querySelectorAll('.pv-tab')].map(function(tab){
      return { id: tab.getAttribute('data-tab-id'), active: tab.classList.contains('is-active'), title: (tab.innerText || '').trim() };
    });
  })()`);
}
async function readPreviewState() {
  const tabs = await readTabStrip();
  if (!tabs.length) return { tabs: [], active: null, states: [] };
  const activeBefore = (tabs.find((t) => t.active) || tabs[0]).id;
  const states = [];
  for (let i = 0; i < tabs.length; i += 1) {
    await click('.pv-tab-main', i);
    await sleep(900);
    const search = await page.inputValue('.pv-search-input');
    const scrollTop = await page.eval('(function(){ const el = document.querySelector("[data-pv-scroll]"); return el ? el.scrollTop : null; })()');
    states.push({ id: tabs[i].id, title: tabs[i].title, search: search, scrollTop: scrollTop });
  }
  const activeIndex = Math.max(0, tabs.findIndex((t) => t.id === activeBefore));
  await click('.pv-tab-main', activeIndex).catch(() => null);
  await sleep(500);
  return { tabs: tabs, active: activeBefore, states: states };
}
async function setTabSearchAndScroll(index, term, scrollTop) {
  await click('.pv-tab-main', index);
  await sleep(900);
  must(await exists('.pv-search-input'), '预览搜索框不存在（标签 #' + index + '）');
  await page.type('.pv-search-input', term);
  await sleep(600);
  await waitExpr('(function(){ const el = document.querySelector("[data-pv-scroll]"); return Boolean(el && el.scrollHeight > el.clientHeight + 20); })()', 15000, '预览正文可滚动');
  const applied = await page.eval(`(function(){
    const el = document.querySelector('[data-pv-scroll]');
    if (!el) return null;
    const max = Math.max(0, el.scrollHeight - el.clientHeight);
    el.scrollTop = Math.min(${Number(scrollTop)}, max);
    el.dispatchEvent(new Event('scroll', { bubbles: true }));
    return { scrollTop: el.scrollTop, max: max };
  })()`);
  await sleep(1000);
  return { term: term, scrollTop: applied && applied.scrollTop, maxScroll: applied && applied.max };
}

async function dragSplitterBy(ariaLabel, dx) {
  const selector = '.splitter[aria-label=' + JSON.stringify(ariaLabel) + ']';
  const box = await page.box(selector);
  must(box && box.visible, '分隔线不可见：' + ariaLabel);
  const x0 = box.cx, y0 = box.cy;
  const send = (type, x, buttons) => page.browser.send('Input.dispatchMouseEvent', { type, x, y: y0, button: type === 'mouseMoved' ? 'none' : 'left', buttons, clickCount: type === 'mouseMoved' ? 0 : 1 }, page.sessionId);
  await send('mouseMoved', x0, 0);
  await send('mousePressed', x0, 1);
  for (let i = 1; i <= 8; i += 1) { await send('mouseMoved', x0 + (dx * i) / 8, 1); await sleep(25); }
  await send('mouseReleased', x0 + dx, 0);
  await sleep(800);
}
async function ensureWidthChanged(selector, before, ariaLabel) {
  const after = await readWidth(selector);
  if (after !== null && Math.abs(after - before) >= 8) return { method: 'drag', before, after };
  await page.focus('.splitter[aria-label=' + JSON.stringify(ariaLabel) + ']');
  for (let i = 0; i < 4; i += 1) await page.pressKey('ArrowRight');
  await sleep(600);
  const afterKeyboard = await readWidth(selector);
  must(afterKeyboard !== null && Math.abs(afterKeyboard - before) >= 8, '拖动/键盘都没有改变宽度：' + ariaLabel);
  return { method: 'keyboard-fallback', before, after: afterKeyboard };
}

async function enterConversationMode() {
  if (await exists('[data-im="board"]')) {
    await click('#composer-mode-switch .mode-btn');
    await waitExpr('!document.querySelector(\'[data-im="board"]\')', 20000, '回到对话页');
    await sleep(900);
  }
  await waitExpr('document.querySelector(".stream") && document.querySelector("#composer-mode-switch .mode-btn")', 60000, '对话页就绪');
  await sleep(500);
}
async function openSettingsPage() {
  if (await exists('[data-testid="settings-page"]')) return;
  const clicked = await page.eval(`(function(){
    const btn = [...document.querySelectorAll('button')].find(function(b){ const label = b.getAttribute('aria-label') || ''; return label.indexOf('设置') === 0; });
    if (!btn) return false; btn.click(); return true;
  })()`);
  must(clicked, '找不到设置入口按钮');
  await waitExpr('document.querySelector(\'[data-testid="settings-page"]\')', 20000, '设置页');
  await sleep(700);
}
async function backFromSettingsPage() {
  must(await exists('[data-testid="settings-back"]'), '设置页没有返回按钮');
  await click('[data-testid="settings-back"]');
  await waitExpr('!document.querySelector(\'[data-testid="settings-page"]\')', 20000, '离开设置页');
  await sleep(900);
  await waitExpr('document.querySelector(".stream") || document.querySelector(\'[data-im="board"]\')', 30000, '返回后应用就绪');
}
async function selectSettingsCategory(id) { await click('[data-settings-nav-tab=' + JSON.stringify(id) + ']'); await sleep(500); }
async function readNotice() {
  return page.eval(`(function(){ const el = document.querySelector('.set-notice'); if (!el) return null; return { text: (el.innerText || '').trim(), className: el.className }; })()`);
}
async function waitNotice(kind, contains, timeout, label) {
  const deadline = Date.now() + (timeout || 20000);
  let last = null;
  while (Date.now() < deadline) {
    last = await readNotice().catch(() => null);
    if (last && (!kind || last.className.indexOf(kind) >= 0) && (!contains || last.text.indexOf(contains) >= 0)) return last;
    await sleep(300);
  }
  throw new Error('等待设置提示超时（' + (label || kind) + '），最后值=' + JSON.stringify(last));
}

function codeReplyText() {
  const py = []; for (let i = 1; i <= 70; i += 1) py.push('line_' + String(i).padStart(3, '0') + ' = "alpha payload ' + i + '"');
  const js = []; for (let i = 1; i <= 70; i += 1) js.push('const line' + String(i).padStart(3, '0') + ' = "beta payload ' + i + '";');
  return '下面是两段示例（' + RUN_ID + '）：\n\n```python\n' + py.join('\n') + '\n```\n\n```javascript\n' + js.join('\n') + '\n```\n';
}
const RESOLUTION_JSON = JSON.stringify({ relation: 'new', target_requirement_id: null, target_version: null, corrections: [], limits: [], removed_limits: [], reason: '界面验收：无限制' });

async function providerReset() { await fetch(PROVIDER + '/__reset', { method: 'POST', headers: { 'content-type': 'application/json' }, body: '{}' }).catch(() => null); }
async function providerScript(steps, fallback) {
  const body = fallback ? { steps, default: fallback } : { steps };
  const res = await fetch(PROVIDER + '/__script', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body) });
  must(res.ok, '设置 fake provider 脚本失败：HTTP ' + res.status);
}

async function archiveEvidence(tag) {
  const stamp = new Date().toISOString().replace(/[-:T.]/g, '').slice(0, 14);
  const base = join(OUT, 'layout-failure-' + tag + '-' + stamp);
  const files = [];
  try {
    const providerLog = await fetchJson(PROVIDER + '/__log');
    const p = base + '-provider-log.json';
    writeFileSync(p, JSON.stringify(providerLog, null, 2), 'utf8');
    files.push(p);
  } catch {}
  try {
    const pageInfo = { url: await page.eval('location.href'), consoleErrors: (page.consoleErrors || []).slice(-40) };
    const p = base + '-page.json';
    writeFileSync(p, JSON.stringify(pageInfo, null, 2), 'utf8');
    files.push(p);
  } catch {}
  return files;
}

async function main() {
  log('== #47 layout-only == runId=' + RUN_ID);
  const head = execFileSync('git', ['-C', WT, 'rev-parse', 'HEAD'], { encoding: 'utf8' }).trim();
  const status = execFileSync('git', ['-C', WT, 'status', '--porcelain=v1'], { encoding: 'utf8' }).trim();
  log('HEAD=' + head);
  if (status) log('工作树未干净（只读记录）：\n' + status);
  browser = await CdpBrowser.launch({ port: 9333 });
  page = await Page.create(browser);
  await page.setViewport(1440, 900);
  await page.goto(BASE + '/?layout=' + RUN_ID);
  await waitExpr('document.querySelector(".stream") && document.querySelector("#composer-mode-switch .mode-btn")', 60000, '应用就绪');
  await sleep(1200);
  record('环境-0 HEAD 与工作树记录', true, { head, worktreeDirty: Boolean(status), dataDir: process.env.QIO_DATA_DIR || '(由 run.ps1 指定 ui-data-final2)' });

  // 清掉持久化旧标签
  for (let pass = 0; pass < 2; pass += 1) {
    if ((await exists('.preview-reopen')) && !(await exists('.pv-tab'))) { await click('.preview-reopen').catch(() => null); await sleep(700); }
    let guard = 0;
    while ((await count('.pv-tab-close')) > 0 && guard < 12) { await click('.pv-tab-close', 0); await sleep(350); guard += 1; }
    await sleep(500);
  }
  if (await exists('button[aria-label^="收起预览"]')) { await click('button[aria-label^="收起预览"]').catch(() => null); await sleep(600); }

  // 1) 发消息拿两个代码块
  await providerReset();
  await providerScript([{ chunks: [RESOLUTION_JSON], chunk_delay_ms: 0 }, { chunks: [codeReplyText()], chunk_delay_ms: 0 }], { chunks: [codeReplyText()], chunk_delay_ms: 0 });
  await page.type('#composer-input', 'LAYOUT-VERIFY ' + RUN_ID + '：请回复两段示例代码');
  await waitExpr('document.querySelector(".send-btn") && document.querySelector(".send-btn").getAttribute("data-ready") === "true"', 10000, '消息可发送');
  await click('.send-btn');
  const marker = '下面是两段示例（' + RUN_ID + '）';
  const deadline = Date.now() + 120000;
  let waitState = null;
  while (Date.now() < deadline) {
    waitState = await page.eval('(function(){ return { hasMarker: document.body.innerText.indexOf(' + JSON.stringify(marker) + ') >= 0, buttons: document.querySelectorAll(".code-preview-btn").length }; })()');
    if (waitState.hasMarker && waitState.buttons >= 2) break;
    await sleep(500);
  }
  must(waitState && waitState.hasMarker && waitState.buttons >= 2, '代码块未出现：' + JSON.stringify(waitState));
  record('#47-L0 助手回复两段代码块（本轮唯一标记）', true, waitState);
  await shot('layout-reply-codeblocks');

  // 2) 整页 reload：让消息持久化（local_ 临时 id → 服务端 id）
  await page.reload();
  await sleep(1000);
  await waitExpr('document.querySelector(".stream") && document.querySelector("#composer-mode-switch .mode-btn")', 60000, '刷新后对话页');
  await waitExpr('document.body.innerText.indexOf(' + JSON.stringify(marker) + ') >= 0', 60000, '刷新后消息仍在');
  await sleep(1500);
  const afterReloadIds = await page.eval('(function(){ return [...document.querySelectorAll(".code-preview-btn")].map(function(el){ return el.getAttribute("aria-label") || ""; }); })()');
  record('#47-L1 刷新后消息已持久化（代码块按钮仍在）', afterReloadIds.length >= 2, { buttons: afterReloadIds });

  // 3) 点最后两个代码块预览按钮，等阅读器可搜索
  const total = afterReloadIds.length;
  await click('.code-preview-btn', total - 2);
  await waitExpr('document.querySelector(".pv-reader")', 25000, '预览阅读器');
  const searchDeadline = Date.now() + 120000;
  let readerState = null;
  while (Date.now() < searchDeadline) {
    readerState = await page.eval('(function(){' +
      'const reader = document.querySelector(".pv-reader");' +
      'const tab = document.querySelector(".pv-tab.is-active");' +
      'const state = document.querySelector(".pv-state");' +
      'return { tabId: tab ? tab.getAttribute("data-tab-id") : null, readerKind: reader ? reader.getAttribute("data-pv-kind") : null,' +
        ' tools: Boolean(document.querySelector(".pv-tools")), search: Boolean(document.querySelector(".pv-search-input")),' +
        ' scroll: Boolean(document.querySelector("[data-pv-scroll]")), stateText: state ? (state.innerText || "").trim().slice(0, 160) : null };' +
    '})()');
    if (readerState && readerState.search) break;
    await sleep(1000);
  }
  if (!readerState || !readerState.search) {
    const files = await archiveEvidence('reader-stuck');
    await shotSafe('layout-reader-stuck');
    record('#47-L2 持久化消息的代码预览进入可搜索/可滚动状态', false, {
      readerState, failureArchives: files,
      note: '刷新后（消息已持久化）打开代码预览仍停在读取态：阅读器不进入 ready，没有搜索框/滚动区',
    });
    return;
  }
  record('#47-L2 持久化消息的代码预览进入可搜索/可滚动状态', true, readerState);
  await click('.code-preview-btn', total - 1);
  await waitExpr('document.querySelectorAll(".pv-tab").length === 2', 25000, '第二个预览标签');
  await sleep(2500);
  const tabs = await readTabStrip();
  record('#47-L3 打开两个预览标签', tabs.length === 2, { tabs });
  await shot('layout-two-tabs');

  // 4) 两个标签各输搜索词 + 滚动
  const term0 = 'alpha-' + RUN_ID;
  const term1 = 'beta-' + RUN_ID;
  await setTabSearchAndScroll(0, term0, 420);
  await setTabSearchAndScroll(1, term1, 260);
  const previewBefore = await readPreviewState();
  const searchOk = previewBefore.states.length === 2 &&
    String(previewBefore.states[0].search || '').indexOf(term0) >= 0 &&
    String(previewBefore.states[1].search || '').indexOf(term1) >= 0 &&
    Number(previewBefore.states[0].scrollTop) > 0 && Number(previewBefore.states[1].scrollTop) > 0;
  record('#47-L4 两个标签各自有搜索词与滚动位置（DOM）', searchOk, previewBefore);
  const prefsWithTabs = await waitLayout(function (p) {
    return Array.isArray(p.previewTabs) && p.previewTabs.length === 2 &&
      p.previewTabs.every(function (tab) { return tab.position && String(tab.position.search || '').trim(); });
  }, '预览标签/搜索词写入服务端', 20000);
  record('#47-L5 搜索词与阅读位置进入 conversation_layout（服务端真值）', true, {
    tabs: prefsWithTabs.previewTabs.map(function (tab) { return { id: tab.id, search: tab.position.search, scrollTop: tab.position.scrollTop }; }),
  });

  // 5) 拖分隔线 + 收起左栏
  const leftBefore = await readWidth('.left-pane');
  await dragSplitterBy('调整左侧任务栏宽度', 60);
  const draggedLeft = await ensureWidthChanged('.left-pane', leftBefore, '调整左侧任务栏宽度');
  const rightBefore = await readWidth('.right-pane');
  await dragSplitterBy('调整文件预览宽度', -70);
  const draggedRight = await ensureWidthChanged('.right-pane', rightBefore, '调整文件预览宽度');
  await click('[aria-label="收起任务栏"]');
  await waitExpr('!document.querySelector(".left-pane")', 12000, '左栏收起');
  const prefsBeforeReset = await waitLayout(function (p) {
    return p.leftCollapsed === true && Number(p.leftWidth) >= 330 && p.rightOpen === true && p.previewFull === false;
  }, '拖动/收起写入服务端', 20000);
  record('#47-L6 拖动左右分隔线 + 收起左栏（服务端已记录）', true, {
    left: draggedLeft, right: draggedRight,
    prefs: { leftWidth: prefsBeforeReset.leftWidth, leftCollapsed: prefsBeforeReset.leftCollapsed, rightWidth: prefsBeforeReset.rightWidth, rightOpen: prefsBeforeReset.rightOpen },
  });
  await shot('layout-custom');

  // 6) 设置页还原默认布局
  await openSettingsPage();
  await selectSettingsCategory('regular');
  await click('[data-settings-key="regular.window.reset-layout"] button');
  const okNotice = await waitNotice('ok', '已还原默认布局', 20000, '还原默认布局成功提示');
  const prefsReset = await waitLayout(function (p) {
    return p.leftWidth === 292 && p.rightWidth === 420 && p.leftCollapsed === false && p.rightOpen === false && p.previewFull === false && p.previewTabs.length === 2;
  }, '服务端布局回默认', 20000);
  const keptSearch = prefsReset.previewTabs.length === 2 &&
    String(prefsReset.previewTabs[0].position.search || '').indexOf(term0) >= 0 &&
    String(prefsReset.previewTabs[1].position.search || '').indexOf(term1) >= 0;
  record('#47-L7 还原默认布局：成功反馈 + 服务端偏好回默认 + 标签/搜索保留', Boolean(okNotice) && keptSearch, {
    notice: okNotice,
    prefs: { leftWidth: prefsReset.leftWidth, rightWidth: prefsReset.rightWidth, leftCollapsed: prefsReset.leftCollapsed, rightOpen: prefsReset.rightOpen, previewFull: prefsReset.previewFull },
    tabs: prefsReset.previewTabs.map(function (tab) { return { id: tab.id, search: tab.position.search, scrollTop: tab.position.scrollTop }; }),
  });
  await shot('layout-reset-notice');

  // 7) 返回对话断言
  await backFromSettingsPage();
  await enterConversationMode();
  await waitExpr('document.querySelector(".conversation-workspace")', 20000, '对话页工作区');
  await sleep(1200);
  const afterReset = await overviewFacts();
  record('#47-L8 返回对话：左栏 292 展开、预览收起、预览入口显示 2 标签',
    afterReset.leftPane === true && Math.abs(Number(afterReset.leftWidth) - 292) <= 1 && afterReset.rightPane === false && afterReset.overlay === false && afterReset.reopen === true && /2/.test(String(afterReset.reopenLabel || '')),
    afterReset);
  await shot('layout-after-reset');

  // 8) 重开预览：标签/当前/搜索/滚动都在
  await click('.preview-reopen');
  await waitExpr('document.querySelector(".pv-tab")', 20000, '重开预览标签');
  await sleep(2500);
  const previewAfterReopen = await readPreviewState();
  const reopenMatches = previewAfterReopen.states.length === 2 &&
    previewAfterReopen.active === previewBefore.active &&
    previewAfterReopen.states.every(function (state, i) { return state.search === previewBefore.states[i].search; }) &&
    previewAfterReopen.states.every(function (state, i) { return Math.abs(Number(state.scrollTop) - Number(previewBefore.states[i].scrollTop)) <= 6; });
  record('#47-L9 重新打开预览：两标签/当前标签/搜索词/滚动位置都在', reopenMatches, { activeBefore: previewBefore.active, activeAfter: previewAfterReopen.active, after: previewAfterReopen.states });
  await shot('layout-reopened');

  // 9) 整页刷新后仍成立
  await page.reload();
  await sleep(1000);
  await waitExpr('document.querySelector(".stream") && document.querySelector("#composer-mode-switch .mode-btn")', 60000, '刷新后对话页');
  await waitExpr('document.querySelector(".pv-tab")', 40000, '刷新后预览标签恢复');
  await sleep(2500);
  const afterReload2 = await overviewFacts();
  const prefsAfterReload = await waitLayout(function (p) {
    return p.leftWidth === 292 && p.rightWidth === 420 && p.leftCollapsed === false && p.previewFull === false && p.previewTabs.length === 2;
  }, '刷新后服务端偏好', 20000);
  const tabsAfterReload = await readTabStrip();
  const activeAfterReload = (tabsAfterReload.find(function (tab) { return tab.active; }) || {}).id || null;
  const savedStates = prefsAfterReload.previewTabs.map(function (tab) { return { id: tab.id, search: tab.position.search, scrollTop: tab.position.scrollTop }; });
  const reloadMatches = Math.abs(Number(afterReload2.leftWidth) - 292) <= 1 && afterReload2.rightPane === true && afterReload2.previewFull === 'false' &&
    tabsAfterReload.length === 2 && activeAfterReload === previewBefore.active &&
    savedStates.every(function (state, i) { return state.search === previewBefore.states[i].search; }) &&
    savedStates.every(function (state, i) { return Math.abs(Number(state.scrollTop) - Number(previewBefore.states[i].scrollTop)) <= 6; });
  record('#47-L10 整页刷新：默认布局字段仍默认 + 标签/当前标签/搜索词/位置都在', reloadMatches, {
    dom: afterReload2, domTabs: tabsAfterReload, active: activeAfterReload, activeBefore: previewBefore.active, savedStates,
    prefs: { leftWidth: prefsAfterReload.leftWidth, rightWidth: prefsAfterReload.rightWidth, leftCollapsed: prefsAfterReload.leftCollapsed, previewFull: prefsAfterReload.previewFull, rightOpen: prefsAfterReload.rightOpen },
  });
  await shot('layout-after-reload');

  // 10) 窄窗：预览占位 + 800×600
  if (!(await exists('.right-pane')) && !(await exists('.preview-overlay'))) {
    if (await exists('.preview-reopen')) { await click('.preview-reopen'); await sleep(1200); }
  }
  const beforeNarrow = await overviewFacts();
  if (!(beforeNarrow.rightPane === true || beforeNarrow.overlay === true)) {
    markUnverified('#47-L11 窄窗（800×600+预览占位）自动收起/全窗口', '预览未能打开，无法在预览占位条件下验证窄窗布局', { facts: beforeNarrow });
    markUnverified('#47-L12 拉宽回分栏', '同上', {});
  } else {
    await page.setViewport(800, 600);
    await sleep(1500);
    const narrow = await overviewFacts();
    const prefsNarrow = await layoutPrefs();
    record('#47-L11 窄窗（800×600+预览占位）：自动收起左栏且预览临时全窗口，未写成用户偏好',
      narrow.leftAuto === 'true' && narrow.previewFull === 'true' && prefsNarrow.leftCollapsed === false && prefsNarrow.previewFull === false,
      { rule: '中央下限 460 + 分隔条 12 + 左栏下限 232 = 704；无预览时允许左栏并排（272/460 正确）', facts: narrow, prefs: { leftCollapsed: prefsNarrow.leftCollapsed, previewFull: prefsNarrow.previewFull } });
    await shot('layout-narrow');
    await page.setViewport(1440, 900);
    await sleep(1600);
    const wide = await overviewFacts();
    const prefsWide = await layoutPrefs();
    record('#47-L12 拉宽到 ≥1280：回到分栏，偏好仍是默认',
      wide.leftPane === true && wide.rightPane === true && wide.overlay === false && wide.leftAuto === 'false' && wide.previewFull === 'false' &&
      Math.abs(Number(wide.leftWidth) - 292) <= 1 && prefsWide.leftWidth === 292 && prefsWide.previewFull === false,
      { facts: wide, prefs: { leftWidth: prefsWide.leftWidth, previewFull: prefsWide.previewFull } });
    await shot('layout-wide-restored');
  }
}

let fatal = null;
try { await main(); } catch (error) {
  fatal = error;
  const files = await archiveEvidence('fatal').catch(() => []);
  record('layout 主流程未跑完', false, { error: String(error && error.stack || error).slice(0, 1500), failureArchives: files });
  await shotSafe('layout-fatal');
} finally {
  try { if (browser) await browser.close(); } catch {}
  const pass = results.filter((r) => r.ok).length;
  const unv = results.filter((r) => r.unverified).length;
  const fail = results.filter((r) => !r.ok && !r.unverified).length;
  const summary = {
    runId: RUN_ID, finishedAt: new Date().toISOString(),
    head: (() => { try { return execFileSync('git', ['-C', WT, 'rev-parse', 'HEAD'], { encoding: 'utf8' }).trim(); } catch { return ''; } })(),
    base: BASE, api: API, provider: PROVIDER, out: OUT,
    pass, fail, unverified: unv, total: results.length,
    results, simulated, unverifiedNotes: unverified,
    consoleErrors: page ? page.consoleErrors.slice(0, 60) : [],
    note: '#47 宽窗布局定向复跑：发消息→整页 reload（消息持久化）→点最后两个代码块预览→四步布局断言→窄窗。不覆盖 summary.json。',
  };
  writeFileSync(join(OUT, 'summary-layout.json'), JSON.stringify(summary, null, 2), 'utf8');
  log('');
  log('== layout 结果：PASS ' + pass + ' / FAIL ' + fail + ' / UNVERIFIED ' + unv + ' / TOTAL ' + results.length + ' ==');
  log('summary-layout=' + join(OUT, 'summary-layout.json'));
  if (fatal) log('FATAL=' + String(fatal && fatal.message || fatal));
  process.exit(fail > 0 || fatal ? 1 : 0);
}