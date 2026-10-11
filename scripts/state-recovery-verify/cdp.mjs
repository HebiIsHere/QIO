/**
 * 第八轮状态恢复收尾 · 独立验收 D 的 CDP 浏览器驱动（只给验收用，不参与产品打包）。
 *
 * 由 scripts/closure-c-verify/cdp.mjs 改写为**独立命名**版本，并把端口 / profile / 输出目录
 * 全部换成 QIO_SR_* 前缀，避免与其它工作区的采集互相抢端口。
 *
 * 环境变量：
 *   QIO_SR_BROWSER      浏览器可执行文件（默认先找 Edge，再找 Chrome）
 *   QIO_SR_PROBE_PORT   调试端口（默认 9788）
 *   QIO_SR_PROFILE      用户目录（默认 %TEMP%\qio-sr-edge）
 */
import { spawn, spawnSync } from "node:child_process";
import { existsSync, mkdirSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { setTimeout as sleep } from "node:timers/promises";
import { inflateSync } from "node:zlib";

const EDGE = "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe";
const CHROME = "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe";

export function browserPath() {
  if (process.env.QIO_SR_BROWSER) return process.env.QIO_SR_BROWSER;
  if (existsSync(EDGE)) return EDGE;
  if (existsSync(CHROME)) return CHROME;
  throw new Error("找不到 Edge / Chrome：用 QIO_SR_BROWSER 指定");
}

export function decodePng(buffer) {
  if (buffer.readUInt32BE(0) !== 0x89504e47) throw new Error("不是 PNG");
  let offset = 8;
  let width = 0;
  let height = 0;
  let colorType = 6;
  let bitDepth = 8;
  const idat = [];
  while (offset < buffer.length) {
    const length = buffer.readUInt32BE(offset);
    const type = buffer.toString("ascii", offset + 4, offset + 8);
    const data = buffer.subarray(offset + 8, offset + 8 + length);
    if (type === "IHDR") {
      width = data.readUInt32BE(0);
      height = data.readUInt32BE(4);
      bitDepth = data[8];
      colorType = data[9];
      if (data[12] !== 0) throw new Error("不支持隔行 PNG");
    } else if (type === "IDAT") {
      idat.push(data);
    } else if (type === "IEND") {
      break;
    }
    offset += 12 + length;
  }
  if (bitDepth !== 8) throw new Error("只支持 8 位 PNG，收到 " + bitDepth);
  const channels = colorType === 6 ? 4 : colorType === 2 ? 3 : colorType === 0 ? 1 : -1;
  if (channels < 0) throw new Error("不支持的 PNG 颜色类型 " + colorType);
  const raw = inflateSync(Buffer.concat(idat));
  const stride = width * channels;
  const out = Buffer.alloc(width * height * 4, 255);
  let prev = Buffer.alloc(stride, 0);
  let pos = 0;
  for (let y = 0; y < height; y += 1) {
    const filter = raw[pos];
    pos += 1;
    const line = Buffer.from(raw.subarray(pos, pos + stride));
    pos += stride;
    for (let i = 0; i < stride; i += 1) {
      const a = i >= channels ? line[i - channels] : 0;
      const b = prev[i];
      const c = i >= channels ? prev[i - channels] : 0;
      let value = line[i];
      if (filter === 1) value += a;
      else if (filter === 2) value += b;
      else if (filter === 3) value += (a + b) >> 1;
      else if (filter === 4) {
        const p = a + b - c;
        const pa = Math.abs(p - a);
        const pb = Math.abs(p - b);
        const pc = Math.abs(p - c);
        value += pa <= pb && pa <= pc ? a : pb <= pc ? b : c;
      }
      line[i] = value & 0xff;
    }
    for (let x = 0; x < width; x += 1) {
      const src = x * channels;
      const dst = (y * width + x) * 4;
      out[dst] = line[src];
      out[dst + 1] = channels === 1 ? line[src] : line[src + 1];
      out[dst + 2] = channels === 1 ? line[src] : line[src + 2];
      out[dst + 3] = channels === 4 ? line[src + 3] : 255;
    }
    prev = line;
  }
  return { width, height, data: out };
}

export class Browser {
  constructor(options = {}) {
    this.port = Number(options.port ?? process.env.QIO_SR_PROBE_PORT ?? 9788);
    this.profile = options.profile ?? process.env.QIO_SR_PROFILE ?? join(process.env.TEMP || ".", "qio-sr-edge");
    this.outDir = options.outDir ?? join(process.env.TEMP || ".", "qio-sr-shots");
    this.httpFails = [];
    this.consoleErrors = [];
    /** 真实请求集合（Network.requestWillBeSent）：用来核对「界面点下去真的发出了什么」 */
    this.requests = [];
    this.msgId = 0;
    this.pending = new Map();
  }

  /** 清掉**同 profile** 的僵尸实例（上一轮异常结束会留下还占着调试端口的浏览器）。 */
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

  async launch(extraArgs = []) {
    for (let attempt = 0; attempt < 2; attempt += 1) {
      try {
        return await this.launchOnce(extraArgs);
      } catch (error) {
        if (attempt === 1) throw error;
        this.killStale();
        await sleep(1500);
      }
    }
  }

  async launchOnce(extraArgs = []) {
    this.killStale();
    await sleep(500);
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
        "--use-gl=angle",
        "--use-angle=swiftshader",
        "--enable-unsafe-swiftshader",
        "--window-size=1440,900",
        "--force-device-scale-factor=1",
        ...extraArgs,
        "about:blank",
      ],
      { stdio: "ignore" },
    );
    let ready = false;
    for (let i = 0; i < 120 && !ready; i += 1) {
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
    await this.send("Runtime.enable");
    await this.send("Log.enable");
    await this.send("Page.enable");
    await this.send("Network.enable");
    await this.send("DOM.enable");
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
    if (msg.method === "Network.requestWillBeSent") {
      const req = msg.params.request;
      this.requests.push({
        method: req.method,
        url: req.url,
        body: typeof req.postData === "string" ? req.postData : null,
        at: Date.now(),
      });
      if (this.requests.length > 4000) this.requests.shift();
    }
    if (msg.method === "Network.loadingFailed") {
      this.httpFails.push("FAILED " + msg.params.errorText + " " + msg.params.requestId);
    }
    if (msg.method === "Runtime.exceptionThrown") {
      this.consoleErrors.push(String(msg.params.exceptionDetails?.text ?? ""));
    }
    if (msg.method === "Log.entryAdded" && msg.params.entry.level === "error") {
      this.consoleErrors.push(String(msg.params.entry.text ?? ""));
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
    await this.send("Emulation.setDeviceMetricsOverride", {
      width,
      height,
      deviceScaleFactor: 1,
      mobile: false,
    });
    await sleep(500);
  }

  async navigate(url, waitMs = 3000) {
    await this.send("Page.navigate", { url });
    await sleep(waitMs);
  }

  async reload(waitMs = 3000) {
    await this.send("Page.reload", { ignoreCache: true });
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

  async screenshotPng(clip, settle = false) {
    const params = { format: "png" };
    if (clip) params.clip = { ...clip, scale: 1 };
    if (settle) {
      await this.send("Page.captureScreenshot", { format: "png" });
      await sleep(200);
    }
    const shot = await this.send("Page.captureScreenshot", params);
    return Buffer.from(shot.data, "base64");
  }

  async shotFile(name, clip, settle = true) {
    const png = await this.screenshotPng(clip, settle);
    const file = join(this.outDir, name.endsWith(".png") ? name : name + ".png");
    mkdirSync(dirname(file), { recursive: true });
    writeFileSync(file, png);
    return file;
  }

  /** 真实鼠标点击：先取元素中心，再发 pressed/released。 */
  async clickSelector(selector) {
    const point = await this.evalJs(
      "(function () { const el = document.querySelector(" + JSON.stringify(selector) + "); if (!el) return null; const b = el.getBoundingClientRect(); return { x: Math.round(b.x + b.width / 2), y: Math.round(b.y + b.height / 2) }; })()",
    );
    if (!point) return false;
    await this.send("Input.dispatchMouseEvent", { type: "mouseMoved", x: point.x, y: point.y, button: "none" });
    await this.send("Input.dispatchMouseEvent", { type: "mousePressed", x: point.x, y: point.y, button: "left", clickCount: 1 });
    await this.send("Input.dispatchMouseEvent", { type: "mouseReleased", x: point.x, y: point.y, button: "left", clickCount: 1 });
    return true;
  }

  /** 真实拖动某张卡片（E1/F1 的时间窗场景要用真实指针事件，不能用 commit 代替）。 */
  async dragCard(cardId, dx, dy) {
    const point = await this.evalJs(
      "(function () { const el = document.querySelector('[data-card-id=\"" + cardId + "\"][data-im=card]'); if (!el) return null; const b = el.getBoundingClientRect(); return { x: Math.round(b.x + 20), y: Math.round(b.y + 12) }; })()",
    );
    if (!point) return false;
    await this.send("Input.dispatchMouseEvent", { type: "mouseMoved", x: point.x, y: point.y, button: "none" });
    await this.send("Input.dispatchMouseEvent", { type: "mousePressed", x: point.x, y: point.y, button: "left", clickCount: 1 });
    for (let step = 1; step <= 6; step += 1) {
      await this.send("Input.dispatchMouseEvent", {
        type: "mouseMoved",
        x: Math.round(point.x + (dx * step) / 6),
        y: Math.round(point.y + (dy * step) / 6),
        button: "left",
        buttons: 1,
      });
      await sleep(40);
    }
    await this.send("Input.dispatchMouseEvent", { type: "mouseReleased", x: point.x + dx, y: point.y + dy, button: "left", clickCount: 1 });
    return true;
  }

  /** 受控网络延迟（真实传输层延迟，不是页面内桩）：用来制造「请求还在飞」的时间窗。 */
  async setLatency(ms) {
    await this.send("Network.emulateNetworkConditions", {
      offline: false,
      latency: ms,
      downloadThroughput: -1,
      uploadThroughput: -1,
    });
  }

  async clearLatency() {
    await this.send("Network.emulateNetworkConditions", {
      offline: false,
      latency: 0,
      downloadThroughput: -1,
      uploadThroughput: -1,
    });
  }

  /**
   * 正常关闭（优雅）：CDP Browser.close，让 Chromium 把 profile 里最近的 localStorage 写入落盘。
   * 实测：直接 child.kill() 会丢掉最近的本机记录改动（重开后又出现旧值），
   * 那是装置问题而不是产品问题，所以「正常关闭重开」的旅程必须走这一条。
   */
  async closeGraceful(timeoutMs = 20000) {
    try {
      const version = await (
        await fetch("http://127.0.0.1:" + this.port + "/json/version", { signal: AbortSignal.timeout(2000) })
      ).json();
      const ws = new WebSocket(version.webSocketDebuggerUrl);
      await new Promise((resolve, reject) => {
        ws.addEventListener("open", resolve);
        ws.addEventListener("error", reject);
      });
      ws.send(JSON.stringify({ id: 1, method: "Browser.close" }));
      await new Promise((resolve) => {
        ws.addEventListener("close", resolve);
        setTimeout(resolve, 3000);
      });
    } catch {
      /* 回退到强制结束 */
    }
    const deadline = Date.now() + timeoutMs;
    while (Date.now() < deadline && this.child && this.child.exitCode === null) await sleep(200);
    if (this.child && this.child.exitCode === null) this.child.kill();
    try {
      this.ws?.close();
    } catch {
      /* 忽略 */
    }
    await sleep(600);
  }

  /** 关闭浏览器进程（E2 用它制造「实际进程关闭」）。 */
  async close() {
    try {
      this.ws?.close();
    } catch {
      /* 忽略 */
    }
    this.child?.kill();
    await sleep(400);
  }

  /** 等浏览器调试端口彻底消失（关闭重开之间必须确认进程真的走了）。 */
  async waitGone(timeoutMs = 10000) {
    const deadline = Date.now() + timeoutMs;
    while (Date.now() < deadline) {
      try {
        const resp = await fetch("http://127.0.0.1:" + this.port + "/json/version", {
          signal: AbortSignal.timeout(800),
        });
        if (!resp.ok) return true;
      } catch {
        return true;
      }
      await sleep(250);
    }
    return false;
  }
}
