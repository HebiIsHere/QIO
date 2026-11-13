/**
 * 星球 032 验收探针：在 scripts/mp-verify/lib/cdp.mjs 的 Browser 上加三件事——
 * 1. CDP Fetch 拦截（模拟单个来源 503 / 延迟 / 请求体抓取，不改产品代码）；
 * 2. 真实键盘事件（Esc 分层、输入法组字优先）；
 * 3. 少量 DOM 读写助手（按 selector / 文本点击、读取可见文本）。
 *
 * 只用于验收；不参与产品构建。
 */
import { Browser } from "../../mp-verify/lib/cdp.mjs";
import { setTimeout as sleep } from "node:timers/promises";

export class ProbeBrowser extends Browser {
  constructor(options = {}) {
    super(options);
    /** @type {{id:string, urlPart:string, method?:string, action:"fail"|"status"|"delay"|"capture", status?:number, body?:string, delayMs?:number, remaining?:number}[]} */
    this.rules = [];
    this.pausedRequests = [];
    this.pauseLog = [];
    this.captured = [];
    this.fetchHandler = null;
  }

  onMessage(msg) {
    if (msg.method === "Network.requestWillBeSent" && msg.params.request.url.indexOf("/api/") >= 0) {
      this.pauseLog.push({ req: msg.params.request.url.replace(/^https?:\/\/[^/]+/, ""), ts: Date.now() });
    }
    if (msg.method === "Fetch.requestPaused") {
      void this.onPaused(msg.params);
      return;
    }
    super.onMessage(msg);
  }

  async onPaused(params) {
    try {
      await this.handlePaused(params);
    } catch (error) {
      this.pauseLog.push({ error: String(error && error.message ? error.message : error), url: params.request?.url });
      try { await this.send("Fetch.continueRequest", { requestId: params.requestId }); } catch { /* ignore */ }
    }
  }

  async handlePaused(params) {
    const url = params.request?.url ?? "";
    const method = params.request?.method ?? "GET";
    this.pauseLog.push({ url, method });
    if (this.fetchHandler) {
      const handled = await this.fetchHandler(params, url, method);
      if (handled) return;
    }
    const rule = this.rules.find(
      (r) =>
        url.includes(r.urlPart) &&
        (!r.method || r.method === method) &&
        (r.remaining === undefined || r.remaining > 0),
    );
    if (!rule) {
      await this.send("Fetch.continueRequest", { requestId: params.requestId });
      return;
    }
    if (rule.remaining !== undefined) rule.remaining -= 1;
    this.pauseLog.push({ url, matched: rule.action, remaining: rule.remaining });
    if (rule.action === "capture") {
      this.captured.push({ url, method, postData: params.request?.postData ?? null });
      await this.send("Fetch.continueRequest", { requestId: params.requestId });
      return;
    }
    if (rule.delayMs) await sleep(rule.delayMs);
    if (rule.action === "fail") {
      await this.send("Fetch.failRequest", { requestId: params.requestId, errorReason: "Failed" });
      return;
    }
    if (rule.action === "status") {
      const body = Buffer.from(rule.body ?? JSON.stringify({ detail: "验收注入失败" }), "utf8").toString("base64");
      await this.send("Fetch.fulfillRequest", {
        requestId: params.requestId,
        responseCode: rule.status ?? 503,
        responseHeaders: [
          { name: "Content-Type", value: "application/json" },
          // 跨源请求必须带 CORS 头：否则浏览器把 503 当网络层失败（TypeError → 读请求自动重试），
          // 验收注入就不再是「服务端明确 503」了。
          { name: "Access-Control-Allow-Origin", value: "*" },
          { name: "Access-Control-Allow-Headers", value: "*" },
          { name: "Access-Control-Allow-Methods", value: "GET,POST,OPTIONS" },
        ],
        body,
      });
      return;
    }
    await this.send("Fetch.continueRequest", { requestId: params.requestId });
  }

  async enableFetch() {
    await this.send("Fetch.enable", { patterns: [{ urlPattern: "*" }] });
  }

  async disableFetch() {
    await this.send("Fetch.disable");
  }

