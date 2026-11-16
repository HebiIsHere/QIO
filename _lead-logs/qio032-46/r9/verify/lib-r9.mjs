/**
 * QIO #46 r9 真实窗口验收库：连接已安装 QIO 的 WebView2 CDP（不改产品代码）。
 * 基于 scripts/verify-pm-qio032/cdp.mjs 的 CdpBrowser（Node 24 自带 WebSocket）。
 */
import { pathToFileURL } from "node:url";
import { writeFileSync, mkdirSync } from "node:fs";
import { dirname, join } from "node:path";

const CDP_SRC = process.env.QIO_CDP_SRC || "D:/qio-dev/q46-r9/scripts/verify-pm-qio032/cdp.mjs";
const cdp = await import(pathToFileURL(CDP_SRC).href);
const { CdpBrowser } = cdp;

export const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

export async function connectApp({ port, matchUrl = "tauri" } = {}) {
  const version = await (await fetch("http://127.0.0.1:" + port + "/json/version")).json();
  const ws = new WebSocket(version.webSocketDebuggerUrl);
  await new Promise((res, rej) => {
    ws.addEventListener("open", res, { once: true });
    ws.addEventListener("error", () => rej(new Error("CDP WebSocket 连不上")), { once: true });
  });
  const browser = new CdpBrowser(ws, { kill() {} });
  const targets = await (await fetch("http://127.0.0.1:" + port + "/json/list")).json();
  const target = targets.find((t) => t.type === "page" && String(t.url).includes(matchUrl)) || targets.find((t) => t.type === "page");
  if (!target) throw new Error("CDP: 找不到页面 target");
  const attached = await browser.send("Target.attachToTarget", { targetId: target.id, flatten: true });
  const page = new AppPage(browser, attached.sessionId, target.id);
  await page.enable();
  return page;
}

