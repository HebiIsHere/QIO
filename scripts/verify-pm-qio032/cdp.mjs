// 最小 CDP 驱动（PM-QIO032 E2E 的浏览器层）。
//
// 为什么存在：本机**没有** Playwright 包（任务书给的
// C:\Users\zxy\.cache\codex-runtimes\...\node_modules\playwright 在这台机器上不存在；
// 全盘找不到 playwright / playwright-core，npm 缓存里也没有 tarball；python playwright 也没有）。
// 纪律禁止 npm install / 联网下依赖，所以这里用 Node 24 自带的 WebSocket + fetch 直接说
// Chrome DevTools Protocol，把系统 Edge（headless）当浏览器用。
//
// 边界（如实）：只实现验收用到的那一小撮能力 —— 真实鼠标点击（按元素中心坐标派发
// Input.dispatchMouseEvent，真的走浏览器命中测试）、键盘、输入、截图、文件选择器、请求拦截。
// 它不是 Playwright 的替代品，也不承诺 Playwright 语义（自动等待、严格模式等）。
import { spawn } from "node:child_process";
import { accessSync, mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const EDGE_CANDIDATES = [
  "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe",
  "C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe",
];

export function findEdge() {
  const candidates = process.env.QIO_EDGE ? [process.env.QIO_EDGE, ...EDGE_CANDIDATES] : EDGE_CANDIDATES;
  for (const p of candidates) {
    try { accessSync(p); return p; } catch { /* 继续找 */ }
  }
  return EDGE_CANDIDATES[0];
}

export function sleep(ms) { return new Promise((r) => setTimeout(r, ms)); }

export class CdpBrowser {
  constructor(ws, proc) {
    this.ws = ws;
    this.proc = proc;
    this.nextId = 1;
    this.pending = new Map();
    this.listeners = new Set();
    ws.addEventListener("message", (ev) => this._onMessage(ev.data));
  }

  static async launch({ port = 9333, headless = true } = {}) {
    const exe = findEdge();
    const dir = mkdtempSync(join(tmpdir(), "qio-cdp-"));
    const args = [
      headless ? "--headless=new" : "--window-size=1440,900",
      "--remote-debugging-port=" + port,
      "--user-data-dir=" + dir,
      "--no-first-run",
      "--no-default-browser-check",
      "--disable-gpu",
      "--disable-background-networking",
      "--disable-features=Translate,MediaRouter",
      "about:blank",
    ];
    const proc = spawn(exe, args, { stdio: ["ignore", "pipe", "pipe"] });
    let stderr = "";
    proc.stderr.on("data", (d) => { stderr += String(d); });
    const deadline = Date.now() + 30000;
    let version = null;
    while (Date.now() < deadline) {
      try {
        const r = await fetch("http://127.0.0.1:" + port + "/json/version", { signal: AbortSignal.timeout(1500) });
        if (r.ok) { version = await r.json(); break; }
      } catch { /* 还没起来 */ }
      await sleep(250);
    }
    if (!version) {
      proc.kill();
      throw new Error("Edge CDP 没起来（端口 " + port + "）：" + stderr.slice(-400));
    }
    const ws = new WebSocket(version.webSocketDebuggerUrl);
    await new Promise((resolve, reject) => {
      ws.addEventListener("open", resolve, { once: true });
      ws.addEventListener("error", () => reject(new Error("CDP WebSocket 连不上")), { once: true });
    });
    const browser = new CdpBrowser(ws, proc);
    browser.version = version;
    browser.userDataDir = dir;
    return browser;
  }

  _onMessage(raw) {
    let msg;
    try { msg = JSON.parse(raw); } catch { return; }
    if (msg.id && this.pending.has(msg.id)) {
      const entry = this.pending.get(msg.id);
      this.pending.delete(msg.id);
      if (msg.error) entry.reject(new Error("CDP " + entry.method + " 失败：" + JSON.stringify(msg.error)));
      else entry.resolve(msg.result);
      return;
    }
    if (msg.method) {
      for (const fn of this.listeners) { try { fn(msg); } catch { /* listener 自负 */ } }
    }
  }

  on(fn) { this.listeners.add(fn); return () => this.listeners.delete(fn); }

  send(method, params = {}, sessionId) {
    const id = this.nextId++;
    const payload = { id, method, params };
    if (sessionId) payload.sessionId = sessionId;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject, method });
      this.ws.send(JSON.stringify(payload));
      setTimeout(() => {
        if (this.pending.has(id)) { this.pending.delete(id); reject(new Error("CDP 超时：" + method)); }
      }, 60000);
    });
  }

  async close() {
    try { this.ws.close(); } catch { /* ignore */ }
    try { this.proc.kill(); } catch { /* ignore */ }
  }
}