  /** 注入一条请求规则（默认只生效一次）。 */
  rule(rule) {
    this.rules.push({ remaining: 1, ...rule });
    return this;
  }

  clearRules() {
    this.rules = [];
    return this;
  }

  /** 真实键盘按键（keydown + keyup）；escdown 会走页面 window 监听。 */
  async pressKey(key, { code = key, windowsVirtualKeyCode, modifiers = 0 } = {}) {
    const vk = windowsVirtualKeyCode ?? (key === "Escape" ? 27 : key === "Enter" ? 13 : key === "Tab" ? 9 : undefined);
    await this.send("Input.dispatchKeyEvent", { type: "keyDown", key, code, windowsVirtualKeyCode: vk, nativeVirtualKeyCode: vk, modifiers });
    await sleep(30);
    await this.send("Input.dispatchKeyEvent", { type: "keyUp", key, code, windowsVirtualKeyCode: vk, nativeVirtualKeyCode: vk, modifiers });
    await sleep(60);
  }

  /** 输入法组字中的 Esc：合成事件带 isComposing=true（只有组字中的 Esc 才该交给输入法）。 */
  async escapeWhileComposing() {
    return this.evalJs(`(() => {
      const target = document.activeElement || document.body;
      const ev = new KeyboardEvent("keydown", { key: "Escape", code: "Escape", bubbles: true, cancelable: true, isComposing: true });
      Object.defineProperty(ev, "keyCode", { get: () => 229 });
      target.dispatchEvent(ev);
      return { composing: ev.isComposing, defaultPrevented: ev.defaultPrevented };
    })()`);
  }

  /** 设置输入框的值并触发 input 事件（v-model 监听的是 input）。 */
  async setInput(selector, text) {
    return this.evalJs(
      `(() => {
        const el = document.querySelector(${JSON.stringify(selector)});
        if (!el) return false;
        el.focus();
        const setter = Object.getOwnPropertyDescriptor(el.__proto__, "value")?.set || Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set;
        setter?.call(el, ${JSON.stringify(text)});
        el.dispatchEvent(new Event("input", { bubbles: true }));
        return true;
      })()`,
    );
  }

  async focus(selector) {
    return this.evalJs(`(() => { const el = document.querySelector(${JSON.stringify(selector)}); if (!el) return false; el.focus(); return document.activeElement === el; })()`);
  }

  async activeElement() {
    return this.evalJs(`(() => { const el = document.activeElement; return el ? { tag: el.tagName, cls: String(el.className || "").slice(0, 80), label: el.getAttribute?.("aria-label") || null, data: el.getAttribute?.("data-mp-search-open") !== null } : null; })()`);
  }

  async textOf(selector) {
    return this.evalJs(`(() => { const el = document.querySelector(${JSON.stringify(selector)}); return el ? el.textContent.replace(/\\s+/g, " ").trim() : null; })()`);
  }

  async count(selector) {
    return this.evalJs(`document.querySelectorAll(${JSON.stringify(selector)}).length`);
  }

  async exists(selector) {
    return this.evalJs(`!!document.querySelector(${JSON.stringify(selector)})`);
  }

  async visible(selector) {
    return this.evalJs(`(() => { const el = document.querySelector(${JSON.stringify(selector)}); if (!el) return false; const r = el.getBoundingClientRect(); const cs = getComputedStyle(el); return r.width > 0 && r.height > 0 && cs.visibility !== "hidden" && cs.display !== "none"; })()`);
  }

  /** 在 selector 范围内按可见文本点按钮（找不到返回 false）。 */
  async clickByText(container, text) {
    const ok = await this.evalJs(
      `(() => {
        const root = document.querySelector(${JSON.stringify(container)});
        if (!root) return false;
        const target = [...root.querySelectorAll("button")].find((b) => b.textContent.replace(/\\s+/g, "").includes(${JSON.stringify(text)}));
        if (!target) return false;
        target.click();
        return true;
      })()`,
    );
    await sleep(120);
    return ok;
  }

  /** 截当前视口到文件。 */
  async shot(name) {
    const { mkdirSync } = await import("node:fs");
    const { dirname } = await import("node:path");
    mkdirSync(dirname(name), { recursive: true });
    return this.shotFile(name);
  }
}

export { sleep };