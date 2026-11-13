/**
 * QIO 0.3.2 集成轮 · 真实界面往返与视觉证据驱动（ui-verify）
 *
 * 定位：Lead 的**只读验收驱动**。不改产品代码、不写仓库文件；只连本机 backend 8734 /
 * vite 5199 / fake provider 8807，用系统 Edge + CDP（scripts/verify-pm-qio032/cdp.mjs）
 * 真实点击/输入/拖动/截图，把 PASS/FAIL/UNVERIFIED + DOM 事实写成 summary.json。
 *
 * 前置（由 run-ui.ps1 准备或校验）：
 *   1) powershell -NoProfile -ExecutionPolicy Bypass -File scripts/verify-pm-qio032/run.ps1 -KeepRunning
 *      （起 backend/vite/fake provider，建假凭据并完成首次引导；它会先跑一遍自带 E2E，耗时较长）
 *   2) node ui-verify.mjs
 *   3) 收尾：backend/.venv/Scripts/python.exe scripts/e2e_down.py + 结束 provider（run-ui.ps1 默认做）
 *
 * 环境变量：
 *   QIO_E2E_BASE / QIO_E2E_API / QIO_E2E_PROVIDER  默认 5199 / 8734 / 8807
 *   QIO_UI_OUT      产物目录（默认本脚本所在目录）
 *   QIO_UI_CDP_PORT CDP 端口（默认 9333；脚本自己 launch Edge，和 e2e.mjs 一样）
 *   QIO_UI_WT       集成 worktree（默认 D:/qio-dev/pm-qio032-integration）
 *   QIO_UI_ONLY     逗号分隔场景子集（s1,s2,s3,s4,s5,s7,s6），便于单场景复跑
 *
 * 标注规则：任何 CDP 注入（延迟/500/abort）都在 detail 里 simulated=true 并写进 summary.simulated。
 */
import { appendFileSync, existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { execFileSync } from 'node:child_process';

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
const STARTED_AT = new Date().toISOString();
const LOG_FILE = join(OUT, 'run.log');

if (process.argv.includes('--print-plan')) {
  console.log(JSON.stringify({ runId: RUN_ID, scenarios: ['s1 #47宽窗', 's2 #47窄窗', 's3 #46跨页上传', 's4 设置搜索fold', 's5 设置搜索还原布局', 's7 [模拟]保存失败', 's6 主题×尺寸矩阵'], out: OUT }, null, 2));
  process.exit(0);
}

mkdirSync(SHOTS, { recursive: true });
mkdirSync(MATERIALS, { recursive: true });
writeFileSync(LOG_FILE, '', 'utf8');

const cdpUrl = pathToFileURL(join(WT, 'scripts', 'verify-pm-qio032', 'cdp.mjs')).href;
let cdp = null;
try {
  cdp = await import(cdpUrl);
} catch (error) {
  console.error('[FATAL] 无法加载 cdp.mjs：' + cdpUrl + ' :: ' + (error && error.message || error));
  process.exit(2);
}
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
  try { appendFileSync(LOG_FILE, text + '\n', 'utf8'); } catch { /* 日志失败不影响验收 */ }
}

function record(name, ok, detail, opts) {
  const unverified = Boolean(opts && opts.unverified);
  const entry = { name: name, ok: Boolean(ok) && !unverified, unverified: unverified, detail: detail === undefined ? null : detail };
  results.push(entry);
  const tag = unverified ? 'UNVERIFIED' : (entry.ok ? 'PASS' : 'FAIL');
  let body = '';
  if (detail !== undefined) { try { body = ' :: ' + JSON.stringify(detail).slice(0, 1400); } catch { body = ' :: [unserializable]'; } }
  log('[' + tag + '] ' + name + body);
  return entry.ok;
}

function markUnverified(name, reason, detail) {
  unverifiedNotes.push({ name: name, reason: reason });
  const payload = Object.assign({ kind: 'unverified', reason: reason }, detail || {});
  return record(name, false, payload, { unverified: true });
}

function must(cond, message) {
  if (!cond) throw new Error('断言失败：' + message);
}

function headAndStatus() {
  const out = { head: '', status: '' };
  try { out.head = execFileSync('git', ['-C', WT, 'rev-parse', 'HEAD'], { encoding: 'utf8' }).trim(); } catch { /* ignore */ }
  try { out.status = execFileSync('git', ['-C', WT, 'status', '--porcelain=v1'], { encoding: 'utf8' }).trim(); } catch { /* ignore */ }
  return out;
}

async function httpOk(url) {
  try {
    const res = await fetch(url, { signal: AbortSignal.timeout(5000) });
    return res.status < 500;
  } catch { return false; }
}

async function assertServices() {
  const checks = [
    { name: 'backend 8734 /api/health', ok: await httpOk(API + '/api/health') },
    { name: 'vite 5199 /', ok: await httpOk(BASE + '/') },
    { name: 'fake provider 8807 /__health', ok: await httpOk(PROVIDER + '/__health') },
  ];
  for (const c of checks) if (!c.ok) throw new Error('服务未就绪：' + c.name + '（先跑 run.ps1 -KeepRunning，或 run-ui.ps1 -Setup）');
  return checks;
}

async function fetchJson(url, init) {
  const res = await fetch(url, init);
  const text = await res.text();
  let json = null;
  try { json = text ? JSON.parse(text) : null; } catch { json = null; }
  if (!res.ok) throw new Error('HTTP ' + res.status + ' ' + url + ' :: ' + text.slice(0, 300));
  return json;
}

async function providerReset() {
  await fetch(PROVIDER + '/__reset', { method: 'POST', headers: { 'content-type': 'application/json' }, body: '{}' }).catch(() => null);
}

async function providerScript(steps, fallback) {
  const payload = fallback ? { steps: steps, default: fallback } : { steps: steps };
  const res = await fetch(PROVIDER + '/__script', {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(payload),
  });
  must(res.ok, '设置 fake provider 脚本失败：HTTP ' + res.status);
}

function parseLayout(raw) {
  if (raw && typeof raw === 'object') return raw;
  if (typeof raw !== 'string' || !raw.trim()) return null;
  try { return JSON.parse(raw); } catch { return null; }
}

async function fetchUISettings() {
  return fetchJson(API + '/api/settings/ui');
}

async function layoutPrefs() {
  const settings = await fetchUISettings();
  const prefs = parseLayout(settings && settings.conversation_layout);
  must(prefs && typeof prefs === 'object', '服务端没有可解析的 conversation_layout');
  return prefs;
}

async function waitLayout(predicate, opts) {
  const options = opts || {};
  const timeout = options.timeout || 12000;
  const label = options.label || '布局偏好';
  const deadline = Date.now() + timeout;
  let last = null;
  while (Date.now() < deadline) {
    try {
      last = await layoutPrefs();
      if (predicate(last)) return last;
    } catch (error) { last = { error: String(error && error.message || error) }; }
    await sleep(300);
  }
  throw new Error('等待超时（' + label + '），最后值=' + JSON.stringify(last).slice(0, 900));
}

async function shot(name) {
  stepNo += 1;
  const file = join(SHOTS, String(stepNo).padStart(2, '0') + '-' + name + '.png');
  await page.screenshot(file);
  return file;
}

async function clipShot(name, selector, pad) {
  const padding = typeof pad === 'number' ? pad : 8;
  const clip = await page.eval(
    `(function(){
      const el = document.querySelector(${JSON.stringify(selector)});
      if (!el) return null;
      el.scrollIntoView({ block: 'center', inline: 'center' });
      const r = el.getBoundingClientRect();
      return { x: Math.max(0, r.x + scrollX - ${padding}), y: Math.max(0, r.y + scrollY - ${padding}), width: Math.min(r.width + ${padding * 2}, 3800), height: Math.min(r.height + ${padding * 2}, 3800) };
    })()`
  );
  must(clip && clip.width > 4 && clip.height > 4, '裁剪截图目标不可见：' + selector);
  stepNo += 1;
  const file = join(SHOTS, String(stepNo).padStart(2, '0') + '-' + name + '.png');
  const r = await page.browser.send(
    'Page.captureScreenshot',
    { format: 'png', clip: Object.assign({ scale: 1 }, clip), captureBeyondViewport: false },
    page.sessionId
  );
  writeFileSync(file, Buffer.from(r.data, 'base64'));
  return file;
}

async function waitExpr(expression, timeout, label) {
  return page.waitFor(expression, { timeout: timeout || 20000, label: label || '' });
}

async function count(selector) { return page.count(selector); }
async function exists(selector) { return page.exists(selector); }
async function text(selector, index) { return page.text(selector, index || 0); }
async function attr(selector, name, index) { return page.attr(selector, name, index || 0); }
async function inputValue(selector) { return page.inputValue(selector); }

async function readWidth(selector) {
  return page.eval(`(function(){ const el = document.querySelector(${JSON.stringify(selector)}); return el ? el.getBoundingClientRect().width : null; })()`);
}

async function click(selector, index) { return page.click(selector, index || 0, { timeout: 20000 }); }
async function typeInto(selector, value) { return page.type(selector, value); }
async function pressKey(key) { return page.pressKey(key); }

async function readNotice() {
  return page.eval(`(function(){
    const el = document.querySelector('.set-notice');
    if (!el) return null;
    return { text: (el.innerText || el.textContent || '').trim(), className: el.className, role: el.getAttribute('role') };
  })()`);
}

async function waitNotice(kind, contains, timeout, label) {
  const deadline = Date.now() + (timeout || 20000);
  let last = null;
  while (Date.now() < deadline) {
    last = await readNotice().catch(() => null);
    if (last && (!kind || last.className.indexOf(kind) >= 0) && (!contains || last.text.indexOf(contains) >= 0)) return last;
    await sleep(300);
  }
  throw new Error('等待设置提示超时（' + (label || (kind + ' ' + contains)) + '），最后值=' + JSON.stringify(last));
}

