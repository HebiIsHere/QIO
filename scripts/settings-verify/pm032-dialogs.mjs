/**
 * pm032-dialogs —— 设置页窗口级弹窗验收（#82 / #87 / #89 的现场证据）
 *
 * 解决什么问题：设置页里的「清除 / 撤销 / 删除 / 立即整理」确认框与「添加 Key」表单
 * 属于窗口级浮层，必须由证据而不是肉眼判断它们在 800x600 里是否被顶部全局栏
 * （WindowChrome，z-index 240）盖住、按钮是否等宽同高居中、危险色是否真的生效。
 *
 * 做法：
 * - 隔离环境（serve.mjs 起的真实后端 + 浏览器侧只读 fixture）里用 CDP 打开设置页；
 * - 分别打开：模型与服务 → Key 管理的「撤销密钥」确认层（#82/#87）、
 *   记忆与整理 →「立即整理」确认层（#82）、联网搜索 →「清除」确认层（#87）、
 *   「添加 Key」QModal（#89）；
 * - 逐项取 getBoundingClientRect / elementFromPoint / computed style，原始数值写进
 *   JSON 报告；同时按 1440x900 与 800x600 x 亮/暗主题各截图；
 * - 额外键盘交互：Esc 关闭、Tab 不跑出弹窗、关闭后焦点回到触发元素、显式确认才发请求。
 *
 * 只读/不联网：所有 GET 数据来自 lib/fixtures.mjs 的浏览器侧替身；写请求（点确认）
 * 打到隔离后端的数据目录，不碰真实 Key、不联网。
 *
 * 用法（先起环境）：
 *   node scripts/settings-verify/serve.mjs up --label dlg --worktree <worktree> --app-port 5108 --backend-port 8758
 *   node scripts/settings-verify/pm032-dialogs.mjs --url http://127.0.0.1:5108 --api http://127.0.0.1:8758 --cdp-port 9818 --out D:/qio-dev/_lead-logs/pm032-settings/dialogs
 *   node scripts/settings-verify/serve.mjs down --label dlg
 *
 * 可选：--report-label <名字>  --themes dark,light  --viewports 1440x900,800x600  --allow-fail
 */
import { mkdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { VerifyBrowser } from "./lib/browser.mjs";
import { installFixtures } from "./lib/fixtures.mjs";
import { clickCategory, installHelpers, openSettings, waitModelsPanel } from "./lib/probe.mjs";
import { SETTINGS_CATEGORIES } from "./lib/env.mjs";

const args = process.argv.slice(2);
const flag = (name, fallback) => {
  const index = args.indexOf("--" + name);
  if (index < 0) return fallback;
  const value = args[index + 1];
  return value === undefined || value.startsWith("--") ? true : value;
};
const sleep = (ms) => new Promise((done) => setTimeout(done, ms));

const appUrl = String(flag("url", "http://127.0.0.1:5108")).replace(/\/$/, "");
const apiBase = String(flag("api", process.env.QIO_SETTINGS_API || "http://127.0.0.1:8758")).replace(/\/$/, "");
const cdpPort = Number(flag("cdp-port", 9818));
const label = String(flag("report-label", "pm032-dialogs"));
const allowFail = !!flag("allow-fail", false);
const outDir = String(flag("out", join(process.env.TEMP || ".", "qio-pm032-dialogs")));
const parseList = (value, fallback) => {
  const list = String(value == null ? "" : value).split(",").map((item) => item.trim()).filter(Boolean);
  return list.length ? list : fallback;
};
const viewportList = parseList(flag("viewports", ""), ["1440x900", "800x600"]).map((text) => {
  const parts = text.split("x");
  return { width: Number(parts[0]), height: Number(parts[1]), tag: text };
});
const themeList = parseList(flag("themes", ""), ["dark", "light"]);

mkdirSync(outDir, { recursive: true });

const run = {
  label,
  startedAt: new Date().toISOString(),
  appUrl,
  apiBase,
  viewports: viewportList.map((vp) => vp.tag),
  themes: themeList,
  combos: [],
  checks: [],
  screenshots: [],
  raw: {},
  notes: [
    "读取类接口（凭据 / 模型目录 / 联网搜索配置与通道状态 / 审批历史）是浏览器侧 fixture，不含真实 Key 与真实审批内容；",
    "点「确认」时的写请求打到 serve.mjs 起的隔离后端（%TEMP% 数据目录），不联网、不碰真实数据。",
    "遮罩判据按内容视口：遮罩 inset = 既有 --window-frame-inset（28px），0.3.2 窗口外框要求 28px 透明留白带不被页面覆盖；" +
      "因此 scrim 原始值 = (28, 28, vw-56, vh-56) 是设计值，判据看相对内容视口的四个 delta（≤2px），原始值全部写进报告。",
  ],
};

function record(scope, name, ok, detail) {
  run.checks.push({ scope, name, ok: !!ok, detail: detail === undefined ? null : detail });
  console.log((ok ? "  [PASS] " : "  [FAIL] ") + scope + " / " + name + (ok ? "" : " :: " + JSON.stringify(detail).slice(0, 500)));
  return ok;
}

async function shot(browser, name) {
  const file = join(outDir, name + ".png");
  await browser.shotFile(file, null, true);
  run.screenshots.push(file);
  return file;
}

/* ------------------------------------------------------------------ *
 * 页面内测量助手：整个函数会 toString() 后在页面里执行（不经过字符串模板），
 * 因此这里的代码只能依赖浏览器 API，不能引用 Node 侧变量。
 * ------------------------------------------------------------------ */
function pageHelpersFactory() {
  var px = function (value) { var n = parseFloat(value); return Number.isFinite(n) ? n : 0; };
  var norm = function (value) { return String(value == null ? "" : value).replace(/[\s\u3000]+/g, ""); };
  var rect = function (el) {
    if (!el) return null;
    var r = el.getBoundingClientRect();
    var round = function (n) { return Math.round(n * 100) / 100; };
    return { x: round(r.x), y: round(r.y), width: round(r.width), height: round(r.height), left: round(r.left), top: round(r.top), right: round(r.right), bottom: round(r.bottom) };
  };
  var desc = function (el) {
    if (!el) return null;
    var cls = typeof el.className === "string" ? el.className : (el.getAttribute("class") || "");
    return { tag: el.tagName.toLowerCase(), cls: cls.split(/\s+/).filter(Boolean).slice(0, 6).join("."), text: norm(el.innerText || el.textContent || "").slice(0, 32), id: el.id || "", aria: el.getAttribute("aria-label") || "" };
  };
  var visible = function (el) {
    if (!el) return false;
    var r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0 && getComputedStyle(el).visibility !== "hidden";
  };
  var colorProbe = function (name) {
    var probe = document.createElement("span");
    probe.style.color = "var(" + name + ")";
    document.body.appendChild(probe);
    var value = getComputedStyle(probe).color;
    probe.remove();
    return value;
  };
  var css = function (el, prop) { return el ? getComputedStyle(el).getPropertyValue(prop).trim() : null; };
  var center = function (r) { return { x: r.x + r.width / 2, y: r.y + r.height / 2 }; };
  var intersection = function (a, b) {
    if (!a || !b) return 0;
    var w = Math.min(a.right, b.right) - Math.max(a.left, b.left);
    var h = Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top);
    return w > 0 && h > 0 ? Math.round(w * h * 100) / 100 : 0;
  };
  var panelFor = function (kind) { return kind === "confirm" ? document.querySelector(".qio-confirm--layer") : document.querySelector(".qio-modal"); };
  var scrimFor = function (kind) { return kind === "confirm" ? document.querySelector(".qio-confirm-scrim") : document.querySelector(".qio-modal-scrim"); };
  var inPanel = function (panel, el) { return !!(panel && el && panel.contains(el)); };
  var hit = function (panel, scrim, target) {
    if (!target) return { found: false };
    var r = rect(target);
    var c = center(r);
    var el = document.elementFromPoint(c.x, c.y);
    return { found: true, element: desc(el), inPanel: inPanel(panel, el), inScrim: inPanel(scrim, el) };
  };
  var viewport = { width: window.innerWidth, height: window.innerHeight };
  var rootStyle = getComputedStyle(document.documentElement);
  var inset = px(rootStyle.getPropertyValue("--window-frame-inset"));
  var content = { left: inset, top: inset, right: viewport.width - inset, bottom: viewport.height - inset, width: viewport.width - 2 * inset, height: viewport.height - 2 * inset };
  var chromeEl = document.querySelector(".window-chrome");
  var chrome = chromeEl ? { rect: rect(chromeEl), position: css(chromeEl, "position"), zIndex: css(chromeEl, "z-index") } : null;
  var inside = function (child, parent, tolerance) {
    var tol = tolerance == null ? 0.75 : tolerance;
    if (!child || !parent) return false;
    return child.left >= parent.left - tol && child.top >= parent.top - tol && child.right <= parent.right + tol && child.bottom <= parent.bottom + tol;
  };
  var centerOf = function (r) { return { x: Math.round((r.left + r.right) / 2 * 100) / 100, y: Math.round((r.top + r.bottom) / 2 * 100) / 100 }; };

  function measureLayer(kind) {
    var panel = panelFor(kind);
    var scrim = scrimFor(kind);
    if (!panel) return { found: false, kind: kind };
    var titleSel = kind === "confirm" ? ".qio-confirm__title" : ".qio-modal-title";
    var closeSel = kind === "confirm" ? "[data-qio-confirm-close]" : ".qio-modal-head__slot--end button";
    var actionsSel = kind === "confirm" ? ".qio-confirm__actions" : ".qio-modal-actions";
    var titleEl = panel.querySelector(titleSel);
    var closeEl = document.querySelector(closeSel);
    var actionsEl = panel.querySelector(actionsSel);
    var buttons = actionsEl ? Array.prototype.slice.call(actionsEl.querySelectorAll("button")) : [];
    var panelRect = rect(panel);
    var scrimRect = rect(scrim);
    var titleRect = rect(titleEl);
    var closeRect = rect(closeEl);
    var actionsRect = rect(actionsEl);
    var confirmBtn = kind === "confirm" ? panel.querySelector('[data-qio-confirm-action="confirm"]') : (buttons.length ? buttons[buttons.length - 1] : null);
    var cancelBtn = kind === "confirm" ? panel.querySelector('[data-qio-confirm-action="cancel"]') : (buttons.length ? buttons[0] : null);
    var confirmRect = rect(confirmBtn);
    var cancelRect = rect(cancelBtn);
    var padding = {
      left: px(css(panel, "padding-left")), right: px(css(panel, "padding-right")),
      top: px(css(panel, "padding-top")), bottom: px(css(panel, "padding-bottom")),
    };
    var panelContent = panelRect ? {
      left: panelRect.left + padding.left, right: panelRect.right - padding.right,
      top: panelRect.top + padding.top, bottom: panelRect.bottom - padding.bottom,
    } : null;
    var group = null;
    if (actionsEl) {
      var list = (kind === "confirm" ? [cancelBtn, confirmBtn] : buttons).filter(Boolean);
      var rects = list.map(rect);
      if (rects.length) {
        var left = Math.min.apply(null, rects.map(function (r) { return r.left; }));
        var right = Math.max.apply(null, rects.map(function (r) { return r.right; }));
        var top = Math.min.apply(null, rects.map(function (r) { return r.top; }));
        var bottom = Math.max.apply(null, rects.map(function (r) { return r.bottom; }));
        group = { left: left, right: right, top: top, bottom: bottom, width: Math.round((right - left) * 100) / 100, height: Math.round((bottom - top) * 100) / 100 };
      }
    }
    return {
      found: true,
      kind: kind,
      panelTag: desc(panel),
      scrimParentIsBody: !!(scrim && scrim.parentElement === document.body),
      panelParentIsBody: !!(panel.parentElement && panel.parentElement === document.body),
      scrimPosition: scrim ? css(scrim, "position") : null,
      scrimZIndex: scrim ? css(scrim, "z-index") : null,
      panelZIndex: css(panel, "z-index"),
      scrimRect: scrimRect,
      panelRect: panelRect,
      panelScroll: panel ? { scrollHeight: panel.scrollHeight, clientHeight: panel.clientHeight, scrollWidth: panel.scrollWidth, clientWidth: panel.clientWidth } : null,
      chrome: chrome,
      content: content,
      viewport: viewport,
      title: titleRect ? { rect: titleRect, hit: hit(panel, scrim, titleEl), chromeOverlap: intersection(titleRect, chrome && chrome.rect), inContent: inside(titleRect, content) } : null,
      close: closeRect ? { rect: closeRect, hit: hit(panel, scrim, closeEl), chromeOverlap: intersection(closeRect, chrome && chrome.rect), inContent: inside(closeRect, content) } : null,
      actions: actionsRect ? { rect: actionsRect, chromeOverlap: intersection(actionsRect, chrome && chrome.rect), inContent: inside(actionsRect, content) } : null,
      confirm: confirmRect ? { rect: confirmRect, hit: hit(panel, scrim, confirmBtn), chromeOverlap: intersection(confirmRect, chrome && chrome.rect), inContent: inside(confirmRect, content), bg: css(confirmBtn, "background-color"), color: css(confirmBtn, "color"), classes: String(confirmBtn.className || "").split(/\s+/).filter(Boolean) } : null,
      cancel: cancelRect ? { rect: cancelRect, hit: hit(panel, scrim, cancelBtn), chromeOverlap: intersection(cancelRect, chrome && chrome.rect), inContent: inside(cancelRect, content), bg: css(cancelBtn, "background-color"), classes: String(cancelBtn.className || "").split(/\s+/).filter(Boolean) } : null,
      buttons: kind === "confirm" ? [cancelRect, confirmRect].filter(Boolean) : buttons.map(rect),
      group: group,
      panelContent: panelContent,
      panelCenter: panelRect ? centerOf(panelRect) : null,
      contentCenter: centerOf(content),
      tokens: { danger: colorProbe("--danger"), accent: colorProbe("--accent"), onAccent: colorProbe("--on-accent"), bgPanel: colorProbe("--bg-panel") },
      titleCenterX: titleRect ? centerOf(titleRect).x : null,
      actionsCenterX: actionsRect ? centerOf(actionsRect).x : null,
      titleText: titleEl ? norm(titleEl.innerText || titleEl.textContent || "") : null
    };
  }

  function clickText(text, scopeSelector) {
    var scope = scopeSelector ? document.querySelector(scopeSelector) : document;
    if (!scope) return { ok: false, reason: "scope-not-found", scope: scopeSelector };
    var want = norm(text);
    var nodes = Array.prototype.slice.call(scope.querySelectorAll("button, [role=button], a, summary")).filter(visible);
    var hitEl = nodes.find(function (el) { return norm(el.innerText || el.textContent || "") === want; }) ||
      nodes.find(function (el) { return norm(el.innerText || el.textContent || "").indexOf(want) >= 0; });
    if (!hitEl) return { ok: false, reason: "not-found", seen: nodes.map(function (el) { return norm(el.innerText || el.textContent || ""); }).slice(0, 40) };
    hitEl.scrollIntoView({ block: "center" });
    hitEl.click();
    return { ok: true, text: norm(hitEl.innerText || hitEl.textContent || ""), el: desc(hitEl) };
  }

  function focusInfo() {
    var el = document.activeElement;
    if (!el || el === document.body) return { has: false };
    return { has: true, el: desc(el), marked: el.getAttribute("data-pm032-trigger") === "1", inDialog: !!el.closest(".qio-confirm--layer, .qio-modal") };
  }

  function dialogOpen(kind) {
    var panel = panelFor(kind);
    return !!panel && visible(panel);
  }

  function overlaySummary() {
    return {
      confirmScrim: !!document.querySelector(".qio-confirm-scrim"),
      modalScrim: !!document.querySelector(".qio-modal-scrim"),
      topAtCenter: desc(document.elementFromPoint(innerWidth / 2, innerHeight / 2))
    };
  }

  return { measureLayer: measureLayer, clickText: clickText, focusInfo: focusInfo, dialogOpen: dialogOpen, overlaySummary: overlaySummary, desc: desc, rect: rect };
}

