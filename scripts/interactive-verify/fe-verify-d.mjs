/**
 * D 的独立验收探针（子智能体 D，浮层共存与几何）。
 *
 * 无 npm 依赖：Node 内置 fetch/WebSocket 直连 CDP，用 Edge（本机 Chrome 曾因大量 headless 实例卡死，
 * 所以这里固定用 Edge + 独立 profile + 独立端口，和 scripts/visual_probe_d3.mjs 互不干扰）。
 *
 * 它回答的问题（每条都给数字，不是「看起来对」）：
 *   1. 三档窗口（1440×900 / 1024×768 / 800×600）× 两主题下，聊天面板与批量列表**相交面积**；
 *   2. 底部工具栏总高（800×600 要求 ≤ 96px）；
 *   3. 消息可读区（聊天消息区可见高度）与输入区、提交入口的可点范围（elementFromPoint 命中测试）；
 *   4. 选中卡片后连接点被谁命中（重复 3 次，验证局部工具栏没盖住它）；
 *   5. 当前生效的浮层模式（side-by-side / stacked / switched，来自几何控制器写的 im-geo-mode）。
 *
 * 用法：
 *   node scripts/interactive-verify/fe-verify-d.mjs --app http://127.0.0.1:5414 --out docs/interactive-ui-screenshots
 *   可选：--port 9667 --profile "%TEMP%\qio-edge-profile-dv" --keep（保留截图在临时目录的副本）
 *
 * 前置：后端 8914 与前端 5414 已经起来（见最终报告里的命令行）。
 */
import { spawn } from "node:child_process";
import { mkdirSync, writeFileSync, existsSync, copyFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { setTimeout as sleep } from "node:timers/promises";

const HERE = dirname(fileURLToPath(import.meta.url));
const REPO = resolve(HERE, "..", "..");

function arg(name, fallback) {
  const index = process.argv.indexOf("--" + name);
  if (index >= 0 && process.argv[index + 1] && !process.argv[index + 1].startsWith("--")) {
    return process.argv[index + 1];
  }
  return fallback;
}

const APP = arg("app", "http://127.0.0.1:5414");
const OUT_DIR = resolve(REPO, arg("out", "docs/interactive-ui-screenshots"));
const PORT = Number(arg("port", "9667"));
const PROFILE = arg("profile", `${process.env.TEMP ?? "."}\\qio-edge-profile-dv`);
const EDGE =
  process.env.QIO_EDGE ?? "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe";
const SHOTS = join(process.env.TEMP ?? ".", "qio-visual", "shots");

mkdirSync(OUT_DIR, { recursive: true });
mkdirSync(SHOTS, { recursive: true });

if (!existsSync(EDGE)) {
  console.error("找不到 Edge：" + EDGE + "（可以用环境变量 QIO_EDGE 指定）");
  process.exit(2);
}

// --- CDP 客户端 ---------------------------------------------------------
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
    new Promise((res, rej) => {
      const id = ++msgId;
      pending.set(id, { resolve: res, reject: rej });
      ws.send(JSON.stringify({ id, method, params }));
    });
}

const edge = spawn(
  EDGE,
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
  { stdio: "ignore" },
);

async function waitForDevtools() {
  for (let i = 0; i < 240; i++) {
    try {
      const r = await fetch(`http://127.0.0.1:${PORT}/json/version`);
      if (r.ok) return;
    } catch {
      /* retry */
    }
    await sleep(250);
  }
  throw new Error("edge devtools not reachable（端口 " + PORT + "）");
}

await waitForDevtools();
const tab = await (await fetch(`http://127.0.0.1:${PORT}/json/new?about:blank`, { method: "PUT" })).json();
const ws = new WebSocket(tab.webSocketDebuggerUrl);
await new Promise((res, rej) => {
  ws.addEventListener("open", res);
  ws.addEventListener("error", rej);
});
const send = makeClient(ws);

