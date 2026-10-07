#!/usr/bin/env node
/**
 * 独立复核（子智能体 E）自己的探针脚本 —— 不复用任何开发智能体的脚本。
 *
 * 为什么要单独一份：验收要求「每条结论都能复现，并写清做了什么 + 实际输出」。
 * 这份脚本把每条检查编成一个场景，自己生成 visual_probe 的步骤 JSON、自己起浏览器，
 * 把原始 JSON 结果打印出来，截图直接落进 docs/interactive-ui-screenshots/e-*.png。
 * 它不改产品代码，只读页面与只读接口；所有对网络的干扰都只发生在浏览器里，并会标注「模拟」。
 *
 * 用法（在仓库根目录跑）：
 *   node scripts/interactive-verify/fe-verify-e.mjs layout
 *   node scripts/interactive-verify/fe-verify-e.mjs connect
 *   node scripts/interactive-verify/fe-verify-e.mjs aesthetics
 *   node scripts/interactive-verify/fe-verify-e.mjs group
 *   node scripts/interactive-verify/fe-verify-e.mjs batch
 *   node scripts/interactive-verify/fe-verify-e.mjs chat
 *   node scripts/interactive-verify/fe-verify-e.mjs chat-fail
 *   node scripts/interactive-verify/fe-verify-e.mjs card-draft
 *   node scripts/interactive-verify/fe-verify-e.mjs all
 *
 * 环境变量：QIO_E_FE / QIO_E_BE / QIO_E_PORT / QIO_E_RAW
 */
import { spawnSync } from "node:child_process";
import { mkdirSync, writeFileSync } from "node:fs";
import { join, resolve } from "node:path";
import { tmpdir } from "node:os";

const ROOT = process.cwd();
const FE = process.env.QIO_E_FE || "http://127.0.0.1:5431";
const PORT = process.env.QIO_E_PORT || "9351";
const RAW = process.env.QIO_E_RAW || join(tmpdir(), "qio-e-verify");
const SHOTS = resolve(ROOT, "docs/interactive-ui-screenshots");
const BOARD = "board_default";
mkdirSync(RAW, { recursive: true });
mkdirSync(SHOTS, { recursive: true });

function runProbe(name, steps) {
  const file = join(RAW, "steps-" + name + ".json");
  writeFileSync(file, JSON.stringify(steps, null, 1), "utf8");
  const res = spawnSync("node", ["scripts/visual_probe.mjs", "@" + file], {
    cwd: ROOT,
    encoding: "utf8",
    maxBuffer: 64 * 1024 * 1024,
    env: {
      ...process.env,
      QIO_PROBE_PORT: PORT,
      QIO_PROBE_PROFILE: join(RAW, "chrome-profile"),
      QIO_PROBE_OUT: SHOTS,
    },
  });
  let parsed = null;
  try {
    parsed = JSON.parse(res.stdout || "{}");
  } catch (err) {
    parsed = { parseError: String(err), stdoutTail: String(res.stdout || "").slice(-3000) };
  }
  writeFileSync(join(RAW, "raw-" + name + ".json"), JSON.stringify(parsed, null, 1), "utf8");
  return { file, parsed, stderr: String(res.stderr || "").slice(-1500), status: res.status };
}

/* 注入页面里的小工具。注意：这一整段是普通字符串，不参与本文件的模板插值。 */
const HELPERS = [
  "window.__e = {",
  "  q: (s) => document.querySelector(s),",
  "  qa: (s) => Array.from(document.querySelectorAll(s)),",
  "  r: (el) => { if (!el) return null; const b = el.getBoundingClientRect();",
  "    return { x: Math.round(b.left), y: Math.round(b.top), w: Math.round(b.width), h: Math.round(b.height), right: Math.round(b.right), bottom: Math.round(b.bottom) }; },",
  "  hit: (sel) => { const el = document.querySelector(sel); if (!el) return { sel, missing: true };",
  "    const b = el.getBoundingClientRect(); const x = Math.round(b.left + b.width / 2), y = Math.round(b.top + b.height / 2);",
  "    const t = document.elementFromPoint(x, y);",
  "    return { sel, ok: !!t && (el === t || el.contains(t)), top: t ? (t.getAttribute('data-im') || t.tagName) : null }; },",
  "  click: (sel) => { const el = document.querySelector(sel); if (!el) return false; el.click(); return true; },",
  "  set: (sel, v) => { const el = document.querySelector(sel); if (!el) return false;",
  "    const proto = el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;",
  "    Object.getOwnPropertyDescriptor(proto, 'value').set.call(el, v);",
  "    el.dispatchEvent(new Event('input', { bubbles: true })); return true; },",
  "  text: (sel) => { const el = document.querySelector(sel); return el ? ((el.innerText || el.value || '') + '').replace(/\\s+/g, ' ').trim() : null; },",
  "  clickText: (sel, needle) => { const el = Array.from(document.querySelectorAll(sel)).find((n) => ((n.innerText || '') + '').includes(needle)); if (!el) return false; el.click(); return true; },",
  "  rectArea: (a, b) => { if (!a || !b) return 0; const w = Math.min(a.right, b.right) - Math.max(a.x, b.x); const h = Math.min(a.bottom, b.bottom) - Math.max(a.y, b.y); return w > 0 && h > 0 ? w * h : 0; },",
  "  seedBatch: (ids, key) => { localStorage.setItem('qio.interactive.intentBatches', JSON.stringify({ version: 2, updatedAt: new Date().toISOString(), records: ids.map((id) => ({ id, key: key, at: Date.now() })) })); return ids.length; },",
  "  pendingIntentIds: async () => { const r = await fetch('http://127.0.0.1:8931/api/interactive/boards/board_default/intents'); const j = await r.json();",
  "    return (j.intents || []).filter((i) => ['pending','needs_update','waiting_dependency','waiting_confirm'].indexOf(i.status) >= 0).map((i) => i.id); },",
  "  recStart: () => { window.__eReq = []; const of = window.fetch; window.fetch = function () { try { window.__eReq.push(String(arguments[0])); } catch (e) {} return of.apply(this, arguments); }; return true; },",
  "  rec: () => (window.__eReq || []).slice(),",
  "};",
  "'helpers-ready'",
].join("\n");

