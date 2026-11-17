// QIO 互动模式底部操作区 · 最终取证 frames.mjs（阶段 2，verifier）
//
// 目标：改动完成后，对 qio-action 的 /#/interactive 做可复跑的实机取证：
//   (a) 1000x600 收起/展开截图 + 逐帧数值（data-ic-* 契约 + 两段 SVG dasharray）
//   (b) 收起 / 快速反向 / 展开途中拖动 / 窗口变宽变矮 的连续性与夹取
//   (c) 点击/聚焦输入框自动展开、把手点击、键盘 Enter/Space/↑/↓/Home/End、Esc 层级、拖动、减少动画
//   (e) 1440x900 / 1000x600 / 800x600 / 720x600 收起+展开截图与命中
//   (f) 亮/暗主题
//   (g) 排队中 / 待确认入口状态（fake provider 真 SSE）
//   (h) 开合前后草稿/附件/滚动位置保持、发送/模式切换仍走原流程、chatFrameState 收起/卸载为 null
//   (i) summary.json + report.md（中文，逐项 PASS/FAIL/UNVERIFIED + 证据文件名）
//
// 跑法（由 run.ps1 起服务后调用）：
//   node scripts/verify-interactive-action/frames.mjs                 # 全部
//   node scripts/verify-interactive-action/frames.mjs --only=motion   # 单场景
// 环境变量：QIO_IA_BASE / QIO_IA_API / QIO_IA_PROVIDER / QIO_IA_OUT
//
// 纪律：不改产品代码/测试；所有断言读真实 UI / 真实 provider 台账；
//      做不到的项如实写 UNVERIFIED（绝不伪造 PASS）；浏览器层 = 系统 Edge + CDP（无 Playwright）。
import { mkdirSync, writeFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { CdpBrowser, Page, sleep } from "../verify-pm-qio032/cdp.mjs";

const HERE = dirname(fileURLToPath(import.meta.url));
const ROOT = resolve(process.env.QIO_IA_ROOT || join(HERE, "..", ".."));
const BASE = process.env.QIO_IA_BASE || "http://127.0.0.1:5199";
const API = process.env.QIO_IA_API || "http://127.0.0.1:8734";
const PROVIDER = process.env.QIO_IA_PROVIDER || "http://127.0.0.1:8807";
const OUT = resolve(process.env.QIO_IA_OUT || join(ROOT, "_lead-logs", "interactive-action", "final"));
const SHOTS = join(OUT, "shots");
mkdirSync(SHOTS, { recursive: true });
const CDP_PORT = Number(process.env.QIO_CDP_PORT || "9333");
const ONLY = (process.argv.find((a) => a.startsWith("--only=")) || "").split("=")[1] || process.env.QIO_IA_ONLY || "";
const RUN_ID = new Date().toISOString().replace(/[-:T.]/g, "").slice(0, 14);

// 到角目标：PLAN §5-1「约 0.22s」；预览实测 218.4ms（preview-baseline.json）。
// PASS 窗口取 216±40ms（含真实几何与帧量化的合理偏差）；超出即 FAIL，不粉饰。
// 到角目标：Lead 裁决区间 0.19–0.25s（预览基准 216–218ms）；PLAN 原文「约 0.22s」。
const THRESHOLDS = {
  toCornerTargetMs: 216,
  toCornerMinMs: 190,
  toCornerMaxMs: 250,
  settleMaxMs: 1200,
};

const MANUAL_STEPS = [
  "真实厂商 SSE / Tauri 原生窗口 / Windows 原生拖放不在本 harness 边界内（fake provider 只证明 QIO 自己的链路）。",
  "附件只验证「真实文件选择器 → 上传/登记 → 开合后 chip 仍在」；图片/音频/视频的真实可读性与交付形态不在本轮（属另一条验收链路）。",
  "触屏 / 触控笔手势未覆盖（鼠标指针 + 键盘已覆盖）。",
];

const results = [];
const metrics = {};
const consoleErrors = [];
let page = null;
let stepNo = 0;

function record(name, ok, detail) {
  const status = ok === null ? "UNVERIFIED" : ok ? "PASS" : "FAIL";
  results.push({ name: name, status: status, detail: detail === undefined ? null : detail });
  console.log("[" + status + "] " + name + (detail === undefined ? "" : " :: " + JSON.stringify(detail).slice(0, 800)));
  return status;
}
function note(text) { console.log("      · " + text); }
async function shot(name) {
  stepNo += 1;
  const file = join(SHOTS, String(stepNo).padStart(2, "0") + "-" + name + ".png");
  await page.screenshot(file);
  return file;
}
function round(value, digits) {
  if (value === null || value === undefined || !isFinite(value)) return null;
  const factor = Math.pow(10, digits === undefined ? 1 : digits);
  return Math.round(value * factor) / factor;
}

/* ── fake provider 控制面（与 verify-pm-qio032/e2e.mjs 同一协议） ─────────── */
async function providerReset() {
  await fetch(PROVIDER + "/__reset", { method: "POST", headers: { "content-type": "application/json" }, body: "{}" });
}
async function providerScript(steps) {
  const r = await fetch(PROVIDER + "/__script", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ steps: steps }) });
  return r.status;
}
async function providerLog() {
  try {
    const r = await fetch(PROVIDER + "/__log");
    const j = await r.json();
    return (j && j.requests) || [];
  } catch (error) { return []; }
}
function chatRequests(log) { return log.filter((r) => String(r.path || r.url || "").indexOf("chat/completions") >= 0); }
function bodyText(req) {
  const b = req && (req._body !== undefined ? req._body : req.body !== undefined ? req.body : req.json !== undefined ? req.json : req.payload);
  if (typeof b === "string") return b;
  if (b && typeof b === "object") { try { return JSON.stringify(b); } catch (error) { return ""; } }
  try { return JSON.stringify(req || ""); } catch (error) { return ""; }
}
async function waitTurnsIdle(timeout) {
  const deadline = Date.now() + (timeout || 120000);
  while (Date.now() < deadline) {
    try {
      const snap = await (await fetch(API + "/api/turns/queue")).json();
      if (!snap.running && !(snap.queued || []).length) return true;
    } catch (error) { /* 后端忙：继续等 */ }
    await sleep(500);
  }
  return false;
}

/* ── 正常界面入口 / 板面卡片（复用 verify-pm-qio032/e2e.mjs 的既有做法） ──── */
async function waitAppReady(timeout) {
  await page.waitFor("document.querySelector('.stream') && document.querySelector('#composer-mode-switch .mode-btn')", { timeout: timeout || 45000, label: "对话页就绪" });
  await sleep(600);
}
async function enterInteractive() {
  const before = await page.attr("#composer-mode-switch .mode-btn", "data-current-mode");
  await page.click("#composer-mode-switch .mode-btn");
  await page.waitFor("document.querySelector('[data-im=\"board\"]')", { timeout: 30000, label: "互动板面" });
  await sleep(1300);
  const after = await page.attr("#composer-mode-switch .mode-btn", "data-current-mode");
  const hash = await page.eval("location.hash");
  return { before: before, after: after, hash: hash };
}
async function backToConversation() {
  await page.click("#composer-mode-switch .mode-btn");
  await page.waitFor("!document.querySelector('[data-im=\"board\"]')", { timeout: 20000, label: "回到对话页" });
  await sleep(900);
}
async function cardCount() { return page.count('[data-im="card"]'); }
async function openAddMenu(kind) {
  const before = await cardCount();
  await page.click('[data-im="add-menu"]');
  await page.waitFor("document.querySelector('[data-im=\"add-menu-list\"]')", { timeout: 8000, label: "添加菜单" });
  await page.click(kind === "file" ? '[data-im="add-file"]' : '[data-im="add-text"]');
  await page.waitFor("document.querySelectorAll('[data-im=\"card\"]').length > " + before, { timeout: 15000, label: "新卡片出现" });
  await sleep(600);
  return { before: before, after: await cardCount() };
}
async function selectCard(index) {
  await page.click('[data-im="card"] [data-im="card-body"]', index).catch(async () => { await page.click('[data-im="card"]', index); });
  await sleep(400);
  return { selected: /selected/.test((await page.attr('[data-im="card"]', "class", index)) || "") };
}
async function clickToolbar(hook) {
  await page.waitFor("document.querySelector('[data-im=\"card-toolbar-host\"] [data-im=" + JSON.stringify(hook) + "]')", { timeout: 8000, label: "工具栏 " + hook });
  const info = await page.box('[data-im="card-toolbar-host"] [data-im="' + hook + '"]', 0);
  if (!info || !info.visible) throw new Error("工具栏控件不可见：" + hook);
  await page.clickAt(info.cx, info.cy, info);
  return info;
}
async function setTextCardContent(index, text) {
  await selectCard(index);
  if (!(await page.exists('[data-im="card-editor"]'))) {
    await clickToolbar("card-icon-edit").catch(() => null);
    await sleep(600);
  }
  const editorSel = '[data-im="card-editor"]';
  if (!(await page.exists(editorSel))) return { ok: false, reason: "没有找到卡片编辑器" };
  const target = await page.eval("(function(){ const host=document.querySelector(" + JSON.stringify(editorSel) + "); const t=host && (host.matches('textarea')?host:host.querySelector('textarea,[contenteditable=true]')); return t ? (t.tagName.toLowerCase()==='textarea'?'ta':'ce') : null; })()");
  if (target === "ta") {
    await page.eval("(function(){ const host=document.querySelector(" + JSON.stringify(editorSel) + "); const t=host.matches('textarea')?host:host.querySelector('textarea'); t.focus(); t.select(); return true; })()");
    await page.browser.send("Input.insertText", { text: text }, page.sessionId);
    await page.eval("(function(){ const host=document.querySelector(" + JSON.stringify(editorSel) + "); const t=host.matches('textarea')?host:host.querySelector('textarea'); t.dispatchEvent(new Event('input',{bubbles:true})); return true; })()");
  } else {
    await page.eval("(function(){ const host=document.querySelector(" + JSON.stringify(editorSel) + "); const t=host.querySelector('[contenteditable=true]')||host; t.focus(); document.execCommand('selectAll'); return true; })()");
    await page.browser.send("Input.insertText", { text: text }, page.sessionId);
  }
  await sleep(400);
  await page.pressKey("Escape");
  await sleep(700);
  if (await page.exists(editorSel)) {
    await clickToolbar("card-icon-edit").catch(() => null);
    await sleep(500);
  }
  const body = await page.text('[data-im="card"] [data-im="card-body"]', index);
  return { ok: body.indexOf(text.slice(0, 12)) >= 0 && !(await page.exists(editorSel)), body: body.slice(0, 120) };
}

/* ── 键盘（cdp.mjs 的 pressKey 没有 Space/Home/End，这里补齐） ─────────────── */
const KEY_TABLE = {
  Enter: { vk: 13, code: "Enter", key: "Enter", text: "\r" },
  Space: { vk: 32, code: "Space", key: " ", text: " " },
  Escape: { vk: 27, code: "Escape" },
  Tab: { vk: 9, code: "Tab" },
  ArrowUp: { vk: 38, code: "ArrowUp" },
  ArrowDown: { vk: 40, code: "ArrowDown" },
  ArrowLeft: { vk: 37, code: "ArrowLeft" },
  ArrowRight: { vk: 39, code: "ArrowRight" },
  Home: { vk: 36, code: "Home" },
  End: { vk: 35, code: "End" },
  Backspace: { vk: 8, code: "Backspace" },
};
async function pressKey(key) {
  const table = KEY_TABLE[key];
  if (!table) throw new Error("未支持的按键：" + key);
  const dispatchKey = table.key || key;
  await page.browser.send("Input.dispatchKeyEvent", { type: "rawKeyDown", key: dispatchKey, code: table.code, windowsVirtualKeyCode: table.vk }, page.sessionId);
  if (table.text) {
    await page.browser.send("Input.dispatchKeyEvent", { type: "char", key: dispatchKey, text: table.text, unmodifiedText: table.text }, page.sessionId).catch(() => null);
  }
  await page.browser.send("Input.dispatchKeyEvent", { type: "keyUp", key: dispatchKey, code: table.code, windowsVirtualKeyCode: table.vk }, page.sessionId);
  await sleep(160);
}

