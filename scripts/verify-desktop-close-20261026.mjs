#!/usr/bin/env node
/**
 * QIO 真机桌面验证驱动（2026-10-26，工作流 D 专用；不参与产品构建）
 *
 * 做什么：启动本轮候选 Tauri 壳（debug 构建 + 内嵌前端产物 + 真实 Python 后端），
 * 用 WebView2 的 CDP 端口做真实 DOM 断言与真实鼠标事件，用 Win32 原语做窗口可见性/
 * 进程树/WM_CLOSE/截图，逐场景验证 R9 壳侧关闭协议。
 *
 * 运行：
 *   node scripts/verify-desktop-close-20261026.mjs
 *   node scripts/verify-desktop-close-20261026.mjs --only ask,remember --evidence <dir>
 *
 * 前置：frontend/src-tauri 已构建候选壳（cargo build --features custom-protocol）、
 *       frontend/dist 存在、backend/.venv 存在。
 * 不使用真实模型密钥；不创建任何模型工作；每个场景独立 QIO_DATA_DIR。
 */
import { spawn, execFileSync } from 'node:child_process';
import crypto from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(HERE, '..');
const EXE = path.join(ROOT, 'frontend', 'src-tauri', 'target', 'debug', 'qio.exe');
const PY = path.join(ROOT, 'backend', '.venv', 'Scripts', 'python.exe');
const HELPER = path.join(HERE, 'desktop-native-20261026.ps1');
const POWERSHELL = 'C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe';

const argv = process.argv.slice(2);
function argValue(name, fallback) {
  const index = argv.indexOf(name);
  return index >= 0 && argv[index + 1] ? argv[index + 1] : fallback;
}
const EVID = argValue('--evidence', 'D:\\qio-dev\\_fixpm\\evidence\\D\\f07');
const ONLY = argValue('--only', '');
fs.mkdirSync(EVID, { recursive: true });
fs.mkdirSync(path.join(EVID, 'shots'), { recursive: true });
fs.mkdirSync(path.join(EVID, 'data'), { recursive: true });
const driverLog = path.join(EVID, 'driver.log');
try { fs.unlinkSync(driverLog); } catch (err) { /* fresh log */ }

function log(line) {
  const text = '[' + new Date().toISOString() + '] ' + line;
  console.log(text);
  fs.appendFileSync(driverLog, text + '\n');
}
const results = [];
function record(scenario, id, ok, detail) {
  const item = { scenario, id, ok: Boolean(ok), detail: detail === undefined ? '' : String(detail) };
  results.push(item);
  log((item.ok ? 'PASS ' : 'FAIL ') + scenario + ' :: ' + id + (item.detail ? ' -- ' + item.detail : ''));
}
function sleep(ms) { return new Promise(function (resolve) { setTimeout(resolve, ms); }); }

function ps(action, opts) {
  const options = opts || {};
  const args = ['-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File', HELPER, '-Action', action];
  if (options.pid) { args.push('-TargetPid', String(options.pid)); }
  if (options.hwnd) { args.push('-Hwnd', String(options.hwnd)); }
  if (options.path) { args.push('-Path', options.path); }
  if (options.message !== undefined) { args.push('-Message', String(options.message)); }
  if (options.wparam !== undefined) { args.push('-WParam', String(options.wparam)); }
  if (options.lparam !== undefined) { args.push('-LParam', String(options.lparam)); }
  try {
    const out = execFileSync(POWERSHELL, args, { encoding: 'utf8', windowsHide: true, timeout: 120000 });
    const lines = out.trim().split(/\r?\n/);
    return JSON.parse(lines[lines.length - 1]);
  } catch (err) {
    const stdout = (err && err.stdout) || '';
    const lines2 = String(stdout).trim().split(/\r?\n/);
    const last = lines2[lines2.length - 1] || '';
    try { return JSON.parse(last); } catch (parseErr) { /* fall through */ }
    return { ok: false, error: String((err && err.message) || err) };
  }
}

