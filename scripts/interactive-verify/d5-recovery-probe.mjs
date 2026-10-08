/**
 * D 的第四轮独立验收探针：草稿恢复与失败处理的**真实浏览器**证据。
 *
 * 无 npm 依赖：Node 内置 fetch/WebSocket 直连 CDP，用 Edge + 独立持久化 profile + 独立端口
 * （本机 Chrome 曾因大量 headless 实例卡死，所以固定用 Edge）。
 *
 * 它按用户行为走一遍并留下数字与截图：
 *   1. 首次编辑卡片 → 在 600ms 保存防抖前刷新 → 重新打开编辑器，输入框里有没有那段字；
 *   2. 服务器已有草稿 → 用户确认编辑（清除草稿）→ 刷新 → 旧草稿有没有复活；
 *   3. 本机写入失败（只让 qio.draft.* 抛配额错误）→ 编辑处有没有说明；
 *   4/5. 对话页发送失败（拦截 /api/turns 返回 500）→ 有没有本次原因与找回入口；刷新后再看；
 *   7. 提交失败（拦截 /submissions 返回 500）→ 默认失败区显示的是不是本次原因；
 *   8. 480px 下关掉一个面板 → 切换条与剩余面板的矩形、相交面积、computed style、命中测试。
 *
 * 用法：
 *   node scripts/interactive-verify/d5-recovery-probe.mjs --app http://127.0.0.1:5454
 *        [--label baseline] [--out docs/interactive-ui-screenshots] [--port 9554]
 *        [--profile <目录>] [--only 1,2,3,4,5,7,8] [--reopen]
 *
 * 前置：后端 8954 与前端 5454 已经起来（见报告里的命令行）。
 */