/* ── 开合状态控制 / 动效等待 ──────────────────────────────────────────────── */
async function conversationAttr(name) { return page.attr('[data-ic="conversation"]', name); }
async function isExpanded() { return (await conversationAttr("data-ic-expanded")) === "true"; }
async function waitExpanded(want, timeout) {
  await page.waitFor("(function(){ var r = document.querySelector('[data-ic=\"conversation\"]'); return !!r && r.getAttribute('data-ic-expanded') === '" + (want ? "true" : "false") + "'; })()", { timeout: timeout || 10000, label: "expanded=" + want });
}
async function motionSignature() {
  return page.eval("(function(){ var r = document.querySelector('[data-ic=\"conversation\"]'); if (!r) return ''; var s = getComputedStyle(r); return (r.getAttribute('data-ic-reveal') || '') + '|' + s.getPropertyValue('--ic-stream-h') + '|' + s.getPropertyValue('--ic-panel-h'); })()");
}
async function waitMotionIdle(timeout) {
  const deadline = Date.now() + (timeout || 2500);
  let prev = null;
  let stable = 0;
  while (Date.now() < deadline) {
    const sig = await motionSignature();
    if (sig === prev) stable += 1; else { stable = 0; prev = sig; }
    if (stable >= 2) return true;
    await sleep(90);
  }
  return false;
}
async function realClick(selector, index) {
  const info = await page.box(selector, index || 0);
  if (!info || !info.visible) throw new Error("点不到（不可见）：" + selector);
  const base = { x: info.cx, y: info.cy, button: "left", buttons: 1, clickCount: 1 };
  await page.browser.send("Input.dispatchMouseEvent", { type: "mouseMoved", x: info.cx, y: info.cy, button: "none", buttons: 0 }, page.sessionId);
  await page.browser.send("Input.dispatchMouseEvent", { type: "mousePressed", ...base }, page.sessionId);
  await sleep(24);
  await page.browser.send("Input.dispatchMouseEvent", { type: "mouseReleased", ...base, buttons: 0 }, page.sessionId);
  return info;
}
/** 不等待的“真指针”点击（按下/松开同一坐标、几乎无间隔）：用于动画中把手还在移动的场景。 */
async function quickClick(selector) {
  const info = await page.eval("(function(){ var el=document.querySelector(" + JSON.stringify(selector) + "); if (!el) return null; var r=el.getBoundingClientRect(); if (!(r.width>0&&r.height>0)) return null; return { cx: r.x+r.width/2, cy: r.y+r.height/2 }; })()");
  if (!info) throw new Error("点不到（不可见）：" + selector);
  const base = { x: info.cx, y: info.cy, button: "left", buttons: 1, clickCount: 1 };
  await page.browser.send("Input.dispatchMouseEvent", { type: "mouseMoved", x: info.cx, y: info.cy, button: "none", buttons: 0 }, page.sessionId);
  await page.browser.send("Input.dispatchMouseEvent", { type: "mousePressed", ...base }, page.sessionId);
  await page.browser.send("Input.dispatchMouseEvent", { type: "mouseReleased", ...base, buttons: 0 }, page.sessionId);
  return info;
}
/** 快速读把手中心（不 scrollIntoView、不做命中测试）：拖动/动画中抓把手用。 */
async function leverCenterFast() {
  return page.eval("(function(){ var el=document.querySelector('[data-ic-toggle]'); if (!el) return null; var r=el.getBoundingClientRect(); if (!(r.width>0&&r.height>0)) return null; return { cx: r.x+r.width/2, cy: r.y+r.height/2, x: r.x, y: r.y, width: r.width, height: r.height }; })()");
}
async function pressAt(x, y) {
  const base = { x: x, y: y, button: "left", buttons: 1, clickCount: 1 };
  await page.browser.send("Input.dispatchMouseEvent", { type: "mouseMoved", x: x, y: y, button: "none", buttons: 0 }, page.sessionId);
  await page.browser.send("Input.dispatchMouseEvent", { type: "mousePressed", ...base }, page.sessionId);
}
async function moveAt(x, y) {
  await page.browser.send("Input.dispatchMouseEvent", { type: "mouseMoved", x: x, y: y, button: "left", buttons: 1 }, page.sessionId);
}
async function releaseAt(x, y) {
  await page.browser.send("Input.dispatchMouseEvent", { type: "mouseReleased", x: x, y: y, button: "left", buttons: 0, clickCount: 1 }, page.sessionId);
}
/** 只发 mousePressed（省一次往返，用于抢在展开第一帧前抓住把手）。 */
async function pressFast(x, y) {
  await page.browser.send("Input.dispatchMouseEvent", { type: "mousePressed", x: x, y: y, button: "left", buttons: 1, clickCount: 1 }, page.sessionId);
}
/** 快速轮询 expanded=true（点击展开是同步置位，通常 1 次 eval 内就为 true）。 */
async function waitExpandedFast(timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const value = await page.eval("(function(){ var r = document.querySelector('[data-ic=\"conversation\"]'); return r ? r.getAttribute('data-ic-expanded') : null; })()");
    if (value === "true") return true;
    await sleep(4);
  }
  return false;
}
async function ensureCollapsed() {
  if (await isExpanded()) {
    await realClick('[data-ic-toggle]');
    await waitExpanded(false, 8000);
    await waitMotionIdle(2500);
  }
  await waitMotionIdle(1200);
}
async function ensureExpanded() {
  if (!(await isExpanded())) {
    await realClick('[data-ic-toggle]');
    await waitExpanded(true, 8000);
  }
  await waitMotionIdle(3000);
}

/* ── 页面内 rAF 采样器（读取 PLAN §3.2 契约 + 实时几何） ──────────────────── */
function installProbe() {
  var state = { frames: [], collecting: false, clickT: null, pointerDownT: null, t0: null };
  window.__icProbe = state;

  function rect(el) {
    if (!el) return null;
    var r = el.getBoundingClientRect();
    var rnd = function (v) { return Math.round(v * 10) / 10; };
    return { x: rnd(r.x), y: rnd(r.y), left: rnd(r.left), top: rnd(r.top), width: rnd(r.width), height: rnd(r.height), right: rnd(r.right), bottom: rnd(r.bottom), cx: rnd(r.x + r.width / 2), cy: rnd(r.y + r.height / 2) };
  }
  function num(value) { var n = parseFloat(value); return isFinite(n) ? n : null; }
  function cssVar(root, name) { if (!root) return null; var v = getComputedStyle(root).getPropertyValue(name); return v ? v.trim() : null; }
  function findScroll() {
    var host = document.querySelector('[data-ic-stream]');
    if (!host) return null;
    var nodes = [host].concat(Array.prototype.slice.call(host.querySelectorAll("*")));
    for (var i = 0; i < nodes.length; i += 1) {
      if (nodes[i].scrollHeight > nodes[i].clientHeight + 2) return nodes[i];
    }
    return null;
  }
  function snapshot() {
    var root = document.querySelector('[data-ic="conversation"]');
    var lever = document.querySelector("[data-ic-toggle]");
    var leftPath = document.querySelector('[data-ic-frame-path="left"]');
    var rightPath = document.querySelector('[data-ic-frame-path="right"]');
    var svg = document.querySelector("[data-ic-frame]");
    var glass = document.querySelector("[data-ic-glass]");
    var streamWrap = document.querySelector("[data-ic-stream-wrap]");
    var stream = document.querySelector("[data-ic-stream]");
    var rail = document.querySelector("[data-ic-rail]");
    var composer = document.querySelector("[data-ic-composer]");
    var capsule = document.querySelector("[data-ic-composer] .input-pill") || composer;
    var dash = leftPath ? leftPath.style.strokeDasharray : null;
    var visible = null;
    if (dash && dash !== "none") visible = num(String(dash).split(" ")[0]);
    var scroll = findScroll();
    var leverRect = rect(lever);
    var hitSelf = null;
    if (leverRect && leverRect.width > 0) {
      var hit = document.elementFromPoint(leverRect.cx, leverRect.cy);
      hitSelf = !!(hit && (hit === lever || lever.contains(hit)));
    }
    var activeEl = document.activeElement;
    var active = null;
    if (activeEl) active = activeEl.getAttribute && activeEl.getAttribute("data-ic-toggle") !== null ? "lever" : activeEl.id || activeEl.tagName.toLowerCase();
    var total = null;
    try { total = leftPath ? Math.round(leftPath.getTotalLength() * 100) / 100 : null; } catch (error) { total = null; }
    return {
      t: performance.now(),
      reveal: num(root ? root.getAttribute("data-ic-reveal") : null),
      expanded: root ? root.getAttribute("data-ic-expanded") : null,
      dragging: root ? root.getAttribute("data-ic-dragging") : null,
      reduced: root ? root.getAttribute("data-ic-reduced") : null,
      frameLen: num(root ? root.getAttribute("data-ic-frame-path-length") : null),
      frameHorizontal: num(root ? root.getAttribute("data-ic-frame-horizontal") : null),
      cornerReveal: num(root ? root.getAttribute("data-ic-corner-reveal") : null),
      dash: dash,
      rightDash: rightPath ? rightPath.style.strokeDasharray : null,
      visible: visible,
      totalLen: total,
      leftD: leftPath ? leftPath.getAttribute("d") : null,
      rightD: rightPath ? rightPath.getAttribute("d") : null,
      svgW: svg ? num(svg.getAttribute("width")) : null,
      svgH: svg ? num(svg.getAttribute("height")) : null,
      svgHidden: svg ? svg.hasAttribute("hidden") : null,
      glassOpacity: glass ? num(getComputedStyle(glass).opacity) : null,
      glassInset: glass ? (getComputedStyle(glass).getPropertyValue("--ic-glass-inset") || "").trim() : null,
      panelH: num(cssVar(root, "--ic-panel-h")),
      streamH: num(cssVar(root, "--ic-stream-h")),
      railSide: cssVar(root, "--ic-rail-side"),
      root: rect(root),
      rail: rect(rail),
      lever: leverRect,
      stream: rect(stream),
      streamWrap: rect(streamWrap),
      composer: rect(composer),
      capsule: rect(capsule),
      svg: rect(svg),
      leverHitSelf: hitSelf,
      scrollTop: scroll ? Math.round(scroll.scrollTop) : null,
      scrollMax: scroll ? Math.round(scroll.scrollHeight - scroll.clientHeight) : null,
      active: active,
      theme: document.documentElement.getAttribute("data-theme"),
      vw: window.innerWidth,
      vh: window.innerHeight,
    };
  }
  state.snapshot = snapshot;
  state.start = function () {
    state.frames = [];
    state.collecting = true;
    state.t0 = performance.now();
    state.clickT = null;
    state.pointerDownT = null;
    var tick = function () {
      if (!state.collecting) return;
      state.frames.push(snapshot());
      window.requestAnimationFrame(tick);
    };
    window.requestAnimationFrame(tick);
    return state.t0;
  };
  state.stop = function () { state.collecting = false; return state.frames.length; };
  document.addEventListener("pointerdown", function (event) {
    var target = event.target;
    if (target && target.closest && target.closest("[data-ic-toggle]")) state.pointerDownT = performance.now();
  }, true);
  document.addEventListener("click", function (event) {
    var target = event.target;
    if (target && target.closest && target.closest("[data-ic-toggle]")) state.clickT = performance.now();
  }, true);
  return snapshot();
}
async function ensureProbe() {
  await page.eval("(function(){ if (!window.__icProbe) { (" + installProbe.toString() + ")(); } return true; })()");
}
async function startProbe() {
  await ensureProbe();
  await page.eval("(function(){ window.__icProbe.start(); return true; })()");
}
async function stopProbe() {
  return page.eval("(function(){ var s = window.__icProbe; s.stop(); return { clickT: s.clickT, pointerDownT: s.pointerDownT, frames: s.frames }; })()");
}

/* ── 线框「真的画出来了吗」：把 svg[data-ic-frame] 序列化到离屏 canvas 数像素 ──
 * 数值属性（data-ic-frame-path-length 等）是 JS 算出来的，不代表浏览器真的画出了线；
 * 这里把 SVG 连同解析后的渐变颜色一起离屏渲染，统计非背景像素，能抓到
 * 「渐变零长度 / stop 透明 / 路径为空 / stroke 解析失败」这类“数值对但视觉没有”的问题。 */
function svgPaintProbe() {
  return new Promise(function (resolve) {
    try {
      var svg = document.querySelector("[data-ic-frame]");
      if (!svg) { resolve({ ok: false, reason: "no-svg" }); return; }
      var clone = svg.cloneNode(true);
      clone.removeAttribute("hidden");
      clone.setAttribute("xmlns", "http://www.w3.org/2000/svg");
      var origStops = svg.querySelectorAll("stop");
      var cloneStops = clone.querySelectorAll("stop");
      for (var i = 0; i < cloneStops.length; i += 1) {
        var cs = getComputedStyle(origStops[i]);
        cloneStops[i].setAttribute("stop-color", cs.stopColor || "#000000");
        cloneStops[i].setAttribute("stop-opacity", cs.stopOpacity || "1");
      }
      var origPaths = svg.querySelectorAll("path");
      var clonePaths = clone.querySelectorAll("path");
      for (var j = 0; j < clonePaths.length; j += 1) {
        var ps = getComputedStyle(origPaths[j]);
        clonePaths[j].style.stroke = ps.stroke;
        clonePaths[j].style.strokeWidth = ps.strokeWidth;
      }
      var box = svg.getBoundingClientRect();
      var w = Math.max(1, Math.round(box.width));
      var h = Math.max(1, Math.round(box.height));
      clone.setAttribute("width", String(w));
      clone.setAttribute("height", String(h));
      var xml = new XMLSerializer().serializeToString(clone);
      var url = "data:image/svg+xml;charset=utf-8," + encodeURIComponent(xml);
      var img = new Image();
      img.onload = function () {
        try {
          var canvas = document.createElement("canvas");
          canvas.width = w;
          canvas.height = h;
          var ctx = canvas.getContext("2d");
          ctx.fillStyle = "#ffffff";
          ctx.fillRect(0, 0, w, h);
          ctx.drawImage(img, 0, 0, w, h);
          var data = ctx.getImageData(0, 0, w, h).data;
          var painted = 0;
          var minX = w, maxX = 0, minY = h, maxY = 0;
          for (var q = 0; q < data.length; q += 4) {
            var r = data[q], g = data[q + 1], b = data[q + 2], a = data[q + 3];
            if (a > 10 && (Math.abs(r - 255) > 6 || Math.abs(g - 255) > 6 || Math.abs(b - 255) > 6)) {
              painted += 1;
              var idx = q / 4;
              var x = idx % w;
              var y = Math.floor(idx / w);
              if (x < minX) minX = x;
              if (x > maxX) maxX = x;
              if (y < minY) minY = y;
              if (y > maxY) maxY = y;
            }
          }
          resolve({ ok: true, painted: painted, width: w, height: h, bbox: painted ? { x: minX, y: minY, right: maxX, bottom: maxY } : null });
        } catch (error) { resolve({ ok: false, reason: "canvas:" + String(error) }); }
      };
      img.onerror = function () { resolve({ ok: false, reason: "image-load-failed" }); };
      img.src = url;
    } catch (error) { resolve({ ok: false, reason: String(error) }); }
  });
}