export class Page {
  constructor(browser, sessionId, targetId) {
    this.browser = browser;
    this.sessionId = sessionId;
    this.targetId = targetId;
    this.consoleErrors = [];
    this.dialogs = [];
    this.fileChooserWaiters = [];
    this.fetchFailRules = [];
    this.fetchDelayRules = [];
    this.navPromise = null;
    browser.on((msg) => {
      if (msg.sessionId !== this.sessionId) return;
      const p = msg.params || {};
      if (msg.method === "Runtime.consoleAPICalled" && p.type === "error") {
        this.consoleErrors.push((p.args || []).map((a) => a.value ?? a.description ?? "").join(" ").slice(0, 300));
      }
      if (msg.method === "Runtime.exceptionThrown") {
        this.consoleErrors.push(("页面异常：" + (p.exceptionDetails && (p.exceptionDetails.exception && p.exceptionDetails.exception.description || p.exceptionDetails.text) || "")).slice(0, 300));
      }
      if (msg.method === "Page.fileChooserOpened") {
        for (const w of this.fileChooserWaiters.splice(0)) w(p);
      }
      if (msg.method === "Page.javascriptDialogOpening") {
        this.dialogs.push(p.message || "");
        this.browser.send("Page.handleJavaScriptDialog", { accept: true }, this.sessionId).catch(() => null);
      }
      if (msg.method === "Fetch.requestPaused") {
        const requestId = p.requestId;
        const req = p.request || { url: "", method: "GET" };
        const delay = this.fetchDelayRules.find((r) => r.match(req.url, req.method));
        if (delay) {
          setTimeout(() => { this.browser.send("Fetch.continueRequest", { requestId }, this.sessionId).catch(() => null); }, delay.ms);
          return;
        }
        const rule = this.fetchFailRules.find((r) => r.match(req.url, req.method));
        if (rule) this.browser.send("Fetch.failRequest", { requestId, errorReason: rule.errorReason || "Failed" }, this.sessionId).catch(() => null);
        else this.browser.send("Fetch.continueRequest", { requestId }, this.sessionId).catch(() => null);
      }
    });
  }

  static async create(browser, { url = "about:blank" } = {}) {
    const created = await browser.send("Target.createTarget", { url });
    const attached = await browser.send("Target.attachToTarget", { targetId: created.targetId, flatten: true });
    const page = new Page(browser, attached.sessionId, created.targetId);
    await browser.send("Page.enable", {}, attached.sessionId);
    await browser.send("Runtime.enable", {}, attached.sessionId);
    await browser.send("Page.setInterceptFileChooserDialog", { enabled: true }, attached.sessionId).catch(() => null);
    return page;
  }

  _armNav() {
    this.navPromise = new Promise((resolve) => {
      const off = this.browser.on((msg) => {
        if (msg.sessionId === this.sessionId && msg.method === "Page.loadEventFired") { off(); resolve(); }
      });
      setTimeout(() => { off(); resolve(); }, 45000);
    });
  }

  async goto(url) {
    this._armNav();
    await this.browser.send("Page.navigate", { url }, this.sessionId);
    await this.navPromise;
    await sleep(400);
    return true;
  }

  async reload() {
    this._armNav();
    await this.browser.send("Page.reload", {}, this.sessionId);
    await this.navPromise;
    await sleep(400);
  }