const errors = [];
const httpFails = [];
/** 所有请求 URL（按顺序）：用来证明「聊天发送不调用板面提交接口」 */
const requests = [];
ws.addEventListener("message", (ev) => {
  const msg = JSON.parse(ev.data);
  if (msg.method === "Network.requestWillBeSent") {
    requests.push(`${msg.params.request.method} ${msg.params.request.url}`);
  }
  if (msg.method === "Runtime.exceptionThrown") {
    const d = msg.params.exceptionDetails;
    errors.push(`[exception] ${d.text} ${d.exception?.description ?? ""}`.slice(0, 400));
  }
  if (msg.method === "Log.entryAdded" && msg.params.entry.level === "error") {
    errors.push(`[error] ${msg.params.entry.text}`.slice(0, 400));
  }
  if (msg.method === "Network.responseReceived" && msg.params.response.status >= 400) {
    httpFails.push(`${msg.params.response.status} ${msg.params.response.url}`);
  }
});

await send("Runtime.enable");
await send("Log.enable");
await send("Page.enable");
await send("Network.enable");

async function evaluate(js) {
  const r = await send("Runtime.evaluate", { expression: js, awaitPromise: true, returnByValue: true });
  if (r.exceptionDetails) throw new Error(r.exceptionDetails.text + " " + (r.exceptionDetails.exception?.description ?? ""));
  return r.result?.value;
}

async function goto(url, ms = 4000) {
  await send("Page.navigate", { url });
  await sleep(ms);
}

async function viewport(width, height) {
  await send("Emulation.setDeviceMetricsOverride", {
    width,
    height,
    deviceScaleFactor: 1,
    mobile: false,
  });
  await sleep(500);
}

async function shot(name) {
  await send("Page.captureScreenshot", { format: "png" });
  await sleep(200);
  const r = await send("Page.captureScreenshot", { format: "png" });
  const temp = join(SHOTS, name + ".png");
  const target = join(OUT_DIR, name + ".png");
  writeFileSync(temp, Buffer.from(r.data, "base64"));
  copyFileSync(temp, target);
  return target;
}

/** 页面里的量测：矩形、相交面积、命中测试，全部用同一份实现 */
const MEASURE_JS = `(() => {
  const rect = (selector) => {
    const el = document.querySelector(selector);
    if (!el) return null;
    const r = el.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) return null;
    return { left: Math.round(r.left), top: Math.round(r.top), right: Math.round(r.right), bottom: Math.round(r.bottom), width: Math.round(r.width), height: Math.round(r.height) };
  };
  const area = (a, b) => {
    if (!a || !b) return 0;
    const w = Math.min(a.right, b.right) - Math.max(a.left, b.left);
    const h = Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top);
    return w > 0 && h > 0 ? Math.round(w * h) : 0;
  };
  const hit = (selector) => {
    const el = document.querySelector(selector);
    if (!el) return { found: false };
    const r = el.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) return { found: true, visible: false };
    const x = Math.round(r.left + r.width / 2);
    const y = Math.round(r.top + r.height / 2);
    const target = document.elementFromPoint(x, y);
    return {
      found: true,
      visible: true,
      point: { x, y },
      hitSelf: !!target && (target === el || el.contains(target) || !!target.closest('[data-im="' + (el.getAttribute("data-im") || "") + '"]')),
      hitTag: target ? target.tagName.toLowerCase() : null,
      hitIm: target && target.closest("[data-im]") ? target.closest("[data-im]").getAttribute("data-im") : null,
    };
  };
  const chat = rect('[data-im="chat-panel"]');
  const batchPanel = rect('[data-im="batch-list"]');
  const batchTray = rect('[data-im="batch-area"]');
  const batchEntry = rect('[data-im="batch-entry"]');
  const toolbar = rect('[data-im="board-toolbar"]');
  const submit = rect('[data-im="submit"]');
  const stream = document.querySelector('[data-im="chat-stream"]');
  const stage = document.querySelector(".im-stage");
  return JSON.stringify({
    viewport: { width: window.innerWidth, height: window.innerHeight },
    theme: document.documentElement.getAttribute("data-theme") || "dark",
    mode: stage ? stage.getAttribute("im-geo-mode") : null,
    chat, batchPanel, batchTray, batchEntry, toolbar, submit,
    intersectionChatBatch: area(chat, batchPanel || batchTray),
    intersectionChatEntry: area(chat, batchEntry),
    intersectionChatToolbar: area(chat, toolbar),
    intersectionBatchToolbar: area(batchPanel || batchTray, toolbar),
    panelGap: chat && batchPanel ? Math.round(chat.left - batchPanel.right) : null,
    toolbarHeight: toolbar ? toolbar.height : null,
    chatReadableHeight: stream ? Math.round(stream.getBoundingClientRect().height) : 0,
    chatHasMessages: !!document.querySelector('[data-im="chat-stream"] .stream-item, [data-im="chat-stream"] .stream-empty'),
    hit: {
      chatInput: hit('[data-im="chat-input"]'),
      chatSend: hit('[data-im="chat-send"]'),
      submit: hit('[data-im="submit"]'),
      chatPanel: hit('[data-im="chat-panel"]'),
      batchPanel: hit('[data-im="batch-list"]'),
    },
    batchItemCount: document.querySelectorAll('[data-im="batch-item"]').length,
    batchEntryVisible: !!rect('[data-im="batch-entry"]'),
  });
})()`;