/* ── 逐帧分析 ─────────────────────────────────────────────────────────────── */
function parsePathStart(d) {
  if (!d) return null;
  const m = /^M\s*(-?[\d.]+)[\s,]+(-?[\d.]+)/.exec(String(d).trim());
  if (!m) return null;
  return { x: Number(m[1]), y: Number(m[2]) };
}
function analyzeFrames(frames, originT, phase) {
  const after = frames.filter((f) => f.t >= originT - 2);
  const before = frames.filter((f) => f.t < originT);
  const pre = before.length ? before[before.length - 1] : null;
  const rel = (t) => round(t - originT, 1);
  const intervals = [];
  for (let i = 1; i < after.length; i += 1) intervals.push(after[i].t - after[i - 1].t);
  intervals.sort((a, b) => a - b);
  const frameStats = {
    samples: after.length,
    intervalMedianMs: intervals.length ? round(intervals[Math.floor(intervals.length / 2)], 1) : null,
    intervalMaxMs: intervals.length ? round(intervals[intervals.length - 1], 1) : null,
    firstFrameMs: after.length ? rel(after[0].t) : null,
  };
  const last = after.length ? after[after.length - 1] : null;
  const targetStreamH = last ? last.streamH : null;
  const targetPanelH = last ? last.panelH : null;
  const startStreamH = pre ? pre.streamH : after.length ? after[0].streamH : null;
  const startPanelH = pre ? pre.panelH : after.length ? after[0].panelH : null;

  // 到角口径（Lead 2026-11-17 裁决，以动画期逐帧值为准）：
  //   当前帧 reveal * 当前帧 data-ic-frame-path-length >= 当前帧 data-ic-frame-horizontal
  // 说明：data-ic-corner-reveal 是完全展开几何下的静态比例，动画中 input 中线随高度上升，
  //       不能用静态比例判定到角；这里只把它作为对照记录。
  let cornerIndex = -1;
  let cornerSource = null;
  if (phase === "expand") {
    for (let i = 0; i < after.length; i += 1) {
      const f = after[i];
      if (f.reveal === null || f.frameLen === null || f.frameHorizontal === null) continue;
      if (f.reveal > 0 && f.reveal * f.frameLen >= f.frameHorizontal) { cornerIndex = i; cornerSource = "reveal*pathLength"; break; }
    }
    if (cornerIndex < 0) {
      for (let i = 0; i < after.length; i += 1) {
        const f = after[i];
        if (f.visible !== null && f.frameHorizontal !== null && f.visible > 0 && f.visible >= f.frameHorizontal) { cornerIndex = i; cornerSource = "dasharray"; break; }
      }
    }
  }
  let corner = null;
  if (cornerIndex >= 0) {
    const cur = after[cornerIndex];
    const prev = cornerIndex > 0 ? after[cornerIndex - 1] : null;
    let interpMs = rel(cur.t);
    let interpBasis = null;
    if (prev && prev.reveal !== null && prev.frameLen !== null && cur.reveal !== null && cur.frameLen !== null) {
      // 以 reveal*pathLength 为观测量，在当前帧与上一帧之间线性插值。
      const prevProduct = prev.reveal * prev.frameLen;
      const curProduct = cur.reveal * cur.frameLen;
      if (curProduct > prevProduct) {
        const frac = (cur.frameHorizontal - prevProduct) / (curProduct - prevProduct);
        if (frac >= 0 && frac <= 1.2) { interpMs = round(prev.t - originT + frac * (cur.t - prev.t), 1); interpBasis = "reveal*pathLength"; }
      }
    }
    if (interpBasis === null && prev && prev.visible !== null && cur.visible !== null && cur.visible > prev.visible) {
      const frac = (cur.frameHorizontal - prev.visible) / (cur.visible - prev.visible);
      if (frac >= 0 && frac <= 1.2) { interpMs = round(prev.t - originT + frac * (cur.t - prev.t), 1); interpBasis = "dasharray"; }
    }
    corner = {
      source: cornerSource,
      interpolation: interpBasis || "frame",
      frameMs: rel(cur.t),
      interpMs: interpMs,
      revealAtCross: round(cur.reveal, 4),
      cornerRevealAtCrossStatic: round(cur.cornerReveal, 4),
      frameHorizontalAtCross: round(cur.frameHorizontal, 2),
      frameLenAtCross: round(cur.frameLen, 2),
      productAtCross: cur.reveal !== null && cur.frameLen !== null ? round(cur.reveal * cur.frameLen, 2) : null,
      visibleAtCross: round(cur.visible, 2),
      nextFrameMs: cornerIndex + 1 < after.length ? rel(after[cornerIndex + 1].t) : null,
    };
  }

  // 高度 50% / 90%（用 --ic-stream-h 精细值，panelH 同时给出）。
  function crossMs(metricName, startV, targetV, fraction) {
    if (startV === null || targetV === null) return null;
    const goal = phase === "expand" ? startV + (targetV - startV) * fraction : startV * (1 - fraction);
    for (let i = 0; i < after.length; i += 1) {
      const f = after[i];
      const v = f[metricName];
      if (v === null) continue;
      const hit = phase === "expand" ? v >= goal : v <= goal;
      if (!hit) continue;
      const prev = i > 0 ? after[i - 1] : null;
      if (prev && prev[metricName] !== null && v !== prev[metricName]) {
        const frac = (goal - prev[metricName]) / (v - prev[metricName]);
        return round(prev.t - originT + frac * (f.t - prev.t), 1);
      }
      return rel(f.t);
    }
    return null;
  }
  const height50 = crossMs("streamH", startStreamH, targetStreamH, 0.5);
  const height90 = crossMs("streamH", startStreamH, targetStreamH, 0.9);
  const panel50 = crossMs("panelH", startPanelH, targetPanelH, 0.5);
  const panel90 = crossMs("panelH", startPanelH, targetPanelH, 0.9);

  // settle：最后一帧reveal/streamH仍在变化之后的第一帧。
  let lastChanging = -1;
  for (let i = 0; i < after.length; i += 1) {
    const f = after[i];
    if (f.reveal === null || f.streamH === null) continue;
    const prev = i > 0 ? after[i - 1] : null;
    if (!prev || prev.reveal === null || prev.streamH === null) continue;
    if (Math.abs(f.reveal - prev.reveal) > 0.0005 || Math.abs(f.streamH - prev.streamH) > 0.05) lastChanging = i;
  }
  const settleMs = lastChanging >= 0 && lastChanging + 1 < after.length ? rel(after[lastChanging + 1].t) : null;

  // 同帧更新：reveal 与 streamH 同一帧一起变化。
  let sameFrame = 0;
  let revealOnly = 0;
  let heightOnly = 0;
  for (let i = 1; i < after.length; i += 1) {
    const a = after[i - 1];
    const b = after[i];
    if (a.reveal === null || b.reveal === null || a.streamH === null || b.streamH === null) continue;
    const dr = Math.abs(b.reveal - a.reveal);
    const dh = Math.abs(b.streamH - a.streamH);
    if (dr > 1e-5 && dh > 0.05) sameFrame += 1;
    else if (dr > 1e-5) revealOnly += 1;
    else if (dh > 0.05) heightOnly += 1;
  }

  // 单调 / 过冲 / 每帧最大跳变（快速反向时用于判「连续更新」）。
  let monotonic = true;
  let overshoot = false;
  let maxRevealStep = 0;
  let maxHeightStep = 0;
  for (let i = 1; i < after.length; i += 1) {
    const a = after[i - 1];
    const b = after[i];
    if (a.reveal !== null && b.reveal !== null) {
      maxRevealStep = Math.max(maxRevealStep, Math.abs(b.reveal - a.reveal));
      if (phase === "expand" && b.reveal < a.reveal - 0.002) monotonic = false;
      if (phase === "collapse" && b.reveal > a.reveal + 0.002) monotonic = false;
      if (phase === "expand" && b.reveal > 1.0005) overshoot = true;
    }
    if (a.streamH !== null && b.streamH !== null) {
      maxHeightStep = Math.max(maxHeightStep, Math.abs(b.streamH - a.streamH));
      if (phase === "expand" && targetStreamH !== null && b.streamH > targetStreamH + 0.5) overshoot = true;
    }
  }

  // 左右对称 + 起点=把手端点（左路径第一个 M 点的 x 应等于把手相对面板的左边）。
  let symmetric = true;
  for (let i = 0; i < after.length; i += 1) {
    if (after[i].dash !== after[i].rightDash) symmetric = false;
  }
  let startFromLever = null;
  const settled = last;
  if (settled && settled.root && settled.lever && settled.leftD) {
    const start = parsePathStart(settled.leftD);
    const rightStart = parsePathStart(settled.rightD);
    if (start) {
      // 契约 leverInset=4：线段起点在把手端点向内 4px（conversationFrameGeometry 的 leverInset）。
      const LEVER_INSET = 4;
      const leverLeftRel = round(settled.lever.x - settled.root.x, 1);
      const expectedLeft = round(leverLeftRel + LEVER_INSET, 1);
      const expectedRight = round(settled.root.width - leverLeftRel - LEVER_INSET, 1);
      const startXRounded = round(start.x, 1);
      startFromLever = {
        leftPathStartX: startXRounded,
        expectedLeftPathStartX: expectedLeft,
        leverLeftRel: leverLeftRel,
        leverInset: LEVER_INSET,
        deltaPx: round(Math.abs(startXRounded - expectedLeft), 1),
        rightPathStartX: rightStart ? round(rightStart.x, 1) : null,
        expectedRightPathStartX: expectedRight,
        rightMirrorDeltaPx: rightStart ? round(Math.abs(rightStart.x - expectedRight), 1) : null,
        leverCenterX: settled.lever.cx,
        rootCenterX: settled.root.cx,
      };
    }
  }

  // 线框 / 玻璃 / 竖边接续。
  const frame = {
    pathLenAtSettle: settled ? round(settled.frameLen, 2) : null,
    horizontalAtSettle: settled ? round(settled.frameHorizontal, 2) : null,
    cornerRevealAtSettle: settled ? round(settled.cornerReveal, 4) : null,
    // reveal=1 时实现写 dash="none"（整条路径可见），此时可见长度=全长。
    visibleAtSettle: settled ? (settled.dash === "none" ? round(settled.totalLen, 2) : round(settled.visible, 2)) : null,
    totalLenAtSettle: settled ? round(settled.totalLen, 2) : null,
    leverTopRelPanelAtSettle: settled && settled.root && settled.lever ? round(settled.lever.y - settled.root.y, 1) : null,
    lerPanelHeightAtSettle: settled && settled.root ? round(settled.root.height, 1) : null,
    svgAtSettle: settled ? { width: settled.svgW, height: settled.svgH, hidden: settled.svgHidden } : null,
    glassOpacityAtSettle: settled ? round(settled.glassOpacity, 4) : null,
    glassInsetAtSettle: settled ? settled.glassInset : null,
    verticalContinued: settled && settled.frameLen !== null && settled.frameHorizontal !== null ? settled.frameLen > settled.frameHorizontal + 1 : null,
  };
  const endState = settled
    ? { expanded: settled.expanded, reveal: round(settled.reveal, 4), panelH: settled.panelH, streamH: round(settled.streamH, 2), root: settled.root, lever: settled.lever, capsule: settled.capsule }
    : null;

  return {
    phase: phase,
    frameStats: frameStats,
    corner: corner,
    height50Ms: height50,
    height90Ms: height90,
    panel50Ms: panel50,
    panel90Ms: panel90,
    settleMs: settleMs,
    updateCadence: { sameFrameChanges: sameFrame, revealOnlyFrames: revealOnly, heightOnlyFrames: heightOnly },
    monotonic: monotonic,
    overshoot: overshoot,
    maxRevealStep: round(maxRevealStep, 4),
    maxHeightStep: round(maxHeightStep, 2),
    symmetricStrokes: symmetric,
    startFromLever: startFromLever,
    frameGeometry: frame,
    startState: pre ? { expanded: pre.expanded, reveal: pre.reveal, panelH: pre.panelH, streamH: pre.streamH } : null,
    endState: endState,
    framesUsed: after.length,
  };
}

async function measureToggle(phase, options) {
  const opts = options || {};
  await startProbe();
  const clickInfo = await realClick("[data-ic-toggle]");
  await sleep(opts.settleWaitMs === undefined ? 1600 : opts.settleWaitMs);
  const data = await stopProbe();
  const origin = data.clickT !== null ? data.clickT : data.pointerDownT;
  if (origin === null) throw new Error("采样里没有拿到点击时间（pointerdown/click 监听没命中把手？）");
  return {
    clickInfo: { cx: round(clickInfo.cx, 1), cy: round(clickInfo.cy, 1), width: round(clickInfo.width, 1), height: round(clickInfo.height, 1) },
    pressToClickMs: data.pointerDownT !== null ? round(data.clickT - data.pointerDownT, 1) : null,
    frames: data.frames,
    metrics: analyzeFrames(data.frames, origin, phase),
  };
}