const MEASURE = [
  "(function(){",
  "  var q = window.__e.q, qa = window.__e.qa, R = window.__e.r;",
  "  var toolbar = q('[data-im=\"board-toolbar\"]');",
  "  var rows = 0, children = 0;",
  "  if (toolbar) { var tops = [];",
  "    var kids = qa('.tb-edit > *, .tb-submit > *');",
  "    for (var i = 0; i < kids.length; i++) { var b = kids[i].getBoundingClientRect(); if (b.width <= 0 || b.height <= 0) continue;",
  "      children++; var t = Math.round(b.top + b.height / 2); var seen = false; for (var j = 0; j < tops.length; j++) if (Math.abs(tops[j] - t) <= 12) seen = true;",
  "      if (!seen) tops.push(t); }",
  "    rows = tops.length; }",
  "  var chat = q('[data-im=\"chat-panel\"]'), batch = q('[data-im=\"batch-list\"]');",
  "  var cr = R(chat), br = R(batch);",
  "  var stage = q('[im-geo-mode]');",
  "  var stream = q('[data-im=\"chat-stream\"]');",
  "  var msgs = qa('[data-im=\"chat-stream\"] p');",
  "  return JSON.stringify({",
  "    viewport: { w: innerWidth, h: innerHeight },",
  "    theme: document.documentElement.getAttribute('data-theme'),",
  "    geoMode: stage ? stage.getAttribute('im-geo-mode') : null,",
  "    toolbar: R(toolbar), toolbarRows: rows, toolbarChildren: children,",
  "    toolbarOverflowY: toolbar ? (toolbar.scrollHeight > toolbar.clientHeight + 1) : null,",
  "    chatPresent: !!chat, batchPresent: !!batch,",
  "    chatRect: cr, batchRect: br,",
  "    intersection: window.__e.rectArea(cr, br),",
  "    streamHeight: stream ? stream.clientHeight : null,",
  "    lastMessageHeight: msgs.length ? Math.round(msgs[msgs.length - 1].getBoundingClientRect().height) : null,",
  "    chatInputRect: R(q('[data-im=\"chat-input\"]')),",
  "    sendRect: R(q('[data-im=\"chat-send\"]')),",
  "    submitRect: R(q('[data-im=\"submit\"]')),",
  "    batchEntryRect: R(q('[data-im=\"batch-entry\"]')),",
  "    batchItems: qa('[data-im=\"batch-item\"]').length,",
  "    submitClusterRect: R(q('[data-im=\"submit-cluster\"]')),",
  "    mainRect: R(q('.tb-main')),",
  "    toolbarClickHits: [window.__e.hit('[data-im=\"undo\"]'), window.__e.hit('[data-im=\"search-toggle\"]'), window.__e.hit('[data-im=\"add-menu\"]')],",
  "    batchHit: window.__e.hit('[data-im=\"batch-entry\"]'),",
  "    overlaySwitch: !!q('[data-im=\"overlay-switch\"]'),",
  "    entryText: window.__e.text('[data-im=\"batch-entry\"]'),",
  "    hits: [window.__e.hit('[data-im=\"chat-input\"]'), window.__e.hit('[data-im=\"chat-send\"]'), window.__e.hit('[data-im=\"submit\"]')],",
  "    bodyText: (document.body.innerText || '').slice(0, 200)",
  "  });",
  "})()",
].join("\n");

function openBoth() {
  /* 幂等：已经展开的不再点（点第二次会把它关掉） */
  return [
    { op: "eval", js: "(function(){ if (document.querySelector('[data-im=\"chat-panel\"]')) return 'chat-already-open'; var t = document.querySelector('[data-im=\"chat-toggle\"]'); if (t) { t.click(); return 'opened-chat'; } return 'no-toggle'; })()" },
    { op: "wait", ms: 600 },
    { op: "eval", js: "(function(){ if (document.querySelector('[data-im=\"batch-list\"]')) return 'batch-already-open'; var e = document.querySelector('[data-im=\"batch-entry\"]'); if (e) { e.click(); return 'opened-batch'; } return 'no-entry'; })()" },
    { op: "wait", ms: 900 },
  ];
}

/* ---------------------------------------------------------------- 布局场景 */

function scenarioLayout() {
  var sizes = [
    { w: 1440, h: 900, tag: "1440x900" },
    { w: 1024, h: 768, tag: "1024x768" },
    { w: 800, h: 600, tag: "800x600" },
  ];
  var steps = [
    { op: "navigate", url: FE + "/#/interactive", ms: 4000 },
    { op: "eval", js: HELPERS },
    { op: "eval", js: "(async function(){ var ids = await window.__e.pendingIntentIds(); var n = window.__e.seedBatch(ids, 'session:e-layout'); return JSON.stringify({ seeded: n, ids: ids }); })()" },
    { op: "reload", ms: 3500 },
    { op: "eval", js: HELPERS },
  ];
  sizes.forEach(function (s) {
    ["dark", "light"].forEach(function (theme) {
      steps.push({ op: "viewport", width: s.w, height: s.h, ms: 800 });
      steps.push({ op: "eval", js: "(function(){ document.documentElement.setAttribute('data-theme', '" + theme + "'); return true; })()" });
      steps.push({ op: "wait", ms: 400 });
      openBoth().forEach(function (x) { steps.push(x); });
      steps.push({ op: "eval", js: MEASURE });
      steps.push({ op: "screenshot", name: "e-layout-" + s.tag + "-" + theme });
    });
  });
  steps.push({ op: "eval", js: "(function(){ document.documentElement.setAttribute('data-theme','dark'); return true; })()" });
  return steps;
}

/* ------------------------------------------------------------ 连接点遮挡场景 */

const CONNECT_MEASURE = [
  "(function(){",
  "  var q = window.__e.q, qa = window.__e.qa, R = window.__e.r;",
  "  var bar = R(q('[data-im=\"card-toolbar\"]'));",
  "  var points = qa('[data-im=\"connect-point\"]').map(function (el) {",
  "    var rect = el.getBoundingClientRect();",
  "    var b = R(el);",
  "    var x = Math.round(rect.left + rect.width / 2), y = Math.round(rect.top + rect.height / 2);",
  "    var top = document.elementFromPoint(x, y);",
  "    return { cls: el.className, rect: b, overlap: window.__e.rectArea(b, bar),",
  "      hit: !!top && (el === top || el.contains(top)), topAt: top ? (top.getAttribute('data-im') || top.className || top.tagName) : null };",
  "  });",
  "  return JSON.stringify({ toolbar: bar, points: points, covered: points.filter(function (p) { return p.overlap > 0 || !p.hit; }).length });",
  "})()",
].join("\n");

function scenarioConnect() {
  var steps = [
    { op: "navigate", url: FE + "/#/interactive", ms: 4000 },
    { op: "eval", js: HELPERS },
  ];
  [{ w: 1440, h: 900, tag: "1440x900" }, { w: 1024, h: 768, tag: "1024x768" }, { w: 800, h: 600, tag: "800x600" }].forEach(function (s) {
    steps.push({ op: "viewport", width: s.w, height: s.h, ms: 900 });
    for (var i = 1; i <= 3; i++) {
      steps.push({ op: "drag", from: { selector: '[data-im="card"]', fx: 0.5, fy: 0.25 }, to: { selector: '[data-im="card"]', fx: 0.5, fy: 0.25 }, steps: 2, after: 500 });
      steps.push({ op: "eval", js: "(function(){ var card = document.querySelector('[data-im=\"card\"][data-card-id]'); if (!card) return 'no-card'; card.setAttribute('data-e-pick','1'); return card.getAttribute('data-card-id'); })()" });
      steps.push({ op: "eval", js: CONNECT_MEASURE });
      steps.push({ op: "screenshot", name: "e-connect-" + s.tag + "-r" + i });
    }
  });
  return steps;
}