  async setViewport(width, height) {
    await this.browser.send("Emulation.setDeviceMetricsOverride", {
      width, height, deviceScaleFactor: 1, mobile: false,
    }, this.sessionId);
    await sleep(250);
  }

  async eval(expression) {
    const r = await this.browser.send("Runtime.evaluate", {
      expression, returnByValue: true, awaitPromise: true, userGesture: true,
    }, this.sessionId);
    if (r.exceptionDetails) {
      const d = r.exceptionDetails;
      throw new Error("页面脚本异常：" + ((d.exception && d.exception.description) || d.text));
    }
    return r.result ? r.result.value : undefined;
  }

  async waitFor(expression, { timeout = 20000, step = 150, label = "" } = {}) {
    const deadline = Date.now() + timeout;
    let last = null;
    while (Date.now() < deadline) {
      try {
        last = await this.eval("(function(){ try { return (" + expression + "); } catch (e) { return null; } })()");
      } catch (error) { last = "ERR:" + String(error).slice(0, 120); }
      if (last) return last;
      await sleep(step);
    }
    throw new Error("等待超时（" + (label || expression).slice(0, 80) + "），最后值=" + JSON.stringify(last));
  }

  async count(selector) {
    const n = await this.eval("document.querySelectorAll(" + JSON.stringify(selector) + ").length");
    return typeof n === "number" ? n : 0;
  }

  /** 元素盒模型 + 中心点命中事实（真实点击前的地基，也给 ④ 的「中心与命中」断言用）。 */
  async box(selector, index = 0) {
    const js = "(function(){" +
      "const el = document.querySelectorAll(" + JSON.stringify(selector) + ")[" + index + "];" +
      "if (!el) return null;" +
      "el.scrollIntoView({ block: 'center', inline: 'center' });" +
      "const r = el.getBoundingClientRect();" +
      "const cx = r.x + r.width / 2, cy = r.y + r.height / 2;" +
      "const hit = document.elementFromPoint(cx, cy);" +
      "const st = getComputedStyle(el);" +
      "return { x: r.x, y: r.y, width: r.width, height: r.height, cx: cx, cy: cy," +
      " visible: r.width > 0 && r.height > 0 && st.visibility !== 'hidden' && st.display !== 'none' && Number(st.opacity) > 0.05," +
      " inViewport: cx >= 0 && cy >= 0 && cx <= innerWidth && cy <= innerHeight," +
      " hitTag: hit ? hit.tagName.toLowerCase() : null," +
      " hitIm: hit && hit.closest('[data-im]') ? hit.closest('[data-im]').getAttribute('data-im') : null," +
      " hitIsSelf: !!(hit && (hit === el || el.contains(hit)))," +
      " disabled: !!el.disabled," +
      " text: (el.innerText || el.textContent || '').trim().slice(0, 80) };" +
      "})()";
    return this.eval(js);
  }

  async click(selector, index = 0, { timeout = 20000 } = {}) {
    const deadline = Date.now() + timeout;
    let info = null;
    let lastError = null;
    while (Date.now() < deadline) {
      try {
        info = await this.box(selector, index);
        if (info && info.visible && !info.disabled) break;
      } catch (error) { lastError = error; }
      await sleep(150);
    }
    if (!info || !info.visible) throw new Error("点不到（不可见）：" + selector + " #" + index + (lastError ? " / " + lastError.message : ""));
    return this.clickAt(info.cx, info.cy, info);
  }

  async clickAt(x, y, info) {
    const base = { x: x, y: y, button: "left", buttons: 1, clickCount: 1 };
    await this.browser.send("Input.dispatchMouseEvent", { type: "mouseMoved", x: x, y: y, button: "none", buttons: 0 }, this.sessionId);
    await this.browser.send("Input.dispatchMouseEvent", { type: "mousePressed", ...base }, this.sessionId);
    await sleep(35);
    await this.browser.send("Input.dispatchMouseEvent", { type: "mouseReleased", ...base, buttons: 0 }, this.sessionId);
    await sleep(150);
    return info || { x: x, y: y };
  }