/* ── 场景：(契约) DOM 契约 / 可测接口快照 ─────────────────────────────────── */
async function scenarioContract() {
  // 会话流是「首次展开才挂载」（v-if openedOnce）：先展开一次再收起，保证契约里 data-ic-stream 已存在。
  await ensureExpanded();
  await ensureCollapsed();
  const contract = await page.eval("(function(){" +
    "var root = document.querySelector('[data-ic=\"conversation\"]');" +
    "var required = ['[data-ic-frame]','[data-ic-frame-path=\"left\"]','[data-ic-frame-path=\"right\"]','[data-ic-glass]','[data-ic-stream]','[data-ic-rail]','[data-ic-toggle]','[data-ic-entries]','[data-ic-composer]'];" +
    "var presence = {}; required.forEach(function(s){ presence[s] = document.querySelectorAll(s).length; });" +
    "var attrs = {};" +
    "['data-ic-expanded','data-ic-reveal','data-ic-frame-path-length','data-ic-frame-horizontal','data-ic-corner-reveal'].forEach(function(n){ attrs[n] = root ? root.getAttribute(n) : null; });" +
    "var toggle = document.querySelector('[data-ic-toggle]');" +
    "var gradientIds = [];" +
    "document.querySelectorAll('[data-ic-frame] linearGradient').forEach(function(g){ gradientIds.push(g.id); });" +
    "var v = function(n){ var s = root ? getComputedStyle(root).getPropertyValue(n) : null; return s ? s.trim() : null; };" +
    "return { presence: presence, attrs: attrs," +
    "  toggle: toggle ? { tag: toggle.tagName.toLowerCase(), role: toggle.getAttribute('role'), effectiveRole: toggle.getAttribute('role') || (toggle.tagName.toLowerCase()==='button' ? 'button' : null), resize: toggle.getAttribute('data-ic-resize'), ariaExpanded: toggle.getAttribute('aria-expanded'), ariaControls: toggle.getAttribute('aria-controls'), ariaLabel: toggle.getAttribute('aria-label'), type: toggle.getAttribute('type'), hitSelf: (function(){ var r=toggle.getBoundingClientRect(); var h=document.elementFromPoint(r.x+r.width/2,r.y+r.height/2); return !!(h && (h===toggle || toggle.contains(h))); })() } : null," +
    "  gradientIds: gradientIds, pathCount: document.querySelectorAll('[data-ic-frame] path').length," +
    "  railSide: v('--ic-rail-side'), streamH: v('--ic-stream-h'), panelH: v('--ic-panel-h')," +
    "  railModeSwitch: !!(document.querySelector('[data-ic-rail] .im-mode-switch, [data-ic-rail] .mode-btn, [data-ic-rail] [data-current-mode]'))," +
    "  railToolbar: !!(document.querySelector('[data-ic-rail] .ic-rail-right button, [data-ic-rail] [data-im=\"board-more\"], [data-ic-rail] .board-toolbar'))," +
    "  railButtonCount: document.querySelectorAll('[data-ic-rail] button').length," +
    "  railText: document.querySelector('[data-ic-rail]') ? document.querySelector('[data-ic-rail]').innerText.replace(/\s+/g,' ').trim().slice(0,160) : null," +
    "  entriesCount: document.querySelectorAll('[data-ic-entries]').length, composerCount: document.querySelectorAll('[data-ic-composer]').length };" +
    "})()");
  metrics.contract = contract;
  const missingSelectors = Object.keys(contract.presence).filter((k) => !contract.presence[k]);
  const missingAttrs = Object.keys(contract.attrs).filter((k) => contract.attrs[k] === null);
  const toggleMissing = [];
  if (!contract.toggle) toggleMissing.push("toggle 缺失");
  else {
    if (contract.toggle.effectiveRole !== "button") toggleMissing.push("role=button（含隐式 button role）");
    if (contract.toggle.resize === null) toggleMissing.push("data-ic-resize");
    if (contract.toggle.ariaExpanded === null) toggleMissing.push("aria-expanded");
    if (contract.toggle.ariaControls === null) toggleMissing.push("aria-controls");
    if (contract.toggle.ariaLabel === null) toggleMissing.push("aria-label");
    if (contract.toggle.type !== "button") toggleMissing.push("type=button");
    if (!contract.toggle.hitSelf) toggleMissing.push("把手中心命中自己");
  }
  record("§3.2 组件 DOM 契约：frame/glass/stream/rail/toggle/entries/composer 全部存在", missingSelectors.length === 0, { presence: contract.presence, missing: missingSelectors });
  record("§3.2 根元素测量属性 data-ic-reveal / frame-path-length / frame-horizontal / corner-reveal 已发布", missingAttrs.length === 0, { attrs: contract.attrs, missing: missingAttrs });
  record("§3.2 把手：button + role + data-ic-resize + aria-expanded/controls/label + 中心可命中", toggleMissing.length === 0, { toggle: contract.toggle, missing: toggleMissing });
  record("§3.2/§3.3 操作行内：模式切换（左槽位内容）+ 板面工具栏（右槽位内容）+ 两张镜像 path + 唯一渐变 id", contract.railModeSwitch === true && contract.railToolbar === true && contract.pathCount >= 2 && contract.gradientIds.filter(Boolean).length >= 1, { railModeSwitch: contract.railModeSwitch, railToolbar: contract.railToolbar, railButtonCount: contract.railButtonCount, pathCount: contract.pathCount, gradientIds: contract.gradientIds, toggleExplicitRole: contract.toggle ? contract.toggle.role : null, toggleEffectiveRole: contract.toggle ? contract.toggle.effectiveRole : null });
}

/* ── 场景：(a) 1000x600 开合逐帧 + 关键帧截图 ─────────────────────────────── */
async function scenarioMotion() {
  await page.setViewport(1000, 600);
  await sleep(500);
  await ensureCollapsed();
  const collapsedSnapshot = await page.eval("window.__icProbe ? window.__icProbe.snapshot() : null");
  const collapsedShot = await shot("1000x600-collapsed");
  record("§5-1 1000×600 收起态：操作行 + 胶囊可见、线框隐藏", collapsedSnapshot && collapsedSnapshot.expanded === "false" && collapsedSnapshot.root && collapsedSnapshot.root.height > 0 && collapsedSnapshot.rail !== null && collapsedSnapshot.composer !== null && collapsedSnapshot.svgHidden === true, {
    shot: collapsedShot,
    root: collapsedSnapshot && collapsedSnapshot.root,
    rail: collapsedSnapshot && collapsedSnapshot.rail,
    composer: collapsedSnapshot && collapsedSnapshot.composer,
    svgHidden: collapsedSnapshot && collapsedSnapshot.svgHidden,
    reveal: collapsedSnapshot && collapsedSnapshot.reveal,
  });

  const expand = await measureToggle("expand", { settleWaitMs: 1800 });
  const expandedShot = await shot("1000x600-expanded");
  const strokePixels = await page.eval("(" + svgPaintProbe.toString() + ")()").catch((error) => ({ ok: false, reason: String(error) }));
  metrics.strokeVisibility = strokePixels;
  record("§5-1 线框实际可见：SVG 离屏渲染存在非背景像素（防「数值有路径但视觉不可见」）", strokePixels && strokePixels.ok === true && strokePixels.painted > 50, strokePixels);
  metrics.expand1000 = { metrics: expand.metrics, clickInfo: expand.clickInfo };
  const m = expand.metrics;
  const capsuleMidline = m.endState && m.endState.capsule && m.endState.root ? round(m.endState.capsule.cy - m.endState.root.y, 1) : null;
  const svgEndY = m.frameGeometry.svgAtSettle ? m.frameGeometry.svgAtSettle.height : null;
  record("§5-1 线框竖边终点 = 输入胶囊水平中线（淡出终点口径：.input-pill 中线）", capsuleMidline !== null && svgEndY !== null && Math.abs(svgEndY - capsuleMidline) <= 1.5, {
    svgHeight: svgEndY,
    capsuleMidlineRelPanel: capsuleMidline,
    deltaPx: capsuleMidline !== null && svgEndY !== null ? round(Math.abs(svgEndY - capsuleMidline), 1) : null,
    capsuleRect: m.endState ? m.endState.capsule : null,
    rootRect: m.endState ? m.endState.root : null,
    note: "实现口径：frame.height = inputTop + inputHeight/2（.input-pill 实测中线），SVG height 取整",
  });
  const burst = [];
  const delays = [0, 60, 120, 180, 260, 420];
  for (const delay of delays) {
    await ensureCollapsed();
    await startProbe();
    await realClick("[data-ic-toggle]");
    if (delay > 0) await sleep(delay);
    const file = await shot("1000x600-expand-" + String(delay).padStart(3, "0") + "ms");
    const elapsed = await page.eval("(function(){ var s = window.__icProbe; if (!s || s.clickT === null) return null; return performance.now() - s.clickT; })()").catch(() => null);
    await stopProbe().catch(() => null);
    burst.push({ requestedDelayMs: delay, actualElapsedMs: round(elapsed, 1), file: file });
    await sleep(900);
  }
  metrics.keyframeShots = burst;

  record("§5-1 点击把手展开：左右横线同 dash 对称增长（自把手端点向外）", m.symmetricStrokes === true && (m.startFromLever === null || (m.startFromLever.deltaPx !== null && m.startFromLever.deltaPx <= 2 && m.startFromLever.rightMirrorDeltaPx <= 2)), {
    shot: expandedShot,
    symmetricStrokes: m.symmetricStrokes,
    startFromLever: m.startFromLever,
    frameStats: m.frameStats,
  });
  const cornerMs = m.corner ? m.corner.interpMs : null;
  record("§5-1 点击→横线首次到达转角在 0.19–0.25s（目标 216ms）", cornerMs !== null && cornerMs >= THRESHOLDS.toCornerMinMs && cornerMs <= THRESHOLDS.toCornerMaxMs, {
    clickToCornerMs: cornerMs,
    frameQuantizedMs: m.corner && m.corner.frameMs,
    source: m.corner && m.corner.source,
    interpolation: m.corner && m.corner.interpolation,
    revealAtCross: m.corner && m.corner.revealAtCross,
    productAtCross: m.corner && m.corner.productAtCross,
    staticCornerRevealAtSettle: m.frameGeometry.cornerRevealAtSettle,
    target: THRESHOLDS.toCornerTargetMs,
    window: [THRESHOLDS.toCornerMinMs, THRESHOLDS.toCornerMaxMs],
    keyframes: burst,
  });
  record("§5-1 展开高度曲线：50% / 90% / settle 可测且单调无过冲", m.height50Ms !== null && m.height90Ms !== null && m.settleMs !== null && m.monotonic === true && m.overshoot === false, {
    height50Ms: m.height50Ms,
    height90Ms: m.height90Ms,
    panel50Ms: m.panel50Ms,
    panel90Ms: m.panel90Ms,
    settleMs: m.settleMs,
    monotonic: m.monotonic,
    overshoot: m.overshoot,
    endState: m.endState,
  });
  record("§5-1 高度与揭示同一帧更新（同帧变化为主）", m.updateCadence.sameFrameChanges > 0 && m.updateCadence.revealOnlyFrames <= m.updateCadence.sameFrameChanges, {
    cadence: m.updateCadence,
    maxRevealStep: m.maxRevealStep,
    maxHeightStep: m.maxHeightStep,
  });
  record("§5-1 展开结束：线框满长、竖边接续到胶囊中线、玻璃/面板几何完整", m.frameGeometry.verticalContinued === true && m.endState && m.endState.reveal >= 0.999 && m.frameGeometry.visibleAtSettle !== null && m.frameGeometry.totalLenAtSettle !== null && m.frameGeometry.visibleAtSettle >= m.frameGeometry.totalLenAtSettle - 1 && m.frameGeometry.svgAtSettle && m.frameGeometry.svgAtSettle.hidden === false, {
    frameGeometry: m.frameGeometry,
    svg: m.frameGeometry.svgAtSettle,
  });

  const collapse = await measureToggle("collapse", { settleWaitMs: 1600 });
  const collapsedAfterShot = await shot("1000x600-collapsed-after");
  metrics.collapse1000 = { metrics: collapse.metrics, clickInfo: collapse.clickInfo };
  const c = collapse.metrics;
  record("§5-1 收起：reveal→0、高度→0、无过冲、settle 有界", c.monotonic === true && c.overshoot === false && c.settleMs !== null && c.settleMs <= THRESHOLDS.settleMaxMs && c.endState && c.endState.reveal <= 0.001 && c.endState.expanded === "false", {
    shot: collapsedAfterShot,
    settleMs: c.settleMs,
    height50Ms: c.height50Ms,
    height90Ms: c.height90Ms,
    monotonic: c.monotonic,
    overshoot: c.overshoot,
    endState: c.endState,
  });
}

/* ── 场景：(b1) 快速反向开合 ──────────────────────────────────────────────── */
async function scenarioReverse() {
  await page.setViewport(1000, 600);
  await ensureCollapsed();
  await startProbe();
  await quickClick("[data-ic-toggle]");
  await sleep(90);
  // 反向点击：把手在展开途中持续移动，真指针按下/松开可能落在不同元素上（竞态）。
  // 这里用元素自身的 click() 触发同一条 Vue handler（detail=0），只测「反向时动效连续」；
  // 开合的真指针路径已由 motion 场景的真实鼠标点击验证。
  const reverseClick = await page.eval("(function(){ var el=document.querySelector('[data-ic-toggle]'); if (!el) return false; el.click(); return true; })()");
  await sleep(1500);
  const data = await stopProbe();
  const frames = data.frames;
  const origin = data.pointerDownT !== null ? data.pointerDownT : data.clickT;
  let maxStep = 0;
  let sawMidReveal = false;
  for (let i = 1; i < frames.length; i += 1) {
    const a = frames[i - 1];
    const b = frames[i];
    if (b.t < origin) continue;
    if (a.reveal !== null && b.reveal !== null) {
      maxStep = Math.max(maxStep, Math.abs(b.reveal - a.reveal));
      if (b.reveal > 0.15 && b.reveal < 0.85) sawMidReveal = true;
    }
  }
  let final = frames[frames.length - 1];
  let retried = false;
  if (final.expanded === "true") {
    retried = true;
    await ensureCollapsed();
    await sleep(300);
    final = await page.eval("(function(){ var r = document.querySelector('[data-ic=\"conversation\"]'); return { reveal: parseFloat(r.getAttribute('data-ic-reveal')), expanded: r.getAttribute('data-ic-expanded') }; })()");
  }
  const reverseShot = await shot("1000x600-rapid-reverse");
  metrics.rapidReverse = { frames: frames.length, maxRevealStep: round(maxStep, 4), sawMidReveal: sawMidReveal, final: { reveal: final.reveal, expanded: final.expanded }, retriedToSettle: retried, reverseClickDispatched: reverseClick, method: "expand=quick real click; reverse=element.click()" };
  record("§5-2 快速反向开合：从当前画面连续更新（无跳变），最终停在收起", maxStep <= 0.35 && final.expanded === "false" && final.reveal !== null && final.reveal <= 0.01 && sawMidReveal && retried === false, {
    shot: reverseShot,
    maxRevealStep: round(maxStep, 4),
    sawMidReveal: sawMidReveal,
    frames: frames.length,
    final: { reveal: final.reveal, expanded: final.expanded },
    retriedToSettle: retried,
    method: "展开=真指针 quickClick；反向=element.click()（把手移动中避免指针竞态，见 README）",
  });
}

