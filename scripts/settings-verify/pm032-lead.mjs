
/**
 * Lead 父分支端到端验收（pm-qio032-settings）
 *
 * 覆盖：
 *  A 20 处文案快照（五分区可见文本导出，供逐条核对）
 *  B #90 五分区外框宽度（切分区不得跳变）
 *  C #70 设置行「标签 ↔ 控件」横向距离 / 窄窗上下排列
 *  D #82 窗口级确认框：800×600 下不被顶部导航遮挡（矩形 + elementFromPoint 命中）
 *  E #87 危险「清除」：与「取消」等宽同高、整体居中、危险色未被普通按钮覆盖
 *  F #89 添加 Key：标题与底部按钮组同轴、按钮等宽同高
 *  G 模态键盘：Esc 关闭 / Tab 焦点陷阱 / 关闭后焦点归还
 *  H #63–#65 保留天数字段的请求级行为（非法文本 / 清空 / 显式 0 / 503 回滚 / 重试 / 迟到响应）
 *  I #23 搜索通道「已配置，尚未检查」与检查失败/成功
 *
 * 用法：
 *   node scripts/settings-verify/pm032-lead.mjs --url http://127.0.0.1:5106 \
 *        --api http://127.0.0.1:8756 --out D:\qio-dev\_lead-logs\pm032-settings\lead\shots --label baseline
 *   可选 --only layout,rows,confirm,keyform,keyboard,retention,searchstate  --allow-fail
 */
import { mkdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { VerifyBrowser } from "./lib/browser.mjs";
import { openSettings, installHelpers, waitModelsPanel } from "./lib/probe.mjs";
import { installFixtures } from "./lib/fixtures.mjs";
import { SETTINGS_CATEGORIES, stamp } from "./lib/env.mjs";

const args = process.argv.slice(2);
const flag = (name, fallback) => {
  const index = args.indexOf("--" + name);
  if (index < 0) return fallback;
  const value = args[index + 1];
  return value === undefined || value.startsWith("--") ? true : value;
};
const sleep = (ms) => new Promise((done) => setTimeout(done, ms));
const appUrl = String(flag("url", "http://127.0.0.1:5106")).replace(/\/$/, "");
const apiBase = String(flag("api", process.env.QIO_SETTINGS_API || "http://127.0.0.1:8756")).replace(/\/$/, "");
const cdpPort = Number(flag("cdp-port", 9806));
const outDir = String(flag("out", "D:\\qio-dev\\_lead-logs\\pm032-settings\\lead\\shots"));
const label = String(flag("report-label", flag("label", "run-" + stamp())));
const allowFail = !!flag("allow-fail", false);
const only = String(flag("only", "")).split(",").map((s) => s.trim()).filter(Boolean);
const want = (name) => only.length === 0 || only.includes(name);
mkdirSync(outDir, { recursive: true });

const run = { label, startedAt: new Date().toISOString(), appUrl, checks: [], skipped: [], shots: [], texts: {}, notes: [] };
function record(scope, name, ok, detail) {
  run.checks.push({ scope, name, ok: !!ok, detail: detail === undefined ? null : detail });
  console.log("  " + (ok ? "[PASS]" : "[FAIL]") + " " + scope + "/" + name + (ok ? "" : " :: " + JSON.stringify(detail).slice(0, 400)));
  return ok;
}
function skip(scope, name, reason) {
  run.skipped.push({ scope, name, reason });
  console.log("  [SKIP] " + scope + "/" + name + " :: " + reason);
}
async function shot(browser, name) {
  const file = join(outDir, label + "-" + name + ".png");
  await browser.shotFile(file, null, true);
  run.shots.push(file);
  return file;
}
async function ensureProbe(browser) {
  if (!(await browser.evalJs("!!window.__stg"))) await installHelpers(browser);
}
async function gotoCategory(browser, id) {
  const cat = SETTINGS_CATEGORIES.find((c) => c.id === id);
  const clicked = await browser.evalJs(
    "(() => { const nav = document.querySelector('[data-settings-nav], nav[role=tablist], .settings-nav'); if (!nav) return { ok: false, reason: 'nav-missing' }; " +
      "const items = [...nav.querySelectorAll('[role=tab], [data-settings-category], button, a')].filter((el) => window.__stg.visible(el)); " +
      "const want = " + JSON.stringify([cat.label, ...(cat.aliases ?? [])]) + "; " +
      "const hit = items.find((el) => want.some((w) => window.__stg.norm(el.innerText || el.textContent || '').includes(window.__stg.norm(w)))); " +
      "if (!hit) return { ok: false, reason: 'not-found', seen: items.map((el) => window.__stg.norm(el.innerText || '')) }; hit.click(); return { ok: true, clicked: window.__stg.norm(hit.innerText || '') }; })()",
  );
  if (clicked?.ok) await sleep(420);
  return clicked;
}
async function clickText(browser, text, { exact = false } = {}) {
  return browser.evalJs("window.__stg.clickLabel(" + JSON.stringify(String(text)) + ", { exact: " + (exact ? "true" : "false") + " })");
}
function writeRequests(browser, since, pathPattern) {
  return browser.log.slice(since).filter((e) => e.kind === "request" && e.method !== "GET" && (!pathPattern || pathPattern.test(e.url)));
}

const HELPERS = [];
async function setup(browser) { await openSettings(browser, appUrl); await ensureProbe(browser); }

/* ---------------- A 文案快照 ---------------- */
async function dumpTexts(browser) {
  for (const cat of SETTINGS_CATEGORIES) {
    await gotoCategory(browser, cat.id);
    const text = await browser.evalJs("(() => { const root = window.__stg.root(); const secs = [...root.querySelectorAll('.set-section')].filter((el) => window.__stg.visible(el)); return secs.map((el) => el.innerText || '').join(String.fromCharCode(10, 10)); })()");
    run.texts[cat.id] = String(text || "");
  }
  record("copy", "texts-dumped", Object.keys(run.texts).length === 5, Object.fromEntries(Object.entries(run.texts).map(([k, v]) => [k, v.length])));
}

/* ---------------- B #90 面板外框 ---------------- */
async function measurePanels(browser, vp) {
  await setup(browser);
  await browser.setViewport(vp.width, vp.height);
  await sleep(700);
  const rows = [];
  for (const cat of SETTINGS_CATEGORIES) {
    await gotoCategory(browser, cat.id);
    const data = await browser.evalJs("(() => { const panels = document.querySelector('[data-testid=settings-panels]'); const root = window.__stg.root(); const secs = [...root.querySelectorAll('.set-section')].filter((el) => window.__stg.visible(el)); const r = (el) => { const b = el.getBoundingClientRect(); return { x: +b.x.toFixed(1), w: +b.width.toFixed(1), right: +b.right.toFixed(1) }; }; const sec = secs[0] || null; const inner = sec ? [...sec.children].filter((el) => window.__stg.visible(el)).slice(0, 4).map((el) => ({ cls: String(el.className).slice(0, 40), ...r(el) })) : []; return { panels: panels ? r(panels) : null, section: sec ? { cls: String(sec.className).slice(0, 40), ...r(sec) } : null, inner }; })()");
    rows.push({ category: cat.id, ...data });
  }
  const widths = rows.map((row) => row.section?.w ?? null).filter((w) => w !== null);
  const spread = widths.length ? +(Math.max(...widths) - Math.min(...widths)).toFixed(1) : null;
  const panelWidths = rows.map((row) => row.panels?.w ?? null);
  record("layout-" + vp.width, "sections-share-outer-boundary", spread !== null && spread <= 1.5, { spread, widths, rows: rows.map((row) => ({ c: row.category, w: row.section?.w })) });
  record("layout-" + vp.width, "panels-wrapper-measured", panelWidths.every((w) => w !== null), { panelWidths });
  if (vp.width === 1440) await shot(browser, "layout-1440-" + "panels");
  return rows;
}

/* ---------------- C #70 行几何 ---------------- */
async function measureRows(browser, vp) {
  await setup(browser);
  await browser.setViewport(vp.width, vp.height);
  await sleep(600);
  const all = [];
  for (const cat of SETTINGS_CATEGORIES) {
    await gotoCategory(browser, cat.id);
    const rows = await browser.evalJs("(() => { const root = window.__stg.root(); const out = []; for (const row of root.querySelectorAll('.set-row[data-settings-key]')) { if (!window.__stg.visible(row)) continue; const txt = row.querySelector('.set-row__txt'); const ctl = row.querySelector('.set-row__ctl'); const title = row.querySelector('.set-row__title'); if (!txt || !ctl) continue; const t = txt.getBoundingClientRect(); const c = ctl.getBoundingClientRect(); const tr = title ? title.getBoundingClientRect() : null; if (!t.width || !c.width) continue; out.push({ key: row.getAttribute('data-settings-key'), cat: row.closest('.set-section') ? row.closest('.set-section').getAttribute('data-settings-section') : null, gap: +(c.left - t.right).toFixed(1), titleGap: tr ? +(c.left - tr.right).toFixed(1) : null, stacked: c.top >= t.bottom - 1, txtRight: +t.right.toFixed(1), titleRight: tr ? +tr.right.toFixed(1) : null, ctlLeft: +c.left.toFixed(1), txtW: +t.width.toFixed(1), ctlW: +c.width.toFixed(1) }); } return out; })()");
    all.push(...rows);
  }
  const gaps = all.map((row) => row.titleGap ?? row.gap).filter((g) => Number.isFinite(g));
  gaps.sort((a, b) => a - b);
  const median = gaps.length ? gaps[Math.floor(gaps.length / 2)] : null;
  const max = gaps.length ? gaps[gaps.length - 1] : null;
  const stackedCount = all.filter((row) => row.stacked).length;
  record("rows-" + vp.width, "rows-measured", all.length >= 8, { count: all.length, medianTitleGap: median, maxTitleGap: max, stacked: stackedCount });
  if (vp.width <= 900) record("rows-narrow", "controls-stack-under-label", all.length > 0 && stackedCount >= Math.ceil(all.length * 0.8), { stacked: stackedCount, total: all.length });
  else record("rows-wide", "gap-within-readable-range", median !== null && median <= 160, { medianGap: median, maxGap: max });
  return { vp: vp.width, medianGap: median, maxGap: max, stacked: stackedCount, count: all.length, rows: all.slice(0, 40) };
}

/* ---------------- D/E 确认框 ---------------- */
async function stubsForDangerConfirm(browser) {
  const offs = [];
  offs.push(await browser.route({ method: "GET", path: /\/api\/settings\/search(\?|$)/ }, () => ({ body: { top_k_default: 7, max_fetch_chars: 30000, searxng_url: "", bocha_has_key: true, keyless_fallback: true }, headers: { "Cache-Control": "no-store" } })));
  offs.push(await browser.route({ method: "GET", path: /\/api\/settings\/search\/status/ }, () => ({ body: { channels: [
    { id: "keyless", label: "免密钥检索", configured: true, usable: true, checked_at: "2026-10-28T03:00:00.000Z", checking: false, reason_code: null, message: "" },
    { id: "bocha", label: "博查 API Key", configured: true, usable: null, checked_at: null, checking: false, reason_code: null, message: "" },
    { id: "searxng", label: "自建 SearXNG", configured: false, usable: false, checked_at: null, checking: false, reason_code: "not_configured", message: "未配置" },
  ] } })));
  return offs;
}

async function openDangerConfirm(browser) {
  await browser.setViewport(800, 600);
  await sleep(700);
  await setup(browser);
  await gotoCategory(browser, "models");
  await sleep(500);
  const advanced = await browser.evalJs("window.__stg.expandAdvanced()");
  await sleep(400);
  const clicked = await browser.evalJs("(() => { const el = document.querySelector('[data-testid=settings-search-bocha-clear]'); if (!el) return { ok: false, reason: 'no-clear-button', visible: !!(el && window.__stg.visible(el)) }; el.scrollIntoView({ block: 'center' }); el.click(); return { ok: true, text: window.__stg.norm(el.innerText || '') }; })()");
  await sleep(700);
  return { clicked, advanced };
}

async function measureConfirm(browser, scope) {
  const m = await browser.evalJs("(() => { const scrim = document.querySelector('.qio-confirm-scrim'); const dlg = document.querySelector('.qio-confirm--layer') || document.querySelector('.qio-confirm'); const chrome = document.querySelector('.window-chrome'); const cancel = document.querySelector('[data-qio-confirm-action=cancel]'); const confirm = document.querySelector('[data-qio-confirm-action=confirm]'); const head = document.querySelector('.qio-confirm__head'); const title = document.querySelector('.qio-confirm__title'); const close = document.querySelector('.qio-confirm__close'); const r = (el) => { if (!el) return null; const b = el.getBoundingClientRect(); return { x: +b.x.toFixed(1), y: +b.y.toFixed(1), w: +b.width.toFixed(1), h: +b.height.toFixed(1), right: +b.right.toFixed(1), bottom: +b.bottom.toFixed(1), cx: +(b.x + b.width / 2).toFixed(1), cy: +(b.y + b.height / 2).toFixed(1) }; }; const hit = (el) => { if (!el) return null; const b = el.getBoundingClientRect(); const node = document.elementFromPoint(Math.round(b.x + b.width / 2), Math.round(b.y + b.height / 2)); return { insideDialog: !!dlg && !!node && dlg.contains(node), tag: node ? node.tagName : null, cls: node ? String(node.className).slice(0, 60) : null, inChrome: !!chrome && !!node && chrome.contains(node) }; }; const fp = (el) => { if (!el) return null; const s = getComputedStyle(el); return { bg: s.backgroundColor, color: s.color, border: s.borderColor, shadow: s.boxShadow, img: s.backgroundImage, weight: s.fontWeight }; }; const visible = scrim ? getComputedStyle(scrim).display !== 'none' && scrim.getBoundingClientRect().height > 0 : false; return { visible, vw: window.innerWidth, vh: window.innerHeight, chrome: r(chrome), scrim: r(scrim), dialog: r(dlg), head: r(head), title: r(title), close: r(close), closeHit: hit(close || title), titleHit: hit(title), cancel: r(cancel), confirm: r(confirm), confirmHit: hit(confirm), confirmText: confirm ? window.__stg.norm(confirm.innerText || '') : null, cancelText: cancel ? window.__stg.norm(cancel.innerText || '') : null, cancelFp: fp(cancel), confirmFp: fp(confirm), confirmCls: confirm ? confirm.className : null, dialogInView: dlg ? (() => { const b = dlg.getBoundingClientRect(); return b.top >= 0 && b.left >= 0 && b.bottom <= window.innerHeight + 0.5 && b.right <= window.innerWidth + 0.5; })() : false }; })()");
  record(scope, "confirm-visible", !!m.visible, { visible: m.visible });
  if (!m.visible) return m;
  const overlapChrome = m.chrome && m.dialog ? !(m.dialog.y >= m.chrome.bottom - 0.5 || m.dialog.bottom <= m.chrome.y + 0.5) && m.chrome.w > 0 : false;
  // 顶部导航与弹窗「标题/关闭/确认」矩形是否真的相交（同一列）
  const rectIntersect = (a, b) => !!a && !!b && a.x < b.right && b.x < a.right && a.y < b.bottom && b.y < a.bottom;
  const titleOverlap = rectIntersect(m.title, m.chrome);
  const closeOverlap = rectIntersect(m.close, m.chrome) || rectIntersect(m.title, m.chrome);
  const confirmOverlap = rectIntersect(m.confirm, m.chrome);
  record(scope, "dialog-in-viewport", !!m.dialogInView, { dialog: m.dialog, vw: m.vw, vh: m.vh });
  // #82 根因：设置内容区若成为 fixed 的包含块，遮罩会脱离视口（rect 高远大/原点为负），居中也会错位
  const scrimCoversViewport = !!m.scrim && Math.abs(m.scrim.y) <= 2 && Math.abs(m.scrim.h - m.vh) <= 2 && m.scrim.w <= m.vw + 1;
  record(scope, "scrim-covers-viewport", scrimCoversViewport, { scrim: m.scrim, vw: m.vw, vh: m.vh });
  const centeredY = !!m.dialog && Math.abs(m.dialog.cy - m.vh / 2) <= 8;
  record(scope, "dialog-centered-in-viewport", centeredY, { dialogCy: m.dialog?.cy, viewportCenter: m.vh / 2 });
  record(scope, "title-close-not-covered-by-topbar", !titleOverlap && !closeOverlap && !!m.titleHit?.insideDialog, { titleOverlap, closeOverlap, titleHit: m.titleHit, closeHit: m.closeHit, chrome: m.chrome, dialog: m.dialog });
  record(scope, "confirm-action-not-covered-by-topbar", !confirmOverlap && !!m.confirmHit?.insideDialog, { confirmOverlap, confirmHit: m.confirmHit, confirm: m.confirm });
  run.confirmMetrics = m;
  return m;
}

function checkDangerButtons(m, scope) {
  const c = m.cancel, k = m.confirm;
  const sameW = c && k && Math.abs(c.w - k.w) <= 1;
  const sameH = c && k && Math.abs(c.h - k.h) <= 1;
  const dialogCenter = m.dialog ? m.dialog.cx : null;
  const groupCenter = c && k ? +((Math.min(c.x, k.x) + Math.max(c.right, k.right)) / 2).toFixed(1) : null;
  const centered = dialogCenter !== null && groupCenter !== null && Math.abs(groupCenter - dialogCenter) <= 2;
  const transparent = (fpObj) => !fpObj || !fpObj.bg || /rgba\(0, 0, 0, 0\)|transparent/.test(fpObj.bg);
  const filled = !transparent(m.confirmFp);
  const distinct = JSON.stringify(m.cancelFp) !== JSON.stringify(m.confirmFp);
  record(scope, "danger-button-equal-size", !!(sameW && sameH), { cancel: c, confirm: k });
  record(scope, "button-group-centered", !!centered, { dialogCenter, groupCenter });
  record(scope, "danger-button-filled", filled, { confirmFp: m.confirmFp, cancelFp: m.cancelFp, confirmCls: m.confirmCls });
  record(scope, "danger-style-distinct", distinct && filled, { cancelFp: m.cancelFp, confirmFp: m.confirmFp, confirmCls: m.confirmCls });
  record(scope, "confirm-label-visible", !!m.confirmText, { confirmText: m.confirmText, cancelText: m.cancelText });
}

/* ---------------- F #89 KeyForm ---------------- */
async function measureKeyForm(browser) {
  await browser.setViewport(800, 600);
  await sleep(600);
  await setup(browser);
  await gotoCategory(browser, "models");
  const panel = await waitModelsPanel(browser, { timeout: 15000 });
  const findOpen = "(() => { const root = window.__stg.root(); const btns = [...root.querySelectorAll('button')].filter((el) => window.__stg.visible(el)); const btn = btns.find((el) => /添加\s*Key|新建\s*Key|添加连接|接入新的服务商/.test(window.__stg.norm(el.innerText || ''))); if (!btn) return { ok: false, seen: btns.map((el) => window.__stg.norm(el.innerText || '')).filter(Boolean).slice(0, 40) }; btn.scrollIntoView({ block: 'center' }); btn.click(); return { ok: true, text: window.__stg.norm(btn.innerText || '') }; })()";
  let opened = await browser.evalJs(findOpen);
  if (!opened?.ok) { await browser.evalJs("window.__stg.expandAdvanced()"); await sleep(500); opened = await browser.evalJs(findOpen); }
  await sleep(1000);
  const m = await browser.evalJs("(() => { const modal = document.querySelector('.qio-modal'); if (!modal) return { modal: false }; const head = modal.querySelector('.qio-modal-head'); const title = modal.querySelector('.qio-modal-title'); const actions = modal.querySelector('.qio-modal-actions'); const btns = actions ? [...actions.querySelectorAll('button')].filter((el) => window.__stg.visible(el)) : []; const r = (el) => { if (!el) return null; const b = el.getBoundingClientRect(); return { x: +b.x.toFixed(1), y: +b.y.toFixed(1), w: +b.width.toFixed(1), h: +b.height.toFixed(1), right: +b.right.toFixed(1), bottom: +b.bottom.toFixed(1), cx: +(b.x + b.width / 2).toFixed(1) }; }; return { modal: true, head: r(head), title: r(title), titleCx: title ? +(title.getBoundingClientRect().x + title.getBoundingClientRect().width / 2).toFixed(1) : null, actions: r(actions), actionsCx: actions ? +(actions.getBoundingClientRect().x + actions.getBoundingClientRect().width / 2).toFixed(1) : null, buttons: btns.map((el) => ({ text: window.__stg.norm(el.innerText || ''), ...r(el), cls: el.className })), dialog: r(modal), inView: (() => { const b = modal.getBoundingClientRect(); return b.top >= 0 && b.left >= 0 && b.bottom <= window.innerHeight + 0.5 && b.right <= window.innerWidth + 0.5; })() }; })()");
  record("keyform", "opens", !!m.modal, { opened, panel: panel?.ready });
  if (!m.modal) return m;
  shot(browser, "keyform-800x600");
  const titleCx = m.title ? m.title.cx : m.titleCx;
  const actionsCx = m.actions ? m.actions.cx : m.actionsCx;
  record("keyform", "title-actions-same-axis", titleCx !== null && actionsCx !== null && Math.abs(titleCx - actionsCx) <= 2, { titleCx, actionsCx, title: m.title, actions: m.actions });
  const widths = m.buttons.map((b) => b.w);
  const heights = m.buttons.map((b) => b.h);
  record("keyform", "footer-buttons-equal-size", widths.length >= 2 && Math.max(...widths) - Math.min(...widths) <= 1 && Math.max(...heights) - Math.min(...heights) <= 1, { widths, heights, buttons: m.buttons });
  record("keyform", "keyform-in-viewport", !!m.inView, { dialog: m.dialog });
  // #82 同类：KeyForm 是较高的弹窗，800×600 下标题/关闭不得被顶部导航盖住
  const cover = await browser.evalJs("(() => { const modal = document.querySelector('.qio-modal'); const chrome = document.querySelector('.window-chrome'); const title = document.querySelector('.qio-modal-title'); const close = document.querySelector('.qio-modal .qio-modal-head__slot--end button, .qio-modal [data-qio-icon-button], .qio-modal button[aria-label=关闭]'); const r = (el) => { if (!el) return null; const b = el.getBoundingClientRect(); return { x: b.x, y: b.y, right: b.right, bottom: b.bottom, w: b.width, h: b.height }; }; const inter = (a, b) => !!a && !!b && a.x < b.right && b.x < a.right && a.y < b.bottom && b.y < a.bottom; const hit = (el) => { if (!el) return null; const b = el.getBoundingClientRect(); const n = document.elementFromPoint(Math.round(b.x + b.width / 2), Math.round(b.y + b.height / 2)); return { inModal: !!modal && !!n && modal.contains(n), tag: n ? n.tagName : null }; }; return { chrome: r(chrome), title: r(title), close: r(close), titleOverlap: inter(r(title), r(chrome)), closeOverlap: inter(r(close), r(chrome)), titleHit: hit(title), modalTop: modal ? modal.getBoundingClientRect().top : null, chromeBottom: chrome ? chrome.getBoundingClientRect().bottom : null }; })()");
  record("keyform", "header-not-covered-by-topbar", !cover.titleOverlap && !cover.closeOverlap && !!cover.titleHit?.inModal, cover);
  // 统一的 inline 确认：设置里不应出现 inline 变体（应走窗口层 QConfirm layer）
  const inlineCount = await browser.evalJs("(() => { const root = window.__stg.root(); return [...root.querySelectorAll('.qio-confirm--inline')].filter((el) => window.__stg.visible(el)).length; })()");
  record("confirm", "settings-confirms-use-window-layer", inlineCount === 0, { inlineCount });
  await browser.pressKey("Escape");
  await sleep(500);
  const gone = await browser.evalJs("!document.querySelector('.qio-modal')");
  record("keyform", "esc-closes", !!gone, { gone });
  // #89 窄窗（480）：纵排 + 撑满 + 整体居中
  await browser.setViewport(480, 600);
  await sleep(700);
  await setup(browser);
  await gotoCategory(browser, "models");
  await waitModelsPanel(browser, { timeout: 15000 });
  const opened480 = await browser.evalJs(findOpen);
  await sleep(900);
  const n = await browser.evalJs("(() => { const actions = document.querySelector('.qio-modal .qio-modal-actions'); const modal = document.querySelector('.qio-modal'); if (!actions || !modal) return { found: false }; const r = (el) => { const b = el.getBoundingClientRect(); return { x: +b.x.toFixed(1), y: +b.y.toFixed(1), w: +b.width.toFixed(1), h: +b.height.toFixed(1), right: +b.right.toFixed(1) }; }; const btns = [...actions.querySelectorAll('button')].filter((el) => window.__stg.visible(el)); const a = r(actions); const b0 = btns.length ? r(btns[0]) : null; const b1 = btns.length > 1 ? r(btns[1]) : null; const m = r(modal); const leftGap = b0 ? b0.x - a.x : null; const rightGap = b1 ? a.right - b1.right : null; return { found: true, actions: a, buttons: btns.map((el) => r(el)), widths: btns.map((el) => r(el).w), heights: btns.map((el) => r(el).h), stacked: !!(b0 && b1 && b1.y >= b0.y + b0.h - 1), leftGap: leftGap === null ? null : +leftGap.toFixed(1), rightGap: rightGap === null ? null : +rightGap.toFixed(1), overflow: document.documentElement.scrollWidth > window.innerWidth + 1 }; })()");
  record("keyform-480", "footer-stacked-full-width-centered", !!(n.found && n.stacked && n.widths.length === 2 && Math.max(...n.widths) - Math.min(...n.widths) <= 1 && n.leftGap !== null && n.rightGap !== null && Math.abs(n.leftGap - n.rightGap) <= 2 && !n.overflow), n);
  await shot(browser, "keyform-480x600");
  const gone480 = await browser.evalJs("(() => { const btn = document.querySelector('.qio-modal .qio-modal-actions button'); return !!btn; })()");
  await browser.pressKey("Escape");
  await sleep(400);
  return m;
}

/* ---------------- G 键盘 / 焦点 ---------------- */
async function keyboardChecks(browser) {
  await browser.setViewport(800, 600);
  await sleep(600);
  await setup(browser);
  await gotoCategory(browser, "memory");
  await sleep(600);
  const opened = await browser.evalJs("(() => { const root = window.__stg.root(); const btn = [...root.querySelectorAll('button')].find((el) => window.__stg.visible(el) && window.__stg.norm(el.innerText || '') === '立即整理'); if (!btn) return { ok: false }; btn.setAttribute('data-lead-trigger', '1'); btn.focus(); btn.click(); return { ok: true, focused: document.activeElement === btn }; })()");
  await sleep(800);
  const dialogOpen = await browser.evalJs("!!document.querySelector('.qio-confirm--layer, .qio-confirm-scrim')");
  record("keyboard", "confirm-opens", !!(opened?.ok && dialogOpen), { opened, dialogOpen });
  if (!dialogOpen) return;
  // Tab 循环：焦点必须留在弹窗里
  const inside = [];
  for (let i = 0; i < 8; i += 1) {
    await browser.pressKey("Tab");
    await sleep(60);
    inside.push(await browser.evalJs("(() => { const dlg = document.querySelector('.qio-confirm--layer') || document.querySelector('.qio-modal'); const el = document.activeElement; return { inside: !!dlg && dlg.contains(el), tag: el ? el.tagName : null, text: el ? window.__stg.norm(el.innerText || '') : null }; })()"));
  }
  record("keyboard", "tab-traps-focus", inside.length > 0 && inside.every((x) => x.inside), { inside });
  await browser.pressKey("Escape");
  await sleep(600);
  const closed = await browser.evalJs("!document.querySelector('.qio-confirm--layer')");
  record("keyboard", "esc-closes-confirm", !!closed, { closed });
  const focusBack = await browser.evalJs("(() => { const el = document.activeElement; const trigger = document.querySelector('[data-lead-trigger=\"1\"]'); return { onTrigger: !!trigger && el === trigger, tag: el ? el.tagName : null, text: el ? window.__stg.norm(el.innerText || '') : null }; })()");
  record("keyboard", "focus-returns-to-trigger", !!focusBack.onTrigger, focusBack);
}

/* ---------------- H #63–#65 ---------------- */
const TOOLS_PATH = /\/api\/settings\/tools/;
async function readRetention(browser) {
  return browser.evalJs("(() => { const row = document.querySelector('[data-settings-key=\"tools.records.record-retention\"]'); if (!row) return null; const input = row.querySelector('input'); return { value: input ? input.value : null, sectionText: window.__stg.norm(row.innerText || '') }; })()");
}
async function notices(browser) {
  return browser.evalJs("(() => [...window.__stg.root().querySelectorAll('[role=status], [role=alert]')].filter((el) => window.__stg.visible(el)).map((el) => window.__stg.norm(el.innerText || '')).filter(Boolean))()");
}
async function blameRetention(browser) {
  const inputSel = "[data-settings-key=\"tools.records.record-retention\"] input";
  await browser.evalJs("(() => { const el = document.querySelector('" + inputSel + "'); if (el) el.focus(); return !!el; })()");
}

async function retentionChecks(browser) {
  await browser.setViewport(1440, 900);
  await setup(browser);
  await gotoCategory(browser, "tools");
  await sleep(900);
  const sel = "[data-settings-key=\"tools.records.record-retention\"] input";
  const exists = await browser.evalJs("!!document.querySelector('" + sel + "')");
  record("retention", "field-present", !!exists, { exists });
  if (!exists) return;
  const initial = await readRetention(browser);

  // 1) 非法文本：不发保存请求，显示可读提示，保留最近已保存值
  let mark = browser.log.length;
  await browser.fill(sel, "abc");
  await browser.evalJs("(() => { const el = document.querySelector('" + sel + "'); el.dispatchEvent(new Event('change', { bubbles: true })); el.blur(); return true; })()");
  await sleep(1200);
  const invalidWrites = writeRequests(browser, mark, TOOLS_PATH);
  const afterInvalid = await readRetention(browser);
  const invalidNotice = await notices(browser);
  record("retention", "invalid-text-no-write", invalidWrites.length === 0, { writes: invalidWrites.map((e) => e.method + " " + e.url), afterInvalid, notices: invalidNotice });
  record("retention", "invalid-text-keeps-saved-value", String(afterInvalid?.value) === String(initial?.value), { initial: initial?.value, after: afterInvalid?.value });
  record("retention", "invalid-text-readable-hint", invalidNotice.some((t) => /数字|整数|有效|非法|必填|留空/.test(t)), { notices: invalidNotice });

  // 2) 清空：不得写库为 0；离开再回来回显已保存值
  mark = browser.log.length;
  await browser.fill(sel, "");
  await browser.evalJs("(() => { const el = document.querySelector('" + sel + "'); el.dispatchEvent(new Event('change', { bubbles: true })); el.blur(); return true; })()");
  await sleep(1200);
  const clearWrites = writeRequests(browser, mark, TOOLS_PATH);
  const afterClear = await readRetention(browser);
  record("retention", "clear-does-not-save-zero", clearWrites.length === 0, { writes: clearWrites.map((e) => e.method + " " + e.url), afterClear });
  // 切走再回来
  await gotoCategory(browser, "regular");
  await sleep(400);
  await gotoCategory(browser, "tools");
  await sleep(700);
  const afterReturn = await readRetention(browser);
  record("retention", "return-echoes-saved-value", String(afterReturn?.value) === String(initial?.value), { initial: initial?.value, afterReturn: afterReturn?.value, text: afterReturn?.sectionText });
  // 整页刷新再核对
  await setup(browser);
  await gotoCategory(browser, "tools");
  await sleep(900);
  const afterReload = await readRetention(browser);
  record("retention", "reload-consistent-with-server", String(afterReload?.value) === String(initial?.value), { initial: initial?.value, afterReload: afterReload?.value });

  // 3) 显式 0：真的保存为 0，刷新后仍为 0
  mark = browser.log.length;
  await browser.fill(sel, "0");
  await browser.evalJs("(() => { const el = document.querySelector('" + sel + "'); el.dispatchEvent(new Event('change', { bubbles: true })); el.blur(); return true; })()");
  await sleep(1500);
  const zeroWrites = writeRequests(browser, mark, TOOLS_PATH);
  const zeroBody = zeroWrites.map((e) => e.postData);
  record("retention", "explicit-zero-sends-request", zeroWrites.length >= 1 && zeroBody.some((b) => /"record_retention_days"\s*:\s*0/.test(String(b))), { writes: zeroWrites.map((e) => e.method + " " + e.url + " " + e.postData) });
  await setup(browser);
  await gotoCategory(browser, "tools");
  await sleep(900);
  const zeroAfterReload = await readRetention(browser);
  record("retention", "explicit-zero-persisted", String(zeroAfterReload?.value) === "0", { afterReload: zeroAfterReload?.value });

  // 4) 503 回滚 + 重试
  await setup(browser);
  await gotoCategory(browser, "tools");
  await sleep(900);
  const beforeFail = await readRetention(browser);
  const fail = () => ({ status: 503, body: { detail: "验收注入：服务暂时不可用" } });
  const offs = [
    await browser.route({ method: "POST", path: /\/api\/settings\/tools/ }, fail),
    await browser.route({ method: "PUT", path: /\/api\/settings\/tools/ }, fail),
    await browser.route({ method: "PATCH", path: /\/api\/settings\/tools/ }, fail),
  ];
  await browser.fill(sel, "12");
  await browser.evalJs("(() => { const el = document.querySelector('" + sel + "'); el.dispatchEvent(new Event('change', { bubbles: true })); el.blur(); return true; })()");
  await sleep(1400);
  const afterFail = await readRetention(browser);
  const failNotices = await notices(browser);
  record("retention", "503-rolls-back-to-effective", String(afterFail?.value) === String(beforeFail?.value), { before: beforeFail?.value, after: afterFail?.value });
  record("retention", "503-error-shown-and-short", failNotices.some((t) => /失败|无法|服务/.test(t)) && failNotices.every((t) => t.length <= 160), { notices: failNotices });
  for (const off of offs) off();
  await sleep(200);
  // 重试：同一操作再提交一次，应成功并落库
  mark = browser.log.length;
  await browser.fill(sel, "12");
  await browser.evalJs("(() => { const el = document.querySelector('" + sel + "'); el.dispatchEvent(new Event('change', { bubbles: true })); el.blur(); return true; })()");
  await sleep(1500);
  const retryWrites = writeRequests(browser, mark, TOOLS_PATH);
  await setup(browser);
  await gotoCategory(browser, "tools");
  await sleep(900);
  const afterRetry = await readRetention(browser);
  record("retention", "retry-succeeds", retryWrites.length >= 1 && String(afterRetry?.value) === "12", { writes: retryWrites.length, afterRetry: afterRetry?.value });

  // 5) 迟到响应：旧响应不能覆盖新状态
  let call = 0;
  const echo = async (request) => {
    call += 1;
    return { body: { record_outputs: true, output_retention_days: 90, record_retention_days: call === 1 ? 21 : 33 }, delayMs: call === 1 ? 2000 : 0 };
  };
  const offs2 = [
    await browser.route({ method: "POST", path: /\/api\/settings\/tools/ }, echo),
    await browser.route({ method: "PUT", path: /\/api\/settings\/tools/ }, echo),
  ];
  await browser.fill(sel, "21");
  await browser.evalJs("(() => { const el = document.querySelector('" + sel + "'); el.dispatchEvent(new Event('change', { bubbles: true })); el.blur(); return true; })()");
  await sleep(250);
  await browser.fill(sel, "33");
  await browser.evalJs("(() => { const el = document.querySelector('" + sel + "'); el.dispatchEvent(new Event('change', { bubbles: true })); el.blur(); return true; })()");
  await sleep(3200);
  const afterLate = await readRetention(browser);
  record("retention", "late-old-response-does-not-override", String(afterLate?.value) === "33", { value: afterLate?.value, calls: call });
  for (const off of offs2) off();
  await sleep(200);
}

/* ---------------- I #23 搜索通道状态 ---------------- */
async function searchStateChecks(browser) {
  await browser.setViewport(1440, 900);
  await sleep(400);
  // 已配置、从未检查
  const offs = [];
  offs.push(await browser.route({ method: "GET", path: /\/api\/settings\/search(\?|$)/ }, () => ({ body: { top_k_default: 7, max_fetch_chars: 30000, searxng_url: "", bocha_has_key: true, keyless_fallback: true } })));
  offs.push(await browser.route({ method: "GET", path: /\/api\/settings\/search\/status/ }, () => ({ body: { channels: [
    { id: "keyless", label: "免密钥检索", configured: true, usable: true, checked_at: "2026-10-28T03:00:00.000Z", checking: false, reason_code: null, message: "" },
    { id: "bocha", label: "博查 API Key", configured: true, usable: null, checked_at: null, checking: false, reason_code: null, message: "" },
    { id: "searxng", label: "自建 SearXNG", configured: false, usable: false, checked_at: null, checking: false, reason_code: "not_configured", message: "未配置" },
  ] } })));
  await setup(browser);
  await gotoCategory(browser, "models");
  await sleep(800);
  const stateText = await browser.evalJs("(() => { const root = window.__stg.root(); const chans = [...root.querySelectorAll('[data-channel-state], .set-channel')].filter((el) => window.__stg.visible(el)); return chans.map((el) => window.__stg.norm(el.innerText || '')).join(' | '); })()");
  const bodyText = await browser.evalJs("(() => { const sec = [...window.__stg.root().querySelectorAll('.set-section')].filter((el) => window.__stg.visible(el))[0]; return sec ? window.__stg.norm(sec.innerText || '') : ''; })()");
  const flagged = /已配置[，,]?\s*尚未检查/.test(bodyText) || /已配置[，,]?\s*尚未检查/.test(stateText);
  record("search", "configured-but-unchecked-state", flagged, { stateText, bodySample: bodyText.slice(0, 400) });
  for (const off of offs) off();
  await sleep(200);
  // 检查失败：保留配置与重试入口
  const offs2 = [];
  offs2.push(await browser.route({ method: "GET", path: /\/api\/settings\/search(\?|$)/ }, () => ({ body: { top_k_default: 7, max_fetch_chars: 30000, searxng_url: "", bocha_has_key: true, keyless_fallback: true } })));
  offs2.push(await browser.route({ method: "GET", path: /\/api\/settings\/search\/status/ }, () => ({ body: { channels: [
    { id: "keyless", label: "免密钥检索", configured: true, usable: true, checked_at: "2026-10-28T03:00:00.000Z", checking: false, reason_code: null, message: "" },
    { id: "bocha", label: "博查 API Key", configured: true, usable: null, checked_at: null, checking: false, reason_code: null, message: "" },
    { id: "searxng", label: "自建 SearXNG", configured: false, usable: false, checked_at: null, checking: false, reason_code: "not_configured", message: "未配置" },
  ] } })));
  offs2.push(await browser.route({ method: "POST", path: /\/api\/settings\/search\/check/ }, () => ({ status: 502, body: { detail: "验收注入：检查失败" } })));
  await setup(browser);
  await gotoCategory(browser, "models");
  await sleep(800);
  const checked = await browser.evalJs("(() => { const root = window.__stg.root(); const btn = [...root.querySelectorAll('button')].find((el) => window.__stg.visible(el) && /检查/.test(window.__stg.norm(el.innerText || ''))); if (!btn) return { ok: false }; btn.click(); return { ok: true, text: window.__stg.norm(btn.innerText || '') }; })()");
  await sleep(1600);
  const afterFailText = await browser.evalJs("(() => { const sec = [...window.__stg.root().querySelectorAll('.set-section')].filter((el) => window.__stg.visible(el))[0]; return sec ? window.__stg.norm(sec.innerText || '') : ''; })()");
  const retryBtn = await browser.evalJs("(() => { const root = window.__stg.root(); return [...root.querySelectorAll('button')].filter((el) => window.__stg.visible(el)).map((el) => window.__stg.norm(el.innerText || '')).filter((t) => /检查|重试/.test(t)); })()");
  record("search", "check-failure-keeps-config-and-retry", checked?.ok && /失败|未通过|无法/.test(afterFailText) && retryBtn.length > 0, { checked, retryButtons: retryBtn, sample: afterFailText.slice(0, 400) });
  record("search", "check-failure-not-says-passed", !/检查通过/.test(afterFailText), { sample: afterFailText.slice(0, 300) });
  for (const off of offs2) off();
  run.searchBodySample = afterFailText.slice(0, 800);
}

/* ---------------- 主流程 ---------------- */
const browser = new VerifyBrowser({ port: cdpPort, width: 1440, height: 900, apiBase });
let exitCode = 0;
try {
  await browser.launch();
  await browser.installDialogGuards();
  await browser.navigate(appUrl + "/", 2600);
  const fixtures = await installFixtures(browser);
  run.fixtures = fixtures.hits;

  if (want("layout")) { run.layout = {}; for (const vp of [{ width: 1440, height: 900 }, { width: 800, height: 600 }]) run.layout[vp.width] = await measurePanels(browser, vp); }
  if (want("rows")) { run.rows = {}; for (const vp of [{ width: 1440, height: 900 }, { width: 800, height: 600 }]) run.rows[vp.width] = await measureRows(browser, vp); }
  if (want("copy")) { await setup(browser); await dumpTexts(browser); writeFileSync(join(outDir, label + "-settings-text.json"), JSON.stringify(run.texts, null, 2), "utf8"); }
  if (want("confirm")) {
    const offs = await stubsForDangerConfirm(browser);
    await setup(browser);
    const opened = await openDangerConfirm(browser);
    const m = await measureConfirm(browser, "confirm-800x600");
    if (m && m.visible) { checkDangerButtons(m, "confirm-800x600"); await shot(browser, "confirm-800x600"); }
    await browser.pressKey("Escape");
    await sleep(500);
    record("confirm-800x600", "esc-closes-danger-confirm", await browser.evalJs("!document.querySelector('.qio-confirm--layer')"), {});
    // 宽窗再量一次
    await browser.setViewport(1440, 900);
    await sleep(600);
    const m2 = await openDangerConfirm(browser);
    if (m2?.clicked?.ok) {
      const mm = await measureConfirm(browser, "confirm-1440x900");
      if (mm && mm.visible) { checkDangerButtons(mm, "confirm-1440x900"); await shot(browser, "confirm-1440x900"); }
      await browser.pressKey("Escape");
      await sleep(400);
    } else { skip("confirm-1440x900", "open", "宽窗下找不到危险确认入口"); }
    for (const off of offs) off();
  }
  if (want("keyform")) { await measureKeyForm(browser); }
  if (want("keyboard")) { await keyboardChecks(browser); }
  if (want("retention")) { await retentionChecks(browser); }
  if (want("searchstate")) { await searchStateChecks(browser); }
} catch (error) {
  run.error = String(error && error.stack ? error.stack : error);
  console.error("验收中断：" + run.error);
  exitCode = 1;
} finally {
  run.finishedAt = new Date().toISOString();
  run.summary = { total: run.checks.length, passed: run.checks.filter((c) => c.ok).length, failed: run.checks.filter((c) => !c.ok).length, skipped: run.skipped.length, shots: run.shots.length };
  writeFileSync(join(outDir, label + "-pm032-lead.json"), JSON.stringify(run, null, 2), "utf8");
  const lines = ["# pm032 settings Lead 验收（" + label + "）", "", "- 应用：" + appUrl, "- 检查 " + run.summary.total + "：通过 " + run.summary.passed + " / 失败 " + run.summary.failed + " / 跳过 " + run.summary.skipped, ""];
  for (const c of run.checks) lines.push("- " + (c.ok ? "PASS" : "**FAIL**") + " [" + c.scope + "] " + c.name + (c.ok ? "" : " :: " + JSON.stringify(c.detail).slice(0, 300)));
  if (run.skipped.length) { lines.push("", "## 跳过"); for (const s of run.skipped) lines.push("- [" + s.scope + "] " + s.name + " :: " + s.reason); }
  writeFileSync(join(outDir, label + "-pm032-lead.md"), lines.join("\n"), "utf8");
  if (run.summary.failed > 0 && !allowFail) exitCode = 1;
  await browser.close();
  console.log("检查 " + run.summary.total + " 条：通过 " + run.summary.passed + " / 失败 " + run.summary.failed + " / 跳过 " + run.summary.skipped);
  console.log("产物：" + outDir);
}
process.exit(exitCode);