  /** 真实双击（同一坐标两轮 press/release，clickCount 1→2）。 */
  async doubleClick(selector, index = 0) {
    const info = await this.box(selector, index);
    if (!info || !info.visible) throw new Error("双击目标不可见：" + selector);
    const x = info.cx, y = info.cy;
    const base = { x: x, y: y, button: "left", buttons: 1 };
    await this.browser.send("Input.dispatchMouseEvent", { type: "mouseMoved", x: x, y: y, button: "none", buttons: 0 }, this.sessionId);
    await this.browser.send("Input.dispatchMouseEvent", { type: "mousePressed", ...base, clickCount: 1 }, this.sessionId);
    await this.browser.send("Input.dispatchMouseEvent", { type: "mouseReleased", ...base, buttons: 0, clickCount: 1 }, this.sessionId);
    await sleep(50);
    await this.browser.send("Input.dispatchMouseEvent", { type: "mousePressed", ...base, clickCount: 2 }, this.sessionId);
    await this.browser.send("Input.dispatchMouseEvent", { type: "mouseReleased", ...base, buttons: 0, clickCount: 2 }, this.sessionId);
    await sleep(150);
    return info;
  }

  async focus(selector, index = 0) {
    const ok = await this.eval("(function(){" +
      "const el = document.querySelectorAll(" + JSON.stringify(selector) + ")[" + index + "];" +
      "if (!el) return false; el.focus(); return document.activeElement === el; })()");
    if (!ok) throw new Error("聚焦失败：" + selector);
    return true;
  }

  async type(selector, text) {
    await this.focus(selector);
    await this.browser.send("Input.insertText", { text }, this.sessionId);
    await this.eval("(function(){ const el = document.activeElement; if (el) el.dispatchEvent(new Event('input', { bubbles: true })); return true; })()");
    await sleep(150);
    return this.inputValue(selector);
  }

  async inputValue(selector) {
    return this.eval("(function(){ const el = document.querySelector(" + JSON.stringify(selector) + "); return el ? (el.value || '') : ''; })()");
  }

  async pressKey(key) {
    const table = {
      Escape: { vk: 27, code: "Escape" },
      Enter: { vk: 13, code: "Enter", text: "\r" },
      Tab: { vk: 9, code: "Tab" },
      Backspace: { vk: 8, code: "Backspace" },
      ArrowUp: { vk: 38, code: "ArrowUp" },
      ArrowDown: { vk: 40, code: "ArrowDown" },
      ArrowLeft: { vk: 37, code: "ArrowLeft" },
      ArrowRight: { vk: 39, code: "ArrowRight" },
    }[key];
    if (!table) throw new Error("未支持的按键：" + key);
    await this.browser.send("Input.dispatchKeyEvent", { type: "rawKeyDown", key: key, code: table.code, windowsVirtualKeyCode: table.vk }, this.sessionId);
    if (table.text) {
      await this.browser.send("Input.dispatchKeyEvent", { type: "char", key: key, text: table.text, unmodifiedText: table.text }, this.sessionId).catch(() => null);
    }
    await this.browser.send("Input.dispatchKeyEvent", { type: "keyUp", key: key, code: table.code, windowsVirtualKeyCode: table.vk }, this.sessionId);
    await sleep(150);
  }

  /** 模拟输入法组合输入（compositionstart/update/end + 真实 input 事件）。 */
  async imeCompose(selector, committed) {
    const js = "(function(){" +
      "const el = document.querySelector(" + JSON.stringify(selector) + ");" +
      "if (!el) return { ok: false };" +
      "el.focus();" +
      "el.dispatchEvent(new CompositionEvent('compositionstart', { bubbles: true, data: '' }));" +
      "el.dispatchEvent(new CompositionEvent('compositionupdate', { bubbles: true, data: 'ni' }));" +
      "el.value = 'ni';" +
      "el.dispatchEvent(new InputEvent('input', { bubbles: true, data: 'ni', inputType: 'insertCompositionText', isComposing: true }));" +
      "el.dispatchEvent(new CompositionEvent('compositionend', { bubbles: true, data: " + JSON.stringify(committed) + " }));" +
      "el.value = " + JSON.stringify(committed) + ";" +
      "el.dispatchEvent(new InputEvent('input', { bubbles: true, data: " + JSON.stringify(committed) + ", inputType: 'insertFromComposition' }));" +
      "return { ok: true, value: el.value };" +
      "})()";
    return this.eval(js);
  }

