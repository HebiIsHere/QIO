/**
 * 验收用 CDP 浏览器驱动：在 scripts/mp-verify/lib/cdp.mjs（已经在本机跑通过）之上补三件事，
 * 不重复实现 WebSocket / 截图 / 录屏：
 *
 * 1. **可编程的 API 替身**（Fetch 域拦截）：默认放行真实请求，只有显式 route 过的
 *    URL 才用固定响应（可选延迟 / 失败）。这样「分组保存失败」「迟到响应」这类
 *    时序证据可以确定性地复现，而不是靠真实后端碰运气。
 * 2. 键盘与输入：Tab / Enter / Escape / 文本输入走真实 CDP 输入事件，
 *    键盘可达性检查不能拿 DOM.focus() 冒充。
 * 3. 主题 / 减少动画偏好：写 localStorage 后重载，与产品读的是同一份偏好
 *    （qio-theme / qio-motion），不是伪造 html 属性。
 *
 * 替身边界：所有 stub 只在本机、只在浏览器侧，不发任何外网请求、不用真实 Key。
 */
import { Browser } from "../../mp-verify/lib/cdp.mjs";

const KEY_DEFS = {
  Tab: { code: "Tab", windowsVirtualKeyCode: 9, text: "\t" },
  Enter: { code: "Enter", windowsVirtualKeyCode: 13, text: "\r" },
  Escape: { code: "Escape", windowsVirtualKeyCode: 27, text: "" },
  ArrowDown: { code: "ArrowDown", windowsVirtualKeyCode: 40, text: "" },
  ArrowUp: { code: "ArrowUp", windowsVirtualKeyCode: 38, text: "" },
  ArrowLeft: { code: "ArrowLeft", windowsVirtualKeyCode: 37, text: "" },
  ArrowRight: { code: "ArrowRight", windowsVirtualKeyCode: 39, text: "" },
  Space: { code: "Space", windowsVirtualKeyCode: 32, text: " " },
  Home: { code: "Home", windowsVirtualKeyCode: 36, text: "" },
  End: { code: "End", windowsVirtualKeyCode: 35, text: "" },
};

export class VerifyBrowser extends Browser {
  constructor(options = {}) {
    super(options);
    this.apiBase = String(options.apiBase ?? "").replace(/\/$/, "");
    this.rules = [];
    this.log = [];
    this.intercepting = false;
    this.ruleSeq = 0;
    this.onRequest = options.onRequest ?? null;
    this.requestUrls = new Map();
    this.consoleErrorDetails = [];
  }

  onMessage(msg) {
    // 这两类事件自己记详细信息（带 URL / 异常堆栈），不再走基类的简版记录
    if (msg.method === "Runtime.exceptionThrown") {
      const details = msg.params.exceptionDetails ?? {};
      const text = String(details.exception?.description || details.text || "unknown-exception");
      this.consoleErrors.push(text);
      this.consoleErrorDetails.push({
        text: text.split("\n").slice(0, 3).join(" | "),
        url: details.url ?? null,
        line: details.lineNumber ?? null,
        column: details.columnNumber ?? null,
        stack: (details.stackTrace?.callFrames ?? []).slice(0, 5).map((frame) => frame.functionName + " @" + frame.url + ":" + frame.lineNumber),
      });
      return;
    }
    if (msg.method === "Network.loadingFailed") {
      const url = this.requestUrls.get(msg.params.requestId) || "";
      this.httpFails.push("FAILED " + msg.params.errorText + (url ? " " + url : ""));
      return;
    }
    super.onMessage(msg);
    if (msg.method === "Fetch.requestPaused") {
      this.handlePaused(msg.params).catch((error) => {
        // 拦截器自身出错不能把页面挂死：放行请求，并把原因记进日志。
        this.log.push({ ts: Date.now(), kind: "intercept-error", error: String(error) });
        this.send("Fetch.continueRequest", { requestId: msg.params.requestId }).catch(() => {});
      });
    } else if (msg.method === "Network.requestWillBeSent") {
      const { request, requestId } = msg.params;
      this.requestUrls.set(requestId, request.url);
      if (this.requestUrls.size > 2000) {
        // 长时间采集时避免无界增长：留最近 1000 条
        const keys = [...this.requestUrls.keys()].slice(0, this.requestUrls.size - 1000);
        for (const key of keys) this.requestUrls.delete(key);
      }
      this.log.push({ ts: Date.now(), kind: "request", requestId, method: request.method, url: request.url, postData: request.postData ?? null });
      if (this.onRequest) this.onRequest({ phase: "request", method: request.method, url: request.url, postData: request.postData ?? null });
    } else if (msg.method === "Network.responseReceived") {
      const { response, requestId } = msg.params;
      this.log.push({ ts: Date.now(), kind: "response", requestId, status: response.status, url: response.url });
    }
  }

