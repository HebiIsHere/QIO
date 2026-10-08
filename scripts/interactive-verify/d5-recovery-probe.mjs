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
/** 页面自己的来源：拦截应答必须带上它，否则跨源请求会被浏览器按 CORS 拦掉、应用只能看到 "Failed to fetch" */
const APP_ORIGIN = (function () {
  try {
    return new URL(APP).origin;
  } catch (err) {
    return '*';
  }
})();
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
  // 故意写长：默认失败区必须能完整换行显示长原因，视觉矩阵也要看这一条
  submissions:
    '模拟的提交失败原因-D5（探针拦截）：服务端在处理这次提交时返回了 500，' +
    '诊断信息：上游连接被中途关闭，重试前请先确认板面已经保存。'.repeat(2),
  turns:
    '模拟的发送失败原因-D5（探针拦截）：请求没有到达服务端，' +
    '诊断信息：连接被重置，这段文字还在这台机器上。'.repeat(2),
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
      /**
       * 驱动修正（D2）：跨源 POST 带 JSON 会先发 OPTIONS 预检。上一版把预检也一起拦成 500，
       * 浏览器按 CORS 失败处理，应用只能看到 "Failed to fetch"，注入的 500 详情永远到不了界面。
       * 预检放给真实后端，只拦真正的请求。
       */
      if (hit && url.indexOf('/api/turns') >= 0 && msg.params.request.method === 'OPTIONS') hit = false;
      if (hit && url.indexOf('/submissions') >= 0 && msg.params.request.method === 'OPTIONS') hit = false;
      try {
        if (hit) {
          const body = Buffer.from(JSON.stringify({ detail: INTERCEPT_DETAIL[interceptMode] }), 'utf8').toString('base64');
          /**
           * 带 CORS 头：应用在 5471、接口在 8971，是跨源请求。只给 500 不给 Access-Control-*
           * 的话浏览器会在网络层拦掉，应用只能看到 "Failed to fetch"，
           * 看不到我们注入的原因 —— 那是探针自己的缺陷，不是产品少显示了原因。
           */
          await send('Fetch.fulfillRequest', {
            requestId: requestId,
            responseCode: 500,
            responseHeaders: [
              { name: 'Content-Type', value: 'application/json' },
              { name: 'Access-Control-Allow-Origin', value: APP_ORIGIN },
              { name: 'Access-Control-Allow-Credentials', value: 'true' },
              { name: 'Access-Control-Allow-Headers', value: '*' },
              { name: 'Access-Control-Allow-Methods', value: 'GET,POST,PUT,PATCH,DELETE,OPTIONS' },
            ],
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

/**
 * 找「交互入口」时不能只认 <button>：恢复入口可能是 role=button 的链接式元素。
 * 这里同时记录标签、可见性、能否被自己命中（hitSelf），避免把「存在」当成「可点」。
 */
async function interactiveEntries(pattern) {
  return evaluate(
    '(function(){ var re = new RegExp(' + JSON.stringify(pattern) + ');' +
    ' var nodes = [].slice.call(document.querySelectorAll("button, a, [role=button], [tabindex]"));' +
    ' return nodes.map(function(el){ var name = ((el.textContent || "") + " " + (el.getAttribute("aria-label") || "") + " " + (el.getAttribute("title") || "")).replace(/\\s+/g, " ").trim();' +
    ' if (!re.test(name)) return null; var r = el.getBoundingClientRect();' +
    ' var visible = r.width > 0 && r.height > 0; var hitSelf = false;' +
    ' if (visible) { var at = document.elementFromPoint(Math.round(r.left + r.width / 2), Math.round(r.top + r.height / 2)); hitSelf = !!(at && (at === el || el.contains(at))); }' +
    ' return { tag: el.tagName, role: el.getAttribute("role"), name: name.slice(0, 60), visible: visible, hitSelf: hitSelf }; }).filter(Boolean); })()',
  );
}

/** 真实点击一个「按文本找到的交互入口」；命中不到时退回 DOM 点击并如实记录 */
async function clickEntryByText(pattern) {
  const finder =
    '(function(){ var re = new RegExp(' + JSON.stringify(pattern) + ');' +
    ' var nodes = [].slice.call(document.querySelectorAll("button, a, [role=button], [tabindex]"));' +
    ' for (var i = 0; i < nodes.length; i++) { var name = ((nodes[i].textContent || "") + " " + (nodes[i].getAttribute("aria-label") || "")).replace(/\\s+/g, " ").trim(); if (re.test(name)) return nodes[i]; } return null; })()';
  const raw = await evaluate(
    '(function(){ var re = new RegExp(' + JSON.stringify(pattern) + ');' +
    ' var nodes = [].slice.call(document.querySelectorAll("button, a, [role=button], [tabindex]"));' +
    ' var el = null; for (var i = 0; i < nodes.length; i++) { var name = ((nodes[i].textContent || "") + " " + (nodes[i].getAttribute("aria-label") || "")).replace(/\\s+/g, " ").trim(); if (re.test(name)) { el = nodes[i]; break; } }' +
    ' if (!el) return null; el.scrollIntoView({ block: "center", inline: "center" }); var r = el.getBoundingClientRect();' +
    ' var x = Math.round(r.left + r.width / 2); var y = Math.round(r.top + r.height / 2);' +
    ' var at = document.elementFromPoint(x, y);' +
    ' return { x: x, y: y, text: (el.textContent || "").replace(/\\s+/g, " ").trim().slice(0, 40),' +
    ' hitSelf: !!(at && (at === el || el.contains(at))), at: at ? at.tagName : null }; })()',
  );
  if (!raw) return { clicked: false, reason: "找不到这个入口" };
  await send('Input.dispatchMouseEvent', { type: 'mouseMoved', x: raw.x, y: raw.y, button: 'none' });
  await send('Input.dispatchMouseEvent', { type: 'mousePressed', x: raw.x, y: raw.y, button: 'left', clickCount: 1 });
  await send('Input.dispatchMouseEvent', { type: 'mouseReleased', x: raw.x, y: raw.y, button: 'left', clickCount: 1 });
  await sleep(350);
  if (!raw.hitSelf) {
    await evaluate('(function(){ var el = ' + finder + '; if (!el) return false; el.click(); return true; })()');
    await sleep(300);
    return { clicked: true, fellBackToDom: true, hitSelf: false, at: raw.at, text: raw.text };
  }
  return { clicked: true, hitSelf: true, text: raw.text };
}

/** 点某张卡片自己的「完成编辑」：按卡片作用域找按钮 + 命中测试，鼠标点不到才退回 DOM 点击 */
async function clickCardConfirm(cardId, label) {
  const sel = JSON.stringify('[data-im=card][data-card-id="' + cardId + '"]');
  const raw = await evaluate(
    '(function(){ var re = new RegExp(' + JSON.stringify(label) + '); var card = document.querySelector(' + sel + ');' +
    ' if (!card) return null; var buttons = [].slice.call(card.querySelectorAll("button")); var el = null;' +
    ' for (var i = 0; i < buttons.length; i++) { if (re.test((buttons[i].textContent || "").trim())) { el = buttons[i]; break; } }' +
    ' if (!el) return null; el.scrollIntoView({ block: "center", inline: "center" }); var r = el.getBoundingClientRect();' +
    ' var x = Math.round(r.left + r.width / 2); var y = Math.round(r.top + r.height / 2); var at = document.elementFromPoint(x, y);' +
    ' return { x: x, y: y, text: (el.textContent || "").trim(), hitSelf: !!(at && (at === el || el.contains(at))), at: at ? at.tagName : null }; })()',
  );
  if (!raw) return { clicked: false, reason: "这张卡片里找不到「" + label + "」按钮" };
  await send('Input.dispatchMouseEvent', { type: 'mouseMoved', x: raw.x, y: raw.y, button: 'none' });
  await send('Input.dispatchMouseEvent', { type: 'mousePressed', x: raw.x, y: raw.y, button: 'left', clickCount: 1 });
  await send('Input.dispatchMouseEvent', { type: 'mouseReleased', x: raw.x, y: raw.y, button: 'left', clickCount: 1 });
  await sleep(350);
  if (!raw.hitSelf) {
    await evaluate('(function(){ var re = new RegExp(' + JSON.stringify(label) + '); var card = document.querySelector(' + sel + ');' +
      ' if (!card) return false; var buttons = [].slice.call(card.querySelectorAll("button"));' +
      ' for (var i = 0; i < buttons.length; i++) { if (re.test((buttons[i].textContent || "").trim())) { buttons[i].click(); return true; } } return false; })()');
    await sleep(300);
    return { clicked: true, fellBackToDom: true, hitSelf: false, at: raw.at, text: raw.text };
  }
  return { clicked: true, hitSelf: true, text: raw.text };
}

/** 服务端草稿集合的真实键值（用来区分「旧草稿复活」与「只是多了一条空记录」） */
async function readDraftsMap() {
  const raw = await evaluate(
    '(function(){ return fetch(' + JSON.stringify(BACKEND) + ' + "/api/interactive/drafts/board_default").then(function(r){ return r.text(); }).catch(function(e){ return "读取失败：" + String(e); }); })()',
  );
  try {
    const parsed = JSON.parse(String(raw));
    return parsed && parsed.drafts ? parsed.drafts : {};
  } catch (err) {
    return {};
  }
}

/** 轮询服务端草稿集合里某个键的状态，代替「固定等 1.6 秒」（防抖 + 请求慢时固定等待会假失败） */
async function waitServerDraft(key, shouldExist, timeoutMs) {
  const started = Date.now();
  const needle = '"' + key + '"';
  for (;;) {
    const text = String((await readDraftsFromServer()) || "");
    const has = text.indexOf(needle) >= 0;
    if (has === shouldExist) return { ok: true, has: has, ms: Date.now() - started };
    if (Date.now() - started > (timeoutMs || 6000)) return { ok: false, has: has, ms: Date.now() - started };
    await sleep(200);
  }
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
  ' var batchRect = rect("[data-im=batch-list]");' +
  ' var chatInputRect = rect("[data-im=chat-input]"); var chatSendRect = rect("[data-im=chat-send]");' +
  ' var chatToggleRect = rect("[data-im=chat-toggle]"); var batchEntryRect = rect("[data-im=batch-entry]");' +
  ' var stage = document.querySelector(".im-stage"); var chat = rect("[data-im=chat-panel]"); var barRect = rect("[data-im=overlay-switch]");' +
  ' return JSON.stringify({ viewport: { width: window.innerWidth, height: window.innerHeight },' +
  ' geoMode: stage ? stage.getAttribute("im-geo-mode") : null, switchBar: barRect, chatPanel: chat, batchPanel: batchRect,' +
  ' intersectionBatchSwitchBar: area(batchRect, barRect), batchBottomVsBarTop: (batchRect && barRect) ? batchRect.bottom - barRect.top : null,' +
  ' chatInputRect: chatInputRect, chatSendRect: chatSendRect, chatToggleRect: chatToggleRect, batchEntryRect: batchEntryRect,' +
  ' chatToggle: rect("[data-im=chat-toggle]"), toolbar: rect("[data-im=board-toolbar]"),' +
  ' intersectionChatSwitchBar: area(chat, barRect), chatBottomVsBarTop: (chat && barRect) ? chat.bottom - barRect.top : null,' +
  ' barComputed: cs ? { position: cs.position, left: cs.left, bottom: cs.bottom, height: cs.height, display: cs.display, visibility: cs.visibility, zIndex: cs.zIndex } : null,' +
  ' barStyleAttr: bar ? bar.getAttribute("style") : null,' +
  ' hits: { bar: hit("[data-im=overlay-switch]"), barChatBtn: hit("[data-im=overlay-switch-chat]"), chatInput: hit("[data-im=chat-input]"), chatSend: hit("[data-im=chat-send]"), chatToggle: hit("[data-im=chat-toggle]"), batchEntry: hit("[data-im=batch-entry]"), submit: hit("[data-im=submit]") },' +
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
  /**
   * 驱动修正（D2）：上一版按「最后一张卡片」定位，并且用不带命中测试的按文案点击 ——
   * 编辑器里那张卡片未必是最后一张，按钮被别的浮层挡住时鼠标事件会落到别的元素上，
   * 结果「清除没发生」被误报成产品缺陷。这里改成：按**编辑器所属卡片**定位、
   * 点它自己的「完成编辑」（先命中测试，点不到才退回 DOM 点击），并轮询服务端确认，
   * 每一步都留下证据。断言不变：清除必须同步、刷新后不许复活。
   */
  const editTargetId = await evaluate('(function(){ var ed = document.querySelector("[data-im=card-editor]"); if (!ed) return null; var card = ed.closest("[data-im=card]"); return card ? card.getAttribute("data-card-id") : null; })()');
  const typing2 = await typeInto('[data-im=card-editor]', OLD_DRAFT);
  const key = 'card:' + (editTargetId || '');
  const savedOnServer = await waitServerDraft(key, true, 6000);
  const serverDraftAfterTyping = await readDraftsFromServer();
  const confirmed = await clickCardConfirm(editTargetId, '完成编辑');
  const clearedOnServer = await waitServerDraft(key, false, 6000);
  const serverDraftAfterConfirm = await readDraftsFromServer();
  // 让正式内容与那份草稿分开：撤销这次编辑（撤销只作用于正式内容，不许把早已清除的草稿带回来）
  const undoClick = await clickSelector('[data-im=undo]');
  await sleep(1200);
  const serverDraftAfterUndo = await readDraftsFromServer();
  await reload(6000);
  await waitFor('[data-im=board-toolbar]', 9000);
  await sleep(700);
  const cardId = editTargetId || (await lastCardId()) || scenarioOneCardId;
  await selectAndEditCard(cardId);
  const afterReload = await valueOf('[data-im=card-editor]');
  await shot('r5-d-' + LABEL + '-02-draft-revive');
  const serverDraftAfterReload = await readDraftsFromServer();
  const draftsAfterReload = await readDraftsMap();
  const serverValueAfterReload = Object.prototype.hasOwnProperty.call(draftsAfterReload, 'card:' + cardId)
    ? draftsAfterReload['card:' + cardId]
    : null;
  /**
   * 驱动修正（D2）：只看「键还在不在」会把**空的**记录也判成「旧草稿复活」。
   * 用户确认后重新打开一次编辑器，应用会按设计写一条「存在但正文为空」的草稿（§10.4），
   * 如果这时正式正文本来就是空的，服务端就会留下 card:<id>: ""。
   * 那不是旧草稿复活 —— 所以判定必须按**值**：旧文字一个字符都不许回来。
   */
  const oldTextRevived = serverValueAfterReload === OLD_DRAFT;
  const draftStillOnServer = Object.prototype.hasOwnProperty.call(draftsAfterReload, 'card:' + cardId);
  const ok = afterReload === '' && !oldTextRevived;
  step('场景 2：清除草稿后刷新，旧草稿不复活', ok, {
    oldDraft: OLD_DRAFT,
    editTargetId: editTargetId,
    typing: typing2,
    savedOnServer: savedOnServer,
    confirmedClick: confirmed,
    clearedOnServer: clearedOnServer,
    undoClick: undoClick,
    editorValueAfterReload: afterReload,
    serverValueAfterReload: serverValueAfterReload,
    oldTextRevived: oldTextRevived,
    draftKeyExistsAfterReload: draftStillOnServer,
    serverDraftAfterTyping: serverDraftAfterTyping,
    serverDraftAfterConfirm: serverDraftAfterConfirm,
    serverDraftAfterUndo: serverDraftAfterUndo,
    serverDraftAfterReload: serverDraftAfterReload,
  }, ok ? null : '刷新后旧草稿又回来了（编辑器里冒出 ' + JSON.stringify(afterReload) + '，服务器上那份的值：' + JSON.stringify(serverValueAfterReload) + '；清除同步证据：' + JSON.stringify(clearedOnServer) + '）');
}

// ---------- 场景 3：本机写入失败要可见 ----------
if (want(3)) {
  /**
   * 驱动修正（D2）：这条注入会让后续每一次导航里的 qio.draft.* 写入都失败。
   * 上一版没有在场景 3 结束后撤掉它，于是场景 4/5（对话页草稿恢复）是在「本机草稿根本写不进去」
   * 的环境里跑的，刷新后输入框为空被误判成产品问题。这里记下 identifier，场景 3 一结束就撤掉。
   */
  const storageInjection = await send('Page.addScriptToEvaluateOnNewDocument', {
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
  if (storageInjection && storageInjection.identifier) {
    await send('Page.removeScriptToEvaluateOnNewDocument', { identifier: storageInjection.identifier });
    report.notes.push('场景 3 结束：已撤掉「qio.draft.* 写入失败」的注入，后续场景在正常的本机存储上跑');
  }
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
    /**
     * 驱动修正（D2）：原来只找 <button> 且只认「找回/取回」这类词。失败后应用已经把原文放回输入框，
     * 此时「找回原文」按钮按设计不出现、只有「不再保留」—— 那不是产品缺入口。
     * 这里收集恢复区的全部交互入口（含「不再保留」），并记录可见性与能否命中。
     */
    const buttons = await interactiveEntries('放回|找回|取回|恢复|互换|原文|不再保留');
    await shot('r5-d-' + LABEL + '-04-composer-failure');
    // 拦截应答带了 CORS 头，应用能读到注入的 500 详情；真实网络层错误也算「本次请求的真实原因」
    const reasonVisible = /模拟的发送失败原因-D5|Failed to fetch|NetworkError|网络中断/.test(String(composerText || ''));
    const reasonIsSpecific = /失败原因|失败：|原因：/.test(String(composerText || ''));
    step('场景 4：对话页失败后默认可见本次原因', reasonVisible && reasonIsSpecific, {
      composerText: composerText,
      inputValue: inputValue,
      recoveryEntries: buttons,
    }, reasonVisible && reasonIsSpecific ? null : '对话页没有显示这次发送失败的真实原因');
    const usableEntries = (buttons || []).filter(function (e) { return e.visible && e.hitSelf; });
    step('场景 4：对话页失败后有明确的恢复操作入口', usableEntries.length > 0, {
      recoveryEntries: buttons,
      usableEntries: usableEntries,
    }, usableEntries.length > 0 ? null : '对话页没有任何可见、可点的恢复操作入口');

    if (want(5)) {
      await reload(7000);
      await waitFor('#composer-input', 9000);
      await sleep(1400);
      const afterReloadText = await textOf('.composer');
      const afterReloadValue = await valueOf('#composer-input');
      const afterReloadEntries = await interactiveEntries('放回|找回|取回|恢复|互换|原文|不再保留');
      await shot('r5-d-' + LABEL + '-05-composer-after-reload');
      const stillKnows = /模拟的发送失败原因-D5|Failed to fetch|NetworkError|网络中断|没有发出/.test(String(afterReloadText || ''));
      /**
       * 驱动修正（D2）：失败后应用会自动把原文放回输入框；刷新后若草稿恢复成功，输入框里本来就有原文，
       * 此时「找回原文」按钮按设计不出现（只有「不再保留」）。所以这里两种路径都算通过：
       * 原文已经在输入框里，或者点恢复入口能把它取回来。两条都记录下来。
       */
      const alreadyInInput = afterReloadValue === FAILED_TEXT;
      const restoreClick = alreadyInInput ? { clicked: false, reason: '原文已经在输入框里' } : await clickEntryByText('找回原文|取回|放回');
      const restoredValue = await valueOf('#composer-input');
      await shot('r5-d-' + LABEL + '-05b-composer-restored');
      const recovered = alreadyInInput || restoredValue === FAILED_TEXT;
      const ok5 = stillKnows && recovered;
      step('场景 5：刷新后仍能看到这次失败并取回原文', ok5, {
        composerText: afterReloadText,
        inputValueAfterReload: afterReloadValue,
        alreadyInInput: alreadyInInput,
        entriesAfterReload: afterReloadEntries,
        restoreClick: restoreClick,
        restoredValue: restoredValue,
        expectedText: FAILED_TEXT,
      }, ok5
        ? null
        : '刷新后失败事实丢失，或原文取不回来（刷新后输入框是 ' + JSON.stringify(afterReloadValue) + '，恢复动作：' + JSON.stringify(restoreClick) + '）');
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
  // 拦截应答带了 CORS 头，应用能读到注入的 500 详情；真实网络层错误也算「本次请求的真实原因」
  const showsReason = /模拟的提交失败原因-D5|Failed to fetch|NetworkError|网络中断/.test(String(failureText || ''));
  const reasonLinePresent = /失败原因|原因：/.test(String(failureText || ''));
  step('场景 7：提交失败时默认区域显示本次真实原因', showsReason && reasonLinePresent, {
    submitFailureText: failureText,
    submitStatusText: statusText,
    detailsOpen: detailsOpen,
  }, showsReason && reasonLinePresent ? null : '默认失败区看不到本次真实原因（只显示通用保留说明）');
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

  // 只开着批量列表时，切换条同样不许压在它上面（§11.7：面板、切换条都保持在视口内且互不遮挡）
  const batchOverlap = cramped.intersectionBatchSwitchBar || 0;
  const batchOk = !cramped.batchPanel || (batchOverlap === 0 && (cramped.batchBottomVsBarTop ?? -1) <= 0);
  step('场景 8：只开批量列表时，切换条不压在列表上', batchOk, {
    batchPanel: cramped.batchPanel,
    switchBar: cramped.switchBar,
    intersectionBatchSwitchBar: cramped.intersectionBatchSwitchBar,
    batchBottomVsBarTop: cramped.batchBottomVsBarTop,
    panes: cramped.panes,
  }, batchOk ? null : '切换条与批量列表相交 ' + batchOverlap + 'px²（列表底边越过切换条顶边 ' + (cramped.batchBottomVsBarTop ?? 'null') + 'px）');

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
  const chatUsable = !!chatState.hits && !!chatState.hits.chatInput && chatState.hits.chatInput.hitSelf === true && !!chatState.hits.chatSend && chatState.hits.chatSend.hitSelf === true;
  step('场景 8：480px 切到聊天后，输入框与发送按钮真的可点', chatUsable, {
    chatInputRect: chatState.chatInputRect,
    chatSendRect: chatState.chatSendRect,
    chatInputHit: chatState.hits ? chatState.hits.chatInput : null,
    chatSendHit: chatState.hits ? chatState.hits.chatSend : null,
    switchBar: chatState.switchBar,
  }, chatUsable ? null : '480px 下聊天输入框或发送按钮被别的元素盖住（elementFromPoint 命中的不是它自己）');

  const closed = await evaluate('(function(){ var b = document.querySelector("[data-im=chat-panel] .panel-close") || document.querySelector("[data-im=batch-close]"); if (!b) return false; b.click(); return true; })()');
  await sleep(1000);
  const afterClose = await measureSwitch();
  await shot('r5-d-' + LABEL + '-08c-after-close-panel');
  const barRemains = !!afterClose.switchBar && !!(afterClose.hits && afterClose.hits.bar && afterClose.hits.bar.visible);
  /**
   * 驱动修正（D2）：关掉的是**最后一个**打开的面板时，界面上已经没有面板可切换，
   * 契约 §11.7 要求的「关掉一个面板后仍为切换条预留高度」对这种状态没有直接结论。
   * 用户真正需要的是「有一条回去的路」：切换条仍在，或者两个面板入口仍然可见可点。
   * 两条都记下来，任一条成立即算通过 —— 报告里会写清是哪种。
   */
  const entryUsable = (function (hits) {
    if (!hits) return false;
    var list = [hits.chatToggle, hits.batchEntry];
    for (var i = 0; i < list.length; i++) {
      if (list[i] && list[i].found && list[i].visible && list[i].hitSelf) return true;
    }
    return false;
  })(afterClose.hits);
  const hasWayBack = barRemains || entryUsable;
  step('场景 8：关掉一个面板后仍有一条回去的路（切换条或面板入口）', hasWayBack, {
    closeClicked: closed,
    switchBar: afterClose.switchBar,
    barComputed: afterClose.barComputed,
    barStyleAttr: afterClose.barStyleAttr,
    geoMode: afterClose.geoMode,
    panes: afterClose.panes,
    hits: afterClose.hits,

    barRemains: barRemains,
    entryUsable: entryUsable,
    chatToggleRect: afterClose.chatToggleRect,
    batchEntryRect: afterClose.batchEntryRect,
  }, hasWayBack ? null : '关掉面板后切换条与两个面板入口都不可用：用户没有回去的路');
}

// ---------- 场景 9：480px 下聊天面板的输入区必须真的可见可点（§11.7 / §11.8） ----
if (want(9)) {
  report.notes.push(
    '场景 9：先清掉本机里上一轮的失败事实与草稿做对照，再制造一次长原文发送失败，' +
      '用真实矩形 + elementFromPoint 判断聊天面板的输入区有没有被挤出面板（overflow: hidden 会裁剪）',
  );
  await goto(APP + '/#/interactive', 6500);
  await waitFor('[data-im=board-toolbar]', 10000);
  const clearedKeys = await evaluate(
    '(function(){ var keys=[]; for (var i=0;i<localStorage.length;i++){ var k=localStorage.key(i); if (k && (k.indexOf("qio.draft.")===0 || k.indexOf("qio.chat.failedSend")===0)) keys.push(k); } keys.forEach(function(k){ localStorage.removeItem(k); }); return keys.length; })()',
  );
  await viewport(480, 600);
  await goto(APP + '/#/interactive', 6500);
  await waitFor('[data-im=board-toolbar]', 10000);
  await sleep(900);
  if (await evaluate('!!document.querySelector("[data-im=chat-toggle]")')) await clickSelector('[data-im=chat-toggle]');
  await sleep(900);
  const clean = await measureSwitch();
  /**
   * 诊断：输入框的祖先链几何。480px 下输入框的矩形完全落在面板盒子下方（面板 overflow: hidden 会裁掉它），
   * 这里把每一层的矩形/overflow/position 都记下来，用来区分「面板算得太矮」与「输入框根本不在这个面板里」。
   */
  const chain = await evaluate(
    '(function(){ var input = document.querySelector("[data-im=chat-input]"); var panel = document.querySelector("[data-im=chat-panel]");' +
      ' var out = { panels: document.querySelectorAll("[data-im=chat-panel]").length, inputs: document.querySelectorAll("[data-im=chat-input]").length, chain: [] };' +
      ' if (panel) { var pr = panel.getBoundingClientRect(); var pcs = getComputedStyle(panel); out.panel = { top: Math.round(pr.top), bottom: Math.round(pr.bottom), h: Math.round(pr.height), height: pcs.height, maxHeight: pcs.maxHeight, overflow: pcs.overflow, flex: pcs.flex }; }' +
      ' var el = input; while (el && el !== document.body) { var r = el.getBoundingClientRect(); var cs = getComputedStyle(el);' +
      ' out.chain.push({ tag: el.tagName, cls: String(el.className || "").slice(0, 36), top: Math.round(r.top), bottom: Math.round(r.bottom), h: Math.round(r.height), overflow: cs.overflow, position: cs.position, display: cs.display, flex: cs.flex, minH: cs.minHeight, maxH: cs.maxHeight });' +
      ' el = el.parentElement; } return out; })()',
  );
  await shot('r5-d-' + LABEL + '-09a-chat-composer-clean');
  const cleanOk = !!(clean.hits && clean.hits.chatInput && clean.hits.chatInput.hitSelf);
  step('场景 9：480px 干净状态下聊天输入框与发送按钮可点', cleanOk, {
    clearedKeys: clearedKeys,
    composerChain: chain,
    chatPanel: clean.chatPanel,
    chatInputRect: clean.chatInputRect,
    chatSendRect: clean.chatSendRect,
    chatInputHit: clean.hits ? clean.hits.chatInput : null,
    chatSendHit: clean.hits ? clean.hits.chatSend : null,
    switchBar: clean.switchBar,
  }, cleanOk ? null : '干净状态下 480px 的聊天输入框就已经点不到');

  interceptMode = 'turns';
  const LONG = '很长的失败原文-D2-' + '这段文字在发送失败之后必须能被找回，而且不许把输入框和发送按钮挤出面板。'.repeat(6);
  await typeInto('[data-im=chat-input]', LONG);
  await clickSelector('[data-im=chat-send]');
  await sleep(2400);
  const withFailure = await measureSwitch();
  await shot('r5-d-' + LABEL + '-09b-chat-composer-with-failure');
  const failureEntries = await interactiveEntries('放回|找回|取回|恢复|互换|原文|不再保留');
  interceptMode = null;
  const withOk = !!(withFailure.hits && withFailure.hits.chatInput && withFailure.hits.chatInput.hitSelf);
  /**
   * 同一个失败状态换到桌面最小窗口（800×600）再量一次：
   * 用来划清缺陷范围 —— 是「窄到 480px 才发生」还是「只要面板高度不够就发生」。
   */
  await viewport(800, 600);
  await sleep(700);
  const at800 = await measureSwitch();
  await shot('r5-d-' + LABEL + '-09c-chat-composer-with-failure-800x600');
  const ok800 = !!(at800.hits && at800.hits.chatInput && at800.hits.chatInput.hitSelf);
  step('场景 9：同样有长失败原文时，800×600（桌面最小窗口）聊天输入框仍然可点', ok800, {
    chatPanel: at800.chatPanel,
    chatInputRect: at800.chatInputRect,
    chatInputHit: at800.hits ? at800.hits.chatInput : null,
    chatSendHit: at800.hits ? at800.hits.chatSend : null,
  }, ok800 ? null : '800×600 下长失败原文同样把聊天输入框挤出面板');
  await viewport(480, 600);
  step('场景 9：失败原文较长时，聊天输入框仍然可点（不许被挤出面板）', withOk, {
    chatPanel: withFailure.chatPanel,
    chatInputRect: withFailure.chatInputRect,
    chatSendRect: withFailure.chatSendRect,
    chatInputHit: withFailure.hits ? withFailure.hits.chatInput : null,
    chatSendHit: withFailure.hits ? withFailure.hits.chatSend : null,
    switchBar: withFailure.switchBar,
    failureEntries: failureEntries,
  }, withOk ? null : '失败原文出现之后，480px 的聊天输入框/发送按钮被挤出面板（overflow: hidden 裁掉了输入区）');
}

// ---------- 视觉矩阵：视口 × 明暗（--visual） ---------------------------------
if (flag('visual')) {
  report.notes.push(
    '视觉矩阵（--visual）：明暗通过 html[data-theme] + localStorage(qio-theme) 切换；' +
      '密集卡片与关系线用**真实接口预置板面数据**（【模拟】数据种子），界面仍是应用自己渲染的',
  );
  const THEMES = ['light', 'dark'];
  async function applyTheme(t) {
    const applied = await evaluate(
      '(function(){ try { document.documentElement.dataset.theme = ' + JSON.stringify(t) + '; localStorage.setItem("qio-theme", ' + JSON.stringify(t) + '); } catch (e) {} return document.documentElement.getAttribute("data-theme"); })()',
    );
    await sleep(320);
    return applied;
  }
  async function openInteractive() {
    await goto(APP + '/#/interactive', 6500);
    await waitFor('[data-im=board-toolbar]', 10000);
    await sleep(900);
  }
  async function ensureChat() {
    if (!(await evaluate('!!document.querySelector("[data-im=chat-panel]")'))) {
      if (await evaluate('!!document.querySelector("[data-im=chat-toggle]")')) await clickSelector('[data-im=chat-toggle]');
      await sleep(900);
    }
  }
  async function ensureBatch() {
    if (!(await evaluate('!!document.querySelector("[data-im=batch-list]")'))) {
      if (await evaluate('!!document.querySelector("[data-im=batch-entry]")')) await clickSelector('[data-im=batch-entry]');
      await sleep(1000);
    }
  }
  async function boardCounts() {
    return evaluate(
      '(function(){ return { cards: document.querySelectorAll("[data-im=card]").length, links: document.querySelectorAll("svg.link-layer line.line").length }; })()',
    );
  }
  const visualShots = [];

  // 密集板面 + 关系线：通过真实接口写入板面状态，再让应用自己加载渲染
  const nowIso = new Date().toISOString();
  let seedNote = '视觉矩阵数据种子：未执行';
  try {
    const current = await fetch(BACKEND + '/api/interactive/boards/board_default/state').then((r) => r.json());
    const texts = [
      '第一张：材料清单与来源（这段文字写在卡片里，用来检查密集文字下聊天浮层的可读性）',
      '第二张：为什么需要恢复失败原文（用户点发送之后失败，文字不能丢）',
      '第三张：本机记录与服务器草稿的两种身份',
      '第四张：清除草稿必须同步，刷新后不许复活',
      '第五张：窄窗口下切换条要占住自己那一行',
      '第六张：提交失败默认区要说本次请求的真实原因',
      '第七张：长文本换行与限高滚动',
      '第八张：明暗两套令牌都要成立',
    ];
    const cards = texts.map((content, i) => ({
      id: 'd2c' + i,
      kind: 'text',
      content: content,
      meta: {},
      x: 40 + (i % 4) * 300,
      y: 40 + Math.floor(i / 4) * 220,
      w: 260,
      h: 160,
      checked: i % 2 === 0,
      hidden: false,
      folded: false,
      bookmarked: false,
      deleted: false,
      createdAt: nowIso,
      updatedAt: nowIso,
    }));
    const links = [0, 1, 2].map((i) => ({
      id: 'd2l' + i,
      src: 'd2c' + i,
      dst: 'd2c' + (i + 2),
      direction: false,
      meaning: '用户写下的关系含义 ' + (i + 1),
      deleted: false,
      createdAt: nowIso,
      updatedAt: nowIso,
    }));
    const state = { ...(current.state || {}), cards: cards, links: links, groups: [], selection: ['d2c0', 'd2c1'], updatedAt: nowIso };
    const put = await fetch(BACKEND + '/api/interactive/boards/board_default/state', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ state: state, reason: 'd2-visual-seed' }),
    });
    seedNote = '视觉矩阵数据种子：PUT ' + put.status + '，卡片 ' + cards.length + '，关系线 ' + links.length;
  } catch (err) {
    seedNote = '视觉矩阵数据种子失败：' + String(err);
  }
  report.notes.push(seedNote);

  /**
   * 种子可能被并发写入（别的工作区/别的验收跑批）盖掉：先打开页面确认一次，
   * 不够密就用界面自己的「添加 → 文字」补几张带文字的卡片。
   */
  await viewport(1440, 900);
  await openInteractive();
  let preCounts = await boardCounts();
  if ((preCounts.cards || 0) < 6) {
    for (let i = 0; i < 4; i += 1) {
      await clickSelector('[data-im=add-menu]');
      await waitFor('[data-im=add-menu-list]', 4000);
      await clickSelector('[data-im=add-text]');
      await sleep(900);
      const newId = await lastCardId();
      if (newId) {
        await selectAndEditCard(newId);
        await typeInto('[data-im=card-editor]', '补的密集卡片 ' + (i + 1) + '：用来检查浮层叠在密集卡片文字与关系线上时的可读性。');
        await clickCardConfirm(newId, '完成编辑');
        await sleep(900);
      }
    }
    preCounts = await boardCounts();
  }
  report.notes.push('视觉矩阵卡片数（进入矩阵前）：' + JSON.stringify(preCounts));

  // A. 三个桌面视口 × 明暗：互动板 + 聊天展开（背后是密集卡片与关系线）
  for (const vp of [
    { name: '1440x900', w: 1440, h: 900 },
    { name: '1024x768', w: 1024, h: 768 },
    { name: '800x600', w: 800, h: 600 },
  ]) {
    await viewport(vp.w, vp.h);
    await openInteractive();
    await ensureChat();
    for (const t of THEMES) {
      await applyTheme(t);
      const name = 'r5-d2-visual-' + vp.name + '-' + t + '-chat';
      await shot(name);
      visualShots.push({ name: name, counts: await boardCounts(), theme: t, viewport: vp.name });
    }
  }

  // B. 1024×768 批量列表 + 密集板面
  await viewport(1024, 768);
  await openInteractive();
  await ensureBatch();
  for (const t of THEMES) {
    await applyTheme(t);
    const name = 'r5-d2-visual-1024x768-' + t + '-batch-dense';
    await shot(name);
    visualShots.push({ name: name, counts: await boardCounts(), theme: t });
  }

  // C. 480×600：两个面板都请求展开 → 切换条
  await viewport(480, 600);
  await openInteractive();
  await ensureChat();
  await ensureBatch();
  for (const t of THEMES) {
    await applyTheme(t);
    const name = 'r5-d2-visual-480x600-' + t + '-switch-bar';
    await shot(name);
    visualShots.push({
      name: name,
      theme: t,
      geoMode: await evaluate('(function(){ var s = document.querySelector(".im-stage"); return s ? s.getAttribute("im-geo-mode") : null; })()'),
      counts: await boardCounts(),
    });
  }

  // D. 长失败原文（对话页，拦截 /api/turns）
  interceptMode = 'turns';
  await viewport(1024, 768);
  await goto(APP + '/#/', 7000);
  await waitFor('#composer-input', 12000);
  const longFailed =
    '长失败原文-D2-' +
    '这是一段很长的原稿，用来验证失败恢复区在文字很长时限高滚动、不挤掉输入框与发送按钮。'.repeat(5);
  await typeInto('#composer-input', longFailed);
  await clickSelector('.send-btn');
  await sleep(2000);
  for (const t of THEMES) {
    await applyTheme(t);
    const name = 'r5-d2-visual-1024x768-' + t + '-long-failure-text';
    await shot(name);
    visualShots.push({ name: name, theme: t });
  }
  interceptMode = null;

  // E. 长提交原因（互动板，拦截 /submissions）
  interceptMode = 'submissions';
  await viewport(1440, 900);
  await openInteractive();
  await clickSelector('[data-im=submit]');
  await sleep(2600);
  for (const t of THEMES) {
    await applyTheme(t);
    const name = 'r5-d2-visual-1440x900-' + t + '-long-submit-reason';
    await shot(name);
    visualShots.push({
      name: name,
      theme: t,
      failureText: String((await textOf('[data-im=submit-failure]')) || '').slice(0, 160),
      detailsOpen: await evaluate('!!document.querySelector("[data-im=submit-details-box]")'),
    });
  }
  interceptMode = null;
  await applyTheme('light');

  step('视觉矩阵：截图已生成（视口 × 明暗 + 长文本 + 批量列表）', visualShots.length >= 12, {
    shots: visualShots,
    seed: seedNote,
  });
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
