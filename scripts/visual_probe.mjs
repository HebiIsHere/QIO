/**
 * 无依赖 CDP 视觉验证探针（Chrome headless + Node 内置 WebSocket，无 npm 依赖）。
 *
 * 为什么需要它：QIO 的验收要求「改了 UI 必须实际跑起来看」，而 Codex 应用内浏览器
 * 不一定可控。这个脚本直接驱动本机 Chrome，既能截图，也能拿到客观证据
 * （DOM 几何、对比度、实际字体、Tab 焦点、控制台错误）。
 *
 * 用法：
 *   # 先起服务（后端 8734 + 前端 5199）
 *   python scripts/e2e_up.py
 *   # 再跑步骤（JSON 可以直接给，也可以 @文件）
 *   node scripts/visual_probe.mjs "@steps.json"
 *   node scripts/visual_probe.mjs '[{"op":"navigate","url":"http://127.0.0.1:5199/"},{"op":"screenshot","name":"chat"}]'
 *
 * 支持的 op：
 *   navigate / reload / viewport(width,height) / wait(ms) / screenshot(name)
 *   eval(js) / key(key) / offline(offline:boolean) / cdp(method,params)
 *   platformFonts(selector) / console(tail)
 *
 * 截图输出目录：%TEMP%\qio-visual\shots；Chrome 用户目录：%TEMP%\qio-chrome-profile。
 * 需要 Chrome（脚本顶部 CHROME 常量，可按机器修改）。
 */
import { spawn } from "node:child_process";
import { mkdirSync, writeFileSync } from "node:fs";
import { setTimeout as sleep } from "node:timers/promises";

const CHROME = "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe";
const PORT = 9333;
const PROFILE = `${process.env.TEMP}\\qio-chrome-profile`;
const OUT = `${process.env.TEMP}\\qio-visual\\shots`;
mkdirSync(OUT, { recursive: true });

import { readFileSync } from "node:fs";
const rawArg = process.argv[2] ?? "[]";
const steps = JSON.parse(rawArg.startsWith("@") ? readFileSync(rawArg.slice(1), "utf8") : rawArg);

const chrome = spawn(
  CHROME,
  [
    "--headless=new",
    `--remote-debugging-port=${PORT}`,
    `--user-data-dir=${PROFILE}`,
    "--no-first-run",
    "--no-default-browser-check",
    "--hide-scrollbars",
    "--use-gl=angle",
    "--use-angle=swiftshader",
    "--enable-unsafe-swiftshader",
    "--window-size=1440,900",
    "about:blank",
  ],
  { stdio: "ignore", detached: false },
);

async function waitForDevtools() {
  for (let i = 0; i < 80; i++) {
    try {
      const r = await fetch(`http://127.0.0.1:${PORT}/json/version`);
      if (r.ok) return;
    } catch {
      /* retry */
    }
    await sleep(250);
  }
  throw new Error("chrome devtools not reachable");
}

let msgId = 0;
function makeClient(ws) {
  const pending = new Map();
  ws.addEventListener("message", (ev) => {
    const msg = JSON.parse(ev.data);
    if (msg.id && pending.has(msg.id)) {
      const { resolve, reject } = pending.get(msg.id);
      pending.delete(msg.id);
      if (msg.error) reject(new Error(JSON.stringify(msg.error)));
      else resolve(msg.result);
    }
  });
  return (method, params = {}) =>
    new Promise((resolve, reject) => {
      const id = ++msgId;
      pending.set(id, { resolve, reject });
      ws.send(JSON.stringify({ id, method, params }));
    });
}

await waitForDevtools();
const tabRes = await fetch(`http://127.0.0.1:${PORT}/json/new?about:blank`, { method: "PUT" });
const tab = await tabRes.json();
const ws = new WebSocket(tab.webSocketDebuggerUrl);
await new Promise((res, rej) => {
  ws.addEventListener("open", res);
  ws.addEventListener("error", rej);
});
const send = makeClient(ws);

