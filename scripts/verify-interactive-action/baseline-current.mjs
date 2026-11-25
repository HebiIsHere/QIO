// QIO 互动模式底部操作区 · 改动前（v0.3.4 / q46-r9 同 commit）当前实现基线
//
// 目的：在隔离实例上用真实界面入口进入 /#/interactive，创建 2 张文字卡，
// 记录「改动前」收起/展开与 800x600 窄窗的截图与几何，并把 PLAN.md §3.2
// 组件 DOM 契约的「已存在 / 缺失」列成缺口清单，供阶段 2 对照。
//
// 跑法（由 run.ps1 起服务后调用）：
//   node scripts/verify-interactive-action/baseline-current.mjs
// 环境变量：QIO_IA_BASE / QIO_IA_API / QIO_IA_PROVIDER / QIO_IA_OUT
// 产物：<out>/baseline-current.json + <out>/shots/baseline-*.png
//
// 纪律：只读真实 UI 状态；不修改产品代码；本脚本只证明 harness 可用与「改动前」长什么样。
import { mkdirSync, writeFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { CdpBrowser, Page, sleep } from "../verify-pm-qio032/cdp.mjs";

const HERE = dirname(fileURLToPath(import.meta.url));
const ROOT = resolve(process.env.QIO_IA_ROOT || join(HERE, "..", ".."));
const BASE = process.env.QIO_IA_BASE || "http://127.0.0.1:5199";
const API = process.env.QIO_IA_API || "http://127.0.0.1:8734";
const OUT = resolve(process.env.QIO_IA_OUT || join(ROOT, "_lead-logs", "interactive-action", "baseline"));
const SHOTS = join(OUT, "shots");
const CDP_PORT = Number(process.env.QIO_CDP_PORT || "9333");
mkdirSync(SHOTS, { recursive: true });

const RUN_ID = new Date().toISOString().replace(/[-:T.]/g, "").slice(0, 14);
const results = [];
let page = null;
let stepNo = 0;

function record(name, ok, detail) {
  const status = ok === null ? "UNVERIFIED" : ok ? "PASS" : "FAIL";
  results.push({ name: name, status: status, detail: detail === undefined ? null : detail });
  console.log("[" + status + "] " + name + (detail === undefined ? "" : " :: " + JSON.stringify(detail).slice(0, 900)));
}
async function shot(name) {
  stepNo += 1;
  const file = join(SHOTS, String(stepNo).padStart(2, "0") + "-" + name + ".png");
  await page.screenshot(file);
  return file;
}

/** 页面几何快照（自包含函数，toString 注入页面）。 */
function collectSnapshot() {
  function rect(el) {
    if (!el) return null;
    var r = el.getBoundingClientRect();
    var round = function (v) { return Math.round(v * 10) / 10; };
    return { x: round(r.x), y: round(r.y), width: round(r.width), height: round(r.height), right: round(r.right), bottom: round(r.bottom), cx: round(r.x + r.width / 2), cy: round(r.y + r.height / 2) };
  }
  function attr(el, name) { return el ? el.getAttribute(name) : null; }
  function picked(el, names) { var out = {}; names.forEach(function (n) { out[n] = attr(el, n); }); return out; }
  var root = document.querySelector('[data-ic="conversation"]');
  var style = root ? getComputedStyle(root) : null;
  var cssVars = {};
  ["--ic-panel-h", "--ic-stream-h", "--ic-frame-width", "--ic-frame-height", "--ic-reveal", "--ic-glass-opacity", "--ic-glass-inset", "--ic-fade-top", "--ic-fade-end"].forEach(function (name) {
    cssVars[name] = style ? style.getPropertyValue(name).trim() : null;
  });
  var requiredSelectors = [
    '[data-ic="conversation"]',
    '[data-ic-frame]',
    '[data-ic-frame-path="left"]',
    '[data-ic-frame-path="right"]',
    '[data-ic-glass]',
    '[data-ic-stream]',
    '[data-ic-rail]',
    '[data-ic-toggle]',
    '[data-ic-entries]',
    '[data-ic-composer]'
  ];
  var presence = {};
  requiredSelectors.forEach(function (sel) { presence[sel] = document.querySelectorAll(sel).length; });
  var toggle = document.querySelector('[data-ic-toggle]');
  var toggleRect = toggle ? toggle.getBoundingClientRect() : null;
  var toggleHit = null;
  if (toggleRect && toggleRect.width > 0) {
    var hit = document.elementFromPoint(toggleRect.x + toggleRect.width / 2, toggleRect.y + toggleRect.height / 2);
    toggleHit = !!(hit && (hit === toggle || toggle.contains(hit)));
  }
  var pathDash = {};
  ["left", "right"].forEach(function (side) {
    var path = document.querySelector('[data-ic-frame-path="' + side + '"]');
    if (!path) { pathDash[side] = null; return; }
    var total = null;
    try { total = Math.round(path.getTotalLength() * 100) / 100; } catch (error) { total = null; }
    pathDash[side] = { dash: path.style.strokeDasharray, totalLength: total, d: path.getAttribute("d") };
  });
  var bar = document.querySelector('[data-ic-bar]');
  var entries = document.querySelector('[data-ic-entries]') || document.querySelector(".ic-entries");
  var toolbar = document.querySelector('[data-ic-toolbar]');
  var composer = document.querySelector('[data-ic-composer]') || document.querySelector(".ic-composer");
  var stream = document.querySelector('[data-ic-stream]');
  var resize = document.querySelector('[data-ic-resize]');
  var rail = document.querySelector('[data-ic-rail]');
  var glass = document.querySelector('[data-ic-glass]');
  return {
    innerWidth: window.innerWidth,
    innerHeight: window.innerHeight,
    dpr: window.devicePixelRatio,
    theme: document.documentElement.getAttribute("data-theme"),
    bodyBg: getComputedStyle(document.body).backgroundColor,
    interactiveRoot: rect(document.querySelector(".interactive")),
    floatingChat: rect(document.querySelector(".im-floating-chat")),
    composerInput: rect(document.querySelector("#composer-input") || document.querySelector(".composer textarea")),
    composerRow: rect(document.querySelector(".ic-composer-row")),
    conversation: rect(root),
    conversationClass: root ? root.className : null,
    attrs: root ? picked(root, ["data-ic", "data-ic-expanded", "data-ic-layout", "data-ic-dragging", "data-ic-reveal", "data-ic-frame-path-length", "data-ic-frame-horizontal", "data-ic-corner-reveal"]) : null,
    cssVars: cssVars,
    presence: presence,
    pathDash: pathDash,
    toggle: toggle
      ? {
          rect: rect(toggle),
          role: attr(toggle, "role"),
          ariaExpanded: attr(toggle, "aria-expanded"),
          ariaControls: attr(toggle, "aria-controls"),
          ariaLabel: attr(toggle, "aria-label"),
          dataIcResize: attr(toggle, "data-ic-resize"),
          hitSelf: toggleHit,
          text: (toggle.innerText || "").replace(/\s+/g, " ").trim().slice(0, 40),
        }
      : null,
    bar: rect(bar),
    rail: rect(rail),
    glass: rect(glass),
    entries: rect(entries),
    toolbar: rect(toolbar),
    composer: rect(composer),
    stream: rect(stream),
    streamDisplay: stream ? getComputedStyle(stream).display : null,
    resize: rect(resize),
    barText: bar ? (bar.innerText || "").replace(/\s+/g, " ").trim().slice(0, 160) : null,
    cardCount: document.querySelectorAll('[data-im="card"]').length,
    slotModeSwitchCount: document.querySelectorAll("#mode-switch").length,
    slotToolbarCount: document.querySelectorAll("#toolbar").length,
    toolbarButtons: document.querySelectorAll('[data-ic-toolbar] button, #toolbar button').length,
    rootHeightCss: style ? style.height : null,
    interactivePage: !!document.querySelector(".interactive"),
  };
}

async function waitAppReady(timeout) {
  await page.waitFor("document.querySelector('.stream') && document.querySelector('#composer-mode-switch .mode-btn')", { timeout: timeout || 45000, label: "对话页就绪" });
  await sleep(600);
}

async function enterInteractive() {
  const before = await page.attr("#composer-mode-switch .mode-btn", "data-current-mode");
  await page.click("#composer-mode-switch .mode-btn");
  await page.waitFor("document.querySelector('[data-im=\"board\"]')", { timeout: 30000, label: "互动板面" });
  await sleep(1200);
  const after = await page.attr("#composer-mode-switch .mode-btn", "data-current-mode");
  const hash = await page.eval("location.hash");
  return { before: before, after: after, hash: hash };
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
  await sleep(450);
  const cls = await page.attr('[data-im="card"]', "class", index);
  return { selected: /selected/.test(cls || ""), cls: cls };
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
  if (await page.exists('[data-im="card-editor"]')) {
    await clickToolbar("card-icon-edit").catch(() => null);
    await sleep(500);
  }
  const body = await page.text('[data-im="card"] [data-im="card-body"]', index);
  const editing = await page.exists('[data-im="card-editor"]');
  return { ok: body.indexOf(text.slice(0, 12)) >= 0 && !editing, body: body.slice(0, 120), stillEditing: editing };
}

async function snapshot() { return page.eval("(" + collectSnapshot.toString() + ")()"); }

async function setExpandedState(want) {
  const cur = await page.attr('[data-ic="conversation"]', "data-ic-expanded");
  if (String(cur) === String(want)) return { changed: false, current: cur };
  await page.click('[data-ic-toggle]');
  await sleep(800);
  const after = await page.attr('[data-ic="conversation"]', "data-ic-expanded");
  return { changed: true, before: cur, after: after };
}

const REQUIRED_CONTRACT = {
  "root [data-ic=\"conversation\"]": '[data-ic="conversation"]',
  "[data-ic-frame] SVG": "[data-ic-frame]",
  "[data-ic-frame-path=left]": '[data-ic-frame-path="left"]',
  "[data-ic-frame-path=right]": '[data-ic-frame-path="right"]',
  "[data-ic-glass]": "[data-ic-glass]",
  "[data-ic-stream]": "[data-ic-stream]",
  "[data-ic-rail] 操作行": "[data-ic-rail]",
  "[data-ic-toggle] 把手": "[data-ic-toggle]",
  "[data-ic-entries]": "[data-ic-entries]",
  "[data-ic-composer]": "[data-ic-composer]",
  "槽位 #mode-switch": "#mode-switch",
  "槽位 #toolbar": "#toolbar",
};
const REQUIRED_ROOT_ATTRS = ["data-ic-expanded", "data-ic-layout", "data-ic-dragging", "data-ic-reveal", "data-ic-frame-path-length", "data-ic-frame-horizontal", "data-ic-corner-reveal"];
const REQUIRED_CSS_VARS = ["--ic-panel-h", "--ic-stream-h", "--ic-frame-width", "--ic-frame-height", "--ic-reveal", "--ic-glass-opacity", "--ic-glass-inset"];

function contractGap(snap) {
  const present = {};
  const missing = [];
  Object.keys(REQUIRED_CONTRACT).forEach(function (label) {
    const count = snap.presence[REQUIRED_CONTRACT[label]];
    present[label] = count;
    if (!count) missing.push(label);
  });
  const attrsMissing = REQUIRED_ROOT_ATTRS.filter(function (name) { return !snap.attrs || snap.attrs[name] === null; });
  const cssMissing = REQUIRED_CSS_VARS.filter(function (name) { return !snap.cssVars || !snap.cssVars[name]; });
  const toggleMissing = [];
  if (snap.toggle) {
    if (snap.toggle.role !== "button") toggleMissing.push("role=button");
    if (snap.toggle.dataIcResize === null) toggleMissing.push("data-ic-resize");
    if (snap.toggle.ariaExpanded === null) toggleMissing.push("aria-expanded");
    if (snap.toggle.ariaControls === null) toggleMissing.push("aria-controls");
    if (snap.toggle.ariaLabel === null) toggleMissing.push("aria-label");
  } else {
    toggleMissing.push("toggle 缺失");
  }
  return {
    selectorsPresent: present,
    selectorsMissing: missing,
    rootAttrsMissing: attrsMissing,
    cssVarsMissing: cssMissing,
    toggleAttrsMissing: toggleMissing,
    complete: missing.length === 0 && attrsMissing.length === 0 && toggleMissing.length === 0,
  };
}

async function main() {
  const startedAt = new Date().toISOString();
  let browser = null;
  try {
    browser = await CdpBrowser.launch({ port: CDP_PORT });
    page = await Page.create(browser);
    await page.setViewport(1000, 600);
    console.log("== 改动前基线 == base=" + BASE + " api=" + API + " runId=" + RUN_ID + " root=" + ROOT);

    await fetch(API + "/api/onboarding/seen", { method: "POST" }).catch(() => null);
    await fetch(API + "/api/onboarding/complete", { method: "POST" }).catch(() => null);
    await page.goto(BASE + "/");
    let ready = true;
    try {
      await waitAppReady();
    } catch (error) {
      ready = false;
      record("环境：应用起来（对话页就绪）", false, String(error).slice(0, 400));
    }
    if (ready) record("环境：应用起来（对话页就绪）", true, { url: BASE, theme: await page.eval("document.documentElement.getAttribute('data-theme')") });

    const entry = await enterInteractive();
    record("正常入口：对话页模式切换 → 互动板面", entry.after === "interactive" && String(entry.hash).indexOf("/interactive") >= 0, entry);
    record("互动页出现底部对话组件 [data-ic=\"conversation\"]", (await page.count('[data-ic="conversation"]')) === 1, { count: await page.count('[data-ic="conversation"]'), hash: await page.eval("location.hash") });

    // 2 张文字卡（真实界面入口：添加 → 文字卡 → 选中编辑）
    const add1 = await openAddMenu("text");
    const edit1 = await setTextCardContent(add1.after - 1, "IA-BASE-" + RUN_ID + " 第一张文字卡");
    const add2 = await openAddMenu("text");
    const edit2 = await setTextCardContent(add2.after - 1, "IA-BASE-" + RUN_ID + " 第二张文字卡");
    record("通过真实界面创建 2 张文字卡并写入正文", add1.after === add1.before + 1 && add2.after === add2.before + 1 && edit1.ok && edit2.ok, {
      cards: await cardCount(), edit1: edit1, edit2: edit2,
    });

    await page.setViewport(1000, 600);
    await sleep(700);
    const collapse1 = await setExpandedState("false");
    await sleep(600);
    const collapsed1000 = await snapshot();
    const shotCollapsed1000 = await shot("1000x600-collapsed");
    record("1000×600 收起态：截图 + 几何采集", collapsed1000 && collapsed1000.attrs && collapsed1000.attrs["data-ic-expanded"] === "false", {
      shot: shotCollapsed1000,
      conversation: collapsed1000.conversation,
      bar: collapsed1000.bar,
      composer: collapsed1000.composer,
      toggle: collapsed1000.toggle,
      stateChange: collapse1,
    });

    const expand1 = await setExpandedState("true");
    await sleep(900);
    const expanded1000 = await snapshot();
    const shotExpanded1000 = await shot("1000x600-expanded");
    record("1000×600 点击把手展开（改动前旧实现）", expanded1000 && expanded1000.attrs && expanded1000.attrs["data-ic-expanded"] === "true", {
      shot: shotExpanded1000,
      stateChange: expand1,
      conversation: expanded1000.conversation,
      stream: expanded1000.stream,
      streamDisplay: expanded1000.streamDisplay,
      composer: expanded1000.composer,
      cssPanelH: expanded1000.cssVars["--ic-panel-h"],
      rootHeightCss: expanded1000.rootHeightCss,
    });

    // 窄窗 800x600：展开 + 收起
    await page.setViewport(800, 600);
    await sleep(800);
    const expanded800 = await snapshot();
    const shotExpanded800 = await shot("800x600-expanded");
    const collapse800 = await setExpandedState("false");
    await sleep(600);
    const collapsed800 = await snapshot();
    const shotCollapsed800 = await shot("800x600-collapsed");
    record("800×600 窄窗展开/收起截图 + 几何", expanded800.conversation !== null && collapsed800.attrs["data-ic-expanded"] === "false", {
      shots: [shotExpanded800, shotCollapsed800],
      expanded: { conversation: expanded800.conversation, composer: expanded800.composer, bar: expanded800.bar },
      collapsed: { conversation: collapsed800.conversation, composer: collapsed800.composer, bar: collapsed800.bar },
      stateChange: collapse800,
    });

    // PLAN §1.2/§3.1 冻结的参考几何（假设 1000 视口操作区 976、把手 112、输入 230/56）
    // vs v0.3.4 真实 DOM 实测：这是校准输入是否成立的证据，不伪造一致。
    const plannedGeometry = { panelWidth: 976, panelHeight: 294, leverWidth: 112, leverTop: 0, leverHeight: 32, inputTop: 230, inputHeight: 56, edge: 0.75, corner: 14 };
    const measuredGeometry = {
      panelWidth: expanded1000.conversation.width,
      panelHeight: expanded1000.conversation.height,
      streamHeight: expanded1000.stream ? expanded1000.stream.height : null,
      composerHeight: expanded1000.composer.height,
      composerTopRelPanel: Math.round((expanded1000.composer.y - expanded1000.conversation.y) * 10) / 10,
      composerMidlineRelPanel: Math.round((expanded1000.composer.y + expanded1000.composer.height / 2 - expanded1000.conversation.y) * 10) / 10,
      toggleWidth: expanded1000.toggle.rect.width,
      toggleHeight: expanded1000.toggle.rect.height,
      floatingChatWidth: expanded1000.floatingChat ? expanded1000.floatingChat.width : null,
      interactiveRootWidth: expanded1000.interactiveRoot ? expanded1000.interactiveRoot.width : null,
    };
    record("PLAN §1.2/§3.1 参考几何 vs v0.3.4 实测几何（校准输入偏差，如实记录）", true, {
      plannedGeometry: plannedGeometry,
      measuredGeometry: measuredGeometry,
      deltas: {
        panelWidth: Math.round((measuredGeometry.panelWidth - plannedGeometry.panelWidth) * 10) / 10,
        composerMidline: Math.round((measuredGeometry.composerMidlineRelPanel - (plannedGeometry.inputTop + plannedGeometry.inputHeight / 2)) * 10) / 10,
        composerHeight: Math.round((measuredGeometry.composerHeight - plannedGeometry.inputHeight) * 10) / 10,
      },
      note: "PLAN 的 976/112/0/32/230/56 是「计划几何」；v0.3.4 实测 .ic 宽 902、输入高 98、输入中线相对面板 231。若新实现沿用同一浮层宽度，216–220ms 到角需要用真实几何重新校准。",
    });

    // 契约缺口（改动前，预期全部缺失；这是阶段 2 的对照清单）
    const gapExpanded = contractGap(expanded1000);
    const gapCollapsed = contractGap(collapsed1000);
    const oldImpl = {
      hasFrameSvg: (collapsed1000.presence["[data-ic-frame]"] || 0) > 0,
      hasRevealAttr: collapsed1000.attrs ? collapsed1000.attrs["data-ic-reveal"] !== null : false,
      hasGlass: (collapsed1000.presence["[data-ic-glass]"] || 0) > 0,
      hasRailHook: (collapsed1000.presence["[data-ic-rail]"] || 0) > 0,
      toggleHasResizeAttr: collapsed1000.toggle ? collapsed1000.toggle.dataIcResize !== null : false,
      separateResizeHandle: (collapsed1000.presence["[data-ic-resize]"] || 0) > 0,
    };
    record("改动前事实：当前实现无逐帧揭示/线框 SVG（无 data-ic-frame / data-ic-reveal）", oldImpl.hasFrameSvg === false && oldImpl.hasRevealAttr === false, oldImpl);
    record("契约 §3.2 缺口清单（改动前预期缺失；阶段 2 逐项复验）", true, {
      collapsed: { missing: gapCollapsed.selectorsMissing, attrsMissing: gapCollapsed.rootAttrsMissing, cssMissing: gapCollapsed.cssVarsMissing, toggleMissing: gapCollapsed.toggleAttrsMissing },
      expanded: { missing: gapExpanded.selectorsMissing, attrsMissing: gapExpanded.rootAttrsMissing, cssMissing: gapExpanded.cssVarsMissing, toggleMissing: gapExpanded.toggleAttrsMissing },
      completeBefore: gapCollapsed.complete || gapExpanded.complete,
    });

    const consoleErrors = page.consoleErrors.slice();
    record("取证过程无页面 console error / 未捕获异常", consoleErrors.length === 0, consoleErrors.slice(0, 8));

    const output = {
      meta: {
        generatedAt: startedAt,
        finishedAt: new Date().toISOString(),
        runId: RUN_ID,
        base: BASE,
        api: API,
        root: ROOT,
        gitHead: process.env.QIO_IA_GIT_HEAD || null,
        engine: browser.version && browser.version.Browser,
        viewport: { width: 1000, height: 600 },
        narrowViewport: { width: 800, height: 600 },
        note: "改动前基线：q46-r9 工作树（与 qio-action HEAD 同一 commit v0.3.4）。当前实现只有高度展开，没有线框/揭示动画。",
      },
      checks: results,
      cards: { count: await cardCount(), texts: await page.texts('[data-im="card"] [data-im="card-body"]') },
      snapshots: {
        collapsed1000: collapsed1000,
        expanded1000: expanded1000,
        expanded800: expanded800,
        collapsed800: collapsed800,
      },
      contractGap: { collapsed: gapCollapsed, expanded: gapExpanded, oldImplementationFacts: oldImpl },
      calibrationReference: { plannedGeometry: plannedGeometry, measuredGeometry: measuredGeometry },
      consoleErrors: consoleErrors,
      shots: results.filter(function (r) { return r.detail && (r.detail.shot || r.detail.shots); }),
    };
    const outFile = join(OUT, "baseline-current.json");
    writeFileSync(outFile, JSON.stringify(output, null, 2));
    console.log("已写 " + outFile + "；截图 " + SHOTS);
    const failed = results.filter(function (r) { return r.status === "FAIL"; });
    await browser.close();
    browser = null;
    process.exit(failed.length ? 1 : 0);
  } catch (error) {
    console.error("改动前基线采集失败：" + (error && error.stack ? error.stack : error));
    if (page) { try { await shot("failed"); } catch (e) { /* ignore */ } }
    if (browser) await browser.close().catch(() => null);
    process.exit(1);
  }
}

main();