/* ---------------------------------------------------------------- 审美场景 */

const AESTHETIC_MEASURE = [
  "(function(){",
  "  var q = window.__e.q, qa = window.__e.qa, R = window.__e.r;",
  "  function rgb(s) { var m = String(s).match(/rgba?\\(([^)]+)\\)/); if (!m) return null;",
  "    var p = m[1].split(',').map(function (x) { return parseFloat(x); }); return { r: p[0], g: p[1], b: p[2], a: p.length > 3 ? p[3] : 1 }; }",
  "  function lum(c) { function f(v) { v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); }",
  "    return 0.2126 * f(c.r) + 0.7152 * f(c.g) + 0.0722 * f(c.b); }",
  "  function contrast(fg, bg) { var a = lum(fg), b = lum(bg); var hi = Math.max(a, b), lo = Math.min(a, b); return Math.round(((hi + 0.05) / (lo + 0.05)) * 100) / 100; }",
  "  function bgOf(el) { var n = el; while (n && n !== document.documentElement) { var c = rgb(getComputedStyle(n).backgroundColor);",
  "      if (c && c.a > 0.5) return c; n = n.parentElement; } return rgb(getComputedStyle(document.body).backgroundColor) || { r: 255, g: 255, b: 255, a: 1 }; }",
  "  function probeText(sel, label) { var el = q(sel); if (!el) return { label: label, missing: true }; var cs = getComputedStyle(el);",
  "    var fg = rgb(cs.color), bg = bgOf(el);",
  "    return { label: label, size: cs.fontSize, family: cs.fontFamily.split(',')[0].replace(/\"/g, ''), weight: cs.fontWeight,",
  "      ratio: fg && bg ? contrast(fg, bg) : null, text: (el.innerText || '').slice(0, 30) }; }",
  "  var anims = qa('*').map(function (el) { var cs = getComputedStyle(el); return cs.transitionDuration + '|' + cs.animationDuration; })",
  "    .filter(function (x) { return x !== '0s|0s' && x !== '|'; });",
  "  var animSummary = {}; anims.forEach(function (k) { animSummary[k] = (animSummary[k] || 0) + 1; });",
  "  var buttons = qa('button, a[href]');",
  "  var iconOnly = buttons.filter(function (b) { return !(b.innerText || '').trim(); });",
  "  var svgs = qa('svg').map(function (s) { return R(s); }).filter(Boolean);",
  "  var svgSizes = {}; svgs.forEach(function (s) { var k = s.w + 'x' + s.h; svgSizes[k] = (svgSizes[k] || 0) + 1; });",
  "  var words = ['adapter', '接口', '求差', '状态机', 'DOM', 'payload', 'token', '前端', '后端', '字段', 'schema', 'undefined', 'JSON', 'TODO'];",
  "  var visible = document.body.innerText || '';",
  "  var markup = document.documentElement.outerHTML;",
  "  return JSON.stringify({",
  "    theme: document.documentElement.getAttribute('data-theme'),",
  "    reducedMotion: matchMedia('(prefers-reduced-motion: reduce)').matches,",
  "    text: [probeText('.im-title', '板面标题'), probeText('[data-im=\"board-counts\"]', '次级-数量'), probeText('[data-im=\"save-status\"]', '主状态-保存'),",
  "      probeText('[data-im=\"chat-input\"]', '正文-聊天输入'), probeText('[data-im=\"chat-status\"]', '等宽-聊天状态'), probeText('[data-im=\"submit\"]', '主要操作-提交'),",
  "      probeText('[data-im=\"visible-range\"]', '提交区-可见范围'), probeText('[data-im=\"batch-remaining\"]', '批量-剩余')],",
  "    animations: animSummary, animatedElements: anims.length,",
  "    iconOnlyButtons: iconOnly.length,",
  "    iconOnlyWithoutLabel: iconOnly.filter(function (b) { return !b.getAttribute('aria-label') && !b.getAttribute('title'); }).length,",
  "    svgSizes: svgSizes, svgCount: svgs.length,",
  "    forbiddenVisible: words.filter(function (w) { return visible.indexOf(w) >= 0; }),",
  "    forbiddenInMarkup: words.filter(function (w) { return markup.toLowerCase().indexOf(w.toLowerCase()) >= 0; }),",
  "    visibleText: visible.slice(0, 350)",
  "  });",
  "})()",
].join("\n");

function scenarioAesthetics() {
  return [
    { op: "navigate", url: FE + "/#/interactive", ms: 4000 },
    { op: "eval", js: HELPERS },
    { op: "viewport", width: 1440, height: 900, ms: 700 },
    { op: "eval", js: "(function(){ document.documentElement.setAttribute('data-theme','dark'); return true; })()" },
    { op: "wait", ms: 300 },
    { op: "eval", js: AESTHETIC_MEASURE },
    { op: "screenshot", name: "e-aesthetic-1440x900-dark" },
    { op: "eval", js: "(function(){ document.documentElement.setAttribute('data-theme','light'); return true; })()" },
    { op: "wait", ms: 300 },
    { op: "eval", js: AESTHETIC_MEASURE },
    { op: "screenshot", name: "e-aesthetic-1440x900-light" },
    { op: "cdp", method: "Emulation.setEmulatedMedia", params: { features: [{ name: "prefers-reduced-motion", value: "reduce" }] } },
    { op: "wait", ms: 400 },
    { op: "eval", js: AESTHETIC_MEASURE },
    { op: "screenshot", name: "e-aesthetic-reduced-motion" },
    { op: "cdp", method: "Emulation.setEmulatedMedia", params: { features: [] } },
    { op: "eval", js: "(function(){ document.documentElement.setAttribute('data-theme','dark'); return true; })()" },
  ];
}

/* ------------------------------------------------------------ 重叠成组场景 */

const GROUP_MEASURE = [
  "(function(){",
  "  var q = window.__e.q, qa = window.__e.qa, R = window.__e.r;",
  "  return JSON.stringify({",
  "    cards: qa('[data-im=\"card\"]').map(function (el) { return { id: el.getAttribute('data-card-id'), rect: R(el) }; }),",
  "    groups: qa('[data-im=\"group\"]').map(function (el) { var input = el.querySelector('[data-im=\"group-name\"]');",
  "      return { id: el.getAttribute('data-group-id'), name: input ? input.value : null,",
  "        defaultBadge: !!el.querySelector('.badge.default-name'),",
  "        members: el.querySelector('.count') ? el.querySelector('.count').innerText : null }; }),",
  "    cardCount: qa('[data-im=\"card\"]').length,",
  "    undoDisabled: q('[data-im=\"undo\"]') ? q('[data-im=\"undo\"]').disabled : null,",
  "    redoDisabled: q('[data-im=\"redo\"]') ? q('[data-im=\"redo\"]').disabled : null,",
  "    saveStatus: window.__e.text('[data-im=\"save-status\"]')",
  "  });",
  "})()",
].join("\n");