async function installPmHelpers(browser) {
  await browser.evalJs("window.__pm = (" + pageHelpersFactory.toString() + ")()");
  var ok = await browser.evalJs("!!window.__pm && typeof window.__pm.measureLayer === 'function'");
  if (!ok) throw new Error("页面测量助手安装失败");
}

/* 审批历史 fixture：只让非聊天页的「查看审批结果与历史」入口真实出现（只读替身，
   内容是本机构造的一条已完成记录，不含真实审批内容）。 */
const APPROVAL_HISTORY = [
  {
    approval_id: "ap-fake-1",
    kind: "tool",
    outcome: "executed",
    at: "2026-10-28T03:00:00.000Z",
    summary: "读取了一份本地文件",
    detail: { label: "读取一份本地文件", kind: "tool", ok: true },
    legacy: false,
  },
];

/* 搜索配置 fixture：默认 SEARCH_STATUS 里博查未配置，「清除」按钮不出现；
   这里补一个「已配置」的搜索设置响应，让危险动作入口真实出现（只读替身）。 */
const SEARCH_SETTINGS_CONFIGURED = {
  top_k_default: 5,
  max_fetch_chars: 15000,
  searxng_url: "",
  bocha_has_key: true,
  keyless_fallback: true,
};

async function clickCategoryById(browser, id) {
  const category = SETTINGS_CATEGORIES.find((item) => item.id === id);
  if (!category) throw new Error("未知分类：" + id);
  const result = await clickCategory(browser, category);
  if (!result || !result.ok) throw new Error("切换分类失败：" + JSON.stringify(result));
  await sleep(450);
  return result;
}