/* ── 场景：(b2) 展开途中拖动 / 松手保留 / Esc 取消 / 高度记忆 ─────────────── */
async function readPanelHeight() {
  return page.eval("(function(){ var r = document.querySelector('[data-ic=\"conversation\"]'); return r ? parseFloat(getComputedStyle(r).getPropertyValue('--ic-panel-h')) : null; })()");
}
async function scenarioDrag() {
  await page.setViewport(1000, 600);
  await ensureCollapsed();
  const basePanelH = await readPanelHeight();

  // 展开途中抓把手：点击展开后等 ~150ms（仍在展开中），用「快速读矩形 + 立即按下」抓住移动中的把手。
  // 抓握后高度目标由拖动接管；上移 4 x 20px 应让高度从抓握瞬间继续跟手（且在 220–280 之间可区分）。
  await startProbe();
  let lever0 = null;
  let pressAttempts = 0;
  const sampleDuring = [];
  let draggingSeen = false;
  let pressPanelH = null;
  for (let attempt = 1; attempt <= 3 && !draggingSeen; attempt += 1) {
    pressAttempts = attempt;
    await ensureCollapsed();
    await sleep(300);
    const collapsedLever = await leverCenterFast();
    await pressAt(collapsedLever.cx, collapsedLever.cy);
    await sleep(8);
    await releaseAt(collapsedLever.cx, collapsedLever.cy);
    await sleep(150);
    lever0 = await leverCenterFast();
    if (!lever0) throw new Error("展开途中读不到把手矩形");
    await pressFast(lever0.cx, lever0.cy);
    await sleep(25);
    const pressState = await page.eval("(function(){ var r=document.querySelector('[data-ic=\"conversation\"]'); return { dragging: r.getAttribute('data-ic-dragging'), panelH: parseFloat(getComputedStyle(r).getPropertyValue('--ic-panel-h')), expanded: r.getAttribute('data-ic-expanded') }; })()");
    pressPanelH = pressState.panelH;
    await moveAt(lever0.cx, lever0.cy - 20);
    await sleep(40);
    const probeState = await page.eval("(function(){ var r=document.querySelector('[data-ic=\"conversation\"]'); return { dragging: r.getAttribute('data-ic-dragging'), panelH: parseFloat(getComputedStyle(r).getPropertyValue('--ic-panel-h')), expanded: r.getAttribute('data-ic-expanded') }; })()");
    sampleDuring.push(probeState);
    if (probeState.dragging === "true") { draggingSeen = true; break; }
    await releaseAt(lever0.cx, lever0.cy - 20);
    await sleep(400);
  }
  for (let i = 2; i <= 3 && draggingSeen; i += 1) {
    await moveAt(lever0.cx, lever0.cy - i * 20);
    await sleep(45);
    sampleDuring.push(await page.eval("(function(){ var r=document.querySelector('[data-ic=\"conversation\"]'); return { dragging: r.getAttribute('data-ic-dragging'), panelH: parseFloat(getComputedStyle(r).getPropertyValue('--ic-panel-h')), expanded: r.getAttribute('data-ic-expanded') }; })()"));
  }
  await releaseAt(lever0.cx, lever0.cy - 60);
  await sleep(900);
  const data = await stopProbe();
  const afterRelease = await page.eval("(function(){ var r=document.querySelector('[data-ic=\"conversation\"]'); var keys = Object.keys(localStorage).filter(function(k){ return k.indexOf('qio-interactive-conversation:') === 0; }); return { panelH: parseFloat(getComputedStyle(r).getPropertyValue('--ic-panel-h')), expanded: r.getAttribute('data-ic-expanded'), stored: keys.map(function(k){ return k + '=' + localStorage.getItem(k); }) }; })()");
  const dragShot = await shot("1000x600-drag-during-expand");
  const heights = sampleDuring.map((s) => s.panelH);
  const pressHeight = pressPanelH;
  const draggingHeights = sampleDuring.filter((s) => s.dragging === "true").map((s) => s.panelH);
  const dragMin = draggingHeights.length ? Math.min.apply(null, draggingHeights) : null;
  const dragMax = draggingHeights.length ? Math.max.apply(null, draggingHeights) : null;
  const dragRange = dragMin !== null && dragMax !== null ? round(dragMax - dragMin, 1) : null;
  let maxFrameStep = 0;
  for (let i = 1; i < draggingHeights.length; i += 1) maxFrameStep = Math.max(maxFrameStep, Math.abs(draggingHeights[i] - draggingHeights[i - 1]));
  const smoothFollow = maxFrameStep <= 60;
  const grewWhileDragging = dragRange !== null && dragRange >= 10 && smoothFollow;
  const lastDragHeight = draggingHeights.length ? draggingHeights[draggingHeights.length - 1] : null;
  metrics.dragDuringExpand = { samples: sampleDuring, afterRelease: afterRelease, basePanelH: basePanelH, pressHeight: pressHeight, frames: data.frames.length, pressAttempts: pressAttempts };
  record("§5-2 展开途中拖动把手：data-ic-dragging=true，高度连续跟手（无跳变），保持展开", draggingSeen && grewWhileDragging && afterRelease.expanded === "true", {
    shot: dragShot,
    draggingSeen: draggingSeen,
    pressAttempts: pressAttempts,
    dragRange: dragRange,
    maxFrameStep: maxFrameStep,
    samples: sampleDuring,
    afterRelease: afterRelease,
  });
  const retainedH = afterRelease.panelH;
  record("§5-2 松手保留拖动后的高度（与拖动最后一个采样一致，且写回记忆）", retainedH !== null && lastDragHeight !== null && Math.abs(retainedH - lastDragHeight) <= 5 && afterRelease.stored.length > 0, {
    basePanelH: basePanelH,
    pressHeight: pressHeight,
    dragMin: dragMin,
    dragMax: dragMax,
    retainedPanelH: retainedH,
    lastDragHeight: lastDragHeight,
    retainedBelowDefault280: retainedH !== null ? retainedH < 280 : null,
    stored: afterRelease.stored,
    note: "高度口径 min=220 max=280（600 高窗口）；拖动生效证据看 data-ic-dragging + 连续跟手 + 松手后高度=最后一帧",
  });

  // 拖动松手：同一次 pointerup 的 click（detail>0）应被 suppressClick 吞掉，不收起对话
  const afterDragStillExpanded = await isExpanded();
  const afterDragHeight = await readPanelHeight();
  record("§5-2 拖动超过阈值后松手：同一次 pointerup 的 click 不误触收起（suppressClick）", afterDragStillExpanded === true && afterDragHeight !== null && retainedH !== null && Math.abs(afterDragHeight - retainedH) <= 2, {
    afterDragStillExpanded: afterDragStillExpanded,
    retainedPanelH: retainedH,
    measuredPanelH: afterDragHeight,
    note: "松手那一下的 click 由 suppressClick 吞掉一次；下一次 pointerdown 会重置",
  });

  // 下一次真实点击仍能收起（click 链路没有被吞死）
  await realClick("[data-ic-toggle]");
  const nextClickCollapsed = await waitExpanded(false, 8000).then(() => true).catch(() => false);
  await waitMotionIdle(2500);
  record("§5-2 被 suppressClick 吞掉一次后：下一次真实点击仍能收起", nextClickCollapsed === true, {
    nextClickCollapsed: nextClickCollapsed,
  });

  // 高度记忆：再展开恢复拖动松手后的高度
  const remembered = retainedH;
  await ensureExpanded();
  const reopened = await readPanelHeight();
  record("§5-2 收起再展开：高度记忆恢复（拖动松手后的高度）", reopened !== null && remembered !== null && Math.abs(reopened - remembered) <= 2, {
    remembered: remembered,
    reopened: reopened,
  });

  // Esc 取消拖动：按住并移动 → Esc → 取消瞬间高度恢复且仍展开
  await ensureExpanded();
  const beforeEsc = await readPanelHeight();
  const lever2 = await leverCenterFast();
  await pressAt(lever2.cx, lever2.cy);
  await sleep(60);
  await moveAt(lever2.cx, lever2.cy - 50);
  await sleep(160);
  const duringEscPress = await page.eval("(function(){ var r=document.querySelector('[data-ic=\"conversation\"]'); return { dragging: r.getAttribute('data-ic-dragging'), panelH: parseFloat(getComputedStyle(r).getPropertyValue('--ic-panel-h')) }; })()");
  await pressKey("Escape");
  await sleep(300);
  const afterEscPressed = await page.eval("(function(){ var r=document.querySelector('[data-ic=\"conversation\"]'); return { dragging: r.getAttribute('data-ic-dragging'), panelH: parseFloat(getComputedStyle(r).getPropertyValue('--ic-panel-h')), expanded: r.getAttribute('data-ic-expanded') }; })()");
  const escShot = await shot("1000x600-drag-esc-cancel");
  record("§5-4 拖动中 Esc：取消这次拖动（拖动中已生效、取消瞬间高度恢复、仍展开）", duringEscPress.dragging === "true" && afterEscPressed.dragging !== "true" && afterEscPressed.expanded === "true" && beforeEsc !== null && Math.abs(afterEscPressed.panelH - beforeEsc) <= 2, {
    shot: escShot,
    duringEscPress: duringEscPress,
    afterEscPressed: afterEscPressed,
    heightBeforeEsc: beforeEsc,
  });
  // 松开指针：取消后这一次手势的收尾不应再触发开合
  await moveAt(lever2.cx, lever2.cy - 140);
  await releaseAt(lever2.cx, lever2.cy - 140);
  await sleep(700);
  const stillExpanded = await isExpanded();
  const heightAfterRelease = await readPanelHeight();
  const releaseShot = await shot("1000x600-drag-esc-release");
  record("§5-4 Esc 取消拖动后松开指针：这次手势不被误判成点击，仍展开且高度保持", stillExpanded === true && heightAfterRelease !== null && beforeEsc !== null && Math.abs(heightAfterRelease - beforeEsc) <= 2, {
    shot: releaseShot,
    stillExpanded: stillExpanded,
    heightAfterRelease: heightAfterRelease,
    heightBeforeEsc: beforeEsc,
    note: "Lead 2026-11-17 裁决：onLeverPointerCancel 置 suppressClick=true，取消后的 pointerup click 只被吞一次",
  });
}

/* ── 场景：(b3) 展开中窗口变宽变矮：夹取、不崩 ─────────────────────────────── */
async function scenarioResizeWindow() {
  await page.setViewport(1000, 600);
  await ensureExpanded();
  const before = await page.eval("window.__icProbe ? window.__icProbe.snapshot() : null");
  await page.setViewport(800, 600);
  await sleep(800);
  const narrow = await page.eval("window.__icProbe ? window.__icProbe.snapshot() : null");
  const narrowShot = await shot("1000x600-to-800x600-expanded");
  await page.setViewport(800, 450);
  await sleep(800);
  const short = await page.eval("window.__icProbe ? window.__icProbe.snapshot() : null");
  const shortShot = await shot("800x450-expanded-clamped");
  await page.setViewport(1000, 600);
  await sleep(800);
  const back = await page.eval("window.__icProbe ? window.__icProbe.snapshot() : null");
  const overflow = [narrow, short, back].some((s) => s && s.root && (s.root.right > s.vw + 0.5 || s.root.left < -0.5 || s.root.bottom > s.vh + 0.5));
  metrics.windowResize = { before: { root: before && before.root, panelH: before && before.panelH }, narrow: { root: narrow && narrow.root, panelH: narrow && narrow.panelH }, short: { root: short && short.root, panelH: short && short.panelH }, back: { root: back && back.root, panelH: back && back.panelH } };
  record("§5-2/§5-4 展开中窗口变宽变矮：几何连续更新、夹取、不溢出、仍展开", narrow && short && back && narrow.expanded === "true" && short.expanded === "true" && back.expanded === "true" && !overflow, {
    shots: [narrowShot, shortShot],
    overflow: overflow,
    narrow: metrics.windowResize.narrow,
    short: metrics.windowResize.short,
    back: metrics.windowResize.back,
  });
}