function scenarioGroup() {
  var steps = [
    { op: "navigate", url: FE + "/#/interactive", ms: 4000 },
    { op: "eval", js: HELPERS },
    { op: "viewport", width: 1440, height: 900, ms: 700 },
    { op: "eval", js: "(function(){ window.__e.click('[data-im=\"add-menu\"]'); return true; })()" },
    { op: "wait", ms: 400 },
    { op: "eval", js: "(function(){ return window.__e.click('[data-im=\"add-text\"]'); })()" },
    { op: "wait", ms: 1000 },
    { op: "eval", js: "(function(){ window.__e.click('[data-im=\"add-menu\"]'); return true; })()" },
    { op: "wait", ms: 400 },
    { op: "eval", js: "(function(){ return window.__e.click('[data-im=\"add-text\"]'); })()" },
    { op: "wait", ms: 1200 },
    { op: "eval", js: GROUP_MEASURE },
    /* 给最后两张卡片打上临时标记：探针的 drag 只能按选择器锚定，id 要运行时才知道 */
    { op: "eval", js: "(function(){ var els = window.__e.qa('[data-im=\"card\"][data-card-id]'); if (els.length < 2) return 'too-few'; els[els.length-1].setAttribute('data-e-src','1'); els[els.length-2].setAttribute('data-e-dst','1'); return JSON.stringify([els[els.length-2].getAttribute('data-card-id'), els[els.length-1].getAttribute('data-card-id')]); })()" },
    { op: "drag", from: { selector: '[data-e-src="1"]', fx: 0.5, fy: 0.25 }, to: { selector: '[data-e-dst="1"]', fx: 0.5, fy: 0.35 }, steps: 8, after: 700 },
    { op: "wait", ms: 900 },
    { op: "eval", js: GROUP_MEASURE },
    { op: "screenshot", name: "e-group-after-drop" },
    /* 改名：先改成自定义名，再留空（留空不改动），再用撤销/重做验前后一致 */
    { op: "eval", js: "(function(){ var inputs = window.__e.qa('[data-im=\"group-name\"]'); var el = inputs[inputs.length-1]; el.value = 'E-复核-自定义组名'; el.dispatchEvent(new Event('change', { bubbles: true })); return el.value; })()" },
    { op: "wait", ms: 1500 },
    { op: "eval", js: GROUP_MEASURE },
    { op: "screenshot", name: "e-group-renamed" },
    { op: "eval", js: "(function(){ var inputs = window.__e.qa('[data-im=\"group-name\"]'); var el = inputs[inputs.length-1]; el.value = ''; el.dispatchEvent(new Event('change', { bubbles: true })); return el.value; })()" },
    { op: "wait", ms: 1200 },
    { op: "eval", js: GROUP_MEASURE },
    { op: "eval", js: "(function(){ return window.__e.click('[data-im=\"undo\"]'); })()" },
    { op: "wait", ms: 1200 },
    { op: "eval", js: GROUP_MEASURE },
    { op: "eval", js: "(function(){ return window.__e.click('[data-im=\"redo\"]'); })()" },
    { op: "wait", ms: 1200 },
    { op: "eval", js: GROUP_MEASURE },
    { op: "reload", ms: 3500 },
    { op: "eval", js: HELPERS },
    { op: "wait", ms: 800 },
    { op: "eval", js: GROUP_MEASURE },
    { op: "screenshot", name: "e-group-after-reload" },
  ];
  return steps;
}

/* -------------------------------------------------------------- 批量场景 */

const BATCH_MEASURE = [
  "(function(){",
  "  var q = window.__e.q, qa = window.__e.qa;",
  "  var entry = q('[data-im=\"batch-entry\"]');",
  "  var items = qa('[data-im=\"batch-item\"]').map(function (el) {",
  "    return { id: el.getAttribute('data-intent-id'), state: el.getAttribute('data-im-state'),",
  "      checked: !!el.querySelector('input:checked') }; });",
  "  return JSON.stringify({",
  "    entryPresent: !!entry,",
  "    entryText: entry ? ((entry.innerText || '') + '').replace(/\\s+/g, ' ').trim() : null,",
  "    entries: qa('[data-im=\"batch-entry\"]').length,",
  "    listPresent: !!q('[data-im=\"batch-list\"]'),",
  "    remaining: window.__e.text('[data-im=\"batch-remaining\"]'),",
  "    items: items, itemCount: items.length,",
  "    pendingCount: items.filter(function (i) { return i.state === 'pending'; }).length,",
  "    processedCount: items.filter(function (i) { return i.state !== 'pending'; }).length,",
  "    summary: window.__e.text('.summary'),",
  "    overlaySwitch: !!q('[data-im=\"overlay-switch\"]'),",
  "    geoMode: (function(){ var s = q('[im-geo-mode]'); return s ? s.getAttribute('im-geo-mode') : null; })()",
  "  });",
  "})()",
].join("\n");