async function openSettingsPage(browser) {
  await openSettings(browser, appUrl);
  await installPmHelpers(browser);
}

/* ---------------- 各处弹窗入口 ---------------- */
async function openModelsRevokeConfirm(browser) {
  await clickCategoryById(browser, "models");
  await waitModelsPanel(browser);
  const manage = await browser.evalJs("window.__pm.clickText('管理', '.conn')");
  if (!manage || !manage.ok) return { ok: false, step: "manage-tab", detail: manage };
  await sleep(280);
  const clicked = await browser.evalJs("window.__pm.clickText('撤销密钥', '.conn')");
  if (!clicked || !clicked.ok) return { ok: false, step: "revoke-button", detail: clicked };
  await sleep(400);
  return { ok: await browser.evalJs("window.__pm.dialogOpen('confirm')"), step: "models-revoke", clicked: clicked };
}

async function openMemoryConfirm(browser) {
  await clickCategoryById(browser, "memory");
  const clicked = await browser.evalJs("window.__pm.clickText('立即整理')");
  if (!clicked || !clicked.ok) return { ok: false, step: "run-maintenance", detail: clicked };
  await sleep(400);
  return { ok: await browser.evalJs("window.__pm.dialogOpen('confirm')"), step: "memory-run", clicked: clicked };
}

async function openSearchClearConfirm(browser) {
  await clickCategoryById(browser, "models");
  const clicked = await browser.evalJs("(function () { var el = document.querySelector('[data-testid=settings-search-bocha-clear]'); if (!el) return { ok: false, reason: 'clear-button-missing' }; el.scrollIntoView({ block: 'center' }); el.click(); return { ok: true, text: el.innerText }; })()");
  if (!clicked || !clicked.ok) return { ok: false, step: "search-clear", detail: clicked };
  await sleep(400);
  return { ok: await browser.evalJs("window.__pm.dialogOpen('confirm')"), step: "search-clear", clicked: clicked };
}

async function openKeyFormModal(browser) {
  await clickCategoryById(browser, "models");
  await waitModelsPanel(browser);
  const clicked = await browser.evalJs("window.__pm.clickText('添加 Key')");
  if (!clicked || !clicked.ok) return { ok: false, step: "add-key", detail: clicked };
  await sleep(700);
  return { ok: await browser.evalJs("window.__pm.dialogOpen('modal')"), step: "key-form", clicked: clicked };
}