class Cdp {
  constructor(port) { this.port = port; this.ws = null; this.nextId = 1; this.pending = new Map(); }
  async connect() {
    const deadline = Date.now() + 60000;
    let target = null;
    while (Date.now() < deadline) {
      try {
        const response = await fetch('http://127.0.0.1:' + this.port + '/json/list');
        const list = await response.json();
        target = list.find(function (item) { return item.type === 'page' && item.webSocketDebuggerUrl; }) ||
          list.find(function (item) { return item.webSocketDebuggerUrl; }) || null;
        if (target) { break; }
      } catch (err) { /* not up yet */ }
      await sleep(300);
    }
    if (!target) { throw new Error('no CDP target on port ' + this.port); }
    const self = this;
    this.ws = new WebSocket(target.webSocketDebuggerUrl);
    await new Promise(function (resolve, reject) {
      self.ws.onopen = function () { resolve(); };
      self.ws.onerror = function () { reject(new Error('CDP websocket error')); };
    });
    this.ws.onmessage = function (event) { self.onMessage(event.data); };
    await this.send('Runtime.enable');
    await this.send('Page.enable');
    return target;
  }
  onMessage(data) {
    let message = null;
    try { message = JSON.parse(data); } catch (err) { return; }
    if (message.id && this.pending.has(message.id)) {
      const entry = this.pending.get(message.id);
      this.pending.delete(message.id);
      if (message.error) { entry.reject(new Error(message.error.message || JSON.stringify(message.error))); }
      else { entry.resolve(message.result); }
    }
  }
  send(method, params) {
    const self = this;
    const id = this.nextId++;
    return new Promise(function (resolve, reject) {
      self.pending.set(id, { resolve, reject });
      try { self.ws.send(JSON.stringify({ id: id, method: method, params: params || {} })); }
      catch (err) { self.pending.delete(id); reject(err); return; }
      setTimeout(function () {
        if (self.pending.has(id)) { self.pending.delete(id); reject(new Error('CDP timeout: ' + method)); }
      }, 30000);
    });
  }
  async evaluate(expression) {
    const result = await this.send('Runtime.evaluate', {
      expression: expression, returnByValue: true, awaitPromise: true, userGesture: true
    });
    if (result.exceptionDetails) { throw new Error('eval exception: ' + JSON.stringify(result.exceptionDetails).slice(0, 400)); }
    return result.result ? result.result.value : undefined;
  }
  async waitFor(expression, timeoutMs, label) {
    const deadline = Date.now() + (timeoutMs || 30000);
    while (Date.now() < deadline) {
      try { const value = await this.evaluate(expression); if (value) { return value; } } catch (err) { /* page busy */ }
      await sleep(200);
    }
    throw new Error('timeout waiting for ' + (label || expression));
  }
  async rect(selector) {
    return await this.evaluate('(function(){var el=document.querySelector(' + JSON.stringify(selector) +
      ');if(!el){return null;}var r=el.getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+r.height/2,w:r.width,h:r.height};})()');
  }
  async click(selector) {
    const box = await this.rect(selector);
    if (!box) { throw new Error('element not found: ' + selector); }
    await this.send('Input.dispatchMouseEvent', { type: 'mouseMoved', x: box.x, y: box.y });
    await this.send('Input.dispatchMouseEvent', { type: 'mousePressed', x: box.x, y: box.y, button: 'left', clickCount: 1 });
    await this.send('Input.dispatchMouseEvent', { type: 'mouseReleased', x: box.x, y: box.y, button: 'left', clickCount: 1 });
    return box;
  }
  async clickText(containerSelector, text) {
    const index = await this.evaluate('(function(){var t=' + JSON.stringify(text) +
      ';var list=[].slice.call(document.querySelectorAll(' + JSON.stringify(containerSelector) +
      '));return list.findIndex(function(b){return (b.textContent||"").trim()===t;});})()');
    if (index < 0) { throw new Error('no button with text: ' + text); }
    const box = await this.evaluate('(function(){var el=document.querySelectorAll(' + JSON.stringify(containerSelector) +
      ')[' + index + '];var r=el.getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+r.height/2};})()');
    await this.send('Input.dispatchMouseEvent', { type: 'mouseMoved', x: box.x, y: box.y });
    await this.send('Input.dispatchMouseEvent', { type: 'mousePressed', x: box.x, y: box.y, button: 'left', clickCount: 1 });
    await this.send('Input.dispatchMouseEvent', { type: 'mouseReleased', x: box.x, y: box.y, button: 'left', clickCount: 1 });
    return box;
  }
  close() { try { if (this.ws) { this.ws.close(); } } catch (err) { /* ignore */ } }
}