function scenarioBatch() {
  return [
    { op: "navigate", url: FE + "/#/interactive", ms: 4000 },
    { op: "eval", js: HELPERS },
    { op: "viewport", width: 1440, height: 900, ms: 700 },
    /* 1) 产品自己的「一次产生四项」路径（§9.2 来源①：接口返回的这一批） */
    { op: "eval", js: "(function(){ return window.__e.click('[data-im=\"demo-entry\"]'); })()" },
    { op: "wait", ms: 700 },
    { op: "eval", js: "(function(){ return window.__e.click('[data-im=\"demo-create\"]'); })()" },
    { op: "wait", ms: 2500 },
    { op: "eval", js: BATCH_MEASURE },
    { op: "screenshot", name: "e-batch-created" },
    { op: "eval", js: "(function(){ return window.__e.click('[data-im=\"batch-entry\"]'); })()" },
    { op: "wait", ms: 900 },
    { op: "eval", js: BATCH_MEASURE },
    { op: "screenshot", name: "e-batch-list-4" },
    /* 2) 清掉本机批次记录后刷新：没有可证明来源 → 不出现四项列表（实机版反例 2） */
    { op: "eval", js: "(function(){ try { localStorage.removeItem('qio.interactive.intentBatches'); } catch (e) {} return true; })()" },
    { op: "reload", ms: 3500 },
    { op: "eval", js: HELPERS },
    { op: "eval", js: BATCH_MEASURE },
    { op: "eval", js: "(async function(){ var r = await fetch('/api/interactive/boards/board_default/intents'); var j = await r.json(); return JSON.stringify({ pendingFromApi: (j.intents||[]).filter(function(i){return ['pending','needs_update','waiting_dependency','waiting_confirm'].indexOf(i.status)>=0;}).length }); })()" },
    { op: "screenshot", name: "e-batch-no-record-after-reload" },
    /* 3) 重新走一次演示入口：同一批四项又被记下来 */
    { op: "eval", js: "(function(){ return window.__e.click('[data-im=\"demo-entry\"]'); })()" },
    { op: "wait", ms: 700 },
    { op: "eval", js: "(function(){ return window.__e.click('[data-im=\"demo-create\"]'); })()" },
    { op: "wait", ms: 2500 },
    { op: "eval", js: "(function(){ return window.__e.click('[data-im=\"batch-entry\"]'); })()" },
    { op: "wait", ms: 900 },
    { op: "eval", js: BATCH_MEASURE },
    /* 4) 处理掉一项：入口仍在、剩余数减一、列表仍列出四项（已处理项不可再选） */
    { op: "eval", js: "(function(){ return window.__e.click('[data-im=\"batch-clear\"]'); })()" },
    { op: "wait", ms: 300 },
    { op: "eval", js: "(function(){ var c = document.querySelector('[data-im=\"batch-item\"] input[type=\"checkbox\"]'); if (!c) return 'no-checkbox'; c.click(); return 'checked'; })()" },
    { op: "wait", ms: 300 },
    { op: "eval", js: "(function(){ return window.__e.click('[data-im=\"batch-reject\"]'); })()" },
    { op: "wait", ms: 3000 },
    { op: "eval", js: BATCH_MEASURE },
    { op: "screenshot", name: "e-batch-after-1" },
    /* 5) 刷新：资格与剩余数保持 */
    { op: "reload", ms: 3500 },
    { op: "eval", js: HELPERS },
    { op: "eval", js: BATCH_MEASURE },
    { op: "eval", js: "(function(){ return window.__e.click('[data-im=\"batch-entry\"]'); })()" },
    { op: "wait", ms: 900 },
    { op: "eval", js: BATCH_MEASURE },
    { op: "screenshot", name: "e-batch-after-reload" },
    /* 6) 把剩下的三项也处理掉：全部处理完入口消失 */
    { op: "eval", js: "(function(){ return window.__e.click('[data-im=\"batch-all\"]'); })()" },
    { op: "wait", ms: 400 },
    { op: "eval", js: "(function(){ return window.__e.click('[data-im=\"batch-reject\"]'); })()" },
    { op: "wait", ms: 3500 },
    { op: "eval", js: BATCH_MEASURE },
    { op: "screenshot", name: "e-batch-all-done" },
    /* 7) 复位：重新生成 4 项待审批的演示意图（演示入口本来就是可重复使用的入口） */
    { op: "eval", js: "(function(){ return window.__e.click('[data-im=\"demo-entry\"]'); })()" },
    { op: "wait", ms: 700 },
    { op: "eval", js: "(function(){ return window.__e.click('[data-im=\"demo-create\"]'); })()" },
    { op: "wait", ms: 2500 },
    { op: "eval", js: BATCH_MEASURE },
  ];
}

/* ------------------------------------------------------ 聊天草稿 / 请求独立 */

const CHAT_MEASURE = [
  "(function(){",
  "  var q = window.__e.q;",
  "  var input = q('[data-im=\"chat-input\"]');",
  "  var keys = []; var values = {};",
  "  try { keys = Object.keys(localStorage).filter(function (k) { return k.indexOf('qio.draft.') === 0; });",
  "    keys.forEach(function (k) { values[k] = localStorage.getItem(k); }); } catch (e) { keys = ['<localStorage 不可用>']; }",
  "  var rec = window.__e.rec();",
  "  return JSON.stringify({",
  "    chatOpen: !!q('[data-im=\"chat-panel\"]'),",
  "    inputValue: input ? input.value : null,",
  "    draftStatus: window.__e.text('[data-im=\"chat-draft-status\"]'),",
  "    failure: window.__e.text('[data-im=\"chat-failure\"]'),",
  "    turnError: window.__e.text('[data-im=\"chat-turn-error\"]'),",
  "    requests: rec,",
  "    turnsCalls: rec.filter(function (u) { return u.indexOf('/api/turns') >= 0; }).length,",
  "    submissionsCalls: rec.filter(function (u) { return u.indexOf('/submissions') >= 0; }).length,",
  "    draftKeys: keys, draftValues: values,",
  "    messages: window.__e.qa('[data-im=\"chat-stream\"] p').length,",
  "    submitStatus: window.__e.text('[data-im=\"submit-status\"]')",
  "  });",
  "})()",
].join("\n");

function scenarioChat() {
  return [
    { op: "navigate", url: FE + "/#/interactive", ms: 4000 },
    { op: "eval", js: HELPERS },
    { op: "viewport", width: 1440, height: 900, ms: 700 },
    { op: "eval", js: "(function(){ return window.__e.click('[data-im=\"chat-toggle\"]'); })()" },
    { op: "wait", ms: 700 },
    { op: "eval", js: "(function(){ window.__e.recStart(); return true; })()" },
    { op: "eval", js: "(function(){ return window.__e.set('[data-im=\"chat-input\"]', 'E-复核-刷新前草稿'); })()" },
    { op: "wait", ms: 1500 },
    { op: "eval", js: CHAT_MEASURE },
    { op: "screenshot", name: "e-chat-draft-before-reload" },
    { op: "reload", ms: 3500 },
    { op: "eval", js: HELPERS },
    { op: "eval", js: "(function(){ return window.__e.click('[data-im=\"chat-toggle\"]'); })()" },
    { op: "wait", ms: 800 },
    { op: "eval", js: CHAT_MEASURE },
    { op: "screenshot", name: "e-chat-draft-restored" },
    { op: "eval", js: "(function(){ window.__e.recStart(); return window.__e.set('[data-im=\"chat-input\"]', 'E-复核-真实发送'); })()" },
    { op: "wait", ms: 900 },
    { op: "eval", js: "(function(){ return window.__e.click('[data-im=\"chat-send\"]'); })()" },
    { op: "wait", ms: 4000 },
    { op: "eval", js: CHAT_MEASURE },
    { op: "screenshot", name: "e-chat-after-send" },
    { op: "eval", js: "(function(){ window.__e.recStart(); return window.__e.click('[data-im=\"submit\"]'); })()" },
    { op: "wait", ms: 4000 },
    { op: "eval", js: CHAT_MEASURE },
    { op: "screenshot", name: "e-chat-after-submit" },
  ];
}