/* ---------------- 判定 ---------------- */
function checkLayer(scope, name, data, expect) {
  const checks = [];
  const push = (checkName, ok, detail) => checks.push({ name: checkName, ok: !!ok, detail: detail });
  if (!data || !data.found) {
    record(scope, name + "-found", false, data);
    return false;
  }
  const content = data.content;
  const chrome = data.chrome;
  const elements = [data.title, data.close, data.confirm, data.cancel].filter(Boolean);
  const chromeHits = elements.filter((item) => item.chromeOverlap > 0);
  push("title-close-actions-not-under-chrome", chromeHits.length === 0, { chrome: chrome && chrome.rect, hits: elements.map((item) => item.chromeOverlap) });
  const hitFailures = elements.filter((item) => !item.hit || !item.hit.inPanel);
  push("topmost-hit-inside-panel", hitFailures.length === 0, hitFailures.map((item) => ({ hit: item.hit, rect: item.rect })));
  const outsideContent = elements.filter((item) => !item.inContent);
  push("title-close-actions-inside-content-viewport", outsideContent.length === 0, { content: content, rects: elements.map((item) => item.rect) });
  push("scrim-teleported-to-body", data.scrimParentIsBody, { scrimParentIsBody: data.scrimParentIsBody, panelParentIsBody: data.panelParentIsBody, scrimPosition: data.scrimPosition, scrimZIndex: data.scrimZIndex });
  const scrimDelta = data.scrimRect ? {
    left: Math.round((data.scrimRect.left - content.left) * 100) / 100,
    top: Math.round((data.scrimRect.top - content.top) * 100) / 100,
    right: Math.round((data.scrimRect.right - content.right) * 100) / 100,
    bottom: Math.round((data.scrimRect.bottom - content.bottom) * 100) / 100,
    heightVsViewport: Math.round((data.scrimRect.height - data.viewport.height) * 100) / 100,
  } : null;
  const scrimMatches = data.scrimRect && Math.abs(scrimDelta.left) <= 2 && Math.abs(scrimDelta.top) <= 2 && Math.abs(scrimDelta.right) <= 2 && Math.abs(scrimDelta.bottom) <= 2;
  // 说明：遮罩按既有 --window-frame-inset 贴合「内容视口」（28px 透明留白带不遮，0.3.2 窗口外框契约），
  // 因此判据是相对内容视口而不是 0..innerHeight；原始数值一并写进报告，供对照。
  push("scrim-covers-content-viewport", scrimMatches, { scrim: data.scrimRect, content: content, viewport: data.viewport, delta: scrimDelta });
  const centerOffset = data.panelCenter ? Math.abs(data.panelCenter.x - data.contentCenter.x) : Infinity;
  push("panel-horizontally-centered", centerOffset <= 1, { centerOffset: centerOffset, panelCenter: data.panelCenter, contentCenter: data.contentCenter });
  const verticalOffset = data.panelCenter ? Math.abs(data.panelCenter.y - data.viewport.height / 2) : Infinity;
  push("panel-vertically-centered-in-window", verticalOffset <= 8, { verticalOffset: Math.round(verticalOffset * 100) / 100, panelCenterY: data.panelCenter && data.panelCenter.y, viewportHeight: data.viewport.height });
  if (data.group && data.panelContent) {
    const leftGap = data.group.left - data.panelContent.left;
    const rightGap = data.panelContent.right - data.group.right;
    push("actions-group-centered", Math.abs(leftGap - rightGap) <= 1, { leftGap: Math.round(leftGap * 100) / 100, rightGap: Math.round(rightGap * 100) / 100, diff: Math.round(Math.abs(leftGap - rightGap) * 100) / 100 });
  }
  if (name !== "keyform" && data.cancel && data.confirm) {
    const widthDiff = Math.abs(data.cancel.rect.width - data.confirm.rect.width);
    const heightDiff = Math.abs(data.cancel.rect.height - data.confirm.rect.height);
    push("cancel-confirm-equal-width", widthDiff <= 1, { cancel: data.cancel.rect.width, confirm: data.confirm.rect.width, diff: widthDiff });
    push("cancel-confirm-equal-height", heightDiff <= 1, { cancel: data.cancel.rect.height, confirm: data.confirm.rect.height, diff: heightDiff });
    if (expect && expect.danger) {
      push("danger-confirm-uses-danger-token", data.confirm.bg === data.tokens.danger, { bg: data.confirm.bg, danger: data.tokens.danger, classes: data.confirm.classes, color: data.confirm.color });
      push("danger-confirm-is-solid-not-plain", data.confirm.bg !== "rgba(0, 0, 0, 0)" && data.confirm.bg !== "transparent", { bg: data.confirm.bg });
    }
  }
  for (const check of checks) record(scope, name + "-" + check.name, check.ok, check.detail);
  return checks.every((check) => check.ok);
}

function checkKeyForm(scope, data) {
  if (!data || !data.found) {
    record(scope, "keyform-found", false, data);
    return false;
  }
  const checks = [];
  const push = (name, ok, detail) => checks.push({ name: name, ok: !!ok, detail: detail });
  const chromeHits = [data.title, data.close, data.actions].filter((item) => item && item.chromeOverlap > 0);
  push("title-close-footer-not-under-chrome", chromeHits.length === 0, { chrome: data.chrome && data.chrome.rect, hits: [data.title, data.close, data.actions].map((item) => item && item.chromeOverlap) });
  const hitFailures = [data.title, data.close].filter((item) => item && (!item.hit || !item.hit.inPanel));
  push("title-close-topmost-inside-panel", hitFailures.length === 0, hitFailures.map((item) => item.hit));
  push("footer-buttons-topmost", data.cancel && data.confirm && data.cancel.hit && data.confirm.hit && data.cancel.hit.inPanel && data.confirm.hit.inPanel, { cancel: data.cancel && data.cancel.hit, confirm: data.confirm && data.confirm.hit });
  const rects = data.buttons;
  const widths = rects.map((r) => r.width);
  const heights = rects.map((r) => r.height);
  push("footer-buttons-equal-width", Math.max.apply(null, widths) - Math.min.apply(null, widths) <= 1, { widths: widths });
  push("footer-buttons-equal-height", Math.max.apply(null, heights) - Math.min.apply(null, heights) <= 1, { heights: heights });
  if (data.group && data.panelContent) {
    const leftGap = data.group.left - data.panelContent.left;
    const rightGap = data.panelContent.right - data.group.right;
    push("footer-group-centered", Math.abs(leftGap - rightGap) <= 1, { leftGap: Math.round(leftGap * 100) / 100, rightGap: Math.round(rightGap * 100) / 100 });
  }
  push("title-actions-same-axis", Math.abs((data.titleCenterX || 0) - (data.actionsCenterX || 0)) <= 1, { titleCenterX: data.titleCenterX, actionsCenterX: data.actionsCenterX, diff: Math.abs((data.titleCenterX || 0) - (data.actionsCenterX || 0)) });
  push("footer-primary-and-secondary-colors", data.confirm && data.cancel && data.confirm.bg === data.tokens.accent && (data.cancel.bg === "rgba(0, 0, 0, 0)" || data.cancel.bg === "transparent"), {
    primaryBg: data.confirm && data.confirm.bg, accent: data.tokens.accent, secondaryBg: data.cancel && data.cancel.bg,
    primaryClasses: data.confirm && data.confirm.classes, secondaryClasses: data.cancel && data.cancel.classes
  });
  push("panel-no-horizontal-overflow", data.panelScroll && data.panelScroll.scrollWidth <= data.panelScroll.clientWidth + 1, data.panelScroll);
  push("footer-inside-content-viewport", data.actions && data.actions.inContent, { actions: data.actions && data.actions.rect, content: data.content });
  for (const check of checks) record(scope, "keyform-" + check.name, check.ok, check.detail);
  return checks.every((check) => check.ok);
}