  /** 只在真正用到替身时才开 Fetch 域，避免所有静态资源多走一层 CDP。 */
  async ensureIntercepting() {
    if (this.intercepting) return;
    const patterns = [];
    if (this.apiBase) patterns.push({ urlPattern: this.apiBase + "/api/*" });
    if (!patterns.length) patterns.push({ urlPattern: "*api*" });
    await this.send("Fetch.enable", { patterns });
    this.intercepting = true;
  }

  /**
   * 注册一条替身规则。规则按注册顺序匹配，第一个命中的生效。
   *   match: { method?: "GET"|"POST"|..., path: RegExp|string }  path 匹配 URL 的 pathname+search
   *   reply: async (request) => ({ status?, body?, headers?, delayMs?, abort? })
   * 返回取消函数。
   */
  async route(match, reply) {
    await this.ensureIntercepting();
    const rule = { id: ++this.ruleSeq, match, reply };
    this.rules.push(rule);
    return () => {
      const index = this.rules.indexOf(rule);
      if (index >= 0) this.rules.splice(index, 1);
    };
  }

  async clearRoutes() {
    this.rules = [];
    if (this.intercepting) {
      await this.send("Fetch.disable");
      this.intercepting = false;
    }
  }

  matchRule(method, url) {
    let pathname = url;
    try {
      const parsed = new URL(url);
      pathname = parsed.pathname + parsed.search;
    } catch {
      // 保持原样
    }
    for (const rule of this.rules) {
      const wantMethod = rule.match.method;
      if (wantMethod && String(wantMethod).toUpperCase() !== String(method).toUpperCase()) continue;
      const wantPath = rule.match.path;
      if (!wantPath) return rule;
      if (typeof wantPath === "string") {
        if (pathname.includes(wantPath) || url.includes(wantPath)) return rule;
      } else if (wantPath instanceof RegExp) {
        if (wantPath.test(pathname) || wantPath.test(url)) return rule;
      }
    }
    return null;
  }

  async handlePaused(params) {
    const { requestId, request } = params;
    const rule = this.matchRule(request.method, request.url);
    if (!rule) {
      await this.send("Fetch.continueRequest", { requestId });
      return;
    }
    const reply = await rule.reply({
      method: request.method,
      url: request.url,
      headers: request.headers,
      postData: request.postData ?? null,
    });
    if (reply && reply.delayMs) await new Promise((done) => setTimeout(done, reply.delayMs));
    if (reply && reply.abort) {
      this.log.push({ ts: Date.now(), kind: "stub-abort", url: request.url });
      await this.send("Fetch.failRequest", { requestId, errorReason: reply.errorReason ?? "Failed" });
      return;
    }
    const body = typeof reply?.body === "string" ? reply.body : JSON.stringify(reply?.body ?? {});
    const headers = Object.entries({
      "Content-Type": "application/json; charset=utf-8",
      "Access-Control-Allow-Origin": "*",
      ...(reply?.headers ?? {}),
    }).map(([name, value]) => ({ name, value: String(value) }));
    this.log.push({ ts: Date.now(), kind: "stub", method: request.method, url: request.url, status: reply?.status ?? 200, delayMs: reply?.delayMs ?? 0 });
    await this.send("Fetch.fulfillRequest", {
      requestId,
      responseCode: reply?.status ?? 200,
      responseHeaders: headers,
      body: Buffer.from(body, "utf8").toString("base64"),
    });
  }

  /** 只保留最近 N 条请求日志（长时间采集时避免无界增长）。 */
  trimLog(max = 4000) {
    if (this.log.length > max) this.log.splice(0, this.log.length - max);
  }

  requestEntries(filter = {}) {
    return this.log.filter((entry) => {
      if (entry.kind !== "request") return false;
      if (filter.method && String(filter.method).toUpperCase() !== entry.method) return false;
      if (filter.urlIncludes && !entry.url.includes(filter.urlIncludes)) return false;
      return true;
    });
  }

  /** 在每个新文档执行前注入脚本（原生对话框守卫、故障注入钩子等）。 */
  async addInitScript(source) {
    return this.send("Page.addScriptToEvaluateOnNewDocument", { source });
  }

  /**
   * 守卫 window.confirm / alert / prompt：只计数、不弹原生框。
   * 设置页要求危险操作走 QConfirm；「没有被调用过」必须是可观测事实，不能靠读代码判断。
   */
  async installDialogGuards() {
    const source = [
      "(() => {",
      "  window.__qioDialogCalls = { confirm: 0, alert: 0, prompt: 0, samples: [] };",
      "  const record = (kind) => { window.__qioDialogCalls[kind] += 1; if (window.__qioDialogCalls.samples.length < 20) window.__qioDialogCalls.samples.push(kind); };",
      "  window.confirm = function () { record('confirm'); return true; };",
      "  window.alert = function () { record('alert'); };",
      "  window.prompt = function () { record('prompt'); return null; };",
      "})();",
    ].join("\n");
    return this.addInitScript(source);
  }