  async screenshot(path) {
    const r = await this.browser.send("Page.captureScreenshot", { format: "png" }, this.sessionId);
    writeFileSync(path, Buffer.from(r.data, "base64"));
    return path;
  }

  /** 真实 DataTransfer 拖放：按 dataUrl 造 File，再派发 dragenter/dragover/drop。 */
  async dragFiles(selector, files) {
    const js = "(async function(){" +
      "const target = document.querySelector(" + JSON.stringify(selector) + ");" +
      "if (!target) return { ok: false, reason: 'no-target' };" +
      "const dt = new DataTransfer();" +
      "const specs = " + JSON.stringify(files) + ";" +
      "for (const spec of specs) {" +
      "  const res = await fetch(spec.dataUrl);" +
      "  const blob = await res.blob();" +
      "  dt.items.add(new File([blob], spec.name, { type: spec.type || blob.type }));" +
      "}" +
      "const fire = function(type){ target.dispatchEvent(new DragEvent(type, { bubbles: true, cancelable: true, dataTransfer: dt })); };" +
      "fire('dragenter'); fire('dragover'); fire('drop');" +
      "return { ok: true, count: dt.items.length };" +
      "})()";
    return this.eval(js);
  }

  /** 等页面的 input[type=file].click() 被拦截，然后把文件塞给它（等价于用户选完文件）。 */
  async setFilesOnChooser(files, { timeout = 15000 } = {}) {
    const params = await new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error("没有等到文件选择器")), timeout);
      this.fileChooserWaiters.push((p) => { clearTimeout(timer); resolve(p); });
    });
    await this.browser.send("DOM.setFileInputFiles", { files: files, backendNodeId: params.backendNodeId }, this.sessionId);
    return params;
  }

  /** 网络故障模拟：命中 match(url, method) 的请求直接失败（等价于 Playwright route.abort）。 */
  async failRequests(match, { errorReason = "Failed" } = {}) {
    if (this.fetchFailRules.length === 0) {
      await this.browser.send("Fetch.enable", { patterns: [{ urlPattern: "*", requestStage: "Request" }] }, this.sessionId);
    }
    this.fetchFailRules.push({ match: match, errorReason: errorReason });
    return this.fetchFailRules.length;
  }

  /** 延迟网络回执（[模拟] 等价 Playwright 的 route 延迟 continue），用于迟到回执验收。 */
  async delayRequests(match, ms) {
    if (this.fetchFailRules.length === 0 && this.fetchDelayRules.length === 0) {
      await this.browser.send("Fetch.enable", { patterns: [{ urlPattern: "*", requestStage: "Request" }] }, this.sessionId);
    }
    this.fetchDelayRules.push({ match: match, ms: ms });
    return this.fetchDelayRules.length;
  }

  async clearFailRequests() {
    this.fetchFailRules.length = 0;
    this.fetchDelayRules.length = 0;
    await this.browser.send("Fetch.disable", {}, this.sessionId).catch(() => null);
  }

  async text(selector, index = 0) {
    const t = await this.eval("(function(){ const el = document.querySelectorAll(" + JSON.stringify(selector) + ")[" + index + "]; return el ? (el.innerText || el.textContent || '').trim() : null; })()");
    return t || "";
  }

  async texts(selector) {
    const t = await this.eval("(function(){ return [...document.querySelectorAll(" + JSON.stringify(selector) + ")].map(function(el){ return (el.innerText || el.textContent || '').trim(); }); })()");
    return t || [];
  }

  async attr(selector, name, index = 0) {
    return this.eval("(function(){ const el = document.querySelectorAll(" + JSON.stringify(selector) + ")[" + index + "]; return el ? el.getAttribute(" + JSON.stringify(name) + ") : null; })()");
  }

  async exists(selector) {
    return (await this.count(selector)) > 0;
  }
}