function scenarioChatFail() {
  return [
    { op: "navigate", url: FE + "/#/interactive", ms: 4000 },
    { op: "eval", js: HELPERS },
    { op: "viewport", width: 1440, height: 900, ms: 700 },
    { op: "eval", js: "(function(){ return window.__e.click('[data-im=\"chat-toggle\"]'); })()" },
    { op: "wait", ms: 700 },
    { op: "eval", js: "(function(){ var of = window.fetch; window.__eRealFetch = of; window.__eSim1 = true; window.fetch = function () { if (String(arguments[0]).indexOf('/api/turns') >= 0) { return new Promise(function (_, rej) { setTimeout(function () { rej(new Error('模拟：受理失败（页面级拦截）')); }, 400); }); } return of.apply(this, arguments); }; return true; })()" },
    { op: "eval", js: "(function(){ return window.__e.set('[data-im=\"chat-input\"]', 'E-旧文字'); })()" },
    { op: "wait", ms: 800 },
    { op: "eval", js: "(function(){ return window.__e.click('[data-im=\"chat-send\"]'); })()" },
    { op: "wait", ms: 120 },
    { op: "eval", js: "(function(){ return window.__e.set('[data-im=\"chat-input\"]', 'E-新文字'); })()" },
    { op: "wait", ms: 2500 },
    { op: "eval", js: CHAT_MEASURE },
    { op: "screenshot", name: "e-chat-send-failure" },
    { op: "eval", js: "(function(){ window.__eOrigSet = Storage.prototype.setItem; Storage.prototype.setItem = function () { throw new DOMException('模拟：本机存储已满', 'QuotaExceededError'); }; return true; })()" },
    { op: "eval", js: "(function(){ return window.__e.set('[data-im=\"chat-input\"]', 'E-存储失败文字'); })()" },
    { op: "wait", ms: 1600 },
    { op: "eval", js: CHAT_MEASURE },
    { op: "screenshot", name: "e-chat-draft-save-failure" },
    { op: "eval", js: "(function(){ var b = document.querySelector('[data-im=\"chat-draft-retry\"]'); if (!b) return 'no-retry-button'; b.click(); return 'clicked'; })()" },
    { op: "wait", ms: 1400 },
    { op: "eval", js: CHAT_MEASURE },
    { op: "eval", js: "(function(){ Storage.prototype.setItem = window.__eOrigSet; var b = document.querySelector('[data-im=\"chat-draft-retry\"]'); if (!b) return 'no-retry-button'; b.click(); return 'clicked'; })()" },
    { op: "wait", ms: 1400 },
    { op: "eval", js: CHAT_MEASURE },
    { op: "screenshot", name: "e-chat-draft-retry-ok" },
  ];
}

/* -------------------------------------------------------------- 卡片草稿 */

const CARD_DRAFT_MEASURE = [
  "(function(){",
  "  var q = window.__e.q, qa = window.__e.qa;",
  "  var ta = q('[data-im=\"card-editor\"] textarea');",
  "  return JSON.stringify({",
  "    cardCount: qa('[data-im=\"card\"]').length,",
  "    editorOpen: !!q('[data-im=\"card-editor\"]'),",
  "    editorValue: ta ? ta.value : null,",
  "    hintPresent: !!q('[data-im=\"card-draft-hint\"]'),",
  "    hintText: window.__e.text('[data-im=\"card-draft-hint\"]'),",
  "    retryPresent: !!q('[data-im=\"card-draft-retry\"]'),",
  "    saveStatus: window.__e.text('[data-im=\"save-status\"]'),",
  "    submitStatus: window.__e.text('[data-im=\"submit-status\"]')",
  "  });",
  "})()",
].join("\n");

function scenarioCardDraft() {
  return [
    { op: "navigate", url: FE + "/#/interactive", ms: 4000 },
    { op: "eval", js: HELPERS },
    { op: "viewport", width: 1440, height: 900, ms: 700 },
    { op: "eval", js: CARD_DRAFT_MEASURE },
    { op: "drag", from: { selector: '[data-im="card"]', fx: 0.5, fy: 0.25 }, to: { selector: '[data-im="card"]', fx: 0.5, fy: 0.25 }, steps: 2, after: 600 },
    { op: "eval", js: "(function(){ return window.__e.click('[data-im=\"card-edit\"]'); })()" },
    { op: "wait", ms: 600 },
    { op: "eval", js: CARD_DRAFT_MEASURE },
    { op: "eval", js: "(function(){ var of = window.fetch; window.__eRealFetch = of; window.__eSim2 = true; window.fetch = function () { var m = String(arguments[1] && arguments[1].method || '').toUpperCase(); if (String(arguments[0]).indexOf('/drafts') >= 0 && m === 'PUT') { return Promise.reject(new Error('模拟：草稿写入失败（页面级拦截）')); } return of.apply(this, arguments); }; return true; })()" },
    { op: "eval", js: "(function(){ var ta = document.querySelector('[data-im=\"card-editor\"] textarea'); if (!ta) return false; Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value').set.call(ta, 'E-复核-卡片草稿'); ta.dispatchEvent(new Event('input', { bubbles: true })); return true; })()" },
    { op: "wait", ms: 2200 },
    { op: "eval", js: CARD_DRAFT_MEASURE },
    { op: "screenshot", name: "e-card-draft-failure" },
    { op: "eval", js: "(function(){ var b = document.querySelector('[data-im=\"card-draft-retry\"]'); if (!b) return 'no-retry'; b.click(); return 'clicked'; })()" },
    { op: "wait", ms: 1500 },
    { op: "eval", js: CARD_DRAFT_MEASURE },
    { op: "eval", js: "(function(){ return window.__e.clickText('[data-im=\"card-editor\"] button', '取消'); })()" },
    { op: "wait", ms: 600 },
    { op: "eval", js: CARD_DRAFT_MEASURE },
    { op: "eval", js: "(function(){ return window.__e.click('[data-im=\"card-edit\"]'); })()" },
    { op: "wait", ms: 700 },
    { op: "eval", js: CARD_DRAFT_MEASURE },
    { op: "screenshot", name: "e-card-draft-reopen" },
    { op: "eval", js: "(function(){ if (window.__eRealFetch) window.fetch = window.__eRealFetch; var b = document.querySelector('[data-im=\"card-draft-retry\"]'); if (!b) return 'no-retry'; b.click(); return 'clicked'; })()" },
    { op: "wait", ms: 1600 },
    { op: "eval", js: CARD_DRAFT_MEASURE },
    { op: "screenshot", name: "e-card-draft-retry-ok" },
  ];
}


/* ------------------------------------------- 纯函数级反例（跑在真实页面模块里） */