async function dragSplitterBy(ariaLabel, dx) {
  const selector = '.splitter[aria-label=' + JSON.stringify(ariaLabel) + ']';
  const box = await page.box(selector);
  must(box && box.visible, '分隔线不可见：' + ariaLabel);
  const x0 = box.cx;
  const y0 = box.cy;
  const send = (type, x, buttons) => page.browser.send('Input.dispatchMouseEvent', { type: type, x: x, y: y0, button: type === 'mouseMoved' ? 'none' : 'left', buttons: buttons, clickCount: type === 'mouseMoved' ? 0 : 1 }, page.sessionId);
  await send('mouseMoved', x0, 0);
  await send('mousePressed', x0, 1);
  for (let i = 1; i <= 8; i += 1) {
    await send('mouseMoved', x0 + (dx * i) / 8, 1);
    await sleep(25);
  }
  await send('mouseReleased', x0 + dx, 0);
  await sleep(700);
}

async function ensureWidthChanged(selector, before, ariaLabel) {
  const after = await readWidth(selector);
  if (after !== null && Math.abs(after - before) >= 8) return { method: 'drag', before: before, after: after };
  // 兜底：分隔线本身支持键盘微调（真实 UI 能力），仍记录实际使用的方法
  await page.focus('.splitter[aria-label=' + JSON.stringify(ariaLabel) + ']');
  for (let i = 0; i < 4; i += 1) await pressKey('ArrowRight');
  await sleep(500);
  const afterKeyboard = await readWidth(selector);
  must(afterKeyboard !== null && Math.abs(afterKeyboard - before) >= 8, '拖动/键盘都没有改变宽度：' + ariaLabel);
  return { method: 'keyboard-fallback', before: before, after: afterKeyboard };
}

async function readTabStrip() {
  return page.eval(`(function(){
    return [...document.querySelectorAll('.pv-tab')].map(function(tab){
      const main = tab.querySelector('.pv-tab-main');
      return { id: tab.getAttribute('data-tab-id'), active: tab.classList.contains('is-active'), title: (tab.innerText || '').trim(), ariaSelected: main ? main.getAttribute('aria-selected') : null };
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
    await waitExpr('document.querySelector(".pv-search-input") || document.querySelector("[data-pv-scroll]") || document.querySelector(".pv-problem")', 15000, '标签阅读器就绪').catch(function () { return null; });
    await sleep(700);
    const search = await inputValue('.pv-search-input');
    const scrollTop = await page.eval(`(function(){ const el = document.querySelector('[data-pv-scroll]'); return el ? el.scrollTop : null; })()`);
    states.push({ id: tabs[i].id, title: tabs[i].title, search: search, scrollTop: scrollTop });
  }
  const activeIndex = Math.max(0, tabs.findIndex((t) => t.id === activeBefore));
  await click('.pv-tab-main', activeIndex).catch(() => null);
  await sleep(500);
  return { tabs: tabs, active: activeBefore, states: states };
}

async function setTabSearchAndScroll(index, term, scrollTop) {
  await click('.pv-tab-main', index);
  await sleep(650);
  const inputExists = await exists('.pv-search-input');
  must(inputExists, '预览搜索框不存在（标签 #' + index + '）');
  await typeInto('.pv-search-input', term);
  await sleep(500);
  await waitExpr('(function(){ const el = document.querySelector("[data-pv-scroll]"); return Boolean(el && el.scrollHeight > el.clientHeight + 20); })()', 15000, '预览正文可滚动');
  const applied = await page.eval(`(function(){
    const el = document.querySelector('[data-pv-scroll]');
    if (!el) return null;
    const max = Math.max(0, el.scrollHeight - el.clientHeight);
    el.scrollTop = Math.min(${Number(scrollTop)}, max);
    el.dispatchEvent(new Event('scroll', { bubbles: true }));
    return { scrollTop: el.scrollTop, max: max };
  })()`);
  await sleep(900);
  return { term: term, scrollTop: applied && applied.scrollTop, maxScroll: applied && applied.max };
}

async function enterConversationMode() {
  if (await exists('[data-im="board"]')) {
    await click('#composer-mode-switch .mode-btn');
    await waitExpr('!document.querySelector(\'[data-im="board"]\')', 20000, '回到对话页');
    await sleep(900);
  }
  await waitExpr('document.querySelector(\'.stream\') && document.querySelector(\'#composer-mode-switch .mode-btn\')', 60000, '对话页就绪');
  await sleep(500);
}

async function enterInteractiveMode() {
  await enterConversationMode();
  if (await exists('[data-im="board"]')) return { already: true };
  await click('#composer-mode-switch .mode-btn');
  await waitExpr('document.querySelector(\'[data-im="board"]\')', 30000, '互动板面');
  await sleep(1500);
  return { already: false };
}

async function openSettingsPage() {
  if (await exists('[data-testid="settings-page"]')) return;
  const hasExact = await exists('button[aria-label="设置"]');
  if (hasExact) {
    await click('button[aria-label="设置"]');
  } else {
    const clicked = await page.eval(`(function(){
      const btn = [...document.querySelectorAll('button')].find(function(b){ const label = b.getAttribute('aria-label') || ''; return label.indexOf('设置') === 0; });
      if (!btn) return false;
      btn.click();
      return true;
    })()`);
    must(clicked, '找不到设置入口按钮');
  }
  await waitExpr('document.querySelector(\'[data-testid="settings-page"]\')', 20000, '设置页');
  await sleep(700);
}

async function backFromSettingsPage() {
  must(await exists('[data-testid="settings-back"]'), '设置页没有返回按钮');
  await click('[data-testid="settings-back"]');
  await waitExpr('!document.querySelector(\'[data-testid="settings-page"]\')', 20000, '离开设置页');
  await sleep(900);
  await waitExpr('document.querySelector(\'.stream\') || document.querySelector(\'[data-im="board"]\')', 30000, '返回后应用就绪');
}

async function selectSettingsCategory(id) {
  await click('[data-settings-nav-tab=' + JSON.stringify(id) + ']');
  await sleep(500);
}

async function openSettingsSearch(query) {
  const panelOpen = await page.eval('(function(){ const panel = document.querySelector("[data-testid=settings-search-panel]"); return Boolean(panel && getComputedStyle(panel).display !== "none"); })()');
  if (!panelOpen) {
    await click('[data-testid="settings-search-entry"]');
  }
  await waitExpr('(function(){ const panel = document.querySelector("[data-testid=settings-search-panel]"); return Boolean(panel && getComputedStyle(panel).display !== "none"); })()', 10000, '搜索面板');
  // 搜索面板与输入框是 v-show 常驻：上一次查询不会自动清空（s4 查「单段长度」后 s5 再查会变成拼接）。
  // 用真实键盘先全选删除，再输入本次查询；最后轮询确认 value 已就是 query。
  await page.focus('[data-testid="settings-search-input"]');
  await page.eval('(function(){ const el = document.querySelector("[data-testid=settings-search-input]"); if (!el) return false; el.select(); return true; })()');
  if (await inputValue('[data-testid="settings-search-input"]')) {
    await pressKey('Backspace');
    await sleep(200);
  }
  await typeInto('[data-testid="settings-search-input"]', query);
  const valueDeadline = Date.now() + 2500;
  while (Date.now() < valueDeadline) {
    if (String(await inputValue('[data-testid="settings-search-input"]')) === query) break;
    await sleep(120);
  }
  await sleep(300);
}

async function closeSettingsSearch() {
  if (!(await exists('[data-testid="settings-search-input"]'))) return;
  await page.focus('[data-testid="settings-search-input"]').catch(() => null);
  await pressKey('Escape');
  await sleep(500);
}
function codeReplyText() {
  const pyLines = [];
  for (let i = 1; i <= 70; i += 1) pyLines.push('line_' + String(i).padStart(3, '0') + ' = "alpha payload ' + i + '"');
  const jsLines = [];
  for (let i = 1; i <= 70; i += 1) jsLines.push('const line' + String(i).padStart(3, '0') + ' = "beta payload ' + i + '";');
  return '下面是两段示例（' + RUN_ID + '）：\n\n```python\n' + pyLines.join('\n') + '\n```\n\n```javascript\n' + jsLines.join('\n') + '\n```\n';
}

// 要求解析（每个用户 turn 开始前的一次模型调用）必须回合法 JSON；
// 首次真机跑证明主回答脚本被它吃掉了（resolution_unavailable: 没有返回 JSON 对象）。
// 结构来自 backend/src/agent/prompts.py REQUIREMENT_RESOLUTION_INSTRUCTION 与 security/resolution.py parse。
const RESOLUTION_JSON = JSON.stringify({
  relation: 'new',
  target_requirement_id: null,
  target_version: null,
  corrections: [],
  limits: [],
  removed_limits: [],
  reason: '界面验收测试消息：无限制',
});
// 万能回复：既可以当「要求解析」的 JSON（解析器取第一个 { 到最后一个 }），
// 又包含两段代码块供主回答渲染预览按钮。这样不再依赖模型调用顺序（ping/解析/主回答谁先谁后）。
// 注意：代码块正文里不能出现 { 或 }，否则解析器会把最后一个 } 当成 JSON 结尾。
const UNIVERSAL_REPLY = RESOLUTION_JSON + '\n\n说明（界面验收夹具）：下面是两段示例：\n\n' + codeReplyText().replace('下面是两段示例（仅用于界面验收）：', '');
async function overviewFacts() {
  return page.eval(`(function(){
    const ws = document.querySelector('.conversation-workspace');
    const left = document.querySelector('.left-pane');
    const right = document.querySelector('.right-pane');
    return {
      workspace: Boolean(ws),
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

async function clearPreviewTabs() {
  // 历史轮次会持久化预览标签；场景要的是“本轮自己打开的两个标签”，先把旧标签关干净。
  // 布局偏好是异步读回的：刚进页面时标签可能还没恢复，所以要等一轮再关，并做两遍。
  await sleep(1500);
  for (let pass = 0; pass < 2; pass += 1) {
    if ((await exists('.preview-reopen')) && !(await exists('.pv-tab'))) {
      await click('.preview-reopen').catch(function () { return null; });
      await sleep(700);
    }
    let guard = 0;
    while ((await count('.pv-tab-close')) > 0 && guard < 12) {
      await click('.pv-tab-close', 0);
      await sleep(350);
      guard += 1;
    }
    await sleep(600);
  }
  await collapsePreview();
  await sleep(400);
}

async function warmupModelCall() {
  // 首次模型使用前 QIO 会先发一次 tools=1 的 ping（req0）——它会吃掉 FIFO 的 JSON 步骤。
  // 先发一条“预热”消息把 ping（以及可能的要求解析）消耗掉，正式 s1 再设置 [要求解析 JSON] + 默认代码块。
  await providerReset();
  await providerScript([], { chunks: ['预热回复：链路正常（无代码块）'], chunk_delay_ms: 0 });
  await typeInto('#composer-input', '预热：请回复一句确认（界面验收准备）');
  await waitExpr('document.querySelector(".send-btn") && document.querySelector(".send-btn").getAttribute("data-ready") === "true"', 10000, '预热消息可发送');
  await click('.send-btn');
  await waitExpr('document.body.innerText.indexOf("预热回复") >= 0', 60000, '预热模型回复');
  await waitExpr('document.querySelector(".send-btn") && document.querySelector(".send-btn").getAttribute("data-mode") !== "stop"', 60000, '预热回复结束（发送按钮回到发送态）');
  await sleep(900);
}

async function waitReaderSearch(label, timeout) {
  const deadline = Date.now() + (timeout || 120000);
  let state = null;
  while (Date.now() < deadline) {
    state = await page.eval('(function(){' +
      'const reader = document.querySelector(".pv-reader");' +
      'const tab = document.querySelector(".pv-tab.is-active");' +
      'const problem = document.querySelector(".pv-problem");' +
      'const loading = document.querySelector(".pv-state[aria-busy=true], .pv-state");' +
      'return {' +
        'tabId: tab ? tab.getAttribute("data-tab-id") : null,' +
        'readerKind: reader ? reader.getAttribute("data-pv-kind") : null,' +
        'tools: Boolean(document.querySelector(".pv-tools")),' +
        'search: Boolean(document.querySelector(".pv-search-input")),' +
        'scroll: Boolean(document.querySelector("[data-pv-scroll]")),' +
        'problem: problem ? (problem.innerText || "").trim().slice(0, 240) : null,' +
        'stateText: loading ? (loading.innerText || "").trim().slice(0, 160) : null' +
      '};' +
    '})()');
    if (state && state.search) return state;
    await sleep(1000);
  }
  throw new Error('预览搜索框未出现（' + label + '）：' + JSON.stringify(state));
}
async function s1WideLayout() {
  await enterConversationMode();
  await page.setViewport(1440, 900);
  await sleep(900);
  await clearPreviewTabs();
  const priorRows = await count('[data-test=reply-code-preview]');
  await providerReset();
  // 顺序脚本：第 1 步 JSON（要求解析），第 2 步两段代码块（主回答）；default 也回代码块。
  // 万一有 ping/后台任务先吃掉 JSON，要求解析会报 unavailable（不阻断主回答），主回答仍拿代码块。
  await providerScript([{ chunks: [RESOLUTION_JSON], chunk_delay_ms: 0 }, { chunks: [codeReplyText()], chunk_delay_ms: 0 }], { chunks: [codeReplyText()], chunk_delay_ms: 0 });
  await typeInto('#composer-input', 'UI-VERIFY ' + RUN_ID + '：请回复两段示例代码（界面验收用）');
  await waitExpr('document.querySelector(".send-btn") && document.querySelector(".send-btn").getAttribute("data-ready") === "true"', 10000, '正式消息可发送');
  await click('.send-btn');
  // 用本轮唯一标记判断新回复已落定；[data-test=reply-code-preview] 是“一行代码块预览”容器，
  // 行内按钮是 .code-preview-btn（python / javascript 各一）。历史消息也有按钮，故取最后两个。
  const marker = '下面是两段示例（' + RUN_ID + '）';
  const waitDeadline = Date.now() + 120000;
  let waitState = null;
  while (Date.now() < waitDeadline) {
    waitState = await page.eval('(function(){ return {' +
      'hasMarker: document.body.innerText.indexOf(' + JSON.stringify(marker) + ') >= 0,' +
      'rows: document.querySelectorAll("[data-test=reply-code-preview]").length,' +
      'buttons: document.querySelectorAll(".code-preview-btn").length' +
    '}; })()');
    if (waitState.hasMarker && waitState.buttons >= 2) break;
    await sleep(500);
  }
  must(waitState && waitState.hasMarker && waitState.buttons >= 2, '助手回复代码块未出现：' + JSON.stringify({ priorRows: priorRows, waitState: waitState }));
  await sleep(3000);
  await shot('47-wide-reply-codeblocks');
  const codeButtons = await page.eval('(function(){ return [...document.querySelectorAll(".code-preview-btn")].map(function(el){ return { label: el.getAttribute("aria-label") || "", text: (el.innerText || "").trim() }; }); })()');
  must(codeButtons.length >= 2, '代码块预览按钮不足两个：' + JSON.stringify(codeButtons));
  const pythonButtonIndex = codeButtons.length - 2;
  const javascriptButtonIndex = codeButtons.length - 1;
  await click('.code-preview-btn', pythonButtonIndex);
  await waitExpr('document.querySelectorAll(".pv-tab").length === 1', 25000, '第一个预览标签');
  await waitExpr('document.querySelector(".pv-reader")', 25000, '预览阅读器');
  await sleep(3000);
  await waitReaderSearch('python 标签');
  await click('.code-preview-btn', javascriptButtonIndex);
  await waitExpr('document.querySelectorAll(".pv-tab").length === 2', 25000, '第二个预览标签');
  await sleep(900);
  const tabsAfterOpen = await readTabStrip();
  record('#47-1 宽窗打开两个预览标签（回复代码块入口）', tabsAfterOpen.length === 2, { tabs: tabsAfterOpen });

  const term0 = 'alpha-' + RUN_ID;
  const term1 = 'beta-' + RUN_ID;
  await setTabSearchAndScroll(0, term0, 420);
  await setTabSearchAndScroll(1, term1, 260);
  const previewBefore = await readPreviewState();
  const searchOk = previewBefore.states.length === 2 &&
    String(previewBefore.states[0].search || '').indexOf(term0) >= 0 &&
    String(previewBefore.states[1].search || '').indexOf(term1) >= 0 &&
    Number(previewBefore.states[0].scrollTop) > 0 && Number(previewBefore.states[1].scrollTop) > 0;
  record('#47-2 两个标签各自有搜索词与滚动位置', searchOk, previewBefore);

  const prefsWithTabs = await waitLayout(function (p) {
    return Array.isArray(p.previewTabs) && p.previewTabs.length === 2 &&
      p.previewTabs.every(function (tab) { return tab.position && String(tab.position.search || '').trim(); });
  }, { label: '预览标签/搜索词写入服务端' });
  record('#47-3 搜索词与阅读位置进入 conversation_layout（服务端真值）', true, {
    leftWidth: prefsWithTabs.leftWidth,
    rightWidth: prefsWithTabs.rightWidth,
    tabs: prefsWithTabs.previewTabs.map(function (tab) { return { id: tab.id, search: tab.position.search, scrollTop: tab.position.scrollTop }; }),
  });

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
  }, { label: '拖动/收起写入服务端' });
  record('#47-4 拖动左右分隔线 + 收起左栏（服务端已记录）', true, {
    left: draggedLeft, right: draggedRight,
    prefs: { leftWidth: prefsBeforeReset.leftWidth, leftCollapsed: prefsBeforeReset.leftCollapsed, rightWidth: prefsBeforeReset.rightWidth, rightOpen: prefsBeforeReset.rightOpen, previewFull: prefsBeforeReset.previewFull },
  });
  await shot('47-wide-custom-layout');

  await openSettingsPage();
  await selectSettingsCategory('regular');
  await click('[data-settings-key="regular.window.reset-layout"] button');
  const okNotice = await waitNotice('ok', '已还原默认布局', 20000, '还原默认布局成功提示');
  await shot('47-wide-reset-notice');
  const prefsReset = await waitLayout(function (p) {
    return p.leftWidth === 292 && p.rightWidth === 420 && p.leftCollapsed === false &&
      p.rightOpen === false && p.previewFull === false && p.previewTabs.length === 2;
  }, { label: '服务端布局回默认' });
  const keptSearch = prefsReset.previewTabs.length === 2 &&
    String(prefsReset.previewTabs[0].position.search || '').indexOf(term0) >= 0 &&
    String(prefsReset.previewTabs[1].position.search || '').indexOf(term1) >= 0;
  record('#47-5 还原默认布局：明确成功反馈 + 服务端偏好回默认 + 标签/搜索保留', Boolean(okNotice) && keptSearch, {
    notice: okNotice,
    prefs: { leftWidth: prefsReset.leftWidth, rightWidth: prefsReset.rightWidth, leftCollapsed: prefsReset.leftCollapsed, rightOpen: prefsReset.rightOpen, previewFull: prefsReset.previewFull },
    tabs: prefsReset.previewTabs.map(function (tab) { return { id: tab.id, title: tab.target && tab.target.title, search: tab.position.search, scrollTop: tab.position.scrollTop }; }),
  });

  await backFromSettingsPage();
  await enterConversationMode();
  await waitExpr('document.querySelector(".conversation-workspace")', 20000, '对话页工作区');
  await sleep(900);
  const afterReset = await overviewFacts();
  record('#47-6 返回对话：左栏 292 展开、预览收起、预览入口显示 2 标签',
    afterReset.leftPane === true && Math.abs(Number(afterReset.leftWidth) - 292) <= 1 && afterReset.rightPane === false && afterReset.overlay === false && afterReset.reopen === true && /2/.test(String(afterReset.reopenLabel || '')),
    afterReset);
  await shot('47-wide-after-reset');

  await click('.preview-reopen');
  await waitExpr('document.querySelector(".pv-tab")', 20000, '重开预览标签');
  await sleep(1000);
  const previewAfterReopen = await readPreviewState();
  const reopenMatches = previewAfterReopen.states.length === 2 &&
    previewAfterReopen.active === previewBefore.active &&
    previewAfterReopen.states.every(function (state, i) { return state.search === previewBefore.states[i].search; }) &&
    previewAfterReopen.states.every(function (state, i) { return Math.abs(Number(state.scrollTop) - Number(previewBefore.states[i].scrollTop)) <= 6; });
  record('#47-7 重新打开预览：两标签/当前标签/搜索词/滚动位置都在', reopenMatches, { activeBefore: previewBefore.active, activeAfter: previewAfterReopen.active, after: previewAfterReopen.states });
  await clipShot('47-wide-preview-head', '.pv-head');

  await page.reload();
  await sleep(800);
  await waitExpr('document.querySelector(".stream") && document.querySelector("#composer-mode-switch .mode-btn")', 60000, '刷新后对话页');
  await waitExpr('document.querySelector(".pv-tab")', 40000, '刷新后预览标签恢复');
  await waitExpr('document.querySelector(".right-pane") || document.querySelector(".preview-overlay")', 30000, '刷新后预览栏恢复').catch(function () { return null; });
  await sleep(1400);
  const afterReload = await overviewFacts();
  const prefsAfterReload = await waitLayout(function (p) {
    return p.leftWidth === 292 && p.rightWidth === 420 && p.leftCollapsed === false && p.previewFull === false && p.previewTabs.length === 2;
  }, { label: '刷新后服务端偏好' });
  const tabsAfterReload = await readTabStrip();
  const activeAfterReload = (tabsAfterReload.find(function (tab) { return tab.active; }) || {}).id || null;
  const savedStates = prefsAfterReload.previewTabs.map(function (tab) { return { id: tab.id, search: tab.position.search, scrollTop: tab.position.scrollTop }; });
  const reloadMatches = Math.abs(Number(afterReload.leftWidth) - 292) <= 1 && afterReload.rightPane === true && afterReload.previewFull === 'false' &&
    tabsAfterReload.length === 2 && activeAfterReload === previewBefore.active &&
    savedStates.every(function (state, i) { return state.search === previewBefore.states[i].search; }) &&
    savedStates.every(function (state, i) { return Math.abs(Number(state.scrollTop) - Number(previewBefore.states[i].scrollTop)) <= 6; });
  record('#47-8 整页刷新：默认布局字段仍默认 + 标签/当前标签/搜索词/位置都在', reloadMatches, {
    dom: afterReload,
    domTabs: tabsAfterReload,
    active: activeAfterReload, activeBeforeReload: previewBefore.active,
    savedStates: savedStates,
    note: '回复代码块的正文只在内存；刷新后以 conversation_layout.position 里的 search/scrollTop 为位置证据',
    prefs: { leftWidth: prefsAfterReload.leftWidth, rightWidth: prefsAfterReload.rightWidth, leftCollapsed: prefsAfterReload.leftCollapsed, previewFull: prefsAfterReload.previewFull, rightOpen: prefsAfterReload.rightOpen, tabs: prefsAfterReload.previewTabs.length },
  });
  await shot('47-wide-after-reload');
}

async function s2NarrowLayout() {
  await enterConversationMode();
  const tabs = await readTabStrip();
  if (tabs.length < 2) {
    const reason = 's1 未产出两个预览标签（s1 前置不成立/失败），无法在“预览占位”条件下验证窄窗自动布局；按 UNVERIFIED 记录，不判产品缺陷';
    markUnverified('#47-9 窄窗（800×600+预览占位）：自动收起/全窗口未被写成用户偏好', reason, { tabs: tabs });
    markUnverified('#47-10 窄窗下点击还原默认布局', reason, { tabs: tabs });
    markUnverified('#47-11 窄窗重开预览：自动全窗口', reason, { tabs: tabs });
    markUnverified('#47-12 拉宽回分栏', reason, { tabs: tabs });
    return;
  }
  // 前置：必须有右侧预览占位。800×600 且无预览时规则允许左栏并排（LEFT_MIN232 + 分隔条12 + CENTER_MIN460 = 704），
  // 首次跑把它误判成产品缺陷；现在按真实条件先打开预览再缩窗。
  if (!(await exists('.right-pane')) && !(await exists('.preview-overlay'))) {
    if (await exists('.preview-reopen')) { await click('.preview-reopen'); await sleep(1000); }
  }
  const beforeNarrow = await overviewFacts();
  must(beforeNarrow.rightPane === true || beforeNarrow.overlay === true, 's2 前置不成立：两个标签存在但预览没有打开');
  await page.setViewport(800, 600);
  await sleep(1300);
  const narrow = await overviewFacts();
  const prefsNarrow = await layoutPrefs();
  const autoHappened = narrow.leftAuto === 'true' && narrow.previewFull === 'true';
  const notPersisted = prefsNarrow.leftCollapsed === false && prefsNarrow.previewFull === false;
  record('#47-9 窄窗（800×600 + 预览占位）：自动收起左栏且预览临时全窗口，未被写成用户偏好', autoHappened && notPersisted, {
    rule: '中央下限 460 + 分隔条 12 + 左栏下限 232 = 704，800×600 下三栏并排放不下；无预览时允许左栏并排（272/460 是正确的）',
    facts: narrow,
    prefs: { leftCollapsed: prefsNarrow.leftCollapsed, previewFull: prefsNarrow.previewFull, leftWidth: prefsNarrow.leftWidth, rightWidth: prefsNarrow.rightWidth, rightOpen: prefsNarrow.rightOpen },
  });
  await shot('47-narrow-with-preview');

  let settingsVia = 'chrome';
  try {
    await openSettingsPage();
  } catch (error) {
    settingsVia = 'collapse-first';
    await collapsePreview();
    await openSettingsPage();
  }
  await selectSettingsCategory('regular');
  await click('[data-settings-key="regular.window.reset-layout"] button');
  await waitNotice('ok', '已还原默认布局', 20000, '窄窗还原默认布局');
  const prefsReset = await waitLayout(function (p) { return p.leftWidth === 292 && p.leftCollapsed === false && p.rightOpen === false && p.previewFull === false; }, { label: '窄窗还原后的服务端偏好' });
  record('#47-10 窄窗（预览全窗口）下点击还原：保存成功、偏好回默认', true, { viewport: narrow.viewport, settingsVia: settingsVia, prefs: { leftWidth: prefsReset.leftWidth, rightWidth: prefsReset.rightWidth, leftCollapsed: prefsReset.leftCollapsed, rightOpen: prefsReset.rightOpen, previewFull: prefsReset.previewFull } });
  await backFromSettingsPage();
  await enterConversationMode();
  await sleep(900);

  if (!(await exists('.preview-reopen'))) {
    const reason = '窄窗还原后没有预览恢复入口（标签丢失），无法继续验证重开/拉宽；按 UNVERIFIED 记录';
    markUnverified('#47-11 窄窗重开预览：自动全窗口', reason, {});
    markUnverified('#47-12 拉宽回分栏', reason, {});
    return;
  }
  await click('.preview-reopen');
  await sleep(1300);
  const narrowReopened = await overviewFacts();
  const prefsReopened = await layoutPrefs();
  record('#47-11 窄窗重开预览：自动全窗口，previewFull 不写偏好',
    (narrowReopened.previewFull === 'true' || narrowReopened.overlay === true) && prefsReopened.previewFull === false && prefsReopened.leftCollapsed === false,
    { facts: narrowReopened, prefs: { previewFull: prefsReopened.previewFull, leftCollapsed: prefsReopened.leftCollapsed, rightOpen: prefsReopened.rightOpen } });

  await page.setViewport(1440, 900);
  await sleep(1400);
  const wideRestored = await overviewFacts();
  const prefsWide = await layoutPrefs();
  const splitBack = wideRestored.leftPane === true && wideRestored.rightPane === true && wideRestored.overlay === false && wideRestored.leftAuto === 'false' && wideRestored.previewFull === 'false';
  record('#47-12 拉宽到 ≥1280：回到分栏，偏好仍是默认（窄窗临时态未落盘）',
    splitBack && Math.abs(Number(wideRestored.leftWidth) - 292) <= 1 && prefsWide.leftWidth === 292 && prefsWide.rightWidth === 420 && prefsWide.leftCollapsed === false && prefsWide.previewFull === false,
    { facts: wideRestored, prefs: { leftWidth: prefsWide.leftWidth, rightWidth: prefsWide.rightWidth, leftCollapsed: prefsWide.leftCollapsed, previewFull: prefsWide.previewFull } });
  await shot('47-wide-restored-from-narrow');
}
let status500Rules = [];
let status500ListenerInstalled = false;

async function install500(match, responseCode, responseBody) {
  // cdp.mjs 没有 fulfillRequest：先用超长 delay 让它的 auto-continue 挂起，再由本监听器立即 fulfill。
  // delay 的定时器不会真的等到；脚本结束用 process.exit 收尾。
  await page.delayRequests(match, 30 * 60 * 1000);
  const rule = { match: match, code: responseCode || 500, body: responseBody || '{"ok":false,"error":"simulated 500"}' };
  status500Rules.push(rule);
  if (!status500ListenerInstalled) {
    status500ListenerInstalled = true;
    page.browser.on(function (msg) {
      if (!page || msg.sessionId !== page.sessionId || msg.method !== 'Fetch.requestPaused') return;
      const params = msg.params || {};
      const request = params.request || {};
      const hit = status500Rules.find(function (item) { return item.match(request.url || '', request.method || 'GET', params); });
      if (!hit) return;
      page.browser.send('Fetch.fulfillRequest', {
        requestId: params.requestId,
        responseCode: hit.code,
        responseHeaders: [
          { name: 'Content-Type', value: 'application/json' },
          { name: 'Access-Control-Allow-Origin', value: '*' },
        ],
        body: Buffer.from(hit.body, 'utf8').toString('base64'),
      }, page.sessionId).catch(function () { /* 请求可能已被跳过 */ });
    });
  }
  return rule;
}

async function clearInterceptions() {
  status500Rules = [];
  if (page) await page.clearFailRequests().catch(function () { /* ignore */ });
}

async function shotSafe(name) {
  try { return await shot(name); } catch { return null; }
}

async function readAddMenuNotice() {
  return page.eval(`(function(){
    const host = document.querySelector('[data-im="add-material-status"]');
    if (!host) return null;
    const textEl = host.querySelector('[data-im="add-material-status-text"]') || host;
    return { text: (textEl.innerText || textEl.textContent || '').trim(), className: host.className, retry: Boolean(host.querySelector('[data-im="add-material-retry"]')), batchId: host.getAttribute('data-batch-id') };
  })()`);
}

async function readCardByText(needle) {
  return page.eval(`(function(){
    const cards = [...document.querySelectorAll('[data-im="card"]')];
    const index = cards.findIndex(function (card) { return (card.innerText || '').indexOf(${JSON.stringify(needle)}) >= 0; });
    if (index < 0) return { index: -1, count: cards.length };
    const card = cards[index];
    return {
      index: index,
      count: cards.length,
      text: (card.innerText || '').trim().slice(0, 300),
      unlinked: Boolean(card.querySelector('[data-im="file-card-unlinked"]')),
      retry: Boolean(card.querySelector('[data-im="card-attach-retry"]')),
      statusText: (function(){ const el = card.querySelector('[data-im="card-attach-status-text"]'); return el ? (el.innerText || '').trim() : ''; })(),
      statusTone: (function(){ const el = card.querySelector('[data-im="card-attach-status"]'); return el ? el.className : ''; })(),
    };
  })()`);
}

async function readToolbarState() {
  return page.eval(`(function(){
    const host = document.querySelector('[data-im="card-toolbar-host"]');
    const attachBtn = (host || document).querySelector('[data-im="card-attach-file"]');
    return {
      present: Boolean(host),
      attachText: attachBtn ? (attachBtn.innerText || '').trim() : '',
      openPresent: Boolean((host || document).querySelector('[data-im="card-attachment-open"]')),
      retryPresent: Boolean((host || document).querySelector('[data-im="card-attach-retry"]')),
      buttons: [...(host || document).querySelectorAll('button')].map(function (b) { return (b.innerText || b.getAttribute('aria-label') || '').trim(); }).filter(Boolean).slice(0, 20),
    };
  })()`);
}

async function s3CrossPageUpload() {
  await enterInteractiveMode();
  await sleep(600);
  const boardBefore = await count('[data-im="card"]');
  const fixtureName = 'crosspage-' + RUN_ID + '.txt';
  const fixturePath = join(MATERIALS, fixtureName);
  writeFileSync(fixturePath, 'cross page upload fixture ' + RUN_ID + '\n' + '正文行用于确认附件可读\n'.repeat(12), 'utf8');

  await page.delayRequests(function (url, method) { return method === 'POST' && String(url).indexOf('/api/attachments/upload') >= 0; }, 4500);
  simulated.push('③-1 上传延迟 4.5s（CDP Fetch delayRequests，等价 route 延迟；真实端点 POST /api/attachments/upload）');
  await click('[data-im="add-menu"]');
  await waitExpr('document.querySelector("[data-im=\\"add-menu-list\\"]")', 10000, '添加菜单');
  const chooser = page.setFilesOnChooser([fixturePath], { timeout: 20000 });
  await click('[data-im="add-file"]');
  await chooser;
  await waitExpr('document.querySelectorAll("[data-im=\\"card\\"]").length > ' + boardBefore, 20000, '上传中文件卡出现');
  await sleep(500);
  await shot('46-uploading-interactive');

  await openSettingsPage();
  await shot('46-uploading-settings');
  await sleep(6500);
  await backFromSettingsPage();
  await waitExpr('document.querySelector("[data-im=\\"board\\"]")', 30000, '返回互动板面');
  await waitExpr('document.querySelectorAll("[data-im=\\"card\\"]").length >= ' + (boardBefore + 1), 30000, '返回后板面卡片恢复');
  await sleep(1500);
  const notice = await readAddMenuNotice();
  const countAfter = await count('[data-im="card"]');
  await shot('46-returned-board');
  await sleep(2500);
  const countStable = await count('[data-im="card"]');
  await clearInterceptions();

  const card = await readCardByText(fixtureName);
  if (card.index >= 0) {
    await click('[data-im="card"] [data-im="card-body"]', card.index).catch(function () { return click('[data-im="card"]', card.index); });
    await sleep(500);
  }
  const toolbar = await readToolbarState();
  const noticeOk = Boolean(notice) && /is-ok/.test(String(notice.className || '')) && /已接收|已保存|已关联|成功/.test(String(notice.text || ''));
  const successEvidence = noticeOk ? { channel: 'add-material-status', notice: notice } : { channel: 'card-association', note: '单文件 AddMenu 的成功提示按设计 3s 自动消失；以卡片关联事实为准', notice: notice };

  record('#46-1 上传在途时切到设置页（两张截图）', true, { shots: ['46-uploading-interactive', '46-uploading-settings'], simulated: true });
  record('#46-2 返回后只多一张卡且数量稳定（无重复卡）', countAfter === boardBefore + 1 && countStable === countAfter, { before: boardBefore, after: countAfter, stable: countStable });
  record('#46-3 文件卡已关联：工具栏变「重新关联附件」且无「未关联」提示',
    card.index >= 0 && /重新关联附件/.test(toolbar.attachText) && card.unlinked === false,
    { card: card, toolbar: toolbar, openEntry: toolbar.openPresent });
  record('#46-4 界面没有要求重新选文件/重新导入；成功状态可读',
    card.retry === false && toolbar.retryPresent === false && (noticeOk || card.index >= 0),
    successEvidence);

  // 补充：单文件拖放（批次通道，含「已接收」明细）；非跨页，仅补批次状态证据
  // 首次跑读得太早（进行中 1 份 / uploading）；这次轮询到批次结算（无 uploading/pending、进行中 0 份）再断言。
  try {
    const beforeDrop = await count('[data-im="card"]');
    const dropName = 'dropbatch-' + RUN_ID + '.txt';
    const dataUrl = 'data:text/plain;base64,' + Buffer.from('drag batch ' + RUN_ID + '\n', 'utf8').toString('base64');
    await page.dragFiles('[data-im="add-menu"]', [{ name: dropName, type: 'text/plain', dataUrl: dataUrl }]);
    const deadline = Date.now() + 30000;
    let batchState = null;
    while (Date.now() < deadline) {
      batchState = await page.eval(`(function(){
        const host = document.querySelector('[data-im="add-material-status"]');
        const items = [...document.querySelectorAll('[data-im="add-batch-item"]')].map(function(li){ return { name: li.getAttribute('data-name'), status: li.getAttribute('data-status'), text: (li.innerText||'').trim() }; });
        const summary = host ? (host.innerText || '').trim() : '';
        const active = items.some(function(item){ return item.status === 'uploading' || item.status === 'pending'; });
        return { summary: summary, items: items, active: active };
      })()`);
      if (batchState.items.length >= 1 && !batchState.active && !/进行中\s*[1-9]\s*份/.test(batchState.summary)) break;
      await sleep(500);
    }
    await shot('46-drop-batch-success');
    const settled = Boolean(batchState) && batchState.items.length >= 1 && batchState.active === false && /已接收\s*1\s*份/.test(batchState.summary) && batchState.items.some(function (i) { return i.status === 'succeeded'; });
    record('#46-5 补充：单文件拖放批次结算后显示「已接收 1 份」（非跨页）', settled, { batch: batchState, cardsBefore: beforeDrop, cardsAfter: await count('[data-im="card"]') });
  } catch (error) {
    markUnverified('#46-5 补充：单文件拖放批次证据', '拖放批次未在超时内结算为「已接收」：' + String(error && error.message || error).slice(0, 300));
  }
  await enterConversationMode();
}

async function s4SettingsSearchFold() {
  await openSettingsPage();
  await selectSettingsCategory('regular');
  await openSettingsSearch('单段长度');
  const hits = await page.eval(`(function(){ return [...document.querySelectorAll('[data-search-hit]')].map(function(el){ return el.getAttribute('data-search-hit'); }); })()`);
  must(hits.indexOf('memory.fragment.max-tokens') >= 0, '搜索「单段长度」没有命中 memory.fragment.max-tokens；实际=' + JSON.stringify(hits));
  await click('[data-search-hit="memory.fragment.max-tokens"]');
  await sleep(1500);
  const facts = await page.eval(`(function(){
    const fold = document.querySelector('[data-settings-fold="memory.advanced"]');
    const toggle = fold ? fold.querySelector('[data-settings-fold-toggle="memory.advanced"]') : null;
    const target = document.querySelector('[data-settings-key="memory.fragment.max-tokens"]');
    const input = target ? target.querySelector('input') : null;
    const rect = target ? target.getBoundingClientRect() : null;
    if (input && !document.activeElement) input.focus();
    input && input.focus();
    return {
      foldExpanded: toggle ? toggle.getAttribute('aria-expanded') : null,
      targetExists: Boolean(target),
      targetVisible: Boolean(target) && getComputedStyle(target).display !== 'none' && getComputedStyle(target).visibility !== 'hidden',
      highlighted: Boolean(target && target.className.indexOf('is-revealed') >= 0),
      inputExists: Boolean(input),
      inputLabel: input ? input.getAttribute('aria-label') : null,
      inputFocused: Boolean(input) && document.activeElement === input,
      category: (document.querySelector('[data-settings-page]') || {}).dataset ? document.querySelector('[data-settings-page]').getAttribute('data-settings-page') : null,
      rect: rect ? { x: rect.x, y: rect.y, width: rect.width, height: rect.height } : null,
    };
  })()`);
  const ok = facts.foldExpanded === 'true' && facts.targetExists && facts.targetVisible && facts.inputExists && facts.inputLabel === '单段长度目标' && facts.inputFocused === true;
  record('#47-13 设置搜索「单段长度」：自动展开「记忆·高级选项」并定位到可聚焦输入框', ok, facts);
  await clipShot('settings-search-fold', '[data-settings-key="memory.fragment.max-tokens"]');
}

async function snapshotSettingsSearch() {
  return page.eval(`(function(){
    const input = document.querySelector('[data-testid="settings-search-input"]');
    const panel = document.querySelector('[data-testid="settings-search-panel"]');
    return {
      inputValue: input ? input.value : null,
      panelDisplay: panel ? getComputedStyle(panel).display : null,
      panelVisible: Boolean(panel && getComputedStyle(panel).display !== 'none'),
      hits: [...document.querySelectorAll('[data-search-hit]')].map(function(el){ return el.getAttribute('data-search-hit'); }),
      groups: [...document.querySelectorAll('.set-search__group')].map(function(el){ return (el.innerText || '').trim(); }),
      countText: (function(){ const el = document.querySelector('.set-search__count'); return el ? (el.innerText || '').trim() : null; })(),
      emptyText: (function(){ const el = document.querySelector('[data-testid="settings-search-empty"]'); return el ? (el.innerText || '').trim() : null; })(),
      errorText: (function(){ const el = document.querySelector('[data-testid="settings-search-error"]'); return el ? (el.innerText || '').trim() : null; })(),
    };
  })()`);
}
async function s5SettingsSearchResetLayout() {
  if (!(await exists('[data-testid="settings-page"]'))) await openSettingsPage();
  await selectSettingsCategory('regular');
  await openSettingsSearch('还原默认布局');
  const hitSelector = '[data-search-hit="regular.window.reset-layout"]';
  let hit = false;
  const hitDeadline = Date.now() + 2500;
  while (Date.now() < hitDeadline) {
    hit = await exists(hitSelector);
    if (hit) break;
    await sleep(250);
  }
  const searchState = await snapshotSettingsSearch();
  if (!hit) {
    await shot('47-14-search-miss');
    const archives = await archiveScenarioEvidence('s5-search-miss').catch(function () { return []; });
    record('#47-14 设置搜索「还原默认布局」：命中并定位到真实按钮', false, {
      inputValue: searchState.inputValue,
      expectedQuery: '还原默认布局',
      hits: searchState.hits,
      groups: searchState.groups,
      countText: searchState.countText,
      emptyText: searchState.emptyText,
      errorText: searchState.errorText,
      panelVisible: searchState.panelVisible,
      note: '轮询 2.5s 仍无命中；同一搜索框在 s4 能命中「单段长度」，说明搜索通道可用；已把输入值/命中列表/分组/空态与 provider/backend 证据归档',
      failureArchives: archives,
    });
    return;
  }
  await click(hitSelector);
  let facts = null;
  let highlightObserved = false;
  for (let attempt = 0; attempt < 12; attempt += 1) {
    await sleep(120);
    facts = await page.eval(`(function(){
    const target = document.querySelector('[data-settings-key="regular.window.reset-layout"]');
    const button = target ? target.querySelector('button') : null;
    return {
      hit: Boolean(document.querySelector('[data-search-hit="regular.window.reset-layout"]')),
      targetExists: Boolean(target),
      targetVisible: Boolean(target) && getComputedStyle(target).display !== 'none',
      highlighted: Boolean(target && target.className.indexOf('is-revealed') >= 0),
      buttonText: button ? (button.innerText || '').trim() : null,
      category: (document.querySelector('[data-settings-page]') || {}).getAttribute ? document.querySelector('[data-settings-page]').getAttribute('data-settings-page') : null,
      searchPanelOpen: (function(){ const panel = document.querySelector('[data-testid="settings-search-panel"]'); return panel ? getComputedStyle(panel).display !== 'none' : false; })(),
    };
  })()`);
    if (facts && facts.highlighted) { highlightObserved = true; break; }
  }
  // 高亮只存活 1600ms（HIGHLIGHT_MS），慢一轮会刚好错过；定位是否成功以「目标可见 + 按钮文案」为准，高亮单独记录。
  const ok = Boolean(facts) && facts.hit && facts.targetExists && facts.targetVisible && /还原默认布局/.test(String(facts.buttonText || ''));
  record('#47-14 设置搜索「还原默认布局」：命中并定位到真实按钮', ok, Object.assign({ inputValue: searchState.inputValue, hitKeys: searchState.hits, groups: searchState.groups, highlightObserved: highlightObserved }, facts));
  await clipShot('settings-search-reset-layout', '[data-settings-key="regular.window.reset-layout"]');
  await closeSettingsSearch();
  await backFromSettingsPage();
}

async function s7SaveFailureRetry() {
  await openSettingsPage();
  await selectSettingsCategory('regular');
  // 本场景里 PUT /api/settings/ui 只有「还原默认布局」会发；不必依赖 postData 字段是否被 CDP 暴露。
  await install500(function (url, method) {
    return method === 'PUT' && String(url).indexOf('/api/settings/ui') >= 0;
  }, 500, '{"ok":false,"error":"simulated 500"}');
  simulated.push('③-2 [模拟] PUT /api/settings/ui（conversation_layout）返回 500：CDP Fetch fulfillRequest，只影响还原布局这一次写入');
  await click('[data-settings-key="regular.window.reset-layout"] button');
  const err = await waitNotice('err', '还原默认布局失败', 25000, '保存失败提示');
  const retryVisible = await exists('[data-settings-action="retry"]');
  await shot('47-save-failure-500');
  record('#47-15 [模拟] 布局保存失败（HTTP 500）：明确失败提示 + 重试入口', Boolean(err) && retryVisible, { notice: err, simulated: true });
  await clearInterceptions();
  await click('[data-settings-action="retry"]');
  const ok = await waitNotice('ok', '已还原默认布局', 25000, '重试成功提示');
  await shot('47-save-retry-ok');
  record('#47-16 [模拟] 重试成功：显示「已还原默认布局」', Boolean(ok), { notice: ok, simulated: true });
  await backFromSettingsPage();
}
async function collapsePreview() {
  if (await exists('button[aria-label^="收起预览"]')) {
    await click('button[aria-label^="收起预览"]');
    await sleep(700);
  } else if (await exists('.pv-head')) {
    await pressKey('Escape');
    await sleep(600);
  }
}

async function probeControls(required) {
  const source = '(function(){' +
    'const required = ' + JSON.stringify(required) + ';' +
    'const controls = required.map(function (sel) {' +
      'const el = document.querySelector(sel);' +
      'if (!el) return { sel: sel, present: false };' +
      'const r = el.getBoundingClientRect();' +
      'const st = getComputedStyle(el);' +
      'const cx = r.x + r.width / 2, cy = r.y + r.height / 2;' +
      'const inViewport = cx >= 0 && cy >= 0 && cx <= innerWidth && cy <= innerHeight;' +
      'const hit = inViewport ? document.elementFromPoint(cx, cy) : null;' +
      'return { sel: sel, present: true, visible: r.width > 0 && r.height > 0 && st.visibility !== "hidden" && st.display !== "none", inViewport: inViewport, hitIsSelf: Boolean(hit && (hit === el || el.contains(hit))), disabled: Boolean(el.disabled), rect: { x: Math.round(r.x), y: Math.round(r.y), width: Math.round(r.width), height: Math.round(r.height) } };' +
    '});' +
    'const overflowSelectors = [".conversation-workspace", ".interactive", ".composer", ".pv-head", ".set-search", ".update-card", ".qio-settings"];' +
    'const overflow = [];' +
    'for (const sel of overflowSelectors) {' +
      'const el = document.querySelector(sel);' +
      'if (!el) continue;' +
      'overflow.push({ sel: sel, scrollWidth: el.scrollWidth, clientWidth: el.clientWidth, overflowX: el.scrollWidth - el.clientWidth > 1 });' +
    '}' +
    'const notices = [...document.querySelectorAll(".set-notice,[role=alert],[data-im=add-material-status-text]")].map(function (el) { return (el.innerText || el.textContent || "").trim(); }).filter(Boolean);' +
    'const duplicateNotices = notices.filter(function (item, index) { return notices.indexOf(item) !== index; });' +
    'return {' +
      'controls: controls,' +
      'overflow: overflow,' +
      'notices: notices,' +
      'duplicateNotices: [...new Set(duplicateNotices)],' +
      'docOverflow: document.documentElement.scrollWidth - innerWidth > 1,' +
      'viewport: { w: innerWidth, h: innerHeight, docScrollWidth: document.documentElement.scrollWidth }' +
    '};' +
  '})()';
  return page.eval(source);
}

function probePass(probe) {
  const controlsOk = probe.controls.every(function (c) { return c.present && c.visible && c.inViewport && c.hitIsSelf; });
  const overflowOk = !probe.docOverflow && !probe.overflow.some(function (o) { return o.overflowX; });
  const duplicatesOk = probe.duplicateNotices.length === 0;
  return { controlsOk: controlsOk, overflowOk: overflowOk, duplicatesOk: duplicatesOk, pass: controlsOk && overflowOk && duplicatesOk };
}

/** 把 probe 失败子项展开成可读清单（Lead 要求矩阵失败要能一眼看出是哪一项）。 */
function probeFailSummary(probe) {
  if (!probe) return { pass: true, note: '未执行（可选探针）' };
  const checks = probePass(probe);
  return {
    pass: checks.pass,
    controlsOk: checks.controlsOk,
    failingControls: probe.controls.filter(function (c) { return !(c.present && c.visible && c.inViewport && c.hitIsSelf); }).map(function (c) { return { sel: c.sel, present: c.present, visible: c.visible, inViewport: c.inViewport, hitIsSelf: c.hitIsSelf }; }),
    overflowOk: checks.overflowOk,
    overflowing: probe.overflow.filter(function (o) { return o.overflowX; }),
    docOverflow: probe.docOverflow,
    duplicatesOk: checks.duplicatesOk,
    duplicateNotices: probe.duplicateNotices,
  };
}

async function applyTheme(theme) {
  await openSettingsPage();
  await selectSettingsCategory('regular');
  await click('[data-settings-key="regular.theme"] [data-settings-option=' + theme + ']');
  await waitExpr('document.documentElement.getAttribute("data-theme") === ' + JSON.stringify(theme), 12000, '主题生效：' + theme);
  await sleep(600);
  await backFromSettingsPage();
}

async function captureCombo(theme, width, height) {
  const label = theme + '-' + width + 'x' + height;
  await enterConversationMode();
  await page.setViewport(width, height);
  await sleep(1100);
  const shots = [];
  const residualBoardCards = await count('[data-im="card"]');
  shots.push(await shot('matrix-' + label + '-conversation'));
  const baseConvProbe = await probeControls(['#composer-mode-switch .mode-btn', '#composer-input', '.send-btn']);
  if (!(await exists('.right-pane'))) {
    if (await exists('.preview-reopen')) await click('.preview-reopen');
  }
  await waitExpr('document.querySelector(".pv-head")', 20000, '预览标题行').catch(function () { return null; });
  if (await exists('.pv-head')) shots.push(await clipShot('matrix-' + label + '-preview-head', '.pv-head'));
  // 800×600 下预览会自动全窗口，背景被 inert；对基础控件与预览控件分开探测，避免互相遮蔽造成假失败。
  const previewSearchPresent = await exists('.pv-search-input');
  const previewProbe = (await exists('.pv-head')) ? await probeControls(['.pv-head']) : null;
  await collapsePreview();
  await enterInteractiveMode();
  await sleep(900);
  shots.push(await shot('matrix-' + label + '-interactive'));
  if (await exists('.composer')) shots.push(await clipShot('matrix-' + label + '-interactive-bottom', '.composer'));
  const interactiveProbe = await probeControls(['[data-im=add-menu]', '#composer-input', '.send-btn']);
  await enterConversationMode();
  await sleep(600);
  await openSettingsPage();
  await selectSettingsCategory('regular');
  await openSettingsSearch('单段长度');
  await sleep(700);
  shots.push(await shot('matrix-' + label + '-settings-search'));
  const settingsProbe = await probeControls(['[data-testid=settings-search-input]', '.set-search__hit']);
  await closeSettingsSearch();
  shots.push(await shot('matrix-' + label + '-settings'));
  await backFromSettingsPage();
  await enterConversationMode();
  await collapsePreview();

  const convPreviewPass = previewProbe ? probePass(previewProbe) : { pass: true };
  const checks = [probePass(baseConvProbe), convPreviewPass, probePass(interactiveProbe), probePass(settingsProbe)];
  const duplicates = [...new Set([].concat(baseConvProbe.duplicateNotices, previewProbe ? previewProbe.duplicateNotices : [], interactiveProbe.duplicateNotices, settingsProbe.duplicateNotices))];
  const allPass = checks.every(function (c) { return c.pass; });
  record('视觉-' + label + ' 控件可达 / 无横向溢出 / 无重复提示', allPass, {
    residualBoardCards: residualBoardCards,
    convBase: probeFailSummary(baseConvProbe),
    convPreview: Object.assign(probeFailSummary(previewProbe), { searchInputPresent: previewSearchPresent }),
    interactive: probeFailSummary(interactiveProbe),
    settings: probeFailSummary(settingsProbe),
    duplicates: duplicates,
  });
  record('视觉-' + label + ' 关键截图（对话底部/预览标题/互动底部/设置搜索）', shots.length >= 5, { shots: shots });
  return { label: label, shots: shots, pass: allPass };
}

async function captureFailureEvidence() {
  await openSettingsPage();
  await selectSettingsCategory('about');
  try {
    await waitExpr('document.querySelector(\'[data-settings-key="about.update"] [data-action="check"]:not([disabled])\')', 8000, '检查更新按钮可用');
    await click('[data-settings-key="about.update"] [data-action="check"]');
  } catch (error) {
    await shotSafe('matrix-failure-click-failed');
    markUnverified('视觉-失败浮层（真实入口）', '没能点击「检查更新」（按钮不可用或更新操作在途）：' + String(error && error.message || error).slice(0, 240));
    await backFromSettingsPage();
    return;
  }
  const deadline = Date.now() + 20000;
  let state = null;
  while (Date.now() < deadline) {
    state = await page.eval('(function(){' +
      'const line = document.querySelector(".update-card .line.err, .update-card .line.ok, .update-card .line");' +
      'if (!line) return null;' +
      'return { text: (line.innerText || "").trim(), className: line.className, role: line.getAttribute("role") };' +
    '})()').catch(function () { return null; });
    if (state && (/\berr\b/.test(String(state.className)) || /\bok\b/.test(String(state.className)))) break;
    await sleep(500);
  }
  const failed = Boolean(state && /\berr\b/.test(String(state.className)));
  if (failed) {
    record('视觉-失败态（真实入口：设置→关于与更新→检查更新）', true, { state: state, note: '真实入口的失败提示是内联 .line.err，不是浮层；已保留截图' });
    await clipShot('matrix-failure-inline', '.update-card');
    markUnverified('视觉-失败浮层（真实入口）', '真实入口只出现内联失败提示（.line.err），没有浮层形态；失败态本身已取证，见截图 matrix-failure-inline', { realFailure: state });
  } else {
    await shotSafe('matrix-failure-check');
    markUnverified('视觉-失败浮层（真实入口）', '浏览器环境下更新检查没有落失败态（Tauri updater 不可用时的具体表现未触发），没有取得失败浮层证据', { state: state });
  }
  await backFromSettingsPage();
}

async function s6VisualMatrix() {
  const combos = [];
  for (const theme of ['light', 'dark']) {
    await applyTheme(theme);
    combos.push(await captureCombo(theme, 1440, 900));
    combos.push(await captureCombo(theme, 800, 600));
  }
  await page.setViewport(1440, 900);
  await sleep(700);
  await captureFailureEvidence();
  record('视觉-矩阵总览（2 主题 × 2 尺寸）', combos.length === 4 && combos.every(function (c) { return c.pass; }), { combos: combos.map(function (c) { return { label: c.label, pass: c.pass, shots: c.shots.length }; }) });
}

const failureArchives = [];

/** 失败时把 provider 台账（含 _body）与后端日志末尾 200 行归档，供下一轮独立判断 fixture/产品。 */
async function archiveScenarioEvidence(id) {
  const stamp = new Date().toISOString().replace(/[-:T.]/g, '').slice(0, 14);
  const base = join(OUT, 'failure-' + id + '-' + stamp);
  const files = [];
  try {
    const providerLog = await fetchJson(PROVIDER + '/__log');
    const providerPath = base + '-provider-log.json';
    writeFileSync(providerPath, JSON.stringify(providerLog, null, 2), 'utf8');
    files.push(providerPath);
  } catch (error) {
    log('[evidence] provider /__log 归档失败：' + String(error && error.message || error));
  }
  try {
    const backendLog = join(process.env.TEMP || '.', 'qio-e2e-logs', 'backend.log');
    if (existsSync(backendLog)) {
      const lines = readFileSync(backendLog, 'utf8').split('\n');
      const tail = lines.slice(Math.max(0, lines.length - 200)).join('\n');
      const backendPath = base + '-backend-tail.log';
      writeFileSync(backendPath, tail, 'utf8');
      files.push(backendPath);
    }
  } catch (error) {
    log('[evidence] backend.log 归档失败：' + String(error && error.message || error));
  }
  try {
    const pageInfo = {
      url: await page.eval('location.href'),
      title: await page.eval('document.title'),
      consoleErrors: (page.consoleErrors || []).slice(-40),
    };
    const pagePath = base + '-page.json';
    writeFileSync(pagePath, JSON.stringify(pageInfo, null, 2), 'utf8');
    files.push(pagePath);
  } catch { /* 页面已崩时忽略 */ }
  failureArchives.push({ scenario: id, at: new Date().toISOString(), files: files });
  if (files.length) log('[evidence] 失败归档：' + files.join(', '));
  return files;
}
async function runScenario(id, label, fn) {
  log('');
  log('---- ' + id + ' ' + label + ' ----');
  try {
    await fn();
  } catch (error) {
    const archives = await archiveScenarioEvidence(id).catch(function () { return []; });
    record(id + ' 场景未跑完', false, { error: String(error && error.stack || error).slice(0, 1600), failureArchives: archives });
    await shotSafe(id + '-failed');
  } finally {
    await clearInterceptions();
  }
}

async function main() {
  log('== QIO ui-verify == runId=' + RUN_ID);
  log('base=' + BASE + ' api=' + API + ' provider=' + PROVIDER + ' cdp=' + CDP_PORT);
  log('WT=' + WT);
  log('OUT=' + OUT);
  const envInfo = headAndStatus();
  log('HEAD=' + envInfo.head);
  if (envInfo.status) log('工作树未干净（只读记录）：\n' + envInfo.status);
  await assertServices();
  await fetch(API + '/api/onboarding/complete', { method: 'POST' }).catch(function () { return null; });
  browser = await CdpBrowser.launch({ port: CDP_PORT });
  page = await Page.create(browser);
  await page.setViewport(1440, 900);
  await page.goto(BASE + '/?ui-verify=' + RUN_ID);
  await waitExpr('document.querySelector(".stream") && document.querySelector("#composer-mode-switch .mode-btn")', 60000, '应用就绪');
  await sleep(900);
  record('环境-0 服务与集成 HEAD 记录', true, {
    head: envInfo.head,
    worktreeDirty: Boolean(envInfo.status),
    base: BASE, api: API, provider: PROVIDER, cdpPort: CDP_PORT,
    out: OUT,
  });
  await preflight();
  const run = function (id) { return ONLY.length === 0 || ONLY.indexOf(id) >= 0; };
  if (run('s1')) await runScenario('s1', '#47 布局还原（宽窗）', s1WideLayout);
  if (run('s2')) await runScenario('s2', '#47 窄窗自动布局', s2NarrowLayout);
  if (run('s3')) await runScenario('s3', '#46 跨页上传', s3CrossPageUpload);
  if (run('s4')) await runScenario('s4', '设置搜索 fold', s4SettingsSearchFold);
  if (run('s5')) await runScenario('s5', '设置搜索还原布局', s5SettingsSearchResetLayout);
  if (run('s7')) await runScenario('s7', '[模拟] 保存失败→重试', s7SaveFailureRetry);
  if (run('s6')) await runScenario('s6', '文案与视觉矩阵', s6VisualMatrix);
}

function writeSummary() {
  const pass = results.filter(function (r) { return r.ok; }).length;
  const unverified = results.filter(function (r) { return r.unverified; }).length;
  const fail = results.filter(function (r) { return !r.ok && !r.unverified; }).length;
  const summary = {
    runId: RUN_ID,
    startedAt: STARTED_AT,
    finishedAt: new Date().toISOString(),
    base: BASE, api: API, provider: PROVIDER, cdpPort: CDP_PORT, out: OUT,
    head: (headAndStatus() || {}).head || '',
    pass: pass, fail: fail, unverified: unverified, total: results.length,
    results: results,
    simulated: simulated,
    unverifiedNotes: unverifiedNotes,
    failureArchives: failureArchives,
    consoleErrors: page ? page.consoleErrors.slice(0, 80) : [],
    note: '真实界面驱动：系统 Edge + CDP（scripts/verify-pm-qio032/cdp.mjs）；模型调用只打本机 fake provider；所有 [模拟] 见 simulated。',
  };
  writeFileSync(join(OUT, 'summary.json'), JSON.stringify(summary, null, 2), 'utf8');
  // 文字版结论（Lead 直接引用进交付报告）：每条 PASS/FAIL/UNVERIFIED + 一句事实 + 证据文件。
  const report = [];
  report.push('# QIO 0.3.2 · 真实界面验收报告（ui-verify）');
  report.push('');
  report.push('- runId: `' + RUN_ID + '`');
  report.push('- HEAD: `' + (summary.head || '') + '`');
  report.push('- 时间: ' + summary.startedAt + ' → ' + summary.finishedAt);
  report.push('- 环境: base=' + BASE + ' api=' + API + ' provider=' + PROVIDER + ' cdp=' + CDP_PORT);
  report.push('- 结果: **PASS ' + summary.pass + ' / FAIL ' + summary.fail + ' / UNVERIFIED ' + summary.unverified + ' / TOTAL ' + summary.total + '**');
  report.push('');
  report.push('## 逐条结论');
  report.push('');
  results.forEach(function (entry, index) {
    const tag = entry.unverified ? 'UNVERIFIED' : (entry.ok ? 'PASS' : 'FAIL');
    const detail = entry.detail || {};
    let fact = '';
    for (const key of ['note', 'reason']) { if (typeof detail[key] === 'string' && detail[key]) { fact = detail[key]; break; } }
    if (!fact && detail.error) fact = String(detail.error).split('\n')[0];
    if (!fact && detail.shots) fact = '截图：' + detail.shots.join(', ');
    if (!fact && detail.failureArchives) fact = '失败归档：' + detail.failureArchives.join(', ');
    if (!fact) { try { fact = JSON.stringify(detail).slice(0, 260); } catch { fact = '(detail 不可序列化)'; } }
    const evidence = [];
    if (Array.isArray(detail.shots)) evidence.push.apply(evidence, detail.shots);
    if (Array.isArray(detail.failureArchives)) evidence.push.apply(evidence, detail.failureArchives);
    if (!evidence.length) evidence.push('summary.json results[' + index + ']');
    report.push('- **[' + tag + ']** ' + entry.name + ' — ' + fact + '；证据：' + evidence.join(' , '));
  });
  report.push('');
  report.push('## [模拟] 项');
  if (summary.simulated.length) for (const item of summary.simulated) report.push('- ' + item);
  else report.push('- （无）');
  report.push('');
  report.push('## UNVERIFIED（不是 PASS）');
  if (summary.unverifiedNotes.length) for (const item of summary.unverifiedNotes) report.push('- ' + item.name + '：' + item.reason);
  else report.push('- （无）');
  report.push('');
  report.push('## 失败归档');
  if (summary.failureArchives.length) for (const item of summary.failureArchives) report.push('- ' + item.scenario + '：' + item.files.join(' , '));
  else report.push('- （无）');
  report.push('');
  report.push('## 页面 console errors（最多 80 条，完整见 summary.json）');
  if (summary.consoleErrors.length) for (const item of summary.consoleErrors) report.push('- ' + String(item).slice(0, 400));
  else report.push('- （无）');
  writeFileSync(join(OUT, 'REPORT.md'), report.join('\n') + '\n', 'utf8');
  writeFileSync(join(OUT, 'console.log'), results.map(function (r) {
    const tag = r.unverified ? 'UNVERIFIED' : (r.ok ? 'PASS' : 'FAIL');
    return '[' + tag + '] ' + r.name + (r.detail ? ' :: ' + JSON.stringify(r.detail) : '');
  }).join('\n') + '\n', 'utf8');
  return summary;
}

let fatal = null;
try {
  await main();
} catch (error) {
  fatal = error;
  record('主流程未跑完', false, { error: String(error && error.stack || error).slice(0, 2000) });
} finally {
  await clearInterceptions().catch(function () { return null; });
  try { if (browser) await browser.close(); } catch { /* ignore */ }
  const summary = writeSummary();
  log('');
  log('== 结果：PASS ' + summary.pass + ' / FAIL ' + summary.fail + ' / UNVERIFIED ' + summary.unverified + ' / TOTAL ' + summary.total + ' ==');
  log('summary=' + join(OUT, 'summary.json'));
  log('shots=' + SHOTS);
  if (fatal) log('FATAL=' + String(fatal && fatal.message || fatal));
  process.exit(summary.fail > 0 || fatal ? 1 : 0);
}
async function preflight() {
  const checks = [];
  const apiSettings = await fetchUISettings().catch(function (error) { return { error: String(error && error.message || error) }; });
  record('环境-2 后端会话可用（GET /api/settings/ui，只读核对服务端真值）', !apiSettings.error, {
    typewriter_cps: apiSettings.typewriter_cps,
    hasConversationLayout: Boolean(parseLayout(apiSettings.conversation_layout)),
    error: apiSettings.error || null,
  });
  const collect = async function (group, items) {
    const missing = [];
    for (const sel of items) {
      if (!(await exists(sel))) missing.push(sel);
    }
    checks.push({ group: group, missing: missing });
  };
  await enterConversationMode();
  await collect('conversation', ['#composer-mode-switch .mode-btn', '#composer-input', '.send-btn']);
  await enterInteractiveMode();
  await click('[data-im=add-menu]');
  await sleep(500);
  const menuMissing = [];
  for (const sel of ['[data-im=add-menu-list]', '[data-im=add-file]', '[data-im=add-text]']) {
    if (!(await exists(sel))) menuMissing.push(sel);
  }
  checks.push({ group: 'interactive-menu', missing: menuMissing });
  await click('[data-im=add-menu]').catch(function () { return null; });
  await sleep(300);
  await collect('interactive', ['[data-im=add-menu]']);
  await enterConversationMode();
  await openSettingsPage();
  await selectSettingsCategory('regular');
  await collect('settings', ['[data-testid=settings-page]', '[data-testid=settings-search-entry]', '[data-testid=settings-back]', '[data-settings-nav-tab=regular]', '[data-settings-key="regular.window.reset-layout"] button', '[data-settings-key="regular.theme"] [data-settings-option=light]']);
  await selectSettingsCategory('about');
  await collect('settings-about', ['[data-settings-key="about.update"] [data-action="check"]']);
  await backFromSettingsPage();
  await enterConversationMode();
  const missing = [];
  for (const check of checks) for (const sel of check.missing) missing.push(check.group + ': ' + sel);
  record('环境-1 关键选择器预检（对话/互动/设置）', missing.length === 0, { checks: checks, missing: missing });
  return missing;
}