/* ── 场景：(e) 多视口收起+展开截图与命中 ──────────────────────────────────── */
async function scenarioViewports() {
  const sizes = [[1440, 900], [1000, 600], [900, 600], [800, 600], [720, 600]];
  const out = [];
  let allNoOverflow = true;
  let allHit = true;
  for (const [w, h] of sizes) {
    await page.setViewport(w, h);
    await sleep(700);
    await ensureCollapsed();
    const collapsedShot = await shot(w + "x" + h + "-collapsed");
    const snapCollapsed = await page.eval("window.__icProbe ? window.__icProbe.snapshot() : null");
    await ensureExpanded();
    const expandedShot = await shot(w + "x" + h + "-expanded");
    const snapExpanded = await page.eval("window.__icProbe ? window.__icProbe.snapshot() : null");
    const hits = await page.eval("(function(){" +
      "var hitSelf = function(el){ if(!el) return null; var r=el.getBoundingClientRect(); if(r.width<=0||r.height<=0) return false; var x=r.x+r.width/2, y=r.y+r.height/2; if(x<0||y<0||x>innerWidth||y>innerHeight) return false; var hit=document.elementFromPoint(x,y); return !!(hit && (hit===el || el.contains(hit))); };" +
      "var modeBtn = document.querySelector('#mode-switch .mode-btn') || document.querySelector('#composer-mode-switch .mode-btn');" +
      "var toolbarButtons = Array.prototype.slice.call(document.querySelectorAll('[data-ic-rail] button')).filter(function(b){ return b.getAttribute('data-ic-toggle') === null; });" +
      "var more = document.querySelector('[data-im=\"board-more\"]');" +
      "return { leverHit: hitSelf(document.querySelector('[data-ic-toggle]')), modeHit: hitSelf(modeBtn), modeVisible: !!(modeBtn && modeBtn.getBoundingClientRect().width > 0)," +
      "  railButtons: toolbarButtons.length, railButtonHits: toolbarButtons.filter(hitSelf).length, moreVisible: !!(more && more.getBoundingClientRect().width > 0)," +
      "  tbMode: (function(){ var tb=document.querySelector('[data-im=\"board-toolbar\"]'); return tb ? tb.getAttribute('data-tb-mode') : null; })()," +
      "  addIconOnly: (function(){ var b=document.querySelector('[data-im=\"add-menu\"]'); return b ? b.getAttribute('data-icon-only') : null; })()," +
      "  addTextVisible: (function(){ var b=document.querySelector('[data-im=\"add-menu\"] .tb-label'); if(!b) return false; var r=b.getBoundingClientRect(); var s=getComputedStyle(b); return r.width > 2 && s.visibility !== 'hidden' && s.display !== 'none'; })()," +
      "  detailsBtnVisible: (function(){ var b=document.querySelector('[data-im=\"submit-details\"]'); if(!b) return false; var r=b.getBoundingClientRect(); return r.width > 0 && r.height > 0; })()," +
      "  modeText: modeBtn ? (modeBtn.innerText||'').replace(/\s+/g,' ').trim().slice(0,40) : null };" +
      "})()");
    const noOverflow = snapCollapsed && snapExpanded && snapCollapsed.root && snapExpanded.root && snapExpanded.root.right <= w + 0.5 && snapExpanded.root.x >= -0.5 && snapExpanded.root.bottom <= h + 0.5;
    if (!noOverflow) allNoOverflow = false;
    const hitOk = hits.leverHit === true && hits.modeHit === true && hits.railButtonHits >= 1;
    if (!hitOk) allHit = false;
    out.push({ viewport: w + "x" + h, collapsedShot: collapsedShot, expandedShot: expandedShot, noOverflow: noOverflow, hits: hits, collapsedRoot: snapCollapsed && snapCollapsed.root, expandedRoot: snapExpanded && snapExpanded.root, panelH: snapExpanded && snapExpanded.panelH, railSide: snapExpanded && snapExpanded.railSide });
  }
  metrics.viewports = out;
  record("§5-3/§5-5 视口 1440×900 / 1000×600 / 800×600 / 720×600：收起+展开截图、面板不溢出", allNoOverflow, out);
  record("§5-3 各视口把手 / 模式切换 / 工具按钮仍可命中", allHit, out.map((o) => ({ viewport: o.viewport, hits: o.hits })));

  // 工具栏 rail 三档：1000=full（“添加”文字可见）、800=icons（图标化）、720=more（低频工具进更多）
  const byViewport = {};
  out.forEach((o) => { byViewport[o.viewport] = o; });
  const tier1000 = byViewport["1000x600"] && byViewport["1000x600"].hits ? byViewport["1000x600"].hits : null;
  const tier800 = byViewport["800x600"] && byViewport["800x600"].hits ? byViewport["800x600"].hits : null;
  const tier720 = byViewport["720x600"] && byViewport["720x600"].hits ? byViewport["720x600"].hits : null;
  const tier900 = byViewport["900x600"] && byViewport["900x600"].hits ? byViewport["900x600"].hits : null;
  record("§3.3 工具栏 rail 三档：1000×600=full（“添加”文字可见、更多隐藏）", tier1000 !== null && tier1000.tbMode === "full" && tier1000.addTextVisible === true && tier1000.addIconOnly === null && tier1000.moreVisible === false, { hits1000: tier1000 });
  record("§3.3 工具栏 rail 三档：icon 档实测（900×600，槽宽 >= 300 且 < 344 → icons；添加图标化、更多隐藏）", tier900 !== null && tier900.tbMode === "icons" && tier900.addIconOnly === "true" && tier900.addTextVisible === false && tier900.moreVisible === false, { hits900: tier900, railSide900: byViewport["900x600"] ? byViewport["900x600"].railSide : null });
  record("§3.3 工具栏 rail 三档：800×600 → more（槽宽 289 < icons 下界 300，安全降级；更多菜单可用）", tier800 !== null && tier800.tbMode === "more" && tier800.moreVisible === true && tier800.addIconOnly === "true" && tier800.addTextVisible === false, {
    hits800: tier800,
    railSide800: byViewport["800x600"] ? byViewport["800x600"].railSide : null,
    basis: "800 视口面板宽 702（外层留白 98）→ --ic-rail-side = max(96, floor(702/2 - 62)) = 289 < RAIL_TOOLBAR_ICONS_MIN(300) → more；icons 最长提交态内容 291.5 > 289，硬塞会挤压（Lead 2026-11-17 裁决：预期即 more）",
  });

  // 720×600 = more 档：更多菜单可开、详情可从更多打开、Esc 可关
  await page.setViewport(720, 600);
  await sleep(600);
  await ensureExpanded();
  const moreVisible720 = await page.exists('[data-im="board-more"]');
  const moreInfo720 = moreVisible720 ? await page.box('[data-im="board-more"]') : null;
  let morePanelOpened = false;
  let detailsFromMore = false;
  let panelClosedByEsc = false;
  if (moreVisible720) {
    await realClick('[data-im="board-more"]').catch(() => null);
    await sleep(400);
    morePanelOpened = await page.exists('[data-im="board-more-panel"]');
    if (morePanelOpened) {
      await realClick('[data-im="board-more-details"]').catch(() => null);
      await sleep(500);
      detailsFromMore = await page.exists('[data-im="submit-details-box"]');
      await pressKey("Escape");
      await sleep(400);
      panelClosedByEsc = !(await page.exists('[data-im="submit-details-box"]'));
    }
  }
  const moreShot = await shot("720x600-toolbar-more-panel");
  record("§3.3 工具栏 rail 三档：720×600=more（更多菜单可开、详情可从更多打开、Esc 可关）", tier720 !== null && tier720.tbMode === "more" && tier720.moreVisible === true && morePanelOpened === true && detailsFromMore === true && panelClosedByEsc === true, {
    shot: moreShot,
    hits720: tier720,
    moreButtonVisible: moreVisible720,
    moreButtonHit: moreInfo720 ? moreInfo720.hitSelf : null,
    morePanelOpened: morePanelOpened,
    detailsOpenedFromMore: detailsFromMore,
    closedByEsc: panelClosedByEsc,
  });
}

/* ── 场景：(f) 亮/暗主题 ──────────────────────────────────────────────────── */
async function scenarioThemes() {
  await page.setViewport(1000, 600);
  await sleep(400);
  const out = {};
  for (const theme of ["light", "dark"]) {
    await page.eval("(function(){ localStorage.setItem('qio-theme', " + JSON.stringify(theme) + "); return true; })()");
    await page.reload();
    await page.waitFor("document.querySelector('.stream') || document.querySelector('[data-im=\"board\"]')", { timeout: 45000, label: "主题切换后页面" }).catch(() => null);
    await sleep(900);
    if (!(await page.exists('[data-im="board"]'))) await enterInteractive().catch(() => null);
    await ensureProbe();
    await ensureCollapsed();
    const collapsedShot = await shot("theme-" + theme + "-collapsed");
    const collapsedSnap = await page.eval("window.__icProbe.snapshot()");
    await ensureExpanded();
    const expandedShot = await shot("theme-" + theme + "-expanded");
    const expandedSnap = await page.eval("window.__icProbe.snapshot()");
    const colors = await page.eval("(function(){" +
      "var root = document.querySelector('[data-ic=\"conversation\"]'); var glass = document.querySelector('[data-ic-glass]'); var stop = document.querySelector('[data-ic-frame] linearGradient stop'); var railBtn = document.querySelector('[data-ic-rail] button');" +
      "return { theme: document.documentElement.getAttribute('data-theme'), bodyBg: getComputedStyle(document.body).backgroundColor, rootColor: root ? getComputedStyle(root).color : null," +
      " glassBg: glass ? getComputedStyle(glass).backgroundColor : null, stopColor: stop ? getComputedStyle(stop).stopColor : null," +
      " railBorder: railBtn ? getComputedStyle(railBtn).borderColor : null, railBg: railBtn ? getComputedStyle(railBtn).backgroundColor : null };" +
      "})()");
    out[theme] = { collapsedShot: collapsedShot, expandedShot: expandedShot, colors: colors, collapsed: { root: collapsedSnap.root, rail: collapsedSnap.rail, reveal: collapsedSnap.reveal }, expanded: { root: expandedSnap.root, stream: expandedSnap.stream, panelH: expandedSnap.panelH, reveal: expandedSnap.reveal, glassOpacity: expandedSnap.glassOpacity } };
  }
  const applied = out.light.colors.theme === "light" && out.dark.colors.theme === "dark";
  const distinct = out.light.colors.rootColor !== out.dark.colors.rootColor && out.light.colors.stopColor !== out.dark.colors.stopColor && out.light.colors.glassBg !== out.dark.colors.glassBg;
  metrics.themes = out;
  record("§5-3 亮/暗主题：两主题真实生效、收起+展开截图、线框/玻璃文字颜色随令牌变化", applied && distinct && out.light.expanded.reveal >= 0.999 && out.dark.expanded.reveal >= 0.999, out);
  await page.eval("(function(){ localStorage.setItem('qio-theme', 'light'); return true; })()");
  await page.reload();
  await page.waitFor("document.querySelector('.stream') || document.querySelector('[data-im=\"board\"]')", { timeout: 45000, label: "恢复主题" }).catch(() => null);
  await sleep(800);
  if (!(await page.exists('[data-im="board"]'))) await enterInteractive().catch(() => null);
  await ensureProbe();
}

/* ── 场景：(g) 排队中 / 待确认入口状态 ───────────────────────────────────── */
async function scenarioStates() {
  await page.setViewport(1000, 600);
  await ensureInteractiveSafe();
  await providerReset();
  // 一条慢 SSE：单次响应 40 分片 x 250ms ≈ 10s（provider 独立实测 11.9s）。
  // repeat=4：即使有后台请求（如话题标题）先消费一步，我们的用户轮次仍能拿到慢步骤。
  const slowChunks = [];
  for (let i = 0; i < 40; i += 1) slowChunks.push("慢批次分片" + i + "。");
  await providerScript([{ chunks: slowChunks, chunk_delay_ms: 250, repeat: 4 }]);
  async function sendComposer(text) {
    await page.focus("#composer-input");
    const before = await page.inputValue("#composer-input");
    await page.type("#composer-input", text);
    await page.waitFor("(function(){ var b=document.querySelector('.send-btn'); return b && b.getAttribute('data-ready') === 'true'; })()", { timeout: 12000, step: 100, label: "send-ready" }).catch(() => null);
    const ready = await page.eval("(function(){ var b=document.querySelector('.send-btn'); return b ? b.getAttribute('data-ready') : null; })()");
    await realClick(".send-btn").catch(() => null);
    await sleep(400);
    return { beforeDraft: before, ready: ready, afterDraft: await page.inputValue("#composer-input") };
  }
  const normalize = (text) => (text === null || text === undefined ? null : String(text).replace(/\s+/g, " ").trim());
  const readStatus = async () => normalize(await page.eval("(function(){ var s=document.querySelector('[data-ic-status]'); return s ? s.textContent : null; })()"));
  const readQueueStateRaw = async () => page.eval("(function(){" +
    "var status = document.querySelector('[data-ic-status]'); var rail = document.querySelector('[data-ic-rail]'); var root = document.querySelector('[data-ic=\"conversation\"]');" +
    "var entries = document.querySelector('[data-ic-entries]');" +
    "return { status: status ? status.textContent : null, railText: rail ? rail.innerText : null, entriesText: entries ? entries.innerText : null, expanded: root ? root.getAttribute('data-ic-expanded') : null };" +
    "})()");
  const sendFirst = await sendComposer("QUEUE-RUN-" + RUN_ID);
  const runningSeen = await page.waitFor("(function(){ var s=document.querySelector('[data-ic-status]'); return !!(s && /回复|排队/.test(s.textContent)); })()", { timeout: 20000, step: 120, label: "运行中状态" }).then(() => true).catch(() => false);
  const sendSecond = await sendComposer("QUEUE-WAIT-" + RUN_ID);
  // 快速轮询：抓住「turn 仍在运行 + 队列 >= 1」的那一瞬间取证（不靠固定 sleep）
  let queueSnapshot = null;
  const queueDeadline = Date.now() + 20000;
  while (Date.now() < queueDeadline) {
    let apiNow = null;
    try { apiNow = await (await fetch(API + "/api/turns/queue")).json(); } catch (error) { apiNow = null; }
    const statusNow = await readStatus();
    if (apiNow !== null && apiNow.running && (apiNow.queued || []).length >= 1 && /排队/.test(statusNow || "")) {
      queueSnapshot = { api: apiNow, status: statusNow };
      break;
    }
    await sleep(120);
  }
  const queueApi = queueSnapshot ? queueSnapshot.api : null;
  const rawQueueState = await readQueueStateRaw();
  const queueState = { status: normalize(rawQueueState.status), railText: normalize(rawQueueState.railText), entriesText: normalize(rawQueueState.entriesText), expanded: rawQueueState.expanded };
  const statusBefore = queueSnapshot ? queueSnapshot.status : queueState.status;
  await ensureCollapsed();
  const queueCollapsedShot = await shot("state-queue-collapsed");
  const statusCollapsed = await readStatus();
  const queueCollapsed = await page.eval("window.__icProbe.snapshot()");
  await ensureExpanded();
  const queueExpandedShot = await shot("state-queue-expanded");
  const queueExpanded = await page.eval("window.__icProbe.snapshot()");
  const statusExpanded = await readStatus();
  const queuedSeen = queueSnapshot !== null;
  const queueSeen = /排队|回复中|正在回复/.test((statusBefore || "") + (queueState.railText || ""));
  const queuedWindowEvidence = queueApi !== null && !!queueApi.running && (queueApi.queued || []).length >= 1;
  const queuePreserved = queueSeen && /排队/.test(statusCollapsed || "") && /排队/.test(statusExpanded || "") && statusCollapsed === statusExpanded;
  metrics.queueState = { observed: queueState, statusBefore: statusBefore, statusCollapsed: statusCollapsed, statusExpanded: statusExpanded, queueApi: queueApi, runningSeen: runningSeen, queuedSeen: queuedSeen, sendFirst: sendFirst, sendSecond: sendSecond, collapsedRoot: queueCollapsed.root, expandedRoot: queueExpanded.root };
  record("§5-3 有排队：操作行显示「正在回复/已排队」，收起/展开截图（回复不因收起中断）", queuedSeen && queuedWindowEvidence && queueSeen && queueCollapsed.expanded === "false" && queueExpanded.expanded === "true", {
    shots: [queueCollapsedShot, queueExpandedShot],
    observed: queueState,
    turnsQueue: queueApi,
    runningStatusSeen: runningSeen,
    queuedStatusSeen: queuedSeen,
    sendFirst: sendFirst,
    sendSecond: sendSecond,
  });
  record("§5-3 收起/展开不打断回复、排队状态保持（收起前=收起后=展开后）", queuePreserved, {
    statusBefore: statusBefore,
    statusCollapsed: statusCollapsed,
    statusExpanded: statusExpanded,
    turnsQueueAtObservation: queueApi,
  });

  const idle = await waitTurnsIdle(90000);
  record("§5-3 排队/回复轮次最终回到空闲（收起没有打断回复）", idle === true, { idle: idle });

  await providerReset();
  await providerScript([{ chunks: ["已收到本次板面提交。"], repeat: 8 }]);
  const submitResult = await submitBoard();
  await sleep(1200);
  const pendingState = await page.eval("(function(){" +
    "var body = document.body.innerText.replace(/\s+/g,' ');" +
    "var pending = /待确认|等待确认|已受理|确认生成/.test(body);" +
    "var bars = Array.prototype.slice.call(document.querySelectorAll('[data-ic-rail], [data-im=\"submit-cluster\"], [data-ic-entries]')).map(function(el){ return el.innerText.replace(/\s+/g,' ').trim().slice(0,160); });" +
    "return { pendingTextSeen: pending, bars: bars, understanding: !!document.querySelector('[data-im=\"understanding\"]'), submitLabel: (document.querySelector('[data-im=\"submit\"]')||{}).innerText || null };" +
    "})()");
  await ensureCollapsed();
  const pendingCollapsedShot = await shot("state-pending-collapsed");
  await ensureExpanded();
  const pendingExpandedShot = await shot("state-pending-expanded");
  metrics.pendingState = { submit: submitResult, observed: pendingState };
  record("§5-3 有待确认入口状态：受理/待确认可见、收起/展开截图", submitResult.accepted === true && pendingState.pendingTextSeen === true, {
    shots: [pendingCollapsedShot, pendingExpandedShot],
    submit: submitResult,
    observed: pendingState,
  });
  await waitTurnsIdle(60000);
}