async function backend(run, method, apiPath, body) {
  const headers = { Authorization: 'Bearer ' + run.info.token };
  if (body) { headers['Content-Type'] = 'application/json'; }
  const response = await fetch('http://127.0.0.1:' + run.info.port + apiPath, {
    method: method, headers: headers, body: body ? JSON.stringify(body) : undefined
  });
  const text = await response.text();
  let json = null;
  try { json = JSON.parse(text); } catch (err) { /* not json */ }
  return { status: response.status, json: json, text: text };
}

async function launch(name, opts) {
  const options = opts || {};
  const token = options.token || crypto.randomBytes(24).toString('hex');
  const dataDir = options.dataDir || path.join(EVID, 'data', name + '-' + Date.now());
  fs.mkdirSync(dataDir, { recursive: true });
  const debugPort = 9330 + Math.floor(Math.random() * 150);
  const env = Object.assign({}, process.env, {
    QIO_DATA_DIR: dataDir,
    PYTHON: PY,
    QIO_STARTUP_ERROR_DIALOG: '0',
    QIO_SESSION_TOKEN: token,
    WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS: '--remote-debugging-port=' + debugPort + ' --remote-allow-origins=*'
  });
  if (options.closeBehavior) { env.QIO_CLOSE_BEHAVIOR = options.closeBehavior; }
  const child = spawn(EXE, [], { cwd: ROOT, env: env, stdio: 'ignore', windowsHide: false });
  const run = { name: name, child: child, pid: child.pid, dataDir: dataDir, debugPort: debugPort, token: token, info: null, cdp: new Cdp(debugPort) };
  run.exited = new Promise(function (resolve) { child.on('exit', function (code) { resolve(code); }); });
  const target = await run.cdp.connect();
  log('[' + name + '] shell pid=' + run.pid + ' cdp=' + (target.url || target.title));
  await run.cdp.waitFor('!!(document.querySelector(".window-controls") && document.querySelector(".wc-close"))', 90000, 'window controls');
  let info = null;
  for (let attempt = 0; attempt < 240; attempt += 1) {
    try {
      const value = await run.cdp.evaluate('window.__TAURI_INTERNALS__.invoke("qio_backend_info")');
      if (value && value.port) { info = value; break; }
    } catch (err) { /* boot */ }
    await sleep(250);
  }
  if (!info) { throw new Error('qio_backend_info unavailable'); }
  run.info = info;
  for (let attempt = 0; attempt < 240; attempt += 1) {
    try {
      const response = await backend(run, 'GET', '/api/settings/ui');
      if (response.status === 200) { break; }
    } catch (err) { /* boot */ }
    await sleep(250);
  }
  log('[' + name + '] backend port=' + info.port + ' token=***');
  return run;
}

function shellWindows(run) { return ps('window', { pid: run.pid }); }
function mainWindow(run) { const info = shellWindows(run); return info.main || null; }
function visibleNow(run) { const window = mainWindow(run); return Boolean(window && window.visible); }
async function waitVisible(run, expected, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (visibleNow(run) === expected) { return true; }
    await sleep(200);
  }
  return false;
}
async function waitExit(run, timeoutMs) {
  return await Promise.race([
    run.exited.then(function (code) { return { exited: true, code: code }; }),
    sleep(timeoutMs).then(function () { return { exited: false, code: null }; })
  ]);
}
function sendClose(run) {
  const window = mainWindow(run);
  if (!window) { throw new Error('main window not found'); }
  return ps('close', { hwnd: window.hwnd });
}
function processList() { return ps('processes').processes || []; }
function descendants(run) {
  const all = processList();
  const byParent = new Map();
  for (const item of all) {
    if (!byParent.has(item.ppid)) { byParent.set(item.ppid, []); }
    byParent.get(item.ppid).push(item);
  }
  const found = [];
  const queue = [run.pid];
  while (queue.length) {
    const parent = queue.shift();
    const children = byParent.get(parent) || [];
    for (const child of children) { found.push(child); queue.push(child.pid); }
  }
  return found;
}
function backendPids(run) {
  return descendants(run).filter(function (item) { return /python/i.test(item.name || '') && /agent\.main/.test(item.cmd || ''); });
}
function shot(run, file) {
  const window = mainWindow(run);
  if (!window) { return { ok: false, error: 'no window' }; }
  return ps('screenshot', { hwnd: window.hwnd, path: path.join(EVID, 'shots', file) });
}
function readShellLog(run) {
  const logPath = path.join(run.dataDir, 'logs', 'qio.log');
  try { return fs.readFileSync(logPath, 'utf8'); } catch (err) { return ''; }
}
function firstMatch(text, pattern) {
  const match = text.match(pattern);
  return match ? match[0].slice(0, 300) : '';
}
function cleanup(run) {
  try { run.cdp.close(); } catch (err) { /* ignore */ }
  try {
    if (run.child.exitCode === null) {
      ps('kill-tree', { pid: run.pid });
      run.child.kill();
    }
  } catch (err) { /* ignore */ }
  return sleep(800);
}
function askDialogState(cdp) {
  return cdp.evaluate('(function(){var d=document.querySelector(".close-ask");if(!d){return null;}var title=document.querySelector("#qio-close-ask-title");var buttons=[].map.call(document.querySelectorAll(".close-ask__actions button"),function(b){return (b.textContent||"").trim();});return {title:title?title.textContent.trim():"",buttons:buttons,remember:!!document.querySelector(".close-ask__remember input")};})()');
}

