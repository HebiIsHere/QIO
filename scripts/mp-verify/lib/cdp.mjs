/**
 * 验收用的 CDP 浏览器驱动（Edge / Chrome 均可，全部可被环境变量覆盖）。
 *
 * 覆盖项：
 *   QIO_MP_BROWSER    浏览器可执行文件（默认先找 Edge，再找 Chrome）
 *   QIO_MP_CDP_PORT   调试端口（默认 9801；多人同时跑时各用各的）
 *   QIO_MP_PROFILE    用户目录（默认 %TEMP%\qio-mp-verify\profile-<port>）
 *   QIO_MP_SHOTS      截图输出根目录（默认 %TEMP%\qio-mp-verify\shots）
 *
 * 为什么不直接用 Playwright：仓库里既有的 planet-frames.mjs 把 Playwright 的绝对路径
 * 写死了，换台机器就废；而且它不提供「按元素裁剪 / 等待条件 / 逐帧采样 / 录屏」。
 */
import { spawn, spawnSync } from "node:child_process";
import { existsSync, mkdirSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { setTimeout as sleep } from "node:timers/promises";

const EDGE = "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe";
const CHROME = "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe";
const CHROME_X86 = "C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe";

export function browserPath() {
  if (process.env.QIO_MP_BROWSER) return process.env.QIO_MP_BROWSER;
  for (const candidate of [EDGE, CHROME, CHROME_X86]) {
    if (existsSync(candidate)) return candidate;
  }
  throw new Error("找不到 Edge / Chrome：用 QIO_MP_BROWSER 指定可执行文件");
}

export const tempRoot = () =>
  (process.env.QIO_MP_TEMP || join(process.env.TEMP || process.cwd(), "qio-mp-verify"));

export class Browser {
  constructor(options = {}) {
    this.port = Number(options.port ?? process.env.QIO_MP_CDP_PORT ?? 9801);
    this.profile = options.profile ?? join(tempRoot(), "profile-" + this.port);
    this.outDir = options.outDir ?? join(process.env.QIO_MP_SHOTS || join(tempRoot(), "shots"));
    this.width = options.width ?? 1440;
    this.height = options.height ?? 900;
    this.httpFails = [];
    this.consoleErrors = [];
    this.screencast = null;
    this.msgId = 0;
    this.pending = new Map();
    this.ws = null;
    this.child = null;
  }

  /** 上一轮异常退出可能留下占着调试端口的僵尸实例：先把同一 profile 的清掉。 */
  killStale() {
    if (process.platform !== "win32") return;
    spawnSync(
      "powershell",
      [
        "-NoProfile",
        "-Command",
        "Get-CimInstance Win32_Process -Filter \"Name='msedge.exe' or Name='chrome.exe'\" | Where-Object { $_.CommandLine -like '*" +
          this.profile +
          "*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }",
      ],
      { encoding: "utf8" },
    );
  }

  async launch() {
    this.killStale();
    mkdirSync(this.profile, { recursive: true });
    mkdirSync(this.outDir, { recursive: true });
    this.child = spawn(
      browserPath(),
      [
        "--headless=new",
        "--remote-debugging-port=" + this.port,
        "--user-data-dir=" + this.profile,
        "--no-first-run",
        "--no-default-browser-check",
        "--hide-scrollbars",
        // 无 GPU 的机器上用 SwiftShader 跑 WebGL2：星球场景必须在真渲染下验收
        "--use-gl=angle",
        "--use-angle=swiftshader",
        "--enable-unsafe-swiftshader",
        "--disable-features=CalculateNativeWinOcclusion",
        "--window-size=" + this.width + "," + this.height,
        "--force-device-scale-factor=1",
        "about:blank",
      ],
      { stdio: "ignore" },
    );
    let ready = false;
    for (let i = 0; i < 160 && !ready; i += 1) {
      try {
        const resp = await fetch("http://127.0.0.1:" + this.port + "/json/version", {
          signal: AbortSignal.timeout(1500),
        });
        ready = resp.ok;
      } catch {
        /* 还没起来 */
      }
      if (!ready) await sleep(250);
    }
    if (!ready) throw new Error("浏览器调试端口没起来：" + this.port);
    const tab = await (
      await fetch("http://127.0.0.1:" + this.port + "/json/new?about:blank", { method: "PUT" })
    ).json();
    this.ws = new WebSocket(tab.webSocketDebuggerUrl);
    await new Promise((resolve, reject) => {
      this.ws.addEventListener("open", resolve);
      this.ws.addEventListener("error", reject);
    });
    this.ws.addEventListener("message", (event) => this.onMessage(JSON.parse(event.data)));
    for (const domain of [
      "Runtime.enable",
      "Log.enable",
      "Page.enable",
      "Network.enable",
      "DOM.enable",
      "Performance.enable",
    ]) {
      await this.send(domain);
    }
    return this;
  }

  onMessage(msg) {
    if (msg.id && this.pending.has(msg.id)) {
      const { resolve, reject } = this.pending.get(msg.id);
      this.pending.delete(msg.id);
      if (msg.error) reject(new Error(JSON.stringify(msg.error)));
      else resolve(msg.result);
      return;
    }
    if (msg.method === "Network.responseReceived") {
      const response = msg.params.response;
      if (response.status >= 400) this.httpFails.push(response.status + " " + response.url);
    }
    if (msg.method === "Network.loadingFailed") {
      this.httpFails.push("FAILED " + msg.params.errorText);
    }
    if (msg.method === "Runtime.exceptionThrown") {
      this.consoleErrors.push(String(msg.params.exceptionDetails?.text ?? ""));
    }
    if (msg.method === "Log.entryAdded" && msg.params.entry.level === "error") {
      this.consoleErrors.push(String(msg.params.entry.text ?? ""));
    }
    if (msg.method === "Page.screencastFrame" && this.screencast) {
      const { data, sessionId, metadata } = msg.params;
      this.send("Page.screencastFrameAck", { sessionId }).catch(() => {});
      this.screencast.frames.push({ data, timestamp: metadata?.timestamp ?? 0 });
      if (this.screencast.onFrame) this.screencast.onFrame(this.screencast.frames.length);
    }
  }

  send(method, params = {}) {
    const id = ++this.msgId;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      this.ws.send(JSON.stringify({ id, method, params }));
    });
  }

  async setViewport(width, height) {
    this.width = width;
    this.height = height;
    await this.send("Emulation.setDeviceMetricsOverride", {
      width,
      height,
      deviceScaleFactor: 1,
      mobile: false,
    });
    await sleep(300);
  }

  async navigate(url, waitMs = 2500) {
    await this.send("Page.navigate", { url });
    await sleep(waitMs);
  }

  async reload(waitMs = 2500) {
    await this.send("Page.reload", { ignoreCache: false });
    await sleep(waitMs);
  }

  async evalJs(js) {
    const result = await this.send("Runtime.evaluate", {
      expression: js,
      awaitPromise: true,
      returnByValue: true,
    });
    if (result.exceptionDetails) {
      throw new Error(
        "页面脚本异常：" +
          result.exceptionDetails.text +
          " " +
          (result.exceptionDetails.exception?.description ?? ""),
      );
    }
    return result.result?.value;
  }

  async waitFor(expression, { timeout = 15000, interval = 120 } = {}) {
    const deadline = Date.now() + timeout;
    let last = null;
    while (Date.now() < deadline) {
      last = await this.evalJs("(() => { try { return (" + expression + "); } catch (e) { return 'ERR:' + e.message; } })()");
      if (last === true) return true;
      if (typeof last === "string" && last.startsWith("ERR:")) throw new Error(last);
      await sleep(interval);
    }
    throw new Error("等待条件超时（" + timeout + "ms）：" + expression + "（最后结果 " + JSON.stringify(last) + "）");
  }

  /** 等 n 个真实的 rAF 过去（比 sleep 稳：软件光栅下每帧可能几十毫秒）。 */
  async settleFrames(count = 3, maxWaitMs = 4000) {
    const started = Date.now();
    await this.evalJs(
      "new Promise((resolve) => { let n = " +
        count +
        "; const tick = () => { n -= 1; if (n <= 0) resolve(true); else requestAnimationFrame(tick); }; requestAnimationFrame(tick); })",
    );
    const elapsed = Date.now() - started;
    if (elapsed > maxWaitMs) return elapsed;
    return elapsed;
  }

  async screenshotPng(clip, settle = false) {
    const params = { format: "png" };
    if (clip) {
      params.clip = {
        x: Math.max(0, clip.x),
        y: Math.max(0, clip.y),
        width: Math.max(1, clip.width),
        height: Math.max(1, clip.height),
        scale: 1,
      };
      params.captureBeyondViewport = false;
    }
    if (settle) {
      await this.send("Page.captureScreenshot", { format: "png" });
      await sleep(160);
    }
    const shot = await this.send("Page.captureScreenshot", params);
    return Buffer.from(shot.data, "base64");
  }

  async shotFile(name, clip, settle = true) {
    const png = await this.screenshotPng(clip, settle);
    const file = name.match(/\.png$/) ? name : name + ".png";
    mkdirSync(dirname(file), { recursive: true });
    writeFileSync(file, png);
    return file;
  }

  /** 元素矩形（视口坐标，CSS 像素）。 */
  async rectOf(selector) {
    return this.evalJs(
      "(() => { const el = document.querySelector(" +
        JSON.stringify(selector) +
        "); if (!el) return null; const r = el.getBoundingClientRect(); return { x: r.x, y: r.y, width: r.width, height: r.height, left: r.left, top: r.top, right: r.right, bottom: r.bottom }; })()",
    );
  }

  /** 真实指针点击（pointerdown/up）：星球画布的点击判定依赖 pointer 事件，DOM.click() 不算。 */
  async clickAt(x, y, { button = "left" } = {}) {
    await this.send("Input.dispatchMouseEvent", { type: "mouseMoved", x, y, button: "none" });
    await sleep(20);
    await this.send("Input.dispatchMouseEvent", {
      type: "mousePressed",
      x,
      y,
      button,
      buttons: 1,
      clickCount: 1,
    });
    await sleep(40);
    await this.send("Input.dispatchMouseEvent", {
      type: "mouseReleased",
      x,
      y,
      button,
      buttons: 0,
      clickCount: 1,
    });
  }

  async clickSelector(selector, opts = {}) {
    const rect = await this.rectOf(selector);
    if (!rect || rect.width <= 0 || rect.height <= 0) {
      throw new Error("找不到可点击元素：" + selector);
    }
    await this.clickAt(rect.x + rect.width / 2, rect.y + rect.height / 2, opts);
    return rect;
  }

  /** 放慢/加快 CSS 动画与过渡（注意：**不影响** JS 的 rAF 动画，别混用两条时间线）。 */
  async setAnimationPlaybackRate(rate) {
    await this.evalJs(
      "(() => { for (const a of document.getAnimations()) { try { a.playbackRate = " + Number(rate) + "; } catch (e) {} } return true; })()",
    );
    return rate;
  }

  async metrics() {
    const result = await this.send("Performance.getMetrics");
    const out = {};
    for (const metric of result.metrics ?? []) out[metric.name] = metric.value;
    return out;
  }

  // -- 录屏（CDP Page.startScreencast：真实录屏，不是逐帧截图拼接）--------------
  async startScreencast({ format = "jpeg", quality = 80, maxWidth, maxHeight, everyNthFrame = 1 } = {}) {
    this.screencast = { frames: [], onFrame: null };
    await this.send("Page.startScreencast", {
      format,
      quality,
      maxWidth: maxWidth ?? this.width,
      maxHeight: maxHeight ?? this.height,
      everyNthFrame,
    });
    return this.screencast;
  }

  stopScreencast() {
    const collected = this.screencast;
    this.screencast = null;
    return this.send("Page.stopScreencast")
      .catch(() => {})
      .then(() => collected);
  }

  async close() {
    try {
      this.ws?.close();
    } catch {
      /* 忽略 */
    }
    this.child?.kill();
    await sleep(200);
  }
}