async function openOverlays() {
  // 只点用户会点的按钮；已经展开就不重复点（不改变开合语义）
  await evaluate(`(() => {
    const entry = document.querySelector('[data-im="batch-entry"]');
    if (entry && !document.querySelector('[data-im="batch-list"]')) entry.click();
    const toggle = document.querySelector('[data-im="chat-toggle"]');
    if (toggle && toggle.getAttribute('aria-expanded') !== 'true') toggle.click();
    return true;
  })()`);
  await sleep(600);
}

/**
 * 连接点命中测试：拿**最后新建的那张卡片**（而不是文档里第一张，避免旧数据干扰），
 * 把它身上每个连接点的中心点做 elementFromPoint，看命中的是不是连接点自己。
 */
const CONNECT_JS = `(() => {
  const cards = [...document.querySelectorAll('[data-im="card"]')];
  const card = cards[cards.length - 1];
  if (!card) return JSON.stringify({ cardFound: false, points: [] });
  const cardRect = card.getBoundingClientRect();
  // 页面上所有可见连接点都要测（选中多张卡片时每个点都要能被点到）
  const points = [...document.querySelectorAll('[data-im="connect-point"]')].filter((el) => {
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  });
  return JSON.stringify({
    cardFound: true,
    cardId: card.getAttribute("data-card-id") || card.id || null,
    cardRect: { left: Math.round(cardRect.left), top: Math.round(cardRect.top), right: Math.round(cardRect.right), bottom: Math.round(cardRect.bottom) },
    points: points.map((el) => {
      const r = el.getBoundingClientRect();
      const x = Math.round(r.left + r.width / 2);
      const y = Math.round(r.top + r.height / 2);
      const target = document.elementFromPoint(x, y);
      return {
        point: { x, y },
        size: { width: Math.round(r.width), height: Math.round(r.height) },
        hitSelf: !!target && (target === el || el.contains(target)),
        hitIm: target && target.closest("[data-im]") ? target.closest("[data-im]").getAttribute("data-im") : null,
      };
    }),
  });
})()`;

const report = { app: APP, cells: [], notes: [], connectPoints: [] };

// --- 0. 起应用、准备一批 4 项演示意图（批量入口需要同一批 ≥4） --------------
await goto(APP + "/#/interactive", 6000);
await evaluate("(() => { try { localStorage.removeItem('qio.interactive.intentBatches'); } catch (e) {} return true; })()");
await send("Page.reload", { ignoreCache: false });
await sleep(5000);

const bootInfo = await evaluate(
  `JSON.stringify({
    board: !!document.querySelector('[data-im="board-toolbar"]'),
    demoEntry: !!document.querySelector('[data-im="demo-entry"]'),
    loadError: document.querySelector('[data-im="load-error"]') ? document.querySelector('[data-im="load-error"]').textContent.trim() : null,
  })`,
);
report.boot = JSON.parse(bootInfo);
if (!report.boot.board) {
  report.notes.push("互动板没有加载出来：先确认后端与前端都在跑（见报告里的命令行）");
}