async function scenarioAskTray() {
  const S = 'ask-and-tray';
  const run = await launch('ask-tray', {});
  try {
    const window = mainWindow(run);
    record(S, 'shell starts with a real visible main window', Boolean(window && window.visible), JSON.stringify(window));
    const chrome = await run.cdp.evaluate('(function(){var w=document.querySelector(".window-chrome");return {hasChrome:!!w,buttons:document.querySelectorAll(".window-controls .wc-btn").length,closeLabel:(document.querySelector(".wc-close")||{}).getAttribute?document.querySelector(".wc-close").getAttribute("aria-label"):"",caption:(document.querySelector(".brand-caption")||{}).textContent||""};})()');
    record(S, 'WindowChrome renders 3 window buttons (min/max/close)', chrome.hasChrome && chrome.buttons === 3, JSON.stringify(chrome));
    record(S, 'close button keeps R9 aria-label 关闭', chrome.closeLabel === '关闭', String(chrome.closeLabel));
    record(S, 'global bar keeps PERSONAL AGENT brand', chrome.caption.trim() === 'PERSONAL AGENT', String(chrome.caption));
    const backendBefore = backendPids(run);
    record(S, 'real Python backend process is running as managed child', backendBefore.length > 0, JSON.stringify(backendBefore.map(function (p) { return p.pid + ':' + p.name; })));
    shot(run, '01-startup.png');
    await run.cdp.click('.wc-close');
    const dialog = await run.cdp.waitFor('(function(){var d=document.querySelector(".close-ask");if(!d){return null;}return {title:(document.querySelector("#qio-close-ask-title")||{}).textContent,buttons:[].map.call(document.querySelectorAll(".close-ask__actions button"),function(b){return b.textContent.trim();}),remember:!!document.querySelector(".close-ask__remember input")};})()', 10000, 'ask dialog');
    record(S, 'click real X opens ask dialog', Boolean(dialog), JSON.stringify(dialog));
    record(S, 'ask dialog text/choices match R9 contract', dialog && dialog.title.trim() === '关闭 QIO 主窗口？' && dialog.buttons.join('|') === '最小化到托盘|退出 QIO|取消', JSON.stringify(dialog));
    record(S, 'ask dialog offers remember-choice checkbox', Boolean(dialog && dialog.remember));
    shot(run, '02-ask-dialog.png');
    await run.cdp.clickText('.close-ask__actions button', '取消');
    const closed = await run.cdp.waitFor('!document.querySelector(".close-ask")', 5000, 'dialog closed');
    record(S, 'cancel keeps app running and window visible', Boolean(closed) && run.child.exitCode === null && visibleNow(run));
    await run.cdp.click('.wc-close');
    await run.cdp.waitFor('!!document.querySelector(".close-ask")', 10000, 'ask dialog 2');
    await run.cdp.clickText('.close-ask__actions button', '最小化到托盘');
    record(S, 'ask -> 最小化到托盘 hides real window', await waitVisible(run, false, 8000));
    record(S, 'hidden state keeps shell and backend alive', run.child.exitCode === null && backendPids(run).length > 0);
    const tray = ps('tray-click', { pid: run.pid });
    record(S, 'tray left-click restores window (tray_icon_app callback)', Boolean(tray.ok) && await waitVisible(run, true, 8000), JSON.stringify(tray));
    shot(run, '03-restored-from-tray.png');
    await run.cdp.click('.wc-close');
    await run.cdp.waitFor('!!document.querySelector(".close-ask")', 10000, 'ask dialog 3');
    shot(run, '04-ask-dialog-before-quit.png');
    await run.cdp.clickText('.close-ask__actions button', '退出 QIO');
    const exit = await waitExit(run, 60000);
    record(S, 'choose 退出 QIO exits shell', exit.exited, JSON.stringify(exit));
    record(S, 'shell exits with code 0', exit.exited && exit.code === 0, String(exit.code));
    await sleep(1800);
    const left = backendPids(run);
    record(S, 'managed backend process tree is gone after quit', left.length === 0, JSON.stringify(left.map(function (p) { return p.pid; })));
    const logText = readShellLog(run);
    record(S, 'log: tray hide recorded', /主窗口已隐藏到托盘/.test(logText));
    record(S, 'log: tray restore recorded', /已从托盘恢复主窗口/.test(logText));
    record(S, 'log: quit cleanup verified (graceful/verified)', /verified=true|核验 pid 消失/.test(logText), firstMatch(logText, /\[qio\].*退出.*/g));
  } finally { await cleanup(run); }
}