const PURE_CHECK = [
  "(async function(){",
  "  var A = await import('/src/interactive/approval.ts');",
  "  var B = await import('/src/interactive/board.ts');",
  "  var S = await import('/src/interactive/submission.ts');",
  "  var D = await import('/src/interactive/drafts.ts');",
  "  var KEY = A.INTENT_BATCH_STORAGE_KEY;",
  "  var out = {};",
  "  function reset(){ try { localStorage.removeItem(KEY); } catch (e) {} }",
  "  function mk(id, status, createdAt, submissionId) { return { id: id, status: status, createdAt: createdAt, submissionId: submissionId || null }; }",
  "  /* 反例 1：默认组名 —— 旧实现会按已有名字生成「组 N」 */",
  "  out.groupName_next = B.nextDefaultGroupName(['组 1', '组 2']);",
  "  out.groupName_const = B.DEFAULT_GROUP_NAME;",
  "  out.groupName_legacyRecognized = B.isDefaultGroupName('组 3');",
  "  function card(id) { return { id: id, kind: 'text', content: id, x: 0, y: 0, w: 100, h: 80, checked: true, hidden: false, folded: false, bookmarked: false, deleted: false, createdAt: '2026-01-01T00:00:00.000Z', updatedAt: '2026-01-01T00:00:00.000Z', meta: {} }; }",
  "  var legacy = { boardId: 'b', updatedAt: '', cards: [card('c1'), card('c2')], selection: [], links: [],",
  "    groups: [{ id: 'g_old', name: '组 7', defaultName: true, ordered: false, x: 0, y: 0, w: 1, h: 1, members: ['c1', 'c2'], deleted: false, createdAt: '', updatedAt: '' }] };",
  "  var withNew = B.createGroup(legacy, ['c1'], 'E-新组');",
  "  out.legacyKept = withNew.groups.filter(function (g) { return g.id === 'g_old'; }).map(function (g) { return { name: g.name, defaultName: g.defaultName }; });",
  "  var fresh = B.createGroup({ boardId: 'b', updatedAt: '', cards: [card('c9')], groups: [], links: [], selection: [] }, ['c9']);",
  "  out.newGroupName = fresh.groups[0].name;",
  "  out.newGroupDefaultNameFlag = fresh.groups[0].defaultName;",
  "  out.legacyAfterNormalize = B.normalizeState(legacy).groups.map(function (g) { return g.name; });",
  "  out.renameEmptyKeeps = B.renameGroup(B.normalizeState(legacy), 'g_old', '').groups[0].name;",
  "  /* 反例 2：按创建秒归批 —— 同一秒的两批各两项，绝不能变成四项一批 */",
  "  reset();",
  "  var sameSecond = [mk('a1', 'pending', '2026-10-07T10:00:00.000Z'), mk('a2', 'pending', '2026-10-07T10:00:00.000Z'),",
  "    mk('b1', 'pending', '2026-10-07T10:00:00.000Z'), mk('b2', 'pending', '2026-10-07T10:00:00.000Z')];",
  "  out.sameSecond_allSameSecond = sameSecond.every(function (x) { return x.createdAt.slice(0, 19) === '2026-10-07T10:00:00'; });",
  "  out.sameSecond_batchCount = A.groupIntentsByBatch(sameSecond).length;",
  "  out.sameSecond_listBatches = A.batchesWithList(sameSecond).length;",
  "  out.sameSecond_keys = sameSecond.map(function (x) { return A.batchKeyOf(x); });",
  "  out.sameSecond_hasRecoverable = sameSecond.map(function (x) { return A.hasRecoverableBatch(x); });",
  "  /* 反例 3：资格按「剩余待审批数」判 —— 处理掉三项后列表必须还在 */",
  "  reset();",
  "  A.recordIntentBatch('session:e-4', ['q1', 'q2', 'q3', 'q4']);",
  "  function qs(processed) { return ['q1', 'q2', 'q3', 'q4'].map(function (id, idx) { return mk(id, idx < processed ? 'rejected' : 'pending'); }); }",
  "  function shape(list) { return list.map(function (b) { return { key: b.key, total: b.intentIds.length, pending: b.pendingIds.length }; }); }",
  "  out.qualify_pending4 = shape(A.batchesWithList(qs(0)));",
  "  out.qualify_pending3 = shape(A.batchesWithList(qs(1)));",
  "  out.qualify_pending1 = shape(A.batchesWithList(qs(3)));",
  "  out.qualify_pending0 = shape(A.batchesWithList(qs(4)));",
  "  out.qualify_entryText3 = A.batchEntryText(A.batchesWithList(qs(1))[0]);",
  "  reset();",
  "  A.recordIntentBatch('session:e-3', ['t1', 't2', 't3']);",
  "  A.recordIntentBatch('session:e-1', ['u1']);",
  "  var threePlusOne = [mk('t1', 'pending'), mk('t2', 'pending'), mk('t3', 'pending'), mk('u1', 'pending')];",
  "  out.threePlusOne_batchCount = A.groupIntentsByBatch(threePlusOne).length;",
  "  out.threePlusOne_listBatches = A.batchesWithList(threePlusOne).length;",
  "  /* 已处理项不能再次提交 */",
  "  reset();",
  "  A.recordIntentBatch('session:e-split', ['s1', 's2']);",
  "  var splitBatch = A.groupIntentsByBatch([mk('s1', 'pending'), mk('s2', 'rejected')])[0];",
  "  out.splitBlocked = A.splitBatchIn([mk('s1', 'pending'), mk('s2', 'rejected')], splitBatch, ['s2'], 'approve');",
  "  out.selectableProcessed = A.isBatchItemSelectable(splitBatch, 's2');",
  "  /* 存储被禁用 / 写满 / 损坏：一律各自成批，绝不合并 */",
  "  reset();",
  "  var origSet = Storage.prototype.setItem;",
  "  var four = [mk('f1', 'pending'), mk('f2', 'pending'), mk('f3', 'pending'), mk('f4', 'pending')];",
  "  Storage.prototype.setItem = function () { throw new DOMException('模拟：本机存储已满', 'QuotaExceededError'); };",
  "  A.recordIntentBatch('session:full', ['f1', 'f2', 'f3', 'f4']);",
  "  out.storageFull_batchCount = A.groupIntentsByBatch(four).length;",
  "  out.storageFull_listBatches = A.batchesWithList(four).length;",
  "  Storage.prototype.setItem = origSet;",
  "  try { localStorage.setItem(KEY, '{这不是 JSON'); } catch (e) {}",
  "  out.corrupt_batchCount = A.groupIntentsByBatch(four).length;",
  "  try { localStorage.setItem(KEY, JSON.stringify({ version: 2, records: [{ id: 'f1', key: 'session:x', at: 1 }, { id: 42 }] })); } catch (e) {}",
  "  out.partialCorrupt_batchCount = A.groupIntentsByBatch([mk('f1', 'pending'), mk('f2', 'pending')]).length;",
  "  reset();",
  "  /* 草稿键互不串用 + 可见范围（前端纯函数与服务端同判据） */",
  "  out.draftKeys = [D.draftStorageKey('chat', 'topic-1'), D.draftStorageKey('card', 'c1')];",
  "  out.draftKeysDiffer = D.draftStorageKey('chat', 'topic-1') !== D.draftStorageKey('card', 'c1');",
  "  var note = { id: 'n1', kind: 'text', checked: false, hidden: false, deleted: false };",
  "  var mat = { id: 'm1', kind: 'code', checked: false, hidden: false, deleted: false };",
  "  var st = { boardId: 'b', cards: [note, mat], groups: [{ id: 'g', members: ['n1', 'm1'], deleted: false }],",
  "    links: [{ id: 'l1', src: 'n1', dst: 'm1', deleted: false }], selection: ['n1'], updatedAt: '' };",
  "  var range = S.localVisibleRange(st);",
  "  out.localVisible = { cards: range.cards.map(function (c) { return c.id; }), links: range.links.map(function (l) { return l.id; }),",
  "    groups: range.groups.map(function (g) { return g.members; }), selection: range.selection, notVisible: range.notVisibleCount };",
  "  out.isVisibleCard = { uncheckedNote: S.isVisibleCard(note), material: S.isVisibleCard(mat) };",
  "  return JSON.stringify(out, null, 1);",
  "})()",
].join("\n");