/* ── 场景：(d) 键盘 / 焦点 / Esc / 减少动画 ───────────────────────────────── */
async function scenarioKeyboard() {
  await page.setViewport(1000, 600);
  await ensureCollapsed();
  // 聚焦输入框自动展开
  await page.focus("#composer-input");
  await sleep(300);
  const autoExpanded = await isExpanded();
  await waitMotionIdle(3000);
  const autoReveal = Number(await conversationAttr("data-ic-reveal"));
  record("§5-4 聚焦输入框自动展开（reveal 到 1）", autoExpanded === true && autoReveal >= 0.999, { autoExpanded: autoExpanded, reveal: autoReveal });

  // Enter 收起
  await page.focus("[data-ic-toggle]");
  await pressKey("Enter");
  const enterCollapsed = await waitExpanded(false, 6000).then(() => true).catch(() => false);
  await waitMotionIdle(2500);
  record("§5-4 把手键盘 Enter 开合", enterCollapsed === true, { enterCollapsed: enterCollapsed });

  // Space 展开（原生 button 激活键是 key=" "）
  await page.focus("[data-ic-toggle]");
  await pressKey("Space");
  const spaceExpanded = await waitExpanded(true, 6000).then(() => true).catch(() => false);
  await waitMotionIdle(2500);
  record("§5-4 把手键盘 Space 开合", spaceExpanded === true, { spaceExpanded: spaceExpanded });

  // ↑/↓ 每步 24px、Home/End 边界夹取（默认展开高度 280 == 当前窗口下 max，故先 Home 到 min 再加减）
  await ensureExpanded();
  await page.focus("[data-ic-toggle]");
  await pressKey("Home");
  await waitMotionIdle(2500);
  const hMin = await readPanelHeight();
  await pressKey("ArrowUp");
  await waitMotionIdle(2500);
  const hMinUp = await readPanelHeight();
  await pressKey("ArrowDown");
  await waitMotionIdle(2500);
  const hMinDown = await readPanelHeight();
  await pressKey("End");
  await waitMotionIdle(2500);
  const hMax = await readPanelHeight();
  await pressKey("ArrowUp");
  await waitMotionIdle(2500);
  const hMaxUp = await readPanelHeight();
  await pressKey("Home");
  await waitMotionIdle(2500);
  const hMinAgain = await readPanelHeight();
  await pressKey("ArrowDown");
  await waitMotionIdle(2500);
  const hMinBound = await readPanelHeight();
  const upStep = hMin !== null && hMinUp !== null ? round(hMinUp - hMin, 1) : null;
  const downStep = hMinUp !== null && hMinDown !== null ? round(hMinUp - hMinDown, 1) : null;
  metrics.keyboardHeights = { hMin: hMin, hMinUp: hMinUp, hMinDown: hMinDown, hMax: hMax, hMaxUp: hMaxUp, hMinAgain: hMinAgain, hMinBound: hMinBound, upStep: upStep, downStep: downStep };
  record("§5-4 Home/End 到两端（220 / 当前窗口 max）且 ↑/↓ 步进 24px、两端边界夹取", hMin !== null && hMax !== null && Math.abs(hMin - 220) <= 1 && Math.abs(upStep - 24) <= 1 && Math.abs(downStep - 24) <= 1 && hMax > hMin && Math.abs(hMaxUp - hMax) <= 1 && Math.abs(hMinAgain - hMin) <= 1 && Math.abs(hMinBound - hMin) <= 1, {
    heights: metrics.keyboardHeights,
    note: "高度口径：CONVERSATION_MIN_HEIGHT=220、CONVERSATION_KEYBOARD_STEP=24；600 高窗口下 max = max(280, min(720, contentViewportH - 306)) = 280",
  });

  // Esc 层级：面板（提交详情）打开时先关面板并保持展开；再按 Esc 收起对话
  await page.setViewport(1000, 600);
  await ensureExpanded();
  await page.focus("[data-ic-toggle]");
  const detailsBtn = await page.exists('[data-im="submit-details"]');
  let panelOpened = false;
  if (detailsBtn) {
    await realClick('[data-im="submit-details"]').catch(() => null);
    await sleep(500);
    panelOpened = await page.exists('[data-im="submit-details-box"]');
  }
  const beforeEsc = await page.eval("(function(){ var r=document.querySelector('[data-ic=\"conversation\"]'); return { expanded: r.getAttribute('data-ic-expanded'), panel: !!document.querySelector('[data-im=\"submit-details-box\"]') }; })()");
  await pressKey("Escape");
  await sleep(500);
  const afterEsc1 = await page.eval("(function(){ var r=document.querySelector('[data-ic=\"conversation\"]'); return { expanded: r.getAttribute('data-ic-expanded'), panel: !!document.querySelector('[data-im=\"submit-details-box\"]') }; })()");
  await pressKey("Escape");
  await sleep(600);
  await waitMotionIdle(2500);
  const afterEsc2 = await isExpanded();
  const escShot = await shot("1000x600-esc-layering");
  const escOk = panelOpened
    ? (afterEsc1.panel === false && afterEsc1.expanded === "true" && afterEsc2 === false)
    : (afterEsc1.expanded === "false");
  record("§5-4 Esc 层级：先关面板（保持展开），再收起对话（不越级、不清草稿）", escOk, {
    shot: escShot,
    detailsButtonExists: detailsBtn,
    panelOpened: panelOpened,
    beforeEsc: beforeEsc,
    afterEsc1: afterEsc1,
    expandedAfterSecondEsc: afterEsc2,
  });
}

async function scenarioReduced() {
  await page.setViewport(1000, 600);
  await ensureCollapsed();
  await page.browser.send("Emulation.setEmulatedMedia", { features: [{ name: "prefers-reduced-motion", value: "reduce" }] }, page.sessionId);
  await sleep(400);
  await startProbe();
  await realClick("[data-ic-toggle]");
  await sleep(200);
  const data = await stopProbe();
  const frames = data.frames;
  const last = frames[frames.length - 1];
  const clickT = data.clickT !== null ? data.clickT : data.pointerDownT;
  const clickFrameIndex = frames.findIndex((f) => f.t >= clickT);
  const revealReachedAt = frames.findIndex((f, index) => index >= clickFrameIndex && f.reveal !== null && f.reveal >= 0.999);
  const framesAfterClick = revealReachedAt >= 0 && clickFrameIndex >= 0 ? revealReachedAt - clickFrameIndex : null;
  const reducedShot = await shot("reduced-motion-expanded");
  metrics.reducedMotion = { frames: frames.length, clickFrameIndex: clickFrameIndex, revealReachedAt: revealReachedAt, framesAfterClick: framesAfterClick, reveal: last.reveal, panelH: last.panelH, reduced: last.reduced };
  record("§5-4 减少动画：直接落最终几何（点击后 ≤2 帧到 reveal=1）且 data-ic-reduced=true", framesAfterClick !== null && framesAfterClick <= 2 && last.reveal >= 0.999 && last.reduced === "true", { shot: reducedShot, metrics: metrics.reducedMotion });
  await ensureCollapsed();
  await page.browser.send("Emulation.setEmulatedMedia", { features: [] }, page.sessionId);
  await sleep(300);
  await ensureProbe();
  record("§5-4 恢复常规动画设置后页面仍可用", (await page.exists("[data-ic-toggle]")) === true, { toggleExists: await page.exists("[data-ic-toggle]") });
}

/* ── 场景：(h) 会话不变量 / 发送 / 模式切换 ───────────────────────────────── */
async function findScrollInfo() {
  return page.eval("(function(){" +
    "var host = document.querySelector('[data-ic-stream]'); if (!host) return null;" +
    "var nodes = [host].concat(Array.prototype.slice.call(host.querySelectorAll('*')));" +
    "for (var i=0;i<nodes.length;i+=1) { var el=nodes[i]; if (el.scrollHeight > el.clientHeight + 4) return { top: Math.round(el.scrollTop), max: Math.round(el.scrollHeight - el.clientHeight) }; }" +
    "return { top: null, max: 0 };" +
    "})()");
}
async function setScrollMidRatio(ratio) {
  return page.eval("(function(){" +
    "var host = document.querySelector('[data-ic-stream]'); if (!host) return null;" +
    "var nodes = [host].concat(Array.prototype.slice.call(host.querySelectorAll('*')));" +
    "for (var i=0;i<nodes.length;i+=1) { var el=nodes[i]; if (el.scrollHeight > el.clientHeight + 4) { el.scrollTop = Math.floor((el.scrollHeight - el.clientHeight) * " + ratio + "); return { top: Math.round(el.scrollTop), max: Math.round(el.scrollHeight - el.clientHeight) }; } }" +
    "return { top: null, max: 0 };" +
    "})()");
}
async function scenarioInvariants() {
  await page.setViewport(1000, 600);
  await ensureInteractiveSafe();
  await ensureExpanded();

  await page.focus("#composer-input");
  const draft = "DRAFT-INV-" + RUN_ID;
  await page.type("#composer-input", draft);
  await ensureCollapsed();
  await ensureExpanded();
  const draftAfter = await page.inputValue("#composer-input");
  record("§5-5 开合前后草稿保持", draftAfter === draft, { expected: draft, actual: draftAfter });
  await page.eval("(function(){ var el=document.querySelector('#composer-input'); if (el) { el.value=''; el.dispatchEvent(new Event('input',{bubbles:true})); } return true; })()");

  // 附件保持：真实浏览器文件选择器 → 真实上传/登记 → 收起/展开后 chip 仍在
  let attachment = null;
  try {
    const tmpFile = join(process.env.TEMP || ".", "qio-att-" + RUN_ID + ".txt");
    writeFileSync(tmpFile, "ATTACH-INV-" + RUN_ID + "\n");
    attachment = await addComposerAttachment(tmpFile);
  } catch (error) {
    attachment = { error: String(error && error.message ? error.message : error) };
  }
  const attachmentName = attachment && attachment.name ? attachment.name : null;
  const attachmentBefore = attachment && attachment.state ? attachment.state.chips : [];
  const attachmentSeen = attachmentName !== null && attachmentBefore.some((n) => n.indexOf(attachmentName) >= 0);
  await ensureCollapsed();
  await ensureExpanded();
  const attachmentAfter = await page.eval("(function(){ return Array.prototype.slice.call(document.querySelectorAll('[data-ic-composer] .chip .name')).map(function(el){ return (el.textContent||'').trim(); }); })()");
  record("§5-5 开合前后附件保持（真实文件选择器 → 上传/登记 → 收起/展开 chip 仍在）", attachmentSeen && attachmentAfter.some((n) => n.indexOf(attachmentName) >= 0), {
    file: attachmentName,
    before: attachmentBefore,
    after: attachmentAfter,
    probe: attachment && attachment.state ? attachment.state : attachment,
  });
  await page.eval("(function(){ var btn=document.querySelector('[data-ic-composer] [aria-label^=\"移除附件\"]'); if(btn){ btn.click(); return true; } return false; })()");
  await sleep(700);

  await providerReset();
  await providerScript([{ chunks: ["收到这一条。"], repeat: 30 }]);
  for (let i = 1; i <= 8; i += 1) {
    await page.focus("#composer-input");
    await page.type("#composer-input", "SCROLL-" + RUN_ID + "-" + i);
    await pressKey("Enter");
    await sleep(320);
  }
  await waitTurnsIdle(120000);
  await sleep(900);
  const scrollInfo = await findScrollInfo();
  const scrollBefore2 = await setScrollMidRatio(0.4);
  await sleep(300);
  const scrollBefore = await findScrollInfo();
  await ensureCollapsed();
  await ensureExpanded();
  const scrollAfter = await findScrollInfo();
  const scrollKept = scrollBefore && scrollAfter && scrollBefore.top !== null && scrollBefore.max > 0 && Math.abs(scrollAfter.top - scrollBefore.top) <= 2;
  metrics.scrollInvariant = { scrollInfo: scrollInfo, setResult: scrollBefore2, measuredBefore: scrollBefore, measuredAfter: scrollAfter };
  record("§5-5 开合前后消息滚动位置保持（不跳到底/不回顶）", scrollKept === true, metrics.scrollInvariant);

  await providerReset();
  await providerScript([{ chunks: ["好的，收到。"], repeat: 10 }]);
  const sendText = "SEND-INV-" + RUN_ID;
  await page.focus("#composer-input");
  await page.type("#composer-input", sendText);
  await pressKey("Enter");
  await waitTurnsIdle(60000);
  const log = await providerLog();
  const sent = chatRequests(log).some((r) => bodyText(r).indexOf(sendText) >= 0);
  const draftCleared = (await page.inputValue("#composer-input")) === "";
  const userMessageVisible = await page.eval("(function(){ var host=document.querySelector('[data-ic-stream]'); return host ? host.innerText.indexOf(" + JSON.stringify(sendText) + ") >= 0 : false; })()");
  record("§5-5 发送仍走原流程（真实 Enter → provider 收到正文、草稿清空、消息可见）", sent === true && draftCleared === true && userMessageVisible === true, { sent: sent, draftCleared: draftCleared, userMessageVisible: userMessageVisible, chatRequestCount: chatRequests(log).length });

  await backToConversation();
  const conversationOk = !(await page.exists('[data-im="board"]'));
  const entry = await enterInteractive();
  record("§5-5 模式切换仍走原流程（互动 → 对话 → 互动）", conversationOk === true && entry.after === "interactive", { conversationOk: conversationOk, entry: entry });

  // chatFrameState：Vite dev server 直接 import 同一模块读真实值（不是 mock）
  await ensureExpanded();
  await sleep(500);
  const frameExpanded = await readChatFrameModule();
  await ensureCollapsed();
  await sleep(500);
  const frameCollapsed = await readChatFrameModule();
  await backToConversation();
  await sleep(500);
  const frameUnmounted = await readChatFrameModule();
  await enterInteractive();
  const chatFrameOk = frameExpanded.ok === true && frameExpanded.frame !== null && frameExpanded.frame.w > 0 && frameExpanded.frame.h > 0 && frameCollapsed.ok === true && frameCollapsed.frame === null && frameCollapsed.obstacle === null && frameUnmounted.ok === true && frameUnmounted.frame === null;
  record("§5-5 chatFrameState：展开发布真实矩形、收起/卸载发布 null（无幽灵障碍）", chatFrameOk, {
    expanded: frameExpanded,
    collapsed: frameCollapsed,
    unmounted: frameUnmounted,
    note: "通过 page 内 import('/src/interactive/chatFrameState.ts') 读真实模块状态（Vite dev server）",
  });
}