async function scenarioRememberRestart() {
  const S = 'remember-choice';
  let run = await launch('remember-1', {});
  let dataDir = null;
  try {
    dataDir = run.dataDir;
    await run.cdp.click('.wc-close');
    await run.cdp.waitFor('!!document.querySelector(".close-ask")', 10000, 'ask dialog');
    await run.cdp.click('.close-ask__remember input');
    const checked = await run.cdp.evaluate('!!document.querySelector(".close-ask__remember input").checked');
    record(S, 'remember checkbox can be checked', checked);
    await run.cdp.clickText('.close-ask__actions button', '最小化到托盘');
    record(S, 'remember + 最小化到托盘 hides window', await waitVisible(run, false, 8000));
    const settings = await backend(run, 'GET', '/api/settings/ui');
    record(S, 'remembered choice persisted to backend ui.close_behavior=tray', settings.status === 200 && settings.json && settings.json.close_behavior === 'tray', JSON.stringify(settings.json));
    ps('tray-click', { pid: run.pid });
    record(S, 'tray restore after remembered tray', await waitVisible(run, true, 8000));
    await run.cdp.click('.wc-close');
    const hiddenAgain = await waitVisible(run, false, 8000);
    const dialogSeen = await run.cdp.evaluate('!!document.querySelector(".close-ask")');
    record(S, 'second X honors remembered tray: hides directly, no ask dialog', hiddenAgain && !dialogSeen, 'dialog=' + dialogSeen);
  } finally { await cleanup(run); }
  run = await launch('remember-2', { dataDir: dataDir });
  try {
    await run.cdp.click('.wc-close');
    const hiddenAfterRestart = await waitVisible(run, false, 8000);
    const dialogAfterRestart = await run.cdp.evaluate('!!document.querySelector(".close-ask")');
    record(S, 'after shell restart the remembered tray behavior still applies', hiddenAfterRestart && !dialogAfterRestart, 'hidden=' + hiddenAfterRestart + ' dialog=' + dialogAfterRestart);
    ps('tray-click', { pid: run.pid });
    record(S, 'tray restore after restart', await waitVisible(run, true, 8000));
  } finally { await cleanup(run); }
}

async function scenarioQuitSetting() {
  const S = 'quit-setting';
  const run = await launch('quit-setting', {});
  try {
    const put = await backend(run, 'PUT', '/api/settings/ui', { close_behavior: 'quit' });
    record(S, 'backend accepts ui.close_behavior=quit', put.status === 200 && put.json && put.json.close_behavior === 'quit', JSON.stringify(put.json));
    const dialogBefore = await run.cdp.evaluate('!!document.querySelector(".close-ask")');
    await run.cdp.click('.wc-close');
    const exit = await waitExit(run, 60000);
    record(S, 'X with quit behavior exits without ask dialog', exit.exited && !dialogBefore, JSON.stringify(exit));
    record(S, 'exit code 0 for quit behavior', exit.exited && exit.code === 0, String(exit.code));
    await sleep(1800);
    const left = backendPids(run);
    record(S, 'managed backend tree is gone after quit', left.length === 0, JSON.stringify(left.map(function (p) { return p.pid; })));
    const logText = readShellLog(run);
    record(S, 'log: shell resolved close behavior = quit', /壳侧解析关闭行为 = quit/.test(logText));
    record(S, 'log: quit cleanup verified', /verified=true|核验 pid 消失/.test(logText));
  } finally { await cleanup(run); }
}