/* ---------------- 主流程 ---------------- */
async function runCombo(browser, vp, theme) {
  const tag = theme + "-" + vp.tag;
  const scope = tag;
  console.log("\n=== " + scope + " ===");
  const combo = { viewport: vp, theme: theme, dialogs: {}, keyform: null };
  await browser.setViewport(vp.width, vp.height);
  await browser.setTheme(theme);
  await openSettingsPage(browser);
  run.raw[tag] = {};

  const entries = [
    { key: "models-revoke", open: openModelsRevokeConfirm, expectDanger: true },
    { key: "memory-run", open: openMemoryConfirm, expectDanger: false },
    { key: "search-clear", open: openSearchClearConfirm, expectDanger: true }
  ];
  for (const entry of entries) {
    const opened = await entry.open(browser);
    if (!opened || !opened.ok) {
      record(scope, entry.key + "-open", false, opened);
      combo.dialogs[entry.key] = { opened: opened };
      continue;
    }
    const data = await browser.evalJs("window.__pm.measureLayer('confirm')");
    combo.dialogs[entry.key] = { opened: opened, data: data };
    run.raw[tag][entry.key] = data;
    record(scope, entry.key + "-open", true, { step: opened.step });
    checkLayer(scope, entry.key, data, { danger: entry.expectDanger });
    await shot(browser, "pm032-dialogs-" + entry.key + "-" + tag);
    await browser.pressKey("Escape");
    await sleep(320);
    record(scope, entry.key + "-esc-closes", !(await browser.evalJs("window.__pm.dialogOpen('confirm')")), {});
  }

  const keyOpened = await openKeyFormModal(browser);
  if (!keyOpened || !keyOpened.ok) {
    record(scope, "keyform-open", false, keyOpened);
  } else {
    const data = await browser.evalJs("window.__pm.measureLayer('modal')");
    combo.keyform = { opened: keyOpened, data: data };
    run.raw[tag].keyform = data;
    record(scope, "keyform-open", true, {});
    checkKeyForm(scope, data);
    await shot(browser, "pm032-dialogs-keyform-" + tag);
    await browser.pressKey("Escape");
    await sleep(320);
    record(scope, "keyform-esc-closes", !(await browser.evalJs("window.__pm.dialogOpen('modal')")), {});
  }
  run.combos.push(combo);
  return combo;
}

/** 800x600 深色下的键盘与二次确认：Esc / Tab / 焦点归还 / 显式确认才发请求。 */
async function runInteractions(browser) {
  const scope = "interaction-800x600-dark";
  console.log("\n=== " + scope + " ===");
  await browser.setViewport(800, 600);
  await browser.setTheme("dark");
  await openSettingsPage(browser);
  await clickCategoryById(browser, "models");
  await waitModelsPanel(browser);

  const prepared = await browser.evalJs("(function () { var tab = Array.prototype.slice.call(document.querySelectorAll('.conn button')).find(function (el) { return window.__pm.desc(el).text === '管理'; }); if (!tab) return { ok: false, reason: 'manage-tab' }; tab.click(); return { ok: true }; })()");
  await sleep(300);
  const target = await browser.evalJs("(function () { var btn = Array.prototype.slice.call(document.querySelectorAll('.conn button')).find(function (el) { return window.__pm.desc(el).text === '撤销密钥'; }); if (!btn) return { ok: false }; btn.setAttribute('data-pm032-trigger', '1'); btn.scrollIntoView({ block: 'center' }); var r = btn.getBoundingClientRect(); return { ok: true, x: r.x + r.width / 2, y: r.y + r.height / 2 }; })()");
  if (!prepared || !prepared.ok || !target || !target.ok) {
    record(scope, "trigger-focus-setup", false, { prepared: prepared, target: target });
    return;
  }
  await browser.clickAt(target.x, target.y);
  await sleep(440);
  record(scope, "dialog-opened-by-real-click", await browser.evalJs("window.__pm.dialogOpen('confirm')"), {});
  const focus = await browser.evalJs("window.__pm.focusInfo()");
  record(scope, "initial-focus-on-cancel", !!focus && focus.has && focus.el.text === "取消", focus);

  const tabTrace = [];
  for (let i = 0; i < 12; i += 1) {
    await browser.pressKey("Tab");
    await sleep(60);
    tabTrace.push(await browser.evalJs("window.__pm.focusInfo()"));
  }
  record(scope, "tab-stays-inside-dialog", tabTrace.every((item) => item && item.has && item.inDialog), tabTrace.map((item) => item && item.el.text));
  await browser.pressKey("Tab", { shift: true });
  await sleep(80);
  const shiftTab = await browser.evalJs("window.__pm.focusInfo()");
  record(scope, "shift-tab-stays-inside-dialog", !!shiftTab && shiftTab.has && shiftTab.inDialog, shiftTab);

  await browser.pressKey("Escape");
  await sleep(360);
  record(scope, "esc-closes-dialog", !(await browser.evalJs("window.__pm.dialogOpen('confirm')")), {});
  const restored = await browser.evalJs("window.__pm.focusInfo()");
  record(scope, "focus-returns-to-trigger", !!restored && restored.has && restored.marked === true, restored);

  // 二次确认流程：危险动作不能因为打开弹窗就执行；显式点确认才发请求。
  const beforeRequests = browser.requestEntries({ urlIncludes: "/api/credentials/" }).length;
  await browser.evalJs("(function () { var tab = Array.prototype.slice.call(document.querySelectorAll('.conn button')).find(function (el) { return window.__pm.desc(el).text === '管理'; }); if (tab) tab.click(); return true; })()");
  await sleep(280);
  const revokeOpened = await browser.evalJs("window.__pm.clickText('撤销密钥', '.conn')");
  await sleep(300);
  record(scope, "danger-action-requires-confirmation", !!revokeOpened && revokeOpened.ok && (await browser.evalJs("window.__pm.dialogOpen('confirm')")), revokeOpened);
  const skipped = browser.requestEntries({ urlIncludes: "/api/credentials/" }).length - beforeRequests;
  record(scope, "no-write-before-confirm", skipped === 0, { requests: skipped });
  await shot(browser, "pm032-dialogs-interaction-revoke-confirm");
  await browser.evalJs("(function () { var btn = document.querySelector('[data-qio-confirm-action=confirm]'); if (!btn) return false; btn.click(); return true; })()");
  await sleep(800);
  const requests = browser.requestEntries({ urlIncludes: "/api/credentials/" }).slice(beforeRequests);
  record(scope, "confirm-sends-action-request", requests.length > 0, requests.map((item) => item.method + " " + item.url));
  record(scope, "confirm-closes-dialog", !(await browser.evalJs("window.__pm.dialogOpen('confirm')")), {});
  await shot(browser, "pm032-dialogs-interaction-after-confirm");
}