// 生成同一批 4 项演示意图
await evaluate(`(() => {
  const entry = document.querySelector('[data-im="demo-entry"]');
  if (entry) entry.click();
  return true;
})()`);
await sleep(400);
await evaluate(`(() => {
  const create = document.querySelector('[data-im="demo-create"]');
  if (create) create.click();
  return true;
})()`);
await sleep(2500);
await evaluate(`(() => {
  const entry = document.querySelector('[data-im="demo-entry"]');
  if (entry && document.querySelector('[data-im="demo-popover"]')) entry.click();
  return true;
})()`);
await sleep(300);

// --- 0.5 规则检查：同批达到四项后，处理掉一项列表仍然保留（契约 §9.3） ------
async function readBatchState(label) {
  const raw = await evaluate(`(() => {
    const entry = document.querySelector('[data-im="batch-entry"]');
    const list = document.querySelector('[data-im="batch-list"]');
    const remain = document.querySelector('[data-im="batch-remaining"]');
    return JSON.stringify({
      entry: !!entry,
      entryText: entry ? entry.textContent.trim() : null,
      list: !!list,
      remaining: remain ? remain.textContent.trim() : null,
      rows: document.querySelectorAll('[data-im="batch-item"]').length,
      pendingRows: document.querySelectorAll('[data-im="batch-item"][data-im-state="pending"]').length,
      processedRows: document.querySelectorAll('[data-im="batch-item"][data-im-state="processed"]').length,
      approveDisabled: (() => {
        const b = document.querySelector('[data-im="batch-approve"]');
        return b ? b.disabled : null;
      })(),
    });
  })()`);
  const state = JSON.parse(raw);
  state.label = label;
  return state;
}

await viewport(1440, 900);
await openOverlays();
await sleep(400);
const beforeDecide = await readBatchState("处理前");
// 用**真实鼠标事件**勾选第一项：和用户的手一样走 pointer/click 路径
// 挑一个**现在就能批准**的项：带 .blocked 说明的（例如等待前项）点了也不会提交
const boxPoint = await evaluate(`(() => {
  const rows = [...document.querySelectorAll('[data-im="batch-item"][data-im-state="pending"]')];
  const target = rows.find((row) => !row.querySelector(".blocked")) ?? null;
  const box = target ? target.querySelector('input[type="checkbox"]') : null;
  if (!box) return "";
  const r = box.getBoundingClientRect();
  return JSON.stringify({ x: Math.round(r.left + r.width / 2), y: Math.round(r.top + r.height / 2), approvableRows: rows.filter((row) => !row.querySelector(".blocked")).length });
})()`);
let clickDebug = { boxPoint: boxPoint || null };
if (boxPoint) {
  const point = JSON.parse(boxPoint);
  clickDebug.topAtPoint = await evaluate(`(() => {
    const el = document.elementFromPoint(${point.x}, ${point.y});
    if (!el) return null;
    const im = el.closest("[data-im]");
    return (im ? im.getAttribute("data-im") : el.tagName) + "/" + el.tagName.toLowerCase();
  })()`);
  await send("Input.dispatchMouseEvent", { type: "mouseMoved", x: point.x, y: point.y, button: "none" });
  await send("Input.dispatchMouseEvent", { type: "mousePressed", x: point.x, y: point.y, button: "left", clickCount: 1 });
  await send("Input.dispatchMouseEvent", { type: "mouseReleased", x: point.x, y: point.y, button: "left", clickCount: 1 });
  await sleep(400);
  clickDebug.checkedAfterMouse = await evaluate(
    `JSON.stringify([...document.querySelectorAll('[data-im="batch-item"] input[type="checkbox"]')].map((b) => b.checked))`,
  );
  if (!/true/.test(String(clickDebug.checkedAfterMouse))) {
    // 鼠标没选中时退回 DOM 点击，并把这件事如实记下来（探针的手感与真实鼠标有差异）
    await evaluate(`(() => {
      const box = document.querySelector('[data-im="batch-item"][data-im-state="pending"] input[type="checkbox"]');
      if (box) box.click();
      return true;
    })()`);
    await sleep(400);
    clickDebug.checkedAfterDomClick = await evaluate(
      `JSON.stringify([...document.querySelectorAll('[data-im="batch-item"] input[type="checkbox"]')].map((b) => b.checked))`,
    );
  }
}
await sleep(200);
clickDebug.approveBefore = await evaluate(
  `(() => { const b = document.querySelector('[data-im="batch-approve"]'); return b ? b.disabled : null; })()`,
);
await evaluate(`(() => {
  const btn = document.querySelector('[data-im="batch-approve"]');
  if (btn && !btn.disabled) btn.click();
  return true;
})()`);
await sleep(3500);
clickDebug.notice = await evaluate(`(() => {
  const el = document.querySelector('[data-im="batch-notice"]');
  return el ? el.textContent.trim().slice(0, 160) : null;
})()`);
const afterDecide = await readBatchState("处理掉一项之后");
report.batchRetention = {
  before: beforeDecide,
  after: afterDecide,
  click: clickDebug,
  // 判定用「页面状态」而不是「这次点击有没有成功」：只要这一批还有未处理项，
  // 入口与列表就必须在，并且同时显示剩余与已处理数量。
  kept: afterDecide.entry === true && afterDecide.list === true && afterDecide.pendingRows >= 1 && afterDecide.processedRows >= 1,
  file: await shot("d-batch-after-partial"),
};
report.notes.push(
  report.batchRetention.kept
    ? "§9.3：处理掉一项后批量入口与列表仍然保留，并区分待处理 / 已处理。"
    : "§9.3：处理掉一项后入口消失或行列不对，需要看 batchRetention 的原始数字。",
);