async function scenarioSystemClose() {
  const S = 'system-close';
  let run = await launch('sys-tray', { closeBehavior: 'tray' });
  try {
    sendClose(run);
    record(S, 'WM_CLOSE with QIO_CLOSE_BEHAVIOR=tray hides window', await waitVisible(run, false, 8000));
    const dialogSeen = await run.cdp.evaluate('!!document.querySelector(".close-ask")');
    record(S, 'tray override never opens ask dialog', !dialogSeen);
    ps('tray-click', { pid: run.pid });
    record(S, 'tray restore after system close', await waitVisible(run, true, 8000));
    const logText = readShellLog(run);
    record(S, 'log: env override tray recorded', /QIO_CLOSE_BEHAVIOR 覆盖：tray/.test(logText));
  } finally { await cleanup(run); }
  run = await launch('sys-quit', { closeBehavior: 'quit' });
  try {
    sendClose(run);
    const exit = await waitExit(run, 60000);
    record(S, 'WM_CLOSE with QIO_CLOSE_BEHAVIOR=quit exits app', exit.exited && exit.code === 0, JSON.stringify(exit));
    await sleep(1800);
    const left = backendPids(run);
    record(S, 'backend tree gone after system close quit', left.length === 0, JSON.stringify(left.map(function (p) { return p.pid; })));
    const logText = readShellLog(run);
    record(S, 'log: env override quit recorded', /QIO_CLOSE_BEHAVIOR 覆盖：quit/.test(logText));
  } finally { await cleanup(run); }
}

async function scenarioBlocked() {
  const S = 'blocked-and-duplicate';
  const run = await launch('blocked', {});
  try {
    const pids = backendPids(run);
    record(S, 'backend process tree found before blocking', pids.length > 0, JSON.stringify(pids.map(function (p) { return p.pid; })));
    for (const item of pids) {
      record(S, 'suspend backend pid ' + item.pid + ' (simulates unresponsive backend)', ps('suspend', { pid: item.pid }).ok);
    }
    await run.cdp.click('.wc-close');
    await run.cdp.waitFor('!!document.querySelector(".close-ask")', 10000, 'ask dialog');
    await run.cdp.click('.wc-close');
    await sleep(700);
    const dialogs = await run.cdp.evaluate('document.querySelectorAll(".close-ask").length');
    record(S, 'duplicate X click does not stack ask dialogs', dialogs === 1, 'count=' + dialogs);
    sendClose(run);
    await sleep(700);
    const dialogs2 = await run.cdp.evaluate('document.querySelectorAll(".close-ask").length');
    record(S, 'duplicate WM_CLOSE does not stack ask dialogs', dialogs2 === 1, 'count=' + dialogs2);
    const startedAt = Date.now();
    await run.cdp.clickText('.close-ask__actions button', '退出 QIO');
    const exit = await waitExit(run, 90000);
    const elapsed = Date.now() - startedAt;
    record(S, 'quit still completes when backend is unresponsive', exit.exited, JSON.stringify(exit));
    record(S, 'bounded shutdown + forced kill used the expected budget (>=20s)', elapsed >= 20000, 'elapsedMs=' + elapsed);
    await sleep(1800);
    const left = backendPids(run);
    record(S, 'all backend pids are gone after forced cleanup', left.length === 0, JSON.stringify(left.map(function (p) { return p.pid; })));
    const logText = readShellLog(run);
    record(S, 'log: forced cleanup (taskkill/forced) recorded', /taskkill|强制结束|核验 pid 消失|verified=true/.test(logText), firstMatch(logText, /\[qio\].*(taskkill|强制|退出).*/g));
  } finally { await cleanup(run); }
}


