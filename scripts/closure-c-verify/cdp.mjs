/**
 * C 的自用 CDP 浏览器驱动（只给验收用）。
 *
 * 为什么不直接用 scripts/visual_probe_d3.mjs：那个脚本把调试端口（9666）与用户目录写死，
 * 多个工作区/多个智能体同时跑会互相抢端口；而且它不提供「按元素裁剪截图」与像素级对比度。
 * 这里全部可用环境变量覆盖，并且能返回 PNG 原始字节，用来做**真实文字 vs 真实背景**的测量。
 *
 * 环境变量：
 *   QIO_C_BROWSER  浏览器可执行文件（默认先找 Edge，再找 Chrome）
 *   QIO_C_PROBE_PORT  调试端口（默认 9777）
 *   QIO_C_PROFILE  用户目录（默认 %TEMP%\qio-closure-c-edge）
 */
import { spawn, spawnSync } from "node:child_process";
import { existsSync, mkdirSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { setTimeout as sleep } from "node:timers/promises";
import { inflateSync } from "node:zlib";

const EDGE = "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe";
const CHROME = "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe";

export function browserPath() {
  if (process.env.QIO_C_BROWSER) return process.env.QIO_C_BROWSER;
  if (existsSync(EDGE)) return EDGE;
  if (existsSync(CHROME)) return CHROME;
  throw new Error("找不到 Edge / Chrome：用 QIO_C_BROWSER 指定");
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

function luminance(r, g, b) {
  const lin = [r, g, b].map((value) => {
    const s = value / 255;
    return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
  });
  return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2];
}

export function contrastRatio(rgbA, rgbB) {
  const a = luminance(...rgbA);
  const b = luminance(...rgbB);
  const [hi, lo] = a > b ? [a, b] : [b, a];
  return (hi + 0.05) / (lo + 0.05);
}

/**
 * 「真实文字 vs 真实背景」的对比度：在裁剪图里取出现次数最多的颜色当背景，
 * 取与背景亮度差最大的像素当文字（抗锯齿小字的核心笔画能落到名义色）。
 * 这是像素级证据，不是把令牌代进公式算出来的值。
 */
export function sampleContrast(image, keys) {
  const counts = new Map();
  for (let i = 0; i < image.data.length; i += 4) {
    const key = (image.data[i] << 16) | (image.data[i + 1] << 8) | image.data[i + 2];
    counts.set(key, (counts.get(key) ?? 0) + 1);
  }
  let bgKey = 0;
  let bgCount = -1;
  for (const [key, count] of counts) {
    if (count > bgCount) {
      bgKey = key;
      bgCount = count;
    }
  }
  const bg = [(bgKey >> 16) & 255, (bgKey >> 8) & 255, bgKey & 255];
  const bgLum = luminance(...bg);
  let bestKey = bgKey;
  let bestDelta = -1;
  for (const key of counts.keys()) {
    const rgb = [(key >> 16) & 255, (key >> 8) & 255, key & 255];
    const delta = Math.abs(luminance(...rgb) - bgLum);
    if (delta > bestDelta) {
      bestDelta = delta;
      bestKey = key;
    }
  }
  const fg = [(bestKey >> 16) & 255, (bestKey >> 8) & 255, bestKey & 255];
  return {
    background: bg,
    foregroundSample: fg,
    ratio: Number(contrastRatio(fg, bg).toFixed(2)),
    nominalFromComputed: keys ?? null,
  };
}

export class Browser {
  constructor(options = {}) {
    this.port = Number(options.port ?? process.env.QIO_C_PROBE_PORT ?? 9777);
    this.profile = options.profile ?? process.env.QIO_C_PROFILE ?? join(process.env.TEMP || ".", "qio-closure-c-edge");
    this.outDir = options.outDir ?? join(process.env.TEMP || ".", "qio-closure-c-shots");
    this.httpFails = [];
    this.consoleErrors = [];
    this.fontResponses = [];
    this.msgId = 0;
    this.pending = new Map();
  }

  /**
   * 上一轮异常结束会留下一个还占着调试端口的浏览器（实测：探针进程被终止时子进程不一定跟着走），
   * 新实例连不上端口就报「调试端口没起来」。启动前先清掉**同 profile** 的僵尸实例。
   */
  killStale() {
    if (process.platform !== "win32") return;
    spawnSync("powershell", ["-NoProfile", "-Command",
      "Get-CimInstance Win32_Process -Filter \"Name='msedge.exe' or Name='chrome.exe'\" | Where-Object { $_.CommandLine -like '*" + this.profile + "*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }",
    ], { encoding: "utf8" });
  }

  async launch() {
    this.killStale();
    mkdirSync(this.profile, { recursive: true });
    mkdirSync(this.outDir, { recursive: true });
    this.child = spawn(browserPath(), [
      "--headless=new",
      `--remote-debugging-port=${this.port}`,
      `--user-data-dir=${this.profile}`,
      "--no-first-run",
      "--no-default-browser-check",
      "--hide-scrollbars",
      "--use-gl=angle",
      "--use-angle=swiftshader",
      "--enable-unsafe-swiftshader",
      "--window-size=1440,900",
      "--force-device-scale-factor=1",
      "about:blank",
    ], { stdio: "ignore" });
    let ready = false;
    for (let i = 0; i < 120 && !ready; i += 1) {
      try {
        const resp = await fetch(`http://127.0.0.1:${this.port}/json/version`, { signal: AbortSignal.timeout(1500) });
        ready = resp.ok;
      } catch {
        /* 还没起来 */
      }
      if (!ready) await sleep(250);
    }
    if (!ready) throw new Error("浏览器调试端口没起来：" + this.port);
    const tab = await (await fetch(`http://127.0.0.1:${this.port}/json/new?about:blank`, { method: "PUT" })).json();
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
    await this.send("CSS.enable");
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
      if (response.status >= 400) this.httpFails.push(`${response.status} ${response.url}`);
      if (/\.(woff2?|ttf|otf)(\?|$)/.test(response.url)) {
        this.fontResponses.push({ status: response.status, url: response.url });
      }
    }
    if (msg.method === "Network.loadingFailed") {
      this.httpFails.push(`FAILED ${msg.params.errorText} ${msg.params.requestId}`);
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
    await this.send("Emulation.setDeviceMetricsOverride", { width, height, deviceScaleFactor: 1, mobile: false });
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
    const result = await this.send("Runtime.evaluate", { expression: js, awaitPromise: true, returnByValue: true });
    if (result.exceptionDetails) throw new Error("页面脚本异常：" + result.exceptionDetails.text + " " + (result.exceptionDetails.exception?.description ?? ""));
    return result.result?.value;
  }

  /**
   * settle=true 时先多截一帧再取后一张（视口变化后首帧可能还是旧缓冲）。
   * 软件光栅（swiftshader）下每次整页截图都不便宜，所以只有「给别人看的整页图」才 settle，
   * 对比度用的元素小图直接截一张。
   */
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

  /** 真实渲染用的字体（CDP 平台字体查询）：这是「字体真的加载了」的硬证据，不看 CSS 声明。 */
  async platformFonts(selector) {
    const doc = await this.send("DOM.getDocument", { depth: 1 });
    const found = await this.send("DOM.querySelector", { nodeId: doc.root.nodeId, selector });
    if (!found.nodeId) return null;
    const fonts = await this.send("CSS.getPlatformFontsForNode", { nodeId: found.nodeId });
    return fonts.fonts;
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