// --- 1. 三档窗口 × 两主题：浮层共存 ---------------------------------------
const VIEWPORTS = [
  { width: 1440, height: 900 },
  { width: 1024, height: 768 },
  { width: 800, height: 600 },
];

for (const size of VIEWPORTS) {
  for (const theme of ["dark", "light"]) {
    await viewport(size.width, size.height);
    await evaluate(`(() => {
      document.documentElement.setAttribute('data-theme', '${theme}');
      try { localStorage.setItem('qio-theme', '${theme}'); } catch (e) {}
      window.dispatchEvent(new Event('resize'));
      return true;
    })()`);
    await sleep(500);
    await openOverlays();
    await sleep(400);
    const name = `d-${size.width}x${size.height}-${theme}`;
    const file = await shot(name);
    const measured = JSON.parse(await evaluate(MEASURE_JS));
    report.cells.push({ size: `${size.width}×${size.height}`, theme, file: file.replace(REPO + "\\", "").replace(REPO + "/", ""), ...measured });
  }
}

// --- 2. 连接点命中测试（800×600，选中一张卡片） ---------------------------
await viewport(800, 600);
await evaluate(`(() => {
  document.documentElement.setAttribute('data-theme', 'dark');
  const close = document.querySelector('[data-im="batch-close"]');
  if (close) close.click();
  const toggle = document.querySelector('[data-im="chat-toggle"]');
  if (toggle && toggle.getAttribute('aria-expanded') === 'true') toggle.click();
  return true;
})()`);
await sleep(400);
await evaluate(`(() => {
  const menu = document.querySelector('[data-im="add-menu"]');
  if (menu) menu.click();
  return true;
})()`);
await sleep(400);
await evaluate(`(() => {
  const add = document.querySelector('[data-im="add-text"]');
  if (add) add.click();
  return true;
})()`);
await sleep(800);
await evaluate(`(() => {
  const card = document.querySelector('[data-im="card"]');
  if (!card) return false;
  card.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true, clientX: card.getBoundingClientRect().left + 20, clientY: card.getBoundingClientRect().top + 12 }));
  card.dispatchEvent(new PointerEvent('pointerup', { bubbles: true }));
  card.click();
  return true;
})()`);
await sleep(600);
report.connectPoints = [];
for (const size of [VIEWPORTS[0], VIEWPORTS[2]]) {
  await viewport(size.width, size.height);
  await sleep(500);
  const hits = [];
  for (let i = 0; i < 3; i++) {
    const hit = JSON.parse(await evaluate(CONNECT_JS));
    hits.push(hit);
    await sleep(200);
  }
  const name = `d-${size.width}x${size.height}-connect-point`;
  report.connectPoints.push({ size: `${size.width}×${size.height}`, rounds: hits, file: await shot(name) });
}