  async enableNetworkLog() {
    // Browser.launch 已经 enable 了 Network；这里只说明语义，避免调用方以为没开。
    return true;
  }

  // -- 输入 / 键盘 ------------------------------------------------------------
  async pressKey(key, { shift = false } = {}) {
    const def = KEY_DEFS[key] ?? { code: key, windowsVirtualKeyCode: 0, text: "" };
    const modifiers = shift ? 8 : 0;
    const base = {
      key,
      code: def.code,
      windowsVirtualKeyCode: def.windowsVirtualKeyCode,
      nativeVirtualKeyCode: def.windowsVirtualKeyCode,
      modifiers,
    };
    await this.send("Input.dispatchKeyEvent", { type: "rawKeyDown", ...base, text: "" });
    if (def.text) {
      await this.send("Input.dispatchKeyEvent", { type: "char", ...base, text: def.text, unmodifiedText: def.text });
    }
    await this.send("Input.dispatchKeyEvent", { type: "keyUp", ...base });
  }

  async pressKeyRepeated(key, times) {
    for (let i = 0; i < times; i += 1) {
      await this.pressKey(key);
      await new Promise((done) => setTimeout(done, 40));
    }
  }

  /**
   * 往表单控件写值。用原生 setter + input/change 事件，与用户键入对 Vue v-model
   * 的效果一致；不用 el.value = ...（那样不会触发框架监听）。
   */
  async fill(selector, value) {
    return this.evalJs(
      "(() => { const el = document.querySelector(" + JSON.stringify(selector) + "); " +
        "if (!el) return { ok: false, reason: 'not-found' }; " +
        "if (el.disabled || el.readOnly) return { ok: false, reason: 'disabled' }; " +
        "el.focus(); " +
        "const proto = el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype; " +
        "const setter = Object.getOwnPropertyDescriptor(proto, 'value').set; " +
        "setter.call(el, " + JSON.stringify(String(value)) + "); " +
        "el.dispatchEvent(new Event('input', { bubbles: true })); " +
        "el.dispatchEvent(new Event('change', { bubbles: true })); " +
        "return { ok: true }; })()",
    );
  }

  /** 逐字符真实输入（走 Input.insertText，触发 beforeinput/input）。 */
  async typeText(text) {
    await this.send("Input.insertText", { text: String(text) });
  }

  async focus(selector) {
    return this.evalJs("(() => { const el = document.querySelector(" + JSON.stringify(selector) + "); if (!el) return false; el.focus(); return document.activeElement === el; })()");
  }

  async hover(selector) {
    const rect = await this.rectOf(selector);
    if (!rect || rect.width <= 0) throw new Error("找不到可悬停元素：" + selector);
    await this.send("Input.dispatchMouseEvent", { type: "mouseMoved", x: rect.x + rect.width / 2, y: rect.y + rect.height / 2, button: "none" });
    return rect;
  }

  // -- 偏好（主题 / 减少动画）------------------------------------------------
  async setStoredPreference(key, value) {
    await this.evalJs("(() => { try { localStorage.setItem(" + JSON.stringify(key) + ", " + JSON.stringify(String(value)) + "); return true; } catch (e) { return false; } })()");
  }

  async setTheme(theme, { reload = true } = {}) {
    await this.setStoredPreference("qio-theme", theme);
    if (reload) await this.reload(2200);
    return theme;
  }

  async setMotion(mode, { reload = true } = {}) {
    await this.setStoredPreference("qio-motion", mode);
    if (reload) await this.reload(2200);
    return mode;
  }

  async waitForSelector(selector, { timeout = 12000, visible = false } = {}) {
    return this.waitFor(
      "(() => { const el = document.querySelector(" + JSON.stringify(selector) + "); if (!el) return false; " +
        (visible ? "const r = el.getBoundingClientRect(); return !!(r.width && r.height) && getComputedStyle(el).visibility !== 'hidden';" : "return true;") +
        " })()",
      { timeout },
    );
  }

  async count(selector) {
    return this.evalJs("document.querySelectorAll(" + JSON.stringify(selector) + ").length");
  }

  async text(selector) {
    return this.evalJs("(() => { const el = document.querySelector(" + JSON.stringify(selector) + "); return el ? (el.innerText || el.textContent || '') : null; })()");
  }
}

export { Browser };