export class AppPage {
  constructor(browser, sessionId, targetId) {
    this.browser = browser;
    this.sessionId = sessionId;
    this.targetId = targetId;
    this.rules = [];
    this.paused = [];
    this.consoleErrors = [];
    browser.on((msg) => this._onMessage(msg));
  }
  _onMessage(msg) {
    if (msg.sessionId !== this.sessionId) return;
    const p = msg.params || {};
    if (msg.method === "Runtime.consoleAPICalled" && p.type === "error") {
      this.consoleErrors.push((p.args || []).map((a) => a.value ?? a.description ?? "").join(" ").slice(0, 400));
    }
    if (msg.method === "Runtime.exceptionThrown") {
      const d = p.exceptionDetails || {};
      this.consoleErrors.push(("页面异常：" + ((d.exception && d.exception.description) || d.text || "")).slice(0, 400));
    }
    if (msg.method === "Fetch.requestPaused") this._onPaused(p);
  }
  async _onPaused(p) {
    const req = p.request || { url: "", method: "GET" };
    this.paused.push({ url: req.url, method: req.method, requestId: p.requestId, ts: Date.now() });
    const rule = this.rules.find((r) => r.match(req.url, req.method) && !r.used && !r.heldRequestId);
    if (!rule) { await this.browser.send("Fetch.continueRequest", { requestId: p.requestId }, this.sessionId).catch(() => null); return; }
    if (rule.once) rule.used = true;
    if (rule.hold) {
      // 手动闸门：把请求扣在这里，由场景显式 releaseHeld()（continue / fail / fulfill）。
      rule.heldRequestId = p.requestId;
      rule.heldAt = Date.now();
      return;
    }
    if (rule.delayMs != null) {
      await sleep(rule.delayMs);
      await this.browser.send("Fetch.continueRequest", { requestId: p.requestId }, this.sessionId).catch(() => null);
      return;
    }
    if (rule.failReason) {
      await this.browser.send("Fetch.failRequest", { requestId: p.requestId, errorReason: rule.failReason }, this.sessionId).catch(() => null);
      return;
    }
    if (rule.fulfill) {
      const f = rule.fulfill;
      const body = Buffer.from(typeof f.body === "string" ? f.body : JSON.stringify(f.body), "utf8").toString("base64");
      const headers = Object.entries(Object.assign({ "content-type": "application/json" }, f.headers || {}))
        .map(([k, v]) => ({ name: k, value: String(v) }));
      await this.browser.send("Fetch.fulfillRequest", { requestId: p.requestId, responseCode: f.status || 503, responseHeaders: headers, body }, this.sessionId).catch(() => null);
      return;
    }
    await this.browser.send("Fetch.continueRequest", { requestId: p.requestId }, this.sessionId).catch(() => null);
  }
  addRule(rule) { const r = Object.assign({ once: false, used: false, heldRequestId: null }, rule); this.rules.push(r); return r; }
  clearRules() { this.rules = []; }
  async releaseHeld(rule, action = { type: "continue" }) {
    const requestId = rule && rule.heldRequestId;
    if (!requestId) throw new Error("releaseHeld: 该规则当前没有扣住的请求");
    rule.heldRequestId = null;
    if (action.type === "fail") {
      await this.browser.send("Fetch.failRequest", { requestId, errorReason: action.errorReason || "Failed" }, this.sessionId);
      return true;
    }
    if (action.type === "fulfill") {
      const body = Buffer.from(typeof action.body === "string" ? action.body : JSON.stringify(action.body), "utf8").toString("base64");
      const headers = Object.entries(Object.assign({ "content-type": "application/json" }, action.headers || {}))
        .map(([k, v]) => ({ name: k, value: String(v) }));
      await this.browser.send("Fetch.fulfillRequest", { requestId, responseCode: action.status || 200, responseHeaders: headers, body }, this.sessionId);
      return true;
    }
    await this.browser.send("Fetch.continueRequest", { requestId }, this.sessionId);
    return true;
  }
  async enableFetch(patterns = [{ urlPattern: "*" }]) {
    await this.browser.send("Fetch.enable", { patterns: patterns.map((p) => Object.assign({ requestStage: "Request" }, p)) }, this.sessionId);
  }
  async disableFetch() { await this.browser.send("Fetch.disable", {}, this.sessionId).catch(() => null); }
  async enable() {
    await this.browser.send("Runtime.enable", {}, this.sessionId);
    await this.browser.send("Page.enable", {}, this.sessionId).catch(() => null);
    await sleep(300);
  }
  async eval(expression) {
    const r = await this.browser.send("Runtime.evaluate", { expression, returnByValue: true, awaitPromise: true, userGesture: true }, this.sessionId);
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
      try { last = await this.eval("(function(){ try { return (" + expression + "); } catch (e) { return null; } })()"); } catch { last = null; }
      if (last) return last;
      await sleep(step);
    }
    throw new Error("等待超时（" + (label || expression).slice(0, 100) + "），最后值=" + JSON.stringify(last).slice(0, 300));
  }
  async reload() { await this.browser.send("Page.reload", {}, this.sessionId); await sleep(1200);
    for (let i = 0; i < 100; i += 1) { try { if (await this.eval("document.readyState === \"complete\"")) return; } catch {} await sleep(150); } }
  async screenshot(file) { const r = await this.browser.send("Page.captureScreenshot", { format: "png" }, this.sessionId); if (r && r.data) { mkdirSync(dirname(file), { recursive: true }); writeFileSync(file, Buffer.from(r.data, "base64")); return true; } return false; }
  /** 访问页面 Pinia store（生产构建也可用；不依赖 dev server 源码 import）。 */
  async storeEval(expression) {
    const js = "(async function(){ var el=document.querySelector('#app'); var app=el&&el.__vue_app__; if(!app) throw new Error('no vue app'); var prov=app._context.provides; var vals=Reflect.ownKeys(prov).map(function(k){return prov[k];}); var pinia=vals.find(function(v){return v&&v._s&&typeof v._s.get==='function';}); if(!pinia) throw new Error('no pinia'); var s=pinia._s.get('interactive'); if(!s) throw new Error('no interactive store'); var __r = await (async function(s){ " + expression + " })(s); return __r; })()";
    return this.eval(js);
  }
  async storeState(extra = "") {
    const expr = "return { boardId: s.boardId, saveStatus: s.saveStatus, saveError: s.saveError, dirty: s.dirty, loadError: s.loadError, submitStatus: s.submitStatus, submitError: s.submitError, submitRefreshState: s.submitRefreshState, submitRefreshError: s.submitRefreshError, submitResultUnknown: s.submitResultUnknown, understandingStatus: s.understandingStatus, intentsCount: (s.intents||[]).length, cancelRecovery: s.cancelRecoveryByBoard ? s.cancelRecoveryByBoard : null" + (extra ? ", " + extra : "") + " };";
    return this.storeEval(expr);
  }
  async click(selector, index = 0) {
    const box = await this.eval("(function(){var el=document.querySelectorAll(" + JSON.stringify(selector) + ")[" + index + "]; if(!el) return null; el.scrollIntoView({block:'center'}); var r=el.getBoundingClientRect(); var st=getComputedStyle(el); return {x:r.x+r.width/2,y:r.y+r.height/2,visible:r.width>0&&r.height>0&&st.display!=='none'&&st.visibility!=='hidden'};})()");
    if (!box || !box.visible) throw new Error("点不到：" + selector);
    const b = { x: box.x, y: box.y, button: "left", buttons: 1, clickCount: 1 };
    await this.browser.send("Input.dispatchMouseEvent", { type: "mouseMoved", x: box.x, y: box.y, button: "none", buttons: 0 }, this.sessionId);
    await this.browser.send("Input.dispatchMouseEvent", { type: "mousePressed", ...b }, this.sessionId);
    await sleep(35);
    await this.browser.send("Input.dispatchMouseEvent", { type: "mouseReleased", ...b, buttons: 0 }, this.sessionId);
    await sleep(150);
  }
  async localStorageGet(key) { return this.eval("localStorage.getItem(" + JSON.stringify(key) + ")"); }
  async localStorageSet(key, value) { return this.eval("localStorage.setItem(" + JSON.stringify(key) + "," + JSON.stringify(value) + "), localStorage.getItem(" + JSON.stringify(key) + ")"); }
  async localStorageRemove(key) { return this.eval("localStorage.removeItem(" + JSON.stringify(key) + "), localStorage.getItem(" + JSON.stringify(key) + ")"); }
}

/** 后端 API（token 由被测实例的令牌文件提供）。 */
export function backendClient({ port, token }) {
  const base = "http://127.0.0.1:" + port;
  const call = async (method, path, body) => {
    const headers = { authorization: "Bearer " + token };
    if (body !== undefined) headers["content-type"] = "application/json";
    const res = await fetch(base + path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
    const text = await res.text();
    let data = null;
    try { data = text ? JSON.parse(text) : null; } catch { data = text; }
    return { status: res.status, ok: res.ok, data };
  };
  return { call, get: (p) => call("GET", p), post: (p, b) => call("POST", p, b), put: (p, b) => call("PUT", p, b), del: (p) => call("DELETE", p) };
}