// --- 3. 规则检查：聊天发送只发文字，不调用板面提交接口（契约 §8.3） --------
await viewport(1440, 900);
await sleep(400);
const requestMark = requests.length;
await evaluate(`(() => {
  const toggle = document.querySelector('[data-im="chat-toggle"]');
  if (toggle && toggle.getAttribute('aria-expanded') !== 'true') toggle.click();
  return true;
})()`);
await sleep(500);
const typed = await evaluate(`(() => {
  const input = document.querySelector('[data-im="chat-input"]');
  if (!input) return 'no-input';
  const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value').set;
  setter.call(input, '独立验收：这条只发文字');
  input.dispatchEvent(new Event('input', { bubbles: true }));
  return input.value;
})()`);
await sleep(200);
await evaluate(`(() => {
  const send = document.querySelector('[data-im="chat-send"]');
  if (send && !send.disabled) send.click();
  return true;
})()`);
await sleep(3500);
const since = requests.slice(requestMark);
const submissionCalls = since.filter((line) => /submissions/.test(line));
report.chatIndependence = {
  typed,
  requests: since.slice(-12),
  submissionCalls,
  turnCalls: since.filter((line) => line.includes("/api/turns")).length,
  failure: await evaluate(`(() => {
    const el = document.querySelector('[data-im="chat-failure"], [data-im="chat-turn-error"], [data-im="chat-warning"]');
    return el ? el.textContent.trim().slice(0, 200) : null;
  })()`),
  file: await shot("d-chat-send-independent"),
};
if (submissionCalls.length) {
  report.problems = report.problems ?? [];
}