function scenarioPure() {
  return [
    { op: "navigate", url: FE + "/#/interactive", ms: 4000 },
    { op: "eval", js: HELPERS },
    { op: "eval", js: PURE_CHECK },
  ];
}

/* -------------------------------------------------- 可见范围与提交（实机） */

const VIS_MEASURE = [
  "(function(){",
  "  var q = window.__e.q, qa = window.__e.qa;",
  "  var checks = qa('[data-im=\"check\"]').map(function (el) { return { cardId: el.getAttribute('data-card-id'), checked: el.checked }; });",
  "  var sub = window.__eSub || null;",
  "  var summary = null;",
  "  if (sub) { summary = { status: sub.status, delivered: sub.delivery && sub.delivery.delivered,",
  "    reason: sub.delivery && sub.delivery.reason, checkedCleared: sub.checkedCleared,",
  "    visibleCards: (sub.visibleRange && sub.visibleRange.cards || []).map(function (c) { return c.id; }),",
  "    visibleLinks: (sub.visibleRange && sub.visibleRange.links || []).map(function (l) { return l.id; }),",
  "    notVisibleCount: sub.visibleRange && sub.visibleRange.notVisibleCount }; }",
  "  return JSON.stringify({ rangeText: window.__e.text('[data-im=\"visible-range\"]'),",
  "    submitStatus: window.__e.text('[data-im=\"submit-status\"]'),",
  "    submitDetails: window.__e.text('[data-im=\"submit-details\"]'),",
  "    notConnected: window.__e.text('[data-im=\"not-connected\"]'),",
  "    checks: checks, submission: summary });",
  "})()",
].join("\n");

function scenarioVisibility() {
  return [
    { op: "navigate", url: FE + "/#/interactive", ms: 4000 },
    { op: "eval", js: HELPERS },
    { op: "viewport", width: 1440, height: 900, ms: 700 },
    { op: "eval", js: VIS_MEASURE },
    { op: "drag", from: { selector: '[data-im="card"][data-card-id="s_note_a"]', fx: 0.5, fy: 0.2 }, to: { selector: '[data-im="card"][data-card-id="s_note_a"]', fx: 0.5, fy: 0.2 }, steps: 2, after: 600 },
    { op: "eval", js: "(function(){ var c = document.querySelector('[data-im=\"check\"]'); if (!c) return 'no-checkbox'; if (c.checked) { c.click(); return 'unchecked'; } return 'already-unchecked'; })()" },
    { op: "wait", ms: 1800 },
    { op: "eval", js: VIS_MEASURE },
    { op: "screenshot", name: "e-visibility-unchecked" },
    { op: "eval", js: "(function(){ var c = document.querySelector('[data-im=\"check\"]'); if (!c) return 'no-checkbox'; if (!c.checked) { c.click(); return 'checked'; } return 'already-checked'; })()" },
    { op: "wait", ms: 1800 },
    { op: "eval", js: VIS_MEASURE },
    { op: "eval", js: "(function(){ window.__eSub = null; var of = window.fetch; window.fetch = function () { var args = arguments; return of.apply(this, args).then(function (r) { if (String(args[0]).indexOf('/submissions') >= 0) { r.clone().json().then(function (j) { window.__eSub = j; }).catch(function () {}); } return r; }); }; return true; })()" },
    { op: "eval", js: "(function(){ return window.__e.click('[data-im=\"submit\"]'); })()" },
    { op: "wait", ms: 5000 },
    { op: "eval", js: VIS_MEASURE },
    { op: "screenshot", name: "e-visibility-after-submit" },
  ];
}

const SCENARIOS = {
  layout: { steps: scenarioLayout, note: "布局：3 尺寸 × 暗/亮" },
  connect: { steps: scenarioConnect, note: "连接点是否被局部工具栏盖住（每尺寸重复 3 次）" },
  aesthetics: { steps: scenarioAesthetics, note: "文字层级 / 对比度 / 动效 / 开发用语" },
  group: { steps: scenarioGroup, note: "重叠成组与组名" },
  batch: { steps: scenarioBatch, note: "批量列表资格与剩余数" },
  chat: { steps: scenarioChat, note: "聊天草稿持久化与请求独立" },
  "chat-fail": { steps: scenarioChatFail, note: "聊天受理失败 / 存储失败（模拟）" },
  "card-draft": { steps: scenarioCardDraft, note: "卡片草稿保存失败与重试（模拟）" },
  pure: { steps: scenarioPure, note: "纯函数级反例（默认组名 / 按秒归批 / 剩余数判资格）" },
  visibility: { steps: scenarioVisibility, note: "未勾选注释的可见范围与提交后的勾选" },
};

const wanted = process.argv[2] || "layout";
const names = wanted === "all" ? Object.keys(SCENARIOS) : wanted.split(",");
names.forEach(function (name) {
  const scenario = SCENARIOS[name];
  if (!scenario) {
    console.log("未知场景：" + name + "；可选：" + Object.keys(SCENARIOS).join(", "));
    process.exit(2);
  }
  console.log("\n===== 场景 " + name + "：" + scenario.note + " =====");
  const out = runProbe(name, scenario.steps());
  console.log("步骤文件：" + out.file);
  (out.parsed.results || []).forEach(function (item) {
    if (item.op === "eval") {
      const value = typeof item.value === "string" ? item.value : JSON.stringify(item.value);
      console.log("[eval]" + (item.error ? " ERROR:" + item.error : "") + " " + String(value).slice(0, 4000));
    } else {
      console.log("[" + item.op + "] " + JSON.stringify(item).slice(0, 500));
    }
  });
  console.log("errors=" + JSON.stringify(out.parsed.errors || []));
  console.log("httpFails=" + JSON.stringify(out.parsed.httpFails || []));
  if (out.stderr) console.log("probe-stderr=" + out.stderr.slice(0, 800));
});