/** 真实读取 chatFrameState 桥（Vite dev server 直接 import 同一模块，不 mock）。 */
async function readChatFrameModule() {
  return page.eval("(async function(){ try { var m = await import('/src/interactive/chatFrameState.ts'); return { ok: true, frame: m.readChatFrame(), obstacle: m.chatObstacleFrame() }; } catch (error) { return { ok: false, error: String(error) }; } })()");
}

/** 通过输入胶囊的附件入口（真实文件选择器）添加一个本地文件，等它进入待发送列表。 */
async function addComposerAttachment(filePath) {
  const name = filePath.split(/[\\/]/).pop();
  await ensureExpanded();
  await sleep(300);
  await realClick('[aria-label="添加附件"]');
  await sleep(400);
  const chooser = page.setFilesOnChooser([filePath], { timeout: 20000 });
  await realClick(".attach-menu-item");
  await chooser;
  const deadline = Date.now() + 40000;
  let state = null;
  while (Date.now() < deadline) {
    state = await page.eval("(function(){ var chips=Array.prototype.slice.call(document.querySelectorAll('[data-ic-composer] .chip .name')).map(function(el){ return (el.textContent||'').trim(); }); var hint=document.querySelector('[data-ic-composer] .attach-hint'); var err=document.querySelector('[data-ic-composer] [data-test=\"attach-error\"]'); return { chips: chips, attaching: !!hint, error: err ? (err.textContent||'').trim().slice(0,120) : null }; })()");
    if (state.chips.some((n) => n.indexOf(name) >= 0) && !state.attaching) break;
    if (state.error) break;
    await sleep(400);
  }
  return { name: name, state: state };
}

async function ensureInteractiveSafe() {
  if (!(await page.exists('[data-im="board"]'))) {
    await enterInteractive();
  }
  await ensureProbe();
}

/* ── 板面提交（复用 e2e 的做法：真实点击 + 确认层 + 等受理） ───────────────── */
async function submitBoard() {
  const before = await page.text('[data-im="submit"]').catch(() => "");
  await page.click('[data-im="submit"]').catch(() => null);
  await sleep(600);
  if (await page.exists('[data-im="impact-confirm-submit"]')) {
    await page.click('[data-im="impact-confirm-submit"]').catch(() => null);
    await sleep(500);
  }
  const deadline = Date.now() + 60000;
  let last = "";
  while (Date.now() < deadline) {
    last = (await page.text('[data-im="submit-cluster"]').catch(() => "")) || "";
    const label = (await page.text('[data-im="submit"]').catch(() => "")) || "";
    if (!/正在提交|正在保存/.test(label + last) && (/已提交|提交失败|没有可提交|未重复提交|待确认|已受理/.test(last) || (await page.exists('[data-im="understanding"]')))) {
      await sleep(600);
      return { accepted: true, before: before, text: last.slice(0, 400), label: label };
    }
    await sleep(400);
  }
  return { accepted: false, before: before, text: last.slice(0, 400) };
}

/* ── 产物：summary.json + report.md（中文） ───────────────────────────────── */
function buildReport(summary) {
  const lines = [];
  const pass = summary.results.filter((r) => r.status === "PASS").length;
  const fail = summary.results.filter((r) => r.status === "FAIL").length;
  const unverified = summary.results.filter((r) => r.status === "UNVERIFIED").length;
  const clean = (text) => String(text).split("|").join("/");
  lines.push("# 互动模式底部操作区 · 最终实机取证报告（verifier）");
  lines.push("");
  lines.push("生成时间：" + summary.startedAt + "（runId " + summary.runId + "）");
  lines.push("");
  lines.push("- 被测地址：" + summary.base + "（工作树 " + summary.root + "，HEAD " + (summary.gitHead || "?") + "）");
  lines.push("- 浏览器：系统 Edge + CDP（" + (summary.engine || "?") + "），无 Playwright；视口 " + summary.viewport.width + "×" + summary.viewport.height);
  lines.push("- 数据域：隔离实例；fake provider " + summary.provider + "（真 SSE，非真实厂商）");
  lines.push("- 结论：PASS " + pass + " / FAIL " + fail + " / UNVERIFIED " + unverified + "（共 " + summary.results.length + " 项）");
  lines.push("");
  lines.push("## 逐项结果");
  lines.push("");
  lines.push("| # | 检查项 | 结果 | 证据 / 数值 |");
  lines.push("|---|---|---|---|");
  summary.results.forEach((r, index) => {
    let evidence = "";
    if (r.detail && typeof r.detail === "object") {
      const shotList = [];
      const collect = (value, depth) => {
        if (!value || depth > 3) return;
        if (typeof value === "string" && /\.png$/.test(value)) shotList.push(value.split("/").pop().split("\\").pop());
        else if (Array.isArray(value)) value.forEach((v) => collect(v, depth + 1));
        else if (typeof value === "object") Object.keys(value).forEach((k) => collect(value[k], depth + 1));
      };
      collect(r.detail, 0);
      const numeric = {};
      ["clickToCornerMs", "frameQuantizedMs", "height50Ms", "height90Ms", "settleMs", "up", "down"].forEach((k) => { if (r.detail[k] !== undefined) numeric[k] = r.detail[k]; });
      const bits = [];
      if (shotList.length) bits.push("截图：" + Array.from(new Set(shotList)).join(", "));
      if (Object.keys(numeric).length) bits.push("数值：" + JSON.stringify(numeric));
      if (!bits.length) bits.push(JSON.stringify(r.detail).slice(0, 200));
      evidence = bits.join("；");
    }
    lines.push("| " + (index + 1) + " | " + clean(r.name) + " | " + r.status + " | " + clean(evidence).slice(0, 400) + " |");
  });
  lines.push("");
  lines.push("## 关键数值");
  lines.push("");
  if (summary.metrics.expand1000 && summary.metrics.expand1000.metrics) {
    const m = summary.metrics.expand1000.metrics;
    lines.push("- 1000×600 展开：到角 " + (m.corner && m.corner.interpMs) + "ms（帧量化 " + (m.corner && m.corner.frameMs) + "ms，来源 " + (m.corner && m.corner.source) + "）；高度 50%/90% = " + m.height50Ms + "/" + m.height90Ms + "ms；settle " + m.settleMs + "ms");
    lines.push("- 线框：pathLen " + (m.frameGeometry && m.frameGeometry.pathLenAtSettle) + "，horizontal " + (m.frameGeometry && m.frameGeometry.horizontalAtSettle) + "，cornerReveal " + (m.frameGeometry && m.frameGeometry.cornerRevealAtSettle) + "；同帧更新 " + JSON.stringify(m.updateCadence));
  }
  if (summary.metrics.collapse1000 && summary.metrics.collapse1000.metrics) {
    const c = summary.metrics.collapse1000.metrics;
    lines.push("- 1000×600 收起：settle " + c.settleMs + "ms；高度 50%/90% = " + c.height50Ms + "/" + c.height90Ms + "ms");
  }
  lines.push("- 预览基准（阶段 1，preview-baseline.json）：到角 218.4ms（帧 220.3ms），静态 cornerFraction 0.5722");
  lines.push("");
  lines.push("## 未覆盖 / 手工步骤");
  lines.push("");
  summary.unverified.forEach((item) => lines.push("- " + item));
  summary.manualSteps.forEach((item) => lines.push("- " + item));
  lines.push("");
  lines.push("## 边界（如实声明）");
  lines.push("");
  lines.push("- fake provider 只证明 QIO 自己的链路，不证明真实厂商/SDK；不证明 Tauri 原生窗口与 Windows 原生拖放。");
  lines.push("- 逐帧数值来自浏览器 rAF 采样，到角时刻在「上一帧与首帧满足帧」之间，已给线性插值并同时给出帧量化值。");
  lines.push("- 本目录是验收资产，不是产品代码；不参与打包。");
  lines.push("");
  return lines.join("\n");
}

async function writeArtifacts(startedAt) {
  const finishedAt = new Date().toISOString();
  const summary = {
    runId: RUN_ID,
    kind: "interactive-action-final",
    startedAt: startedAt,
    finishedAt: finishedAt,
    base: BASE,
    api: API,
    provider: PROVIDER,
    root: ROOT,
    gitHead: process.env.QIO_IA_GIT_HEAD || null,
    engine: metrics.engine || null,
    viewport: { width: 1000, height: 600 },
    dataDir: process.env.QIO_IA_DATA_DIR || process.env.QIO_DATA_DIR || null,
    thresholds: THRESHOLDS,
    results: results,
    metrics: metrics,
    consoleErrors: consoleErrors,
    unverified: results.filter((r) => r.status === "UNVERIFIED").map((r) => r.name),
    manualSteps: MANUAL_STEPS,
    shotsDir: SHOTS,
    browserLayer: "系统 Edge + CDP（scripts/verify-pm-qio032/cdp.mjs，无 Playwright）",
  };
  writeFileSync(join(OUT, "summary.json"), JSON.stringify(summary, null, 2));
  writeFileSync(join(OUT, "report.md"), buildReport(summary));
}

/* ── 主流程 ───────────────────────────────────────────────────────────────── */
function wanted(key) {
  if (!ONLY) return true;
  const keys = ONLY.split(",").map((s) => s.trim()).filter(Boolean);
  return keys.indexOf("all") >= 0 || keys.indexOf(key) >= 0;
}

async function main() {
  const startedAt = new Date().toISOString();
  let browser = null;
  try {
    browser = await CdpBrowser.launch({ port: CDP_PORT });
    metrics.engine = browser.version && browser.version.Browser;
    page = await Page.create(browser);
    await page.setViewport(1000, 600);
    console.log("== 互动操作区最终取证 ==  base=" + BASE + " api=" + API + " provider=" + PROVIDER + " out=" + OUT + " runId=" + RUN_ID);
    try {
      await fetch(API + "/api/onboarding/seen", { method: "POST" }).catch(() => null);
      await fetch(API + "/api/onboarding/complete", { method: "POST" }).catch(() => null);
      await page.goto(BASE + "/");
      await waitAppReady();
      record("环境：应用就绪（对话页）", true, { url: BASE, theme: await page.eval("document.documentElement.getAttribute('data-theme')"), engine: metrics.engine });
    } catch (error) {
      record("环境：应用就绪（对话页）", false, { error: String(error && error.message ? error.message : error).slice(0, 400) });
      throw error;
    }
    try {
      const entry = await enterInteractive();
      record("正常入口：对话页模式切换 → 互动板面", entry.after === "interactive" && String(entry.hash).indexOf("/interactive") >= 0, entry);
    } catch (error) {
      record("正常入口：对话页模式切换 → 互动板面", false, { error: String(error && error.message ? error.message : error).slice(0, 400) });
      throw error;
    }
    try {
      const a1 = await openAddMenu("text");
      const e1 = await setTextCardContent(a1.after - 1, "IA-FINAL-" + RUN_ID + " 卡片一");
      const a2 = await openAddMenu("text");
      const e2 = await setTextCardContent(a2.after - 1, "IA-FINAL-" + RUN_ID + " 卡片二");
      record("通过真实界面创建 2 张文字卡", a1.after === a1.before + 1 && a2.after === a2.before + 1 && e1.ok === true && e2.ok === true, { cards: await cardCount(), edit1: e1, edit2: e2 });
    } catch (error) {
      record("通过真实界面创建 2 张文字卡", false, { error: String(error && error.message ? error.message : error).slice(0, 400) });
    }
    await ensureProbe();

    const groups = [
      ["contract", "DOM 契约", scenarioContract],
      ["motion", "1000×600 开合逐帧", scenarioMotion],
      ["reverse", "快速反向开合", scenarioReverse],
      ["drag", "展开途中拖动/松手/Esc/高度记忆", scenarioDrag],
      ["resize", "窗口变宽变矮", scenarioResizeWindow],
      ["viewports", "多视口截图与命中", scenarioViewports],
      ["themes", "亮/暗主题", scenarioThemes],
      ["states", "排队/待确认入口状态", scenarioStates],
      ["keyboard", "键盘/焦点/Esc", scenarioKeyboard],
      ["reduced", "减少动画", scenarioReduced],
      ["invariants", "会话不变量/发送/模式切换", scenarioInvariants],
    ];
    for (const group of groups) {
      const key = group[0];
      const title = group[1];
      const fn = group[2];
      if (!wanted(key)) continue;
      console.log("");
      console.log("---- " + key + " " + title + " ----");
      const before = results.length;
      try {
        await fn();
      } catch (error) {
        record(key + " 场景未跑完", false, { error: String(error && error.stack ? error.stack : error).slice(0, 800) });
        try { await shot(key + "-failed"); } catch (shotError) { /* 截图失败不掩盖原因 */ }
      }
      if (results.length === before) note("（本场景未记录任何检查）");
    }
    consoleErrors.push(...page.consoleErrors);
    record("取证过程无页面 console error / 未捕获异常", consoleErrors.length === 0, consoleErrors.slice(0, 10));
  } catch (error) {
    console.error("取证主流程失败：" + (error && error.stack ? error.stack : error));
    if (results.length === 0) record("取证主流程失败", false, { error: String(error && error.message ? error.message : error).slice(0, 600) });
  } finally {
    try {
      await writeArtifacts(startedAt);
    } catch (error) {
      console.error("写产物失败：" + (error && error.stack ? error.stack : error));
    }
    if (browser) await browser.close().catch(() => null);
  }
  const counts = { PASS: 0, FAIL: 0, UNVERIFIED: 0 };
  results.forEach((r) => { counts[r.status] += 1; });
  console.log("");
  console.log("== 结果：PASS " + counts.PASS + " / FAIL " + counts.FAIL + " / UNVERIFIED " + counts.UNVERIFIED + " ==");
  console.log("产物：" + join(OUT, "summary.json") + " / " + join(OUT, "report.md") + " / " + SHOTS);
  process.exit(counts.FAIL > 0 ? 1 : 0);
}

main();