import { spawn } from 'node:child_process';
import { mkdirSync, writeFileSync, existsSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { setTimeout as sleep } from 'node:timers/promises';

const HERE = dirname(fileURLToPath(import.meta.url));
const REPO = resolve(HERE, '..', '..');

function arg(name, fallback) {
  const index = process.argv.indexOf('--' + name);
  if (index >= 0 && process.argv[index + 1] && !process.argv[index + 1].startsWith('--')) return process.argv[index + 1];
  return fallback;
}
function flag(name) {
  return process.argv.includes('--' + name);
}
function want(id) {
  const only = arg('only', '');
  if (!only) return true;
  return only.split(',').map(function (s) { return s.trim(); }).includes(String(id));
}

const APP = arg('app', 'http://127.0.0.1:5454');
const LABEL = arg('label', 'baseline');
const OUT_DIR = resolve(REPO, arg('out', 'docs/interactive-ui-screenshots'));
const PORT = Number(arg('port', '9554'));
const PROFILE = arg('profile', join(process.env.TEMP || '.', 'qio-chrome-d'));
const EDGE = process.env.QIO_EDGE || 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe';
const BACKEND = arg('backend', 'http://127.0.0.1:8954');

mkdirSync(OUT_DIR, { recursive: true });
if (!existsSync(EDGE)) {
  console.error('找不到 Edge：' + EDGE + '（可以用环境变量 QIO_EDGE 指定）');
  process.exit(2);
}

const report = {
  label: LABEL,
  app: APP,
  profile: PROFILE,
  startedAt: new Date().toISOString(),
  steps: [],
  requests: [],
  httpFails: [],
  pageErrors: [],
  notes: [],
};

function step(name, ok, evidence, note) {
  report.steps.push({ name: name, ok: !!ok, evidence: evidence || null, note: note || null });
  console.log((ok ? 'OK   ' : 'FAIL ') + name + (note ? ' — ' + note : ''));
}

// ---------------- CDP 客户端 ----------------
let msgId = 0;
let ws = null;
let send = null;
let edge = null;
const INTERCEPT_DETAIL = {
  submissions: '模拟的提交失败原因-D5（探针拦截）',
  turns: '模拟的发送失败原因-D5（探针拦截）',
};
let interceptMode = null;

function makeClient(socket) {
  const pending = new Map();
  socket.addEventListener('message', function (ev) {
    const msg = JSON.parse(ev.data);
    if (msg.id && pending.has(msg.id)) {
      const slots = pending.get(msg.id);
      pending.delete(msg.id);
      if (msg.error) slots.reject(new Error(JSON.stringify(msg.error)));
      else slots.resolve(msg.result);
    }
  });
  return function (method, params) {
    return new Promise(function (res, rej) {
      const id = ++msgId;
      pending.set(id, { resolve: res, reject: rej });
      socket.send(JSON.stringify({ id: id, method: method, params: params || {} }));
    });
  };
}

function attachListeners(socket) {
  socket.addEventListener('message', async function (ev) {
    const msg = JSON.parse(ev.data);
    if (msg.method === 'Network.requestWillBeSent') {
      report.requests.push(msg.params.request.method + ' ' + msg.params.request.url);
    }
    if (msg.method === 'Network.responseReceived' && msg.params.response.status >= 400) {
      report.httpFails.push(msg.params.response.status + ' ' + msg.params.response.url);
    }
    if (msg.method === 'Runtime.exceptionThrown') {
      const d = msg.params.exceptionDetails;
      report.pageErrors.push((d.text + ' ' + ((d.exception && d.exception.description) || '')).slice(0, 400));
    }
    if (msg.method === 'Fetch.requestPaused') {
      const requestId = msg.params.requestId;
      const url = String(msg.params.request.url);
      let hit = false;
      if (interceptMode === 'submissions') hit = url.indexOf('/submissions') >= 0;
      if (interceptMode === 'turns') hit = url.indexOf('/api/turns') >= 0;
      try {
        if (hit) {
          const body = Buffer.from(JSON.stringify({ detail: INTERCEPT_DETAIL[interceptMode] }), 'utf8').toString('base64');
          await send('Fetch.fulfillRequest', {
            requestId: requestId,
            responseCode: 500,
            responseHeaders: [{ name: 'Content-Type', value: 'application/json' }],
            body: body,
          });
        } else {
          await send('Fetch.continueRequest', { requestId: requestId });
        }
      } catch (err) {
        report.notes.push('拦截请求失败：' + String(err));
      }
    }
  });
}

function launchEdge() {
  edge = spawn(
    EDGE,
    [
      '--headless=new',
      '--remote-debugging-port=' + PORT,
      '--user-data-dir=' + PROFILE,
      '--no-first-run',
      '--no-default-browser-check',
      '--hide-scrollbars',
      '--use-gl=angle',
      '--use-angle=swiftshader',
      '--enable-unsafe-swiftshader',
      '--window-size=1440,900',
      'about:blank',
    ],
    { stdio: 'ignore' },
  );
}

async function waitForDevtools() {
  for (let i = 0; i < 240; i++) {
    try {
      const r = await fetch('http://127.0.0.1:' + PORT + '/json/version');
      if (r.ok) return;
    } catch (err) {
      // 还没起来，继续等
    }
    await sleep(250);
  }
  throw new Error('edge devtools not reachable（端口 ' + PORT + '）');
}

async function connectTab(url) {
  const created = await fetch('http://127.0.0.1:' + PORT + '/json/new?' + encodeURIComponent(url), { method: 'PUT' });
  const tab = await created.json();
  const socket = new WebSocket(tab.webSocketDebuggerUrl);
  await new Promise(function (res, rej) {
    socket.addEventListener('open', res);
    socket.addEventListener('error', rej);
  });
  ws = socket;
  send = makeClient(socket);
  attachListeners(socket);
  await send('Runtime.enable');
  await send('Page.enable');
  await send('Network.enable');
  await send('Fetch.enable', { patterns: [{ urlPattern: '*', requestStage: 'Request' }] });
}

async function bootBrowser(url) {
  launchEdge();
  await waitForDevtools();
  await connectTab(url);
}

/** 关闭浏览器进程，再用同一个持久化 profile 重开：这才是「关闭重开」 */
async function closeAndReopen(url) {
  if (ws) { try { ws.close(); } catch (err) { /* ignore */ } }
  if (edge) {
    edge.kill();
    for (let i = 0; i < 40 && edge.exitCode === null; i++) await sleep(100);
  }
  report.notes.push('已关闭浏览器进程并用同一个 profile 重开（closeAndReopen）');
  launchEdge();
  await waitForDevtools();
  await connectTab(url);
}

async function evaluate(js) {
  const r = await send('Runtime.evaluate', { expression: js, awaitPromise: true, returnByValue: true });
  if (r.exceptionDetails) {
    throw new Error(r.exceptionDetails.text + ' ' + ((r.exceptionDetails.exception && r.exceptionDetails.exception.description) || ''));
  }
  return r.result ? r.result.value : undefined;
}

async function goto(url, ms) {
  await send('Page.navigate', { url: url });
  await sleep(ms || 5000);
}

async function reload(ms) {
  await send('Page.reload', { ignoreCache: false });
  await sleep(ms || 5000);
}

async function viewport(width, height) {
  await send('Emulation.setDeviceMetricsOverride', { width: width, height: height, deviceScaleFactor: 1, mobile: false });
  await sleep(400);
}

async function shot(name) {
  await sleep(200);
  const r = await send('Page.captureScreenshot', { format: 'png' });
  writeFileSync(join(OUT_DIR, name + '.png'), Buffer.from(r.data, 'base64'));
}

async function waitFor(selector, timeoutMs) {
  const sel = JSON.stringify(selector);
  const started = Date.now();
  while (Date.now() - started < (timeoutMs || 8000)) {
    const found = await evaluate('!!document.querySelector(' + sel + ')');
    if (found) return true;
    await sleep(150);
  }
  return false;
}

async function valueOf(selector) {
  const sel = JSON.stringify(selector);
  return evaluate('(function(){ var el = document.querySelector(' + sel + '); return el ? el.value : null; })()');
}

async function textOf(selector) {
  const sel = JSON.stringify(selector);
  return evaluate('(function(){ var el = document.querySelector(' + sel + '); return el ? el.textContent.trim() : null; })()');
}

/** 真实鼠标点击（取元素中心，用 CDP 发鼠标事件；命中不到时才退回 DOM 点击并记录下来） */
async function clickSelector(selector, allowFallback) {
  const sel = JSON.stringify(selector);
  const raw = await evaluate(
    '(function(){ var el = document.querySelector(' + sel + '); if (!el) return null;' +
    ' el.scrollIntoView({ block: "center", inline: "center" });' +
    ' var r = el.getBoundingClientRect(); if (r.width <= 0 || r.height <= 0) return { empty: true };' +
    ' var x = Math.round(r.left + r.width / 2); var y = Math.round(r.top + r.height / 2);' +
    ' var at = document.elementFromPoint(x, y);' +
    ' var im = at && at.closest ? at.closest("[data-im]") : null;' +
    ' return { x: x, y: y, hits: !!(at && (at === el || el.contains(at))), atIm: im ? im.getAttribute("data-im") : (at ? at.tagName : null) }; })()',
  );
  if (!raw || raw.empty) return { clicked: false, reason: '元素不可见或不存在' };
  await send('Input.dispatchMouseEvent', { type: 'mouseMoved', x: raw.x, y: raw.y, button: 'none' });
  await send('Input.dispatchMouseEvent', { type: 'mousePressed', x: raw.x, y: raw.y, button: 'left', clickCount: 1 });
  await send('Input.dispatchMouseEvent', { type: 'mouseReleased', x: raw.x, y: raw.y, button: 'left', clickCount: 1 });
  await sleep(350);
  if (!raw.hits && allowFallback !== false) {
    await evaluate('(function(){ var el = document.querySelector(' + sel + '); if (el) el.click(); return true; })()');
    await sleep(300);
    return { clicked: true, fallbackToDom: true, atIm: raw.atIm };
  }
  return { clicked: true, hits: raw.hits, atIm: raw.atIm };
}

/** 按可见文字找按钮并真实点击 */
async function clickByText(pattern) {
  const raw = await evaluate(
    '(function(){ var re = new RegExp(' + JSON.stringify(pattern) + ');' +
    ' var buttons = [].slice.call(document.querySelectorAll("button")); var el = null;' +
    ' for (var i = 0; i < buttons.length; i++) { var name = (buttons[i].textContent || "") + " " + (buttons[i].getAttribute("aria-label") || "");' +
    ' if (re.test(name)) { el = buttons[i]; break; } }' +
    ' if (!el) return null; var r = el.getBoundingClientRect();' +
    ' return { x: Math.round(r.left + r.width / 2), y: Math.round(r.top + r.height / 2), text: (el.textContent || "").trim() }; })()',
  );
  if (!raw) return { clicked: false };
  await send('Input.dispatchMouseEvent', { type: 'mouseMoved', x: raw.x, y: raw.y, button: 'none' });
  await send('Input.dispatchMouseEvent', { type: 'mousePressed', x: raw.x, y: raw.y, button: 'left', clickCount: 1 });
  await send('Input.dispatchMouseEvent', { type: 'mouseReleased', x: raw.x, y: raw.y, button: 'left', clickCount: 1 });
  await sleep(350);
  return { clicked: true, text: raw.text };
}

/** 真实输入：聚焦 → 全选 → 插入文本（浏览器发出的 input 事件，Vue 的 v-model 能收到） */
async function typeInto(selector, text) {
  const sel = JSON.stringify(selector);
  const focused = await evaluate(
    '(function(){ var el = document.querySelector(' + sel + '); if (!el) return false; el.focus(); if (el.select) el.select(); return document.activeElement === el; })()',
  );
  if (!focused) return { typed: false };
  await send('Input.insertText', { text: text });
  await sleep(150);
  return { typed: true, value: await valueOf(selector) };
}

async function lastCardId() {
  return evaluate(
    '(function(){ var cards = [].slice.call(document.querySelectorAll("[data-im=card]")); var el = cards[cards.length - 1]; return el ? el.getAttribute("data-card-id") : null; })()',
  );
}

async function selectAndEditCard(cardId) {
  await clickSelector('[data-im=card][data-card-id="' + cardId + '"]');
  await sleep(250);
  const hasEdit = await evaluate('!!document.querySelector("[data-im=card-edit]")');
  if (hasEdit) await clickSelector('[data-im=card-edit]');
  return waitFor('[data-im=card-editor]', 4000);
}

async function draftRecordsInLocalStorage() {
  return evaluate(
    '(function(){ try { return Object.keys(localStorage).filter(function(k){ return k.indexOf("qio.draft.") === 0; }).map(function(k){ return k + "=" + String(localStorage.getItem(k)).slice(0, 90); }); } catch (e) { return ["存储不可读：" + String(e)]; } })()',
  );
}

async function readDraftsFromServer() {
  return evaluate(
    '(function(){ return fetch(' + JSON.stringify(BACKEND) + ' + "/api/interactive/drafts/board_default").then(function(r){ return r.text().then(function(t){ return r.status + " " + t.slice(0, 300); }); }).catch(function(e){ return "读取失败：" + String(e); }); })()',
  );
}

const MEASURE_SWITCH_JS =
  '(function(){ var rect = function(sel){ var el = document.querySelector(sel); if (!el) return null; var r = el.getBoundingClientRect();' +
  ' if (r.width <= 0 || r.height <= 0) return null;' +
  ' return { left: Math.round(r.left), top: Math.round(r.top), right: Math.round(r.right), bottom: Math.round(r.bottom), width: Math.round(r.width), height: Math.round(r.height) }; };' +
  ' var area = function(a, b){ if (!a || !b) return 0; var w = Math.min(a.right, b.right) - Math.max(a.left, b.left); var h = Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top); return (w > 0 && h > 0) ? Math.round(w * h) : 0; };' +
  ' var hit = function(sel){ var el = document.querySelector(sel); if (!el) return { found: false }; var r = el.getBoundingClientRect();' +
  ' if (r.width <= 0 || r.height <= 0) return { found: true, visible: false };' +
  ' var at = document.elementFromPoint(Math.round(r.left + r.width / 2), Math.round(r.top + r.height / 2));' +
  ' var im = at && at.closest ? at.closest("[data-im]") : null;' +
  ' return { found: true, visible: true, hitSelf: !!(at && (at === el || el.contains(at))), hitIm: im ? im.getAttribute("data-im") : (at ? at.tagName : null) }; };' +
  ' var bar = document.querySelector("[data-im=overlay-switch]"); var cs = bar ? getComputedStyle(bar) : null;' +
  ' var stage = document.querySelector(".im-stage"); var chat = rect("[data-im=chat-panel]"); var barRect = rect("[data-im=overlay-switch]");' +
  ' return JSON.stringify({ viewport: { width: window.innerWidth, height: window.innerHeight },' +
  ' geoMode: stage ? stage.getAttribute("im-geo-mode") : null, switchBar: barRect, chatPanel: chat,' +
  ' chatToggle: rect("[data-im=chat-toggle]"), toolbar: rect("[data-im=board-toolbar]"),' +
  ' intersectionChatSwitchBar: area(chat, barRect), chatBottomVsBarTop: (chat && barRect) ? chat.bottom - barRect.top : null,' +
  ' barComputed: cs ? { position: cs.position, left: cs.left, bottom: cs.bottom, height: cs.height, display: cs.display, visibility: cs.visibility, zIndex: cs.zIndex } : null,' +
  ' barStyleAttr: bar ? bar.getAttribute("style") : null,' +
  ' hits: { bar: hit("[data-im=overlay-switch]"), barChatBtn: hit("[data-im=overlay-switch-chat]"), chatInput: hit("[data-im=chat-input]"), chatSend: hit("[data-im=chat-send]"), submit: hit("[data-im=submit]") },' +
  ' panes: { chatPanel: !!document.querySelector("[data-im=chat-panel]"), batchList: !!document.querySelector("[data-im=batch-list]") } }); })()';

async function measureSwitch() {
  return JSON.parse(await evaluate(MEASURE_SWITCH_JS));
}

// =====================================================================
// 主流程
// =====================================================================
await bootBrowser('about:blank');
await goto(APP + '/#/interactive', 6500);
const booted = await waitFor('[data-im=board-toolbar]', 12000);
step('启动：互动板加载出来', booted, { toolbar: await evaluate('!!document.querySelector("[data-im=board-toolbar]")') });
if (!booted) {
  report.notes.push('互动板没有加载出来：先确认后端 8954 与前端 5454 都在跑');
  writeFileSync(join(OUT_DIR, 'd5-' + LABEL + '-probe.json'), JSON.stringify(report, null, 2));
  if (edge) edge.kill();
  process.exit(1);
}
await sleep(800);

// ---------- 场景 1：首次编辑 → 防抖前刷新 ----------
const NEW_TEXT = '刷新前刚写的新文字-D5-' + Date.now();
let scenarioOneCardId = null;
if (want(1)) {
  const addMenuClick = await clickSelector('[data-im=add-menu]');
  const menuOpened = await waitFor('[data-im=add-menu-list]', 4000);
  const addTextClick = await clickSelector('[data-im=add-text]');
  await sleep(1400); // 等板面自己保存（450ms 防抖 + 请求）
  scenarioOneCardId = await lastCardId();
  report.notes.push('场景 1 前置：' + JSON.stringify({ addMenuClick: addMenuClick, menuOpened: menuOpened, addTextClick: addTextClick }));
  const selectResult = await clickSelector('[data-im=card][data-card-id="' + scenarioOneCardId + '"]');
  await sleep(250);
  if (await evaluate('!!document.querySelector("[data-im=card-edit]")')) await clickSelector('[data-im=card-edit]');
  const editorReady = await waitFor('[data-im=card-editor]', 4000);
  const typing = editorReady ? await typeInto('[data-im=card-editor]', NEW_TEXT) : { typed: false };
  const localRecords = await draftRecordsInLocalStorage();
  await reload(6000); // 立刻刷新：远小于 600ms 的保存防抖
  await waitFor('[data-im=board-toolbar]', 9000);
  await sleep(700);
  await selectAndEditCard(scenarioOneCardId);
  const afterReload = await valueOf('[data-im=card-editor]');
  await shot('r5-d-' + LABEL + '-01-card-draft-before-debounce');
  const ok = afterReload === NEW_TEXT;
  step('场景 1：防抖前刷新后，刚输入的文字能恢复', ok, {
    cardId: scenarioOneCardId,
    selectResult: selectResult,
    typing: typing,
    localRecordsBeforeReload: localRecords,
    editorValueAfterReload: afterReload,
    expected: NEW_TEXT,
  }, ok ? null : '刷新后编辑器里不是刚输入的文字（只存在于本机的记录没有被发现）');
}

// ---------- 场景 2：服务器已有草稿 → 用户确认编辑（清除草稿）→ 刷新不许复活 ----------
if (want(2)) {
  const OLD_DRAFT = '服务器上的旧草稿-D5-' + Date.now();
  if (await evaluate('!!document.querySelector("[data-im=card-edit]")')) await clickSelector('[data-im=card-edit]');
  await waitFor('[data-im=card-editor]', 4000);
  await typeInto('[data-im=card-editor]', OLD_DRAFT);
  await sleep(1600); // 等草稿防抖写回服务器
  const serverDraftAfterTyping = await readDraftsFromServer();
  const confirmed = await clickByText('完成编辑');
  await sleep(1600); // 等「清除草稿」的同步请求与板面保存
  const serverDraftAfterConfirm = await readDraftsFromServer();
  // 让正式内容与那份草稿分开：撤销这次编辑（撤销只作用于正式内容，不许把早已清除的草稿带回来）
  const undoClick = await clickSelector('[data-im=undo]');
  await sleep(1600);
  await reload(6000);
  await waitFor('[data-im=board-toolbar]', 9000);
  await sleep(700);
  const cardId = (await lastCardId()) || scenarioOneCardId;
  await selectAndEditCard(cardId);
  const afterReload = await valueOf('[data-im=card-editor]');
  await shot('r5-d-' + LABEL + '-02-draft-revive');
  const serverDraftAfterReload = await readDraftsFromServer();
  const key = 'card:' + cardId;
  const draftStillOnServer = String(serverDraftAfterReload || '').indexOf(key) >= 0;
  const ok = afterReload === '' && !draftStillOnServer;
  step('场景 2：清除草稿后刷新，旧草稿不复活', ok, {
    oldDraft: OLD_DRAFT,
    confirmedClick: confirmed,
    undoClick: undoClick,
    editorValueAfterReload: afterReload,
    serverDraftAfterTyping: serverDraftAfterTyping,
    serverDraftAfterConfirm: serverDraftAfterConfirm,
    serverDraftAfterReload: serverDraftAfterReload,
  }, ok ? null : '刷新后旧草稿又回来了（编辑器里冒出 ' + JSON.stringify(afterReload) + '，服务器草稿仍在：' + draftStillOnServer + '）');
}

// ---------- 场景 3：本机写入失败要可见 ----------
if (want(3)) {
  await send('Page.addScriptToEvaluateOnNewDocument', {
    source:
      '(function(){ var proto = window.Storage && window.Storage.prototype; if (!proto) return; var original = proto.setItem;' +
      ' proto.setItem = function (key, value) { if (String(key).indexOf("qio.draft.") === 0) { var err = new Error("本机存储不可用（探针模拟配额/隐私模式）"); err.name = "QuotaExceededError"; throw err; } return original.call(this, key, value); }; })();',
  });
  await reload(6500);
  await waitFor('[data-im=board-toolbar]', 9000);
  await sleep(700);
  const cardId = (await lastCardId()) || scenarioOneCardId;
  await selectAndEditCard(cardId);
  await typeInto('[data-im=card-editor]', '本机存不下的一段编辑-D5');
  // 服务器草稿保存（600ms 防抖）还没发生时就先看一眼：这正是「本机保护失败且服务器尚未保存」的窗口
  const hintImmediately = await textOf('[data-im=card-draft-hint]');
  const editorValue = await valueOf('[data-im=card-editor]');
  await sleep(1200);
  const hintAfterServerSave = await textOf('[data-im=card-draft-hint]');
  const cardArea = await textOf('[data-im=card]');
  await shot('r5-d-' + LABEL + '-03-local-write-failure');
  const localFailureWords = /失败|没有保存|不可用|无法|存不下|不能恢复|会丢|本机|本地/;
  const explained = localFailureWords.test(String(hintImmediately || '')) || /本机|本地|这台|关闭后|重开后|恢复副本/.test(String(hintAfterServerSave || ''));
  step('场景 3：本机写入失败时编辑处有说明', explained, {
    cardDraftHintImmediately: hintImmediately,
    cardDraftHintAfterServerSave: hintAfterServerSave,
    editorValue: editorValue,
    cardAreaText: cardArea,
  }, explained ? null : '界面上一句解释都没有（本机写入失败后只显示「' + hintImmediately + '」）');
}

// ---------- 场景 4/5：对话页发送失败 → 原因与找回入口；刷新后仍在 ----------
if (want(4) || want(5)) {
  report.notes.push('场景 4/5：发送失败用明确标注的请求拦截制造（/api/turns 返回 500）');
  interceptMode = 'turns';
  await goto(APP + '/#/', 7000);
  const composerReady = await waitFor('#composer-input', 12000);
  if (composerReady) {
    const FAILED_TEXT = '发送失败的原稿-D5-' + Date.now();
    await typeInto('#composer-input', FAILED_TEXT);
    await clickSelector('.send-btn');
    await sleep(1800);
    const composerText = await textOf('.composer');
    const inputValue = await valueOf('#composer-input');
    const buttons = await evaluate('[].slice.call(document.querySelectorAll("button")).map(function(b){ return ((b.textContent || "").trim() + "|" + (b.getAttribute("aria-label") || "")); }).filter(function(n){ return /放回|找回|取回|恢复|互换|原文/.test(n); })');
    await shot('r5-d-' + LABEL + '-04-composer-failure');
    const reasonVisible = /模拟的发送失败原因-D5/.test(String(composerText || ''));
    step('场景 4：对话页失败后默认可见本次原因', reasonVisible, {
      composerText: composerText,
      inputValue: inputValue,
      recoveryButtons: buttons,
    }, reasonVisible ? null : '对话页没有显示本次发送失败的真实原因');
    const hasEntry = Array.isArray(buttons) && buttons.length > 0;
    step('场景 4：对话页有取回失败原文的入口', hasEntry, { recoveryButtons: buttons }, hasEntry ? null : '对话页没有任何取回失败原文的入口');

    if (want(5)) {
      await reload(7000);
      await waitFor('#composer-input', 9000);
      await sleep(800);
      const afterReloadText = await textOf('.composer');
      const afterReloadValue = await valueOf('#composer-input');
      await shot('r5-d-' + LABEL + '-05-composer-after-reload');
      const stillKnows = /模拟的发送失败原因-D5|发送失败|没有发出/.test(String(afterReloadText || ''));
      step('场景 5：刷新后仍能看到这次失败并找回原文', stillKnows && afterReloadValue === FAILED_TEXT, {
        composerText: afterReloadText,
        inputValue: afterReloadValue,
        expectedText: FAILED_TEXT,
      }, stillKnows ? null : '刷新后失败事实消失（失败原文只在内存里，原因也看不到了）');
    }
  } else {
    step('场景 4/5：对话页输入区可用', false, null, '找不到 #composer-input：对话页没有加载出来');
  }
  interceptMode = null;
}

// ---------- 场景 7：提交失败原因默认可见 ----------
if (want(7)) {
  await goto(APP + '/#/interactive', 6500);
  await waitFor('[data-im=board-toolbar]', 10000);
  await sleep(900);
  interceptMode = 'submissions';
  await clickSelector('[data-im=submit]');
  await sleep(2600);
  const failureText = await textOf('[data-im=submit-failure]');
  const detailsOpen = await evaluate('!!document.querySelector("[data-im=submit-details-box]")');
  const statusText = await textOf('[data-im=submit-status]');
  await shot('r5-d-' + LABEL + '-07-submit-failure');
  const showsReason = /模拟的提交失败原因-D5/.test(String(failureText || ''));
  step('场景 7：提交失败时默认区域显示本次真实原因', showsReason, {
    submitFailureText: failureText,
    submitStatusText: statusText,
    detailsOpen: detailsOpen,
  }, showsReason ? null : '默认失败区看不到本次真实原因（只显示通用保留说明）');
  interceptMode = null;
}

// ---------- 场景 8：480px 空间不足时的切换条（定位、切换、关闭一个面板） ----------
if (want(8)) {
  await goto(APP + '/#/interactive', 6500);
  await waitFor('[data-im=board-toolbar]', 10000);
  await viewport(480, 600);
  await clickSelector('[data-im=demo-entry]');
  await sleep(400);
  await clickSelector('[data-im=demo-create]');
  await sleep(2800);
  await evaluate('(function(){ var e = document.querySelector("[data-im=demo-entry]"); if (e && document.querySelector("[data-im=demo-popover]")) e.click(); return true; })()');
  await sleep(400);
  // 用户先开聊天，再开批量列表：空间不足时只留最近打开的那个（批量列表）
  if (await evaluate('!!document.querySelector("[data-im=chat-toggle]")')) await clickSelector('[data-im=chat-toggle]');
  await sleep(600);
  if (await evaluate('!!document.querySelector("[data-im=batch-entry]")')) await clickSelector('[data-im=batch-entry]');
  await sleep(1000);
  const cramped = await measureSwitch();
  await shot('r5-d-' + LABEL + '-08a-switch-bar-cramped');
  const barPositioned = !!cramped.switchBar && !!cramped.barComputed && cramped.barComputed.position !== 'static';
  step('场景 8：空间不足时切换条有真实定位（不是无效的 left/bottom）', barPositioned, {
    switchBar: cramped.switchBar,
    barComputed: cramped.barComputed,
    barStyleAttr: cramped.barStyleAttr,
    geoMode: cramped.geoMode,
    panes: cramped.panes,
  }, barPositioned ? null : '切换条的 computed position 是 ' + (cramped.barComputed ? cramped.barComputed.position : 'null') + '：left/bottom 完全不生效');

  // 用户点切换条上的「看对话」：换成聊天面板
  const switched = await clickSelector('[data-im=overlay-switch-chat]');
  await sleep(900);
  const chatState = await measureSwitch();
  await shot('r5-d-' + LABEL + '-08b-switch-bar-chat');
  const chatOverlap = chatState.intersectionChatSwitchBar || 0;
  const chatOk = !!chatState.switchBar && !!chatState.chatPanel && chatOverlap === 0;
  step('场景 8：切到聊天后，切换条与聊天面板不重叠', chatOk, {
    switched: switched,
    intersectionChatSwitchBar: chatState.intersectionChatSwitchBar,
    chatBottomVsBarTop: chatState.chatBottomVsBarTop,
    switchBar: chatState.switchBar,
    chatPanel: chatState.chatPanel,
    barComputed: chatState.barComputed,
    hits: chatState.hits,
    panes: chatState.panes,
  }, chatOk ? null : '切换条与聊天面板相交 ' + chatOverlap + 'px²（或切换条/面板不存在）');

  // 用户关掉当前显示的面板：空间依然不足，切换条要留在那里让人切回去
  const closed = await evaluate('(function(){ var b = document.querySelector("[data-im=chat-panel] .panel-close") || document.querySelector("[data-im=batch-close]"); if (!b) return false; b.click(); return true; })()');
  await sleep(1000);
  const afterClose = await measureSwitch();
  await shot('r5-d-' + LABEL + '-08c-after-close-panel');
  const barRemains = !!afterClose.switchBar && !!(afterClose.hits && afterClose.hits.bar && afterClose.hits.bar.visible);
  step('场景 8：关掉一个面板后切换条仍在（还能切回去）', barRemains, {
    closeClicked: closed,
    switchBar: afterClose.switchBar,
    barComputed: afterClose.barComputed,
    barStyleAttr: afterClose.barStyleAttr,
    geoMode: afterClose.geoMode,
    panes: afterClose.panes,
    hits: afterClose.hits,
  }, barRemains ? null : '关掉面板后切换条整个消失了：用户没有明确的切换入口');
}

// ---------- 关闭重开（同一 profile）：可选用 --reopen ----------
if (flag('reopen')) {
  report.notes.push('关闭重开验证：同一个 profile、新的浏览器进程');
  await closeAndReopen(APP + '/#/interactive');
  await waitFor('[data-im=board-toolbar]', 12000);
  await sleep(900);
  step('关闭重开：应用重新起得来', true, {
    chatInput: await valueOf('[data-im=chat-input]'),
    composerInput: await valueOf('#composer-input'),
  });
}

report.finishedAt = new Date().toISOString();
const outFile = join(OUT_DIR, 'd5-' + LABEL + '-probe.json');
writeFileSync(outFile, JSON.stringify(report, null, 2));
console.log('');
console.log('结果写入：' + outFile);
const failedSteps = report.steps.filter(function (s) { return !s.ok; }).length;
console.log('步骤：' + report.steps.length + '，失败：' + failedSteps);
if (ws) { try { ws.close(); } catch (err) { /* ignore */ } }
if (edge) edge.kill();
process.exit(failedSteps > 0 ? 1 : 0);