async function scenarioTrayQuit() {
  const S = 'tray-quit';
  const run = await launch('trayquit', {});
  try {
    const right = ps('tray-right-click', { pid: run.pid });
    record(S, 'right-click tray icon opens the native tray menu', Boolean(right.ok), JSON.stringify(right));
    await sleep(1200);
    const invoked = ps('menu-keys', { pid: run.pid });
    record(S, 'tray menu opened and 退出 QIO selected via keyboard (Down,Down,Enter)', Boolean(invoked.ok), JSON.stringify(invoked));
    const exit = await waitExit(run, 60000);
    record(S, 'tray menu quit exits shell with code 0', exit.exited && exit.code === 0, JSON.stringify(exit));
    await sleep(1800);
    const left = backendPids(run);
    record(S, 'managed backend tree is gone after tray quit', left.length === 0, JSON.stringify(left.map(function (p) { return p.pid; })));
    const logText = readShellLog(run);
    record(S, 'log: tray quit completion recorded', /托盘退出完成/.test(logText), firstMatch(logText, /\[qio\].*托盘退出.*/g));
    record(S, 'log: tray quit cleanup verified', /verified=true/.test(logText));
  } finally { await cleanup(run); }
}


async function scenarioTrayProbe() {
  const S = 'tray-probe';
  const run = await launch('trayprobe', {});
  try {
    const right = ps('tray-right-click', { pid: run.pid });
    record(S, 'right-click tray icon accepted', Boolean(right.ok), JSON.stringify(right));
    await sleep(1500);
    const windows = ps('windows-all', { pid: run.pid });
    const classes = (windows.windows || []).map(function (w) { return w.class + '/' + (w.title || '').slice(0, 20) + '/' + w.visible; });
    log('[tray-probe] windows of shell pid: ' + JSON.stringify(classes));
    const uia = ps('uia-dump');
    log('[tray-probe] uia-dump: ' + JSON.stringify(uia).slice(0, 1200));
    const keys = ps('menu-keys', { pid: run.pid });
    log('[tray-probe] menu-keys: ' + JSON.stringify(keys));
    const exit = await waitExit(run, 15000);
    log('[tray-probe] exit after menu-keys: ' + JSON.stringify(exit));
    record(S, 'tray menu keyboard selection quit (diagnostic)', exit.exited);
  } finally { await cleanup(run); }
}

const SCENARIOS = {
  ask: scenarioAskTray,
  remember: scenarioRememberRestart,
  quit: scenarioQuitSetting,
  sys: scenarioSystemClose,
  blocked: scenarioBlocked,
  trayquit: scenarioTrayQuit,
  trayprobe: scenarioTrayProbe
};

async function main() {
  log('evidence dir: ' + EVID);
  log('exe: ' + EXE + ' exists=' + fs.existsSync(EXE));
  log('python: ' + PY + ' exists=' + fs.existsSync(PY));
  if (!fs.existsSync(EXE)) { throw new Error('candidate shell not built: ' + EXE); }
  const names = ONLY ? ONLY.split(',') : Object.keys(SCENARIOS);
  for (const name of names) {
    const scenario = SCENARIOS[name];
    if (!scenario) { log('unknown scenario: ' + name); continue; }
    log('=== scenario ' + name + ' ===');
    try { await scenario(); }
    catch (err) {
      record(name, 'scenario crashed', false, (err && err.stack ? err.stack.split('\n').slice(0, 2).join(' | ') : String(err)));
      await sleep(500);
    }
  }
  const summary = {
    generatedAt: new Date().toISOString(),
    candidateExe: EXE,
    candidateExeMtime: fs.existsSync(EXE) ? fs.statSync(EXE).mtime.toISOString() : null,
    note: 'debug build with embedded frontend dist; real Python backend from backend/.venv',
    results: results
  };
  fs.writeFileSync(path.join(EVID, 'f07-results.json'), JSON.stringify(summary, null, 2));
  const failed = results.filter(function (item) { return !item.ok; });
  log('SUMMARY: ' + (results.length - failed.length) + '/' + results.length + ' checks passed');
  for (const item of failed) { log('  FAILED ' + item.scenario + ' :: ' + item.id + ' -- ' + item.detail); }
  process.exit(failed.length ? 1 : 0);
}

main().catch(function (err) {
  log('FATAL ' + ((err && err.stack) || String(err)));
  process.exit(2);
});