/** 480 宽窄窗：KeyForm 底部动作必须纵排、不溢出、按钮等宽同高（#89）。 */
async function runNarrowKeyForm(browser) {
  const scope = "narrow-480x600-dark";
  console.log("\n=== " + scope + " ===");
  await browser.setViewport(480, 600);
  await browser.setTheme("dark");
  await openSettingsPage(browser);
  const opened = await openKeyFormModal(browser);
  if (!opened || !opened.ok) {
    record(scope, "keyform-open", false, opened);
    return;
  }
  const data = await browser.evalJs("window.__pm.measureLayer('modal')");
  run.raw[scope] = data;
  record(scope, "keyform-open", true, {});
  checkKeyForm(scope, data);
  const direction = await browser.evalJs("(() => { const el = document.querySelector('.qio-modal-actions'); return el ? getComputedStyle(el).flexDirection : null; })()");
  record(scope, "footer-buttons-stack-vertically", direction === "column", { flexDirection: direction });
  await shot(browser, "pm032-dialogs-keyform-" + scope);
  await browser.pressKey("Escape");
  await sleep(320);
  record(scope, "keyform-esc-closes", !(await browser.evalJs("window.__pm.dialogOpen('modal')")), {});
}

/**
 * 其他调用方回归（真实现场）：审批层（UnifiedApprovalModal）不是 QModal/QConfirm 的调用方，
 * 但和确认层共用同一个弹窗仲裁。这里用浏览器侧审批历史 fixture 让非聊天页的审批入口出现，
 * 真实点开审批弹窗：验证它自身的标题/关闭不被顶部全局栏盖住，并与已打开的确认层正确让位/恢复。
 * （历史数据是 fixture；不联网、不含真实审批内容。）
 */