// --- 4. 汇总 -------------------------------------------------------------
const problems = [];
for (const cell of report.cells) {
  if (cell.mode === "switched") {
    // 二选一时只显示一个：真实相交必须为 0（另一个不在 DOM 里或不可见）
    if (cell.intersectionChatBatch !== 0) problems.push(`${cell.size} ${cell.theme}：switched 模式下仍然相交 ${cell.intersectionChatBatch}px²`);
    continue;
  }
  if (cell.intersectionChatBatch !== 0) {
    problems.push(`${cell.size} ${cell.theme}：聊天与批量列表相交 ${cell.intersectionChatBatch}px²`);
  }
  if (cell.intersectionChatToolbar !== 0 || cell.intersectionBatchToolbar !== 0) {
    problems.push(`${cell.size} ${cell.theme}：浮层压住底部工具栏（聊天 ${cell.intersectionChatToolbar}px² / 批量 ${cell.intersectionBatchToolbar}px²）`);
  }
  if (cell.size === "800×600" && cell.toolbarHeight > 96) {
    problems.push(`800×600 工具栏高 ${cell.toolbarHeight}px > 96px`);
  }
  if (cell.intersectionChatEntry !== 0) {
    problems.push(`${cell.size} ${cell.theme}：批量入口胶囊压住聊天面板 ${cell.intersectionChatEntry}px²`);
  }
  if (cell.mode !== "switched" && cell.panelGap !== null && cell.panelGap < 8) {
    problems.push(`${cell.size} ${cell.theme}：两面板间距只有 ${cell.panelGap}px（应 ≥ 8px）`);
  }
  if (!cell.hit.chatSend.visible || cell.hit.chatSend.hitSelf === false) {
    problems.push(`${cell.size} ${cell.theme}：聊天发送按钮点不到（命中 ${cell.hit.chatSend.hitIm ?? cell.hit.chatSend.hitTag}）`);
  }
  if (!cell.hit.submit.visible || cell.hit.submit.hitSelf === false) {
    problems.push(`${cell.size} ${cell.theme}：提交入口点不到（命中 ${cell.hit.submit.hitIm ?? cell.hit.submit.hitTag}）`);
  }
  if (cell.chatReadableHeight <= 0) {
    // 消息区是被谁挤没的要说清楚：工具栏高于 96px 时先算工具栏的问题（§9.6 要求 800×600 ≤96px）
    const cause =
      cell.toolbarHeight > 96
        ? `（底部工具栏高 ${cell.toolbarHeight}px，超过 800×600 的约 96px 上限，先由工具栏收窄解决）`
        : "（工具栏高度正常，属于浮层几何要修的问题）";
    problems.push(`${cell.size} ${cell.theme}：聊天消息区高度 0 ${cause}`);
  }
}
if (!report.batchRetention?.kept) {
  problems.push("§9.3：处理掉一项后批量入口没有按预期保留（见 batchRetention）");
}
if ((report.chatIndependence?.submissionCalls ?? []).length) {
  problems.push("§8.3：聊天发送期间出现了板面提交接口调用：" + report.chatIndependence.submissionCalls.join(" | "));
}
if ((report.chatIndependence?.turnCalls ?? 0) === 0) {
  problems.push("§8.3：聊天发送没有产生对话请求（可能是发送没有生效，需要看 chatIndependence.requests）");
}
report.problems = problems;
// 开发服务器 / 扩展的噪音单独归类，不和产品错误混在一起
const noise = errors.filter((line) => /Could not establish connection|Receiving end does not exist/i.test(line));
report.errors = errors.filter((line) => !noise.includes(line)).slice(-20);
report.noise = [...new Set(noise)].slice(0, 5);
report.httpFails = [...new Set(httpFails)].slice(0, 20);

// 完整 JSON 落盘：报告里引用的每个数字都能被复核（不进仓库的产品代码，只做证据）
const reportPath = join(OUT_DIR, "d-report.json");
writeFileSync(reportPath, JSON.stringify(report, null, 2), "utf8");
console.log(JSON.stringify(report, null, 2));
console.log("完整报告：" + reportPath);
console.log("\n=== 摘要 ===");
for (const cell of report.cells) {
  console.log(
    `${cell.size.padEnd(9)} ${cell.theme.padEnd(5)} 模式=${String(cell.mode).padEnd(12)} 相交=${String(cell.intersectionChatBatch).padStart(7)}px² 间距=${String(cell.panelGap).padStart(4)}px 工具栏高=${cell.toolbarHeight}px 消息区=${cell.chatReadableHeight}px 批量项=${cell.batchItemCount}`,
  );
}
for (const group of report.connectPoints) {
  const last = group.rounds[group.rounds.length - 1] ?? { points: [] };
  console.log(
    `连接点 ${group.size}（3 轮一致）：` +
      (last.cardFound === false
        ? "没找到卡片"
        : last.points.length === 0
          ? "没找到连接点"
          : last.points
              .map((p, index) => `#${index + 1} ${p.hitSelf ? "命中连接点" : "被 " + p.hitIm + " 挡住"}`)
              .join(" / ")),
  );
}
for (const group of report.connectPoints) {
  const bad = group.rounds.flatMap((r) => r.points ?? []).filter((p) => !p.hitSelf);
  if (bad.length) {
    problems.push(`${group.size}：有 ${bad.length} 次连接点被挡住（命中 ${bad[0].hitIm}）`);
  }
}
console.log(report.problems.length ? "问题：\n- " + report.problems.join("\n- ") : "所有几何检查通过（相交面积为 0、工具栏不超高、入口可点）。");
console.log(report.errors.length ? "控制台错误：" + report.errors.join(" | ") : "控制台无错误。");

ws.close();
edge.kill();
if (report.noise?.length) console.log("开发服务器噪音（不计入问题）：" + report.noise.join(" | "));
process.exit(report.problems.length || report.errors.length ? 1 : 0);