const consoleLog = [];
const errors = [];
const httpFails = [];
ws.addEventListener("message", (ev) => {
  const msg = JSON.parse(ev.data);
  if (msg.method === "Network.responseReceived") {
    const r = msg.params.response;
    if (r.status >= 400) httpFails.push(`${r.status} ${r.url}`);
  }
  if (msg.method === "Network.loadingFailed") {
    httpFails.push(`FAILED ${msg.params.errorText} ${msg.params.requestId}`);
  }
  if (msg.method === "Runtime.consoleAPICalled") {
    const text = (msg.params.args ?? [])
      .map((a) => a.value ?? a.description ?? a.type)
      .join(" ");
    consoleLog.push(`[${msg.params.type}] ${text}`.slice(0, 400));
  }
  if (msg.method === "Runtime.exceptionThrown") {
    const d = msg.params.exceptionDetails;
    errors.push(`[exception] ${d.text} ${d.exception?.description ?? ""}`.slice(0, 500));
  }
  if (msg.method === "Log.entryAdded") {
    const e = msg.params.entry;
    const line = `[${e.level}] ${e.text}`.slice(0, 400);
    if (e.level === "error") errors.push(line);
    else consoleLog.push(line);
  }
});

await send("Runtime.enable");
await send("Log.enable");
await send("Page.enable");
await send("Network.enable");

const results = [];
for (const step of steps) {
  try {
    switch (step.op) {
      case "wait":
        await sleep(step.ms ?? 500);
        break;
      case "navigate": {
        await send("Page.navigate", { url: step.url });
        await sleep(step.ms ?? 2500);
        break;
      }
      case "reload": {
        await send("Page.reload", { ignoreCache: true });
        await sleep(step.ms ?? 2500);
        break;
      }
      case "viewport": {
        await send("Emulation.setDeviceMetricsOverride", {
          width: step.width,
          height: step.height,
          deviceScaleFactor: 1,
          mobile: false,
        });
        await sleep(step.ms ?? 700);
        break;
      }
      case "eval": {
        const r = await send("Runtime.evaluate", {
          expression: step.js,
          awaitPromise: true,
          returnByValue: true,
        });
        results.push({ op: "eval", value: r.result?.value, error: r.exceptionDetails?.text });
        break;
      }
      case "screenshot": {
        // 视口尺寸变化后首帧可能仍是旧缓冲：多截一次，取后一张
        await send("Page.captureScreenshot", { format: "png" });
        await sleep(250);
        const r = await send("Page.captureScreenshot", { format: "png" });
        writeFileSync(`${OUT}\\${step.name}.png`, Buffer.from(r.data, "base64"));
        results.push({ op: "screenshot", name: step.name });
        break;
      }
      case "key": {
        const map = { Tab: 9, Enter: 13, Escape: 27, Space: 32 };
        const key = step.key ?? "Tab";
        const code = map[key] ?? 0;
        await send("Input.dispatchKeyEvent", {
          type: "rawKeyDown",
          key,
          windowsVirtualKeyCode: code,
          nativeVirtualKeyCode: code,
        });
        await send("Input.dispatchKeyEvent", {
          type: "keyUp",
          key,
          windowsVirtualKeyCode: code,
          nativeVirtualKeyCode: code,
        });
        await sleep(step.ms ?? 120);
        break;
      }
      case "offline": {
        await send("Network.emulateNetworkConditions", {
          offline: !!step.offline,
          latency: 0,
          downloadThroughput: step.offline ? 0 : -1,
          uploadThroughput: step.offline ? 0 : -1,
        });
        await sleep(step.ms ?? 200);
        break;
      }
      case "cdp": {
        const r = await send(step.method, step.params ?? {});
        results.push({ op: "cdp", method: step.method, value: r });
        break;
      }
      case "platformFonts": {
        await send("DOM.enable");
        await send("CSS.enable");
        const doc = await send("DOM.getDocument", { depth: 1 });
        const q = await send("DOM.querySelector", { nodeId: doc.root.nodeId, selector: step.selector });
        const fonts = await send("CSS.getPlatformFontsForNode", { nodeId: q.nodeId });
        results.push({ op: "platformFonts", selector: step.selector, fonts: fonts.fonts });
        break;
      }
      case "console": {
        results.push({ op: "console", errors: [...errors], log: consoleLog.slice(-step.tail ?? 40) });
        break;
      }
      default:
        results.push({ op: step.op, error: "unknown op" });
    }
  } catch (e) {
    results.push({ op: step.op, error: String(e) });
  }
}

process.stdout.write(
  JSON.stringify(
    { results, errors: errors.slice(-30), httpFails: [...new Set(httpFails)], log: consoleLog.slice(-40) },
    null,
    2,
  ),
);
ws.close();
chrome.kill();
process.exit(0);