async function runApprovalRegression(browser) {
  const scope = "approval-regression-800x600";
  console.log("\n=== " + scope + " ===");
  await browser.setViewport(800, 600);
  await browser.setTheme("dark");
  await openSettingsPage(browser);
  const loaded = await browser.evalJs("(function () { var root = document.querySelector('#app'); var app = root && root.__vue_app__; if (!app) return { ok: false, reason: 'no-vue-app' }; var pinia = app.config.globalProperties.$pinia; if (!pinia) return { ok: false, reason: 'no-pinia' }; var store = pinia._s && pinia._s.get('approvalResults'); if (!store) return { ok: false, reason: 'no-store', stores: Array.from(pinia._s.keys()) }; store.loadHistory(); return { ok: true }; })()");
  record(scope, "approval-history-fixture-loaded", !!(loaded && loaded.ok), loaded);
  await sleep(900);
  const entry = await browser.evalJs("(function () { var el = document.querySelector('[data-test=nonchat-approval-entry]'); if (!el) return { ok: false, reason: 'entry-missing' }; el.click(); return { ok: true, text: el.innerText }; })()");
  await sleep(700);
  record(scope, "approval-entry-opens-modal", !!(entry && entry.ok), entry);
  const data = await browser.evalJs("(function () { var mask = document.querySelector('.ua-mask'); var modal = document.querySelector('.ua-modal'); if (!mask || !modal) return { found: false }; var rect = function (el) { var r = el.getBoundingClientRect(); return { x: r.x, y: r.y, width: r.width, height: r.height, left: r.left, top: r.top, right: r.right, bottom: r.bottom }; }; var chromeEl = document.querySelector('.window-chrome'); var chromeRect = chromeEl ? rect(chromeEl) : null; var title = document.querySelector('#ua-title'); var close = document.querySelector('.ua-close'); var intersection = function (a, b) { if (!a || !b) return 0; var w = Math.min(a.right, b.right) - Math.max(a.left, b.left); var h = Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top); return w > 0 && h > 0 ? Math.round(w * h * 100) / 100 : 0; }; var titleRect = title ? rect(title) : null; var closeRect = close ? rect(close) : null; return { found: true, modal: rect(modal), title: titleRect, close: closeRect, chrome: chromeRect, titleChromeOverlap: intersection(titleRect, chromeRect), closeChromeOverlap: intersection(closeRect, chromeRect), titleHit: title ? !!document.elementFromPoint(titleRect.x + titleRect.width / 2, titleRect.y + titleRect.height / 2)?.closest('.ua-modal') : false, closeHit: close ? !!document.elementFromPoint(closeRect.x + closeRect.width / 2, closeRect.y + closeRect.height / 2)?.closest('.ua-modal') : false, activeInside: !!document.activeElement?.closest('.ua-modal') }; })()");
  record(scope, "approval-modal-rendered", !!(data && data.found), data);
  if (data && data.found) {
    record(scope, "approval-title-close-not-under-chrome", data.titleChromeOverlap === 0 && data.closeChromeOverlap === 0, { title: data.titleChromeOverlap, close: data.closeChromeOverlap, chrome: data.chrome });
    record(scope, "approval-title-close-topmost", data.titleHit && data.closeHit, { titleHit: data.titleHit, closeHit: data.closeHit });
    record(scope, "approval-modal-inside-viewport", data.modal.top >= 0 && data.modal.bottom <= 600 && data.modal.left >= 0 && data.modal.right <= 800, { modal: data.modal });
    await shot(browser, "pm032-dialogs-approval-regression");
  }
  // 让位/恢复：先打开确认层，再打开审批层 → 确认层只隐藏不卸载；Esc 关审批后确认层回来
  await browser.pressKey("Escape");
  await sleep(420);
  record(scope, "approval-esc-closes", !(await browser.evalJs("!!document.querySelector('.ua-mask')")), {});

  const revoke = await openModelsRevokeConfirm(browser);
  record(scope, "confirm-layer-opened-for-yield-test", !!(revoke && revoke.ok), revoke);
  if (!(revoke && revoke.ok)) return;
  const entry2 = await browser.evalJs("(function () { var el = document.querySelector('[data-test=nonchat-approval-entry]'); if (!el) return { ok: false, reason: 'entry-missing' }; el.click(); return { ok: true }; })()");
  await sleep(650);
  const coexist = await browser.evalJs("(function () { var confirmScrim = document.querySelector('.qio-confirm-scrim'); var confirmLayer = document.querySelector('.qio-confirm--layer'); var approval = document.querySelector('.ua-mask'); return { confirmHidden: !!confirmScrim && getComputedStyle(confirmScrim).display === 'none', confirmStillInDom: !!confirmLayer, approvalVisible: !!approval && getComputedStyle(approval).display !== 'none' }; })()");
  record(scope, "confirm-layer-yields-to-approval", !!(entry2 && entry2.ok) && coexist.confirmHidden && coexist.confirmStillInDom && coexist.approvalVisible, coexist);
  await browser.pressKey("Escape");
  await sleep(500);
  const resumed = await browser.evalJs("(function () { var confirmScrim = document.querySelector('.qio-confirm-scrim'); var approval = document.querySelector('.ua-mask'); return { approvalGone: !approval || getComputedStyle(approval).display === 'none', confirmVisible: !!confirmScrim && getComputedStyle(confirmScrim).display !== 'none' }; })()");
  record(scope, "confirm-layer-resumes-after-approval", resumed.approvalGone && resumed.confirmVisible, resumed);
  await browser.pressKey("Escape");
  await sleep(420);
  record(scope, "confirm-layer-still-esc-closable", !(await browser.evalJs("window.__pm.dialogOpen('confirm')")), {});
}

/* ---------------- 启动 ---------------- */
const browser = new VerifyBrowser({ port: cdpPort, width: 1440, height: 900, apiBase: apiBase });
let exitCode = 0;
try {
  await browser.launch();
  await browser.installDialogGuards();
  await browser.route({ method: "GET", path: /\/api\/settings\/search(\?|$)/ }, () => ({ body: SEARCH_SETTINGS_CONFIGURED, headers: { "Cache-Control": "no-store" } }));
  await browser.route({ method: "GET", path: /\/api\/approvals\/history/ }, () => ({ body: APPROVAL_HISTORY, headers: { "Cache-Control": "no-store" } }));
  const fixtures = await installFixtures(browser);
  run.fixtures = fixtures.hits;
  await browser.navigate(appUrl + "/", 2400);

  for (const theme of themeList) {
    for (const vp of viewportList) await runCombo(browser, vp, theme);
  }
  if (themeList.indexOf("dark") >= 0) {
    await runInteractions(browser);
    await runApprovalRegression(browser);
    await runNarrowKeyForm(browser);
  }

  const failures = run.checks.filter((item) => !item.ok);
  run.failures = failures.length;
  run.total = run.checks.length;
  run.finishedAt = new Date().toISOString();
  writeFileSync(join(outDir, "pm032-dialogs-report.json"), JSON.stringify(run, null, 2), "utf8");
  console.log("\n" + (failures.length ? "FAIL" : "PASS") + " · " + run.checks.length + " 条检查，失败 " + failures.length + " 条");
  console.log("报告：" + join(outDir, "pm032-dialogs-report.json"));
  console.log("截图：" + run.screenshots.length + " 张 → " + outDir);
  if (failures.length) {
    console.log("\n失败项：");
    for (const failure of failures) console.log("- " + failure.scope + " / " + failure.name);
    if (!allowFail) exitCode = 1;
  }
} catch (error) {
  console.error("运行失败：" + (error && error.stack ? error.stack : error));
  exitCode = 1;
} finally {
  try {
    await browser.close();
  } catch {
    // 浏览器已退出：忽略
  }
  process.exitCode = exitCode;
}
