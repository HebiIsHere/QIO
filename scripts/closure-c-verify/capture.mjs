/**
 * C 的「同场景 前/后」截图 + 客观测量（只给验收用）。
 *
 * 为什么要有它：本轮的验收点（亮色辅助文字可读性、工具栏密度与窄窗口、错误文案层级、
 * 字体真的加载、聊天与批量列表避让）都不是「看一眼顺眼」就能过的，需要同一份数据、
 * 同一套步骤下跑出的**前后对照截图**与**真实数字**（对比度 / 工具栏高度 / 溢出与覆盖计数）。
 *
 * 用法：
 *   node scripts/closure-c-verify/capture.mjs --label=before
 *   node scripts/closure-c-verify/capture.mjs --label=after
 * 环境变量：QIO_C_APP_PORT（默认 5421）、QIO_C_BACKEND_PORT（默认 8921）。
 */
import { mkdirSync, writeFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { Browser, decodePng, sampleContrast } from "./cdp.mjs";

const here = dirname(fileURLToPath(import.meta.url));
const arg = (name, fallback) => {
  const hit = process.argv.find((a) => a.startsWith("--" + name + "="));
  return hit ? hit.split("=").slice(1).join("=") : fallback;
};
const LABEL = arg("label", "before");
const APP = arg("app", "http://127.0.0.1:" + (process.env.QIO_C_APP_PORT || 5421));
const BACKEND = arg("backend", "http://127.0.0.1:" + (process.env.QIO_C_BACKEND_PORT || 8921));
const BOARD = "board_default";
const OUT = resolve(here, arg("out", "shots"));
const SHOT_DIR = join(OUT, LABEL);
mkdirSync(SHOT_DIR, { recursive: true });

const ALL_SIZES = [[1440, 900], [1024, 768], [800, 600], [480, 600]];
const ALL_SCENES = {
  "1440x900": ["base", "chat", "coexist", "fail", "savefail"],
  "1024x768": ["base", "chat", "coexist", "fail"],
  "800x600": ["base", "chat", "coexist", "fail", "savefail"],
  "480x600": ["base", "chat", "coexist", "fail", "savefail"],
};
/** --sizes=1440x900,800x600 / --scenes=base,chat 可以把采集缩到某一格，便于定位问题 */
const sizeFilter = arg("sizes", "").split(",").filter(Boolean);
const sceneFilter = arg("scenes", "").split(",").filter(Boolean);
const SIZES = ALL_SIZES.filter(([w, h]) => !sizeFilter.length || sizeFilter.includes(w + "x" + h));
const SCENES = Object.fromEntries(
  Object.entries(ALL_SCENES).map(([key, list]) => [key, list.filter((scene) => !sceneFilter.length || sceneFilter.includes(scene))]),
);
const THEMES = arg("themes", "dark,light").split(",").filter(Boolean);
const SLOW_MS = 1000;
const timed = async (label, fn) => {
  const start = Date.now();
  const value = await fn();
  const cost = Date.now() - start;
  if (cost >= SLOW_MS) console.log("  . " + label + " " + cost + "ms");
  return value;
};

async function api(path, init) {
  const resp = await fetch(BACKEND + path, {
    ...(init || {}),
    headers: { "Content-Type": "application/json", Connection: "close", ...((init && init.headers) || {}) },
  });
  return resp.json();
}

/** 固定数据：长中文、代码块、长名称、6 张卡片 / 2 个组 / 6 条关系（密集关系背景）+ 一批演示意图。 */
async function seed() {
  const now = new Date().toISOString();
  const card = (id, kind, x, y, content, meta, checked) => ({
    id, kind, x, y, w: 280, h: 180, content, meta: meta || {}, deleted: false, folded: false,
    hidden: false, checked: Boolean(checked), bookmarked: false, createdAt: now, updatedAt: now,
  });
  const longZh =
    "把这一季度的材料按「谁在做、依赖什么、什么时候要」三个问题重新排一遍：先看差异，再看结论，" +
    "最后把还没确定的假设单独标出来（这段文字故意写得很长，用来验证密集中文与聊天正文不叠读）。";
  const cards = [
    card("c_note_1", "text", 40, 40, longZh, {}, true),
    card("c_note_2", "text", 360, 40, "这一条还没有勾选：QIO 看不到它的文字，提交时也不会被查看。", {}, false),
    card("c_code", "code", 40, 260,
      "def summarize(items):\n    # 这一行很长，用来验证代码块换行与横向滚动：\n    return [{'title': it.title, 'why': it.reason, 'next': it.next_step, 'owner': it.owner} for it in items if it.enabled]\n",
      { language: "python" }, false),
    card("c_file", "file", 360, 260, "2026 年第三季度跨团队协作材料汇总与后续行动项（含附录与修订记录）",
      { name: "2026Q3-跨团队协作材料汇总与后续行动项-最终修订版-v12.pdf" }, false),
    card("c_url", "url", 680, 40, "https://example.invalid/qio/2026-q3-collaboration-review-with-a-very-long-slug",
      { title: "季度评审材料（外部链接）" }, false),
    card("c_note_3", "text", 680, 260, "结论：先交付可验证的一小步，再决定要不要扩大范围。", {}, true),
  ];
  const mk = (id, members) => ({ id, name: "默认组名", defaultName: true, ordered: false, deleted: false, members, createdAt: now, updatedAt: now });
  const groups = [mk("g_1", ["c_note_1", "c_code"]), mk("g_2", ["c_url", "c_note_3"])];
  const link = (id, src, dst, meaning) => ({ id, src, dst, direction: false, meaning, deleted: false, createdAt: now, updatedAt: now });
  const links = [
    link("l_1", "c_note_1", "c_note_2", "放在一起看"),
    link("l_2", "c_note_2", "c_code", "结论依赖这段代码"),
    link("l_3", "c_code", "c_file", "实现依据这份材料"),
    link("l_4", "c_file", "c_note_3", "材料支持这个结论"),
    link("l_5", "c_url", "c_note_1", "外部来源"),
    link("l_6", "c_note_3", "c_note_1", "回到开头重新看"),
  ];
  await api("/api/interactive/boards/" + BOARD + "/state", {
    method: "PUT",
    body: JSON.stringify({ state: { boardId: BOARD, seq: 0, updatedAt: now, cards: [], groups: [], links: [], selection: [] }, reason: "closure-c-reset" }),
  });
  await api("/api/interactive/boards/" + BOARD + "/state", {
    method: "PUT",
    body: JSON.stringify({ state: { boardId: BOARD, seq: 1, updatedAt: now, cards, groups, links, selection: [] }, reason: "closure-c-seed" }),
  });
  return { cards: cards.length, groups: groups.length, links: links.length };
}

/** 意图（批量列表）只铺一次：重复铺会把上一轮的残留意图越堆越多，前后对照就不可比了。 */
async function seedIntents() {
  const list = await api("/api/interactive/boards/" + BOARD + "/intents");
  for (const item of list.intents || []) {
    if (["pending", "needs_update", "waiting_dependency", "waiting_confirm"].includes(item.status)) {
      await api("/api/interactive/intents/" + item.id + "/reject", { method: "POST", body: "{}" });
    }
  }
  const created = await api("/api/interactive/boards/" + BOARD + "/intents", { method: "POST", body: JSON.stringify({ demo: true }) });
  return { intents: (created.created || []).length };
}

/** 页面渲染有先有后：工具栏、提交状态与聊天面板都要就位了再量，否则会量到「还没渲染」的空档 */
const waitReady = "(async () => { const t0 = Date.now(); for (;;) { const tb = document.querySelector('[data-im=\"board-toolbar\"]'); const st = document.querySelector('[data-im=\"submit-status\"]'); if (tb && st && tb.getBoundingClientRect().height > 1) return 'ready'; if (Date.now() - t0 > 10000) return 'timeout'; await new Promise((r) => setTimeout(r, 120)); } })()";
const clearChatInput = "(async () => { const t = document.querySelector('[data-im=\"chat-input\"]'); if (!t) return 'no-input'; const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value').set; setter.call(t, ''); t.dispatchEvent(new Event('input', { bubbles: true })); await new Promise((r) => setTimeout(r, 600)); return 'cleared'; })()";
/**
 * 批量列表要有「本次会话的批次记录」才会出现（契约 §9.2：批次来源必须可证明）。
 * 演示意图是服务端造的，本地没有创建记录，所以这里按界面使用的键写入一条记录，
 * 把 4 个演示意图归成同批 —— 否则「聊天与批量列表共存」这个场景根本没打开过批量列表
 * （改前实测：batch-list 一直不存在，重叠测量是空的，等于没测）。
 */
const batchSeedJs = (ids) => "(function () { try { localStorage.setItem('qio.interactive.intentBatches', JSON.stringify({ version: 2, records: " + JSON.stringify(ids.map((id) => ({ id, key: "closure-c-batch", at: Date.now() }))) + " })); return 'seeded'; } catch (e) { return 'ls-fail'; } })()";
/** 等两个面板真的不相交（几何计划是异步应用的，直接截会拍到过渡帧） */
const waitNoOverlap = "(async () => { const rect = (sel) => { const el = document.querySelector(sel); if (!el) return null; const b = el.getBoundingClientRect(); return { l: b.left, t: b.top, r: b.right, b: b.bottom }; }; const t0 = Date.now(); for (;;) { const a = rect('[data-im=\"chat-panel\"]'); const b = rect('[data-im=\"batch-list\"]'); if (a && b) { const w = Math.max(0, Math.min(a.r, b.r) - Math.max(a.l, b.l)); const h = Math.max(0, Math.min(a.b, b.b) - Math.max(a.t, b.t)); if (w * h === 0) return 'no-overlap'; } if (Date.now() - t0 > 8000) return 'timeout'; await new Promise((r) => setTimeout(r, 150)); } })()";
const openChat = "(async () => { const b = document.querySelector('[data-im=\"chat-toggle\"]'); if (b && !document.querySelector('[data-im=\"chat-panel\"]')) b.click(); await new Promise(r => setTimeout(r, 700)); return 'chat'; })()";
const openBatch = "(async () => { const b = document.querySelector('[data-im=\"batch-entry\"]'); if (b && !document.querySelector('[data-im=\"batch-list\"]')) b.click(); await new Promise(r => setTimeout(r, 900)); return 'batch'; })()";
const typeDraft = "(async () => { const t = document.querySelector('[data-im=\"chat-input\"]'); if (!t) return 'no-input'; const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value').set; setter.call(t, '（C 验收）这段文字只用来让草稿状态出现，不会真的发出去。'); t.dispatchEvent(new Event('input', { bubbles: true })); await new Promise(r => setTimeout(r, 1800)); return 'typed'; })()";
const breakSubmit = "(function () { if (!window.__origFetch) window.__origFetch = window.fetch.bind(window); window.fetch = function (input, init) { const url = String((input && input.url) || input); const method = String((init && init.method) || 'GET').toUpperCase(); if (/\\/submissions$/.test(url) && method === 'POST') { return Promise.resolve(new Response(JSON.stringify({ detail: { error: 'submission_failed', reason: '本机模拟的服务端失败：处理这次提交时出错了，改动与勾选都还在（这段原文故意写得很长，用来验证长失败原文的换行、限高滚动与重试入口可达）' } }), { status: 500, headers: { 'Content-Type': 'application/json' } })); } return window.__origFetch(input, init); }; return 'stubbed-submit'; })()";
const breakSave = "(function () { if (!window.__origFetch) window.__origFetch = window.fetch.bind(window); window.fetch = function (input, init) { const url = String((input && input.url) || input); const method = String((init && init.method) || 'GET').toUpperCase(); if (/\\/boards\\/[^/]+\\/state$/.test(url) && method === 'PUT') { return Promise.resolve(new Response(JSON.stringify({ detail: '本机模拟：保存这次板面改动时服务端出错（原文用于验证保存失败文案层级）' }), { status: 500, headers: { 'Content-Type': 'application/json' } })); } return window.__origFetch(input, init); }; return 'stubbed-save'; })()";
const clickSubmit = "(async () => { const b = document.querySelector('[data-im=\"submit\"]'); if (!b) return 'no-submit'; b.click(); await new Promise(r => setTimeout(r, 2500)); return 'submitted'; })()";
const addTextCard = "(async () => { const m = document.querySelector('[data-im=\"add-menu\"]'); if (m) m.click(); await new Promise(r => setTimeout(r, 300)); const t = document.querySelector('[data-im=\"add-text\"]'); if (t) t.click(); await new Promise(r => setTimeout(r, 2500)); return 'added'; })()";

/** 页内几何 / 溢出 / 覆盖 / 竖排 / 可达性测量（一次 eval 全取回）。 */
const METRICS_JS = String.raw`(() => {
  const q = (s) => document.querySelector(s);
  const box = (el) => { if (!el) return null; const b = el.getBoundingClientRect(); return { x: Math.round(b.x), y: Math.round(b.y), w: Math.round(b.width), h: Math.round(b.height), right: Math.round(b.right), bottom: Math.round(b.bottom) }; };
  const shown = (el) => { if (!el) return false; const b = el.getBoundingClientRect(); const st = getComputedStyle(el); return b.width > 0 && b.height > 0 && st.visibility !== 'hidden' && st.display !== 'none'; };
  const overlap = (a, b) => { if (!a || !b) return null; const w = Math.min(a.right, b.right) - Math.max(a.x, b.x); const h = Math.min(a.bottom, b.bottom) - Math.max(a.y, b.y); return Math.round(Math.max(0, w) * Math.max(0, h)); };
  const out = { viewport: [window.innerWidth, window.innerHeight], theme: document.documentElement.getAttribute('data-theme') || 'dark' };

  const tb = q('[data-im="board-toolbar"]');
  if (tb) {
    // 行数 = .tb-main 的直接子元素（编辑区 / 提交区）按 8px 容差归并出的横向条数。
    // 不用「所有按钮 top 去重」：按钮与分隔线差几像素就会被数成假行（实测把 1 行数成 4 行）；
    // 也不数提交区内部的换行 —— 那是同一行里的文字换行，不是工具栏多了一行。
    // 行数：把 .tb-main 的直接子元素按**垂直区间是否相交**归并（相交 = 同一行）。
    // 不能只看 top：编辑区与提交区高度不同，居中对齐时 top 天然差几像素，会被数成两行。
    const bands = [];
    const main = tb.querySelector('.tb-main');
    if (main) {
      for (const child of main.children) {
        const rect = child.getBoundingClientRect();
        if (rect.width < 1 || rect.height < 1) continue;
        bands.push({ top: rect.top, bottom: rect.bottom });
      }
    }
    bands.sort((a, b) => a.top - b.top);
    const rowTops = [];
    for (const band of bands) {
      const current = rowTops[rowTops.length - 1];
      if (!current || band.top >= current.bottom - 1) rowTops.push({ top: band.top, bottom: band.bottom });
      else current.bottom = Math.max(current.bottom, band.bottom);
    }
    out.toolbar = { box: box(tb), height: Math.round(tb.getBoundingClientRect().height), rows: rowTops.length, rowTops: rowTops.map((row) => Math.round(row.top)) };
    // 提交区内部换成了几「行」文字：窄窗口密度的直接证据
    const cluster = tb.querySelector('[data-im="submit-cluster"]');
    if (cluster) {
      const lines = [];
      for (const child of cluster.children) {
        for (const rect of child.getClientRects()) {
          if (rect.width < 1 || rect.height < 1) continue;
          lines.push({ sel: child.getAttribute('data-im') || child.className, top: Math.round(rect.top), bottom: Math.round(rect.bottom) });
        }
      }
      lines.sort((a, b) => a.top - b.top);
      out.submitLines = lines;
    }
    out.toolbarText = tb.innerText.replace(/\s+/g, ' ').slice(0, 220);
    out.toolbarSubmitBox = box(q('[data-im="submit"]'));
    out.toolbarFailureBox = box(q('[data-im="submit-failure"]'));
  }

  const chat = q('[data-im="chat-panel"]');
  const batch = q('[data-im="batch-list"]');
  const batchEntry = q('[data-im="batch-entry"]');
  const toggle = q('[data-im="chat-toggle"]');
  out.rects = { chat: box(chat), batch: box(batch), batchEntry: box(batchEntry), chatToggle: box(toggle), toolbar: box(tb) };
  out.overlap = {
    chatBatch: overlap(box(chat), box(batch)),
    chatToolbar: overlap(box(chat), box(tb)),
    chatToggleToolbar: overlap(box(toggle), box(tb)),
    batchEntryToolbar: overlap(box(batchEntry), box(tb)),
    chatBatchEntry: overlap(box(chat), box(batchEntry)),
  };
  out.switchBar = shown(q('[data-im="overlay-switch"]')) ? q('[data-im="overlay-switch"]').innerText.trim() : null;

  const watch = ['[data-im="board-toolbar"]', '[data-im="chat-panel"]', '[data-im="chat-toggle"]', '[data-im="batch-entry"]', '[data-im="batch-list"]', '[data-im="impact-dialog"]', '[data-im="tasks-popover"]', '[data-im="search-panel"]', '[data-im="submit-cluster"]', '[data-im="submit-failure"]', '[data-im="help-popover"]', '[data-im="submit-details-box"]'];
  out.clipped = [];
  for (const sel of watch) {
    for (const el of document.querySelectorAll(sel)) {
      if (!shown(el)) continue;
      const b = el.getBoundingClientRect();
      if (b.right > window.innerWidth + 1 || b.left < -1 || b.bottom > window.innerHeight + 1 || b.top < -1) {
        out.clipped.push({ sel, box: box(el), text: (el.innerText || '').replace(/\s+/g, ' ').slice(0, 40) });
      }
    }
  }

  const dead = [];
  for (const b of document.querySelectorAll('button')) {
    if (!shown(b)) continue;
    const r = b.getBoundingClientRect();
    if (r.right <= 0 || r.left >= window.innerWidth || r.bottom <= 0 || r.top >= window.innerHeight) {
      dead.push({ why: 'offscreen', im: b.getAttribute('data-im'), text: (b.textContent || '').trim().slice(0, 18) });
      continue;
    }
    const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
    if (hit && hit !== b && !b.contains(hit) && !hit.contains(b)) {
      const describe = (el) => el && el.tagName ? el.tagName.toLowerCase() + (el.getAttribute('data-im') ? '[' + el.getAttribute('data-im') + ']' : '') + (el.className && typeof el.className === 'string' ? '.' + el.className.trim().split(/\s+/).slice(0, 2).join('.') : '') : String(el);
      dead.push({ why: 'covered', im: b.getAttribute('data-im'), text: (b.textContent || '').trim().slice(0, 18), by: describe(hit), byParent: describe(hit.parentElement) });
    }
  }
  out.deadButtons = dead;

  const narrow = [];
  let leaf = 0;
  for (const el of document.querySelectorAll('#app *')) {
    if (el.children.length) continue;
    const text = (el.textContent || '').trim();
    if (text.length < 2 || !shown(el)) continue;
    leaf += 1;
    const st = getComputedStyle(el);
    if (st.writingMode && st.writingMode !== 'horizontal-tb') { narrow.push({ why: 'writing-mode', text: text.slice(0, 18), mode: st.writingMode }); continue; }
    const fs = parseFloat(st.fontSize) || 14;
    const b = el.getBoundingClientRect();
    if (b.width > 0 && b.width < fs * 1.6 && b.height > fs * 2.4) narrow.push({ why: 'width<1.6em', text: text.slice(0, 18), w: Math.round(b.width), h: Math.round(b.height), fs: Math.round(fs) });
  }
  out.leafTextNodes = leaf;
  out.verticalText = narrow;

  const texts = {};
  const grab = (key, sel) => { const el = q(sel); texts[key] = el ? { text: (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 220), box: box(el), color: getComputedStyle(el).color, fontSize: getComputedStyle(el).fontSize, background: getComputedStyle(el).backgroundColor, fontFamily: getComputedStyle(el).fontFamily } : null; };
  grab('panelHint', '[data-im="chat-panel"] .panel-hint');
  grab('draftStatus', '[data-im="chat-draft-status"]');
  grab('older', '[data-im="chat-older"]');
  grab('empty', '[data-im="chat-empty"]');
  grab('scopeLine', '[data-im="chat-scope"] .scope-line');
  grab('submitStatus', '[data-im="submit-status"]');
  grab('submitRange', '[data-im="visible-range"]');
  grab('submitFailureReason', '[data-im="submit-failure-reason"]');
  grab('submitFailureRetention', '[data-im="submit-failure-retention"]');
  grab('saveStatus', '[data-im="save-status"]');
  grab('impactError', '[data-im="impact-error"]');
  grab('loadError', '[data-im="load-error"]');
  out.texts = texts;

  const input = q('[data-im="chat-input"]');
  out.placeholder = input ? { text: input.placeholder, box: box(input), color: getComputedStyle(input, '::placeholder').color, fontSize: getComputedStyle(input).fontSize } : null;

  out.fonts = (() => { try { return { serif: document.fonts.check('600 19px "Noto Serif SC"', '互动板面'), sans: document.fonts.check('400 14.5px "Segoe UI"', '互动板面'), mono: document.fonts.check('400 11px "Cascadia Mono"', '0123') }; } catch (e) { return String(e); } })();
  out.bodyText = (document.body.innerText || '').replace(/\s+/g, ' ');
  return JSON.stringify(out);
})()`;

/** 需要做像素级对比度的元素（裁剪后取「背景众数 vs 文字最深像素」） */
const CONTRAST_TARGETS = {
  panelHint: '[data-im="chat-panel"] .panel-hint',
  draftStatus: '[data-im="chat-draft-status"]',
  older: '[data-im="chat-older"]',
  empty: '[data-im="chat-empty"]',
  scopeLine: '[data-im="chat-scope"] .scope-line',
  submitFailureReason: '[data-im="submit-failure-reason"]',
  saveStatus: '[data-im="save-status"]',
};

const PLACEHOLDER_TARGET = '[data-im="chat-input"]';
const DEV_TERMS = ["stale_check", "stale_state", "checkId", "impact_confirmation_required", "/api/", "draft_too_long", "undefined", "NaN", " seq"];

async function rectOf(browser, selector) {
  const raw = await browser.evalJs("(() => { const el = document.querySelector(" + JSON.stringify(selector) + "); if (!el) return 'null'; const b = el.getBoundingClientRect(); if (b.width < 1 || b.height < 1) return 'null'; return JSON.stringify({ x: b.x, y: b.y, width: b.width, height: b.height }); })()");
  return raw === "null" ? null : JSON.parse(raw);
}

async function sample(browser, selector, viewport, inset = 0) {
  const rect = await rectOf(browser, selector);
  if (!rect) return null;
  const clip = {
    x: Math.max(0, rect.x + inset),
    y: Math.max(0, rect.y + inset),
    width: Math.max(0, Math.min(rect.width - inset * 2, viewport[0] - Math.max(0, rect.x + inset))),
    height: Math.max(0, Math.min(rect.height - inset * 2, viewport[1] - Math.max(0, rect.y + inset))),
  };
  if (clip.width < 2 || clip.height < 2) return null;
  return sampleContrast(decodePng(await browser.screenshotPng(clip)), null);
}

async function run() {
  const seeded = await seed();
  console.log("铺数据：" + JSON.stringify(seeded));
  /** 重新造一批待审批意图并取回 id：演示意图会自己往前走，跑久了批量列表就空了（实测） */
  async function refreshPendingIntentIds() {
    await seedIntents();
    return (await api("/api/interactive/boards/" + BOARD + "/intents")).intents
      .filter((item) => ["pending", "needs_update", "waiting_dependency", "waiting_confirm"].includes(item.status))
      .map((item) => item.id);
  }
  const intentSeed = await seedIntents();
  console.log("铺意图：" + JSON.stringify(intentSeed));
  const intentIds = await refreshPendingIntentIds();
  console.log("待审批意图 " + intentIds.length + " 条（用于批量列表场景）");
  const browser = await new Browser({ outDir: OUT }).launch();
  const runs = [];
  try {
    for (const [w, h] of SIZES) {
      const key = w + "x" + h;
      for (const theme of THEMES) {
        for (const scene of SCENES[key]) {
          const tag = LABEL + "-" + key + "-" + theme + "-" + scene;
          // 每个场景都重新铺一次板面：上一场景可能加过卡片（savefail），不重置就没有可比性
          await timed("seed", () => seed());
          // 同 URL 的 hash 导航不会真正重新加载页面（实测：场景之间会串状态、还会带上一个场景里打的 fetch 桩），
          // 所以带一个每次不同的查询参数，强制真实导航。
          await timed("navigate", () => browser.navigate(APP + "/?c=" + Date.now() + "#/interactive", 4000));
          await browser.setViewport(w, h);
          await browser.evalJs("document.documentElement.setAttribute('data-theme'," + JSON.stringify(theme) + "); 'ok'");
          await timed("ready", () => browser.evalJs(waitReady));
          if (["chat", "fail"].includes(scene)) {
            await browser.evalJs(openChat);
            await browser.evalJs(typeDraft);
          }
          if (scene === "coexist") {
            // 先写批次记录再重新加载（记录必须在 store 读之前存在），然后同时打开两个面板。
            // 每场都重新造意图：采集要跑十几分钟，演示意图会自己推进完，批量列表就不会再展开
            //（实测：只有最早那场测到了双面板共存，后面的 n/a 是采集脚本的问题，不是界面问题）。
            const ids = await refreshPendingIntentIds();
            await browser.evalJs(batchSeedJs(ids));
            await browser.reload(3500);
            await browser.evalJs(waitReady);
            await browser.evalJs(openChat);
            await browser.evalJs(openBatch);
            await timed("no-overlap", () => browser.evalJs(waitNoOverlap));
          }
          if (scene === "fail") {
            await browser.evalJs(breakSubmit);
            await browser.evalJs(clickSubmit);
          }
          if (scene === "savefail") {
            await browser.evalJs(breakSave);
            await browser.evalJs(addTextCard);
          }
          await new Promise((r) => setTimeout(r, 350));
          const metrics = JSON.parse(await timed("metrics", () => browser.evalJs(METRICS_JS)));
          const contrasts = {};
          await timed("contrast", async () => {
            for (const [name, selector] of Object.entries(CONTRAST_TARGETS)) {
              const sampled = await sample(browser, selector, [w, h]);
              if (sampled) contrasts[name] = { ...sampled, css: metrics.texts[name] ? { color: metrics.texts[name].color, fontSize: metrics.texts[name].fontSize, background: metrics.texts[name].background } : null };
            }
          });
          // 占位说明只在**空输入框**里出现：先量草稿状态，再清空输入框量占位说明。
          // 裁剪要收进边框（否则量到的是输入框边框而不是占位文字）。
          if (scene === "chat" && metrics.placeholder) {
            await browser.evalJs(clearChatInput);
            const sampled = await sample(browser, PLACEHOLDER_TARGET, [w, h], 6);
            if (sampled) contrasts.placeholder = { ...sampled, css: { color: metrics.placeholder.color, fontSize: metrics.placeholder.fontSize, background: null } };
          }
          metrics.contrast = contrasts;
          const visibleText = (metrics.bodyText || "") + " " + Object.values(metrics.texts).filter(Boolean).map((t) => t.text).join(" ");
          metrics.devTermsInVisibleText = DEV_TERMS.filter((term) => visibleText.includes(term));
          delete metrics.bodyText;
          const shot = await timed("screenshot", () => browser.shotFile(LABEL + "/" + tag));
          runs.push({ tag, scene, size: key, theme, shot, metrics });
          const tbInfo = metrics.toolbar ? "toolbar h=" + metrics.toolbar.height + " rows=" + metrics.toolbar.rows : "toolbar=-";
          const hint = metrics.contrast.panelHint ? "hint=" + metrics.contrast.panelHint.ratio : "hint=-";
          console.log([tag, tbInfo, hint, "dead=" + metrics.deadButtons.length, "clip=" + metrics.clipped.length, "devTerms=" + (metrics.devTermsInVisibleText.join("|") || "无")].join("  "));
        }
      }
    }
  } finally {
    await browser.close();
  }
  const report = { label: LABEL, app: APP, seeded, createdAt: new Date().toISOString(), runs, httpFails: [...new Set(browser.httpFails)], consoleErrors: browser.consoleErrors.slice(-10) };
  const suffix = sizeFilter.length || sceneFilter.length || THEMES.length < 2 ? "-partial" : "";
  const reportFile = join(OUT, LABEL + "-report" + suffix + ".json");
  writeFileSync(reportFile, JSON.stringify(report, null, 2), "utf8");
  console.log("报告：" + reportFile);
}

run().catch((error) => {
  console.error("采集失败：" + (error && error.stack ? error.stack : error));
  process.exit(1